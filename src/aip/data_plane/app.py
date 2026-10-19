"""数据面应用：Gateway（MCP + REST + OAuth）、Runtime 等在各自里程碑挂载。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, Response

from aip.data_plane.iam_cache import IamCache, SnapshotRejected
from aip.data_plane.oauth import AuthContext
from aip.data_plane.oauth import router as oauth_router
from aip.data_plane.principal import PrincipalResolver
from aip.libs.common import web
from aip.libs.common.config import get_settings
from aip.libs.tokens.keys import SigningKey, platform_key
from aip.libs.tokens.replay import ReplayStore, ValkeyReplayStore

log = logging.getLogger("aip.data_plane")


class Readiness:
    """就绪条件：IAM 快照、发布快照与策略 bundle 均已加载（architecture.md 4.4，缺失即拒绝服务）。"""

    def __init__(self, iam: IamCache) -> None:
        self.iam = iam
        self.release_snapshot_seq: int | None = None
        self.policy_bundle: str | None = None

    def checks(self) -> dict[str, str]:
        return {
            "iam_snapshot": "loaded" if self.iam.seq is not None else "missing",
            "release_snapshot": "loaded" if self.release_snapshot_seq is not None else "missing",
            "policy_bundle": "loaded" if self.policy_bundle is not None else "missing",
        }

    @property
    def ready(self) -> bool:
        return all(v == "loaded" for v in self.checks().values())


async def pull_snapshots(app: FastAPI, control_plane_url: str, interval_s: float = 2.0) -> None:
    """从控制面拉取 IAM 快照；控制面不可用时保持现有快照继续服务。"""
    cache: IamCache = app.state.iam
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5) as client:
        while True:
            try:
                r = await client.get(
                    "/api/v1/snapshots/iam",
                    params={"after_seq": cache.seq or 0},
                    headers={"Authorization": f"Bearer {get_settings().internal_service_token}"},
                )
                if r.status_code == 200:
                    cache.load(r.json())
            except (httpx.HTTPError, SnapshotRejected, KeyError) as e:
                log.warning("iam snapshot pull failed: %s", e)
            await asyncio.sleep(interval_s)


def create_app(
    *, key: SigningKey | None = None, replay: ReplayStore | None = None, control_plane_url: str | None = None
) -> FastAPI:
    settings = get_settings()
    key = key or platform_key()
    iam = IamCache([key])
    ctx = AuthContext(
        settings=settings,
        resolver=PrincipalResolver(iam),
        key=key,
        replay=replay or ValkeyReplayStore(settings.valkey_url),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(pull_snapshots(app, control_plane_url)) if control_plane_url else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="AIP Data Plane API", version=settings.version, lifespan=lifespan)
    web.install(app)
    readiness = Readiness(iam)
    app.state.readiness = readiness
    app.state.iam = iam
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
