"""数据面应用：Gateway（MCP + REST + OAuth）、Runtime 等在各自里程碑挂载。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from aip.data_plane.iam_cache import IamCache
from aip.data_plane.oauth import AuthContext
from aip.data_plane.oauth import router as oauth_router
from aip.data_plane.pdp import OpaClient, PolicyService
from aip.data_plane.principal import PrincipalResolver
from aip.data_plane.release_cache import ReleaseCache
from aip.data_plane.snapshots import SnapshotCache, SnapshotRejected
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import anchor, ship
from aip.libs.common import web
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey, platform_key
from aip.libs.tokens.replay import ReplayStore, ValkeyReplayStore

log = logging.getLogger("aip.data_plane")


class Readiness:
    """就绪条件：IAM 快照、发布快照与策略 bundle 均已加载（architecture.md 4.4，缺失即拒绝服务）。"""

    def __init__(self, iam: IamCache, releases: ReleaseCache, opa: OpaClient) -> None:
        self.iam, self.releases, self.opa = iam, releases, opa

    def checks(self) -> dict[str, str]:
        policy_ok = (
            self.opa.revision is not None
            and self.releases.seq is not None
            and self.opa.revision == self.releases.policy["revision"]
        )
        return {
            "iam_snapshot": "loaded" if self.iam.seq is not None else "missing",
            "release_snapshot": "loaded" if self.releases.seq is not None else "missing",
            "policy_bundle": "loaded" if policy_ok else "missing",
        }

    @property
    def ready(self) -> bool:
        return all(v == "loaded" for v in self.checks().values())


async def pull_once(client: httpx.AsyncClient, cache: SnapshotCache, token: str) -> bool:
    r = await client.get(
        f"/api/v1/snapshots/{cache.kind}",
        params={"after_seq": cache.seq or 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 200:
        return cache.load(r.json())
    return False


async def pull_snapshots(app: FastAPI, control_plane_url: str, interval_s: float = 2.0) -> None:
    """从控制面拉取快照；控制面不可用时继续使用已加载（或从磁盘恢复）的快照。"""
    token = get_settings().internal_service_token
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5) as client:
        while True:
            for cache in (app.state.iam, app.state.releases):
                try:
                    if await pull_once(client, cache, token) and cache is app.state.releases:
                        await app.state.policy.sync_policy()
                except (httpx.HTTPError, SnapshotRejected, KeyError) as e:
                    log.warning("%s snapshot pull failed: %s", cache.kind, e)
            await asyncio.sleep(interval_s)


async def ship_audit(app: FastAPI, database_url: str) -> None:
    """后台把 spool 写入审计库，并按小时锚定链头（architecture.md 12.4）；审计库不可用时只告警、spool 继续累积。"""
    settings = get_settings()
    engine = create_async_engine(database_url, pool_pre_ping=True)
    last_anchor = 0.0
    try:
        while True:
            try:
                await ship(app.state.audit, engine)
                now = time.monotonic()
                if now - last_anchor >= settings.audit_anchor_interval_s:
                    async with engine.begin() as conn:
                        await anchor(conn, app.state.auth.key)
                    last_anchor = now
            except (OSError, SQLAlchemyError) as e:
                log.warning("audit ship failed, spool keeps buffering: %s", type(e).__name__)
            await asyncio.sleep(settings.audit_ship_interval_s)
    finally:
        await engine.dispose()


def create_app(
    *,
    key: SigningKey | None = None,
    replay: ReplayStore | None = None,
    control_plane_url: str | None = None,
    data_dir: pathlib.Path | None = None,
    opa_url: str | None = None,
) -> FastAPI:
    settings = get_settings()
    key = key or platform_key()
    data_dir = data_dir or pathlib.Path(settings.data_dir)
    iam = IamCache([key], data_dir / "snapshots")
    releases = ReleaseCache([key], data_dir / "snapshots")
    opa = OpaClient(opa_url or settings.opa_url)
    audit = AuditSpool(data_dir / "audit-spool", settings.instance_id, settings.audit_spool_max_bytes)
    policy = PolicyService(iam, releases, opa, audit, env=settings.env if settings.env != "dev" else "test")
    ctx = AuthContext(
        settings=settings,
        resolver=PrincipalResolver(iam),
        key=key,
        replay=replay or ValkeyReplayStore(settings.valkey_url),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 先从磁盘恢复最近一次已验签的快照，控制面宕机时也能启动服务
        iam.restore_from_disk()
        if releases.restore_from_disk():
            with contextlib.suppress(httpx.HTTPError):
                await policy.sync_policy()
        tasks = []
        if control_plane_url:
            tasks.append(asyncio.create_task(pull_snapshots(app, control_plane_url)))
        if settings.audit_database_url:
            tasks.append(asyncio.create_task(ship_audit(app, settings.audit_database_url)))
        yield
        for task in tasks:
            task.cancel()
        await opa.aclose()

    app = FastAPI(title="AIP Data Plane API", version=settings.version, lifespan=lifespan)
    web.install(app)
    readiness = Readiness(iam, releases, opa)
    app.state.readiness = readiness
    app.state.iam = iam
    app.state.releases = releases
    app.state.policy = policy
    app.state.audit = audit
    app.state.auth = ctx
    app.include_router(oauth_router(ctx))

    @app.get("/healthz", response_model=web.Health, tags=["ops"], operation_id="dpHealth")
    async def healthz() -> web.Health:
        return web.Health(status="ok", service="data-plane", version=settings.version)

    @app.get("/readyz", response_model=web.Health, tags=["ops"], operation_id="dpReady")
    async def readyz(response: Response) -> web.Health:
        if not readiness.ready:
            response.status_code = 503
        return web.Health(
            status="ok" if readiness.ready else "degraded",
            service="data-plane",
            version=settings.version,
            checks=readiness.checks(),
        )

    @app.get("/api/v1/catalog", tags=["catalog"], operation_id="catalog")
    async def catalog(
        authorization: Annotated[str | None, Header()] = None,
        x_user_assertion: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """M1：只做认证与 Principal 解析；目录内容在 M4 按发布快照与 PDP 过滤。"""
        await ctx.principal_from_request(authorization, x_user_assertion)
        return {"items": [], "next_cursor": None, "has_more": False}

    return app


app = create_app(control_plane_url=get_settings().control_plane_url or None)
