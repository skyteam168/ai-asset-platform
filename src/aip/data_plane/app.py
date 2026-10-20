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
from fastapi import FastAPI, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aip.connectors.credentials import OpenBaoSecretStore, SecretStore
from aip.data_plane.confirm_api import router as confirm_router
from aip.data_plane.confirmation import MemoryConfirmationStore, PgConfirmationStore
from aip.data_plane.events import EventForwarder
from aip.data_plane.iam_cache import IamCache
from aip.data_plane.mcp_gateway import McpGateway
from aip.data_plane.mcp_gateway import router as mcp_router
from aip.data_plane.oauth import AuthContext
from aip.data_plane.oauth import router as oauth_router
from aip.data_plane.pdp import OpaClient, PolicyService
from aip.data_plane.principal import PrincipalResolver
from aip.data_plane.quota import MemoryQuotaStore, PgQuotaStore
from aip.data_plane.release_cache import ReleaseCache
from aip.data_plane.runtime import (
    ConnectorRegistry,
    ExecutionStore,
    MemoryExecutionStore,
    PgExecutionStore,
    ToolRuntime,
)
from aip.data_plane.snapshots import SnapshotCache, SnapshotRejected
from aip.data_plane.workflow.engine import WorkflowEngine
from aip.data_plane.workflow.llm import LlmClient
from aip.libs.audit.spool import AuditSpool
from aip.libs.audit.store import anchor, ship
from aip.libs.common import web
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient.config import Resolver
from aip.libs.tokens.keys import SigningKey, platform_keys
from aip.libs.tokens.replay import ReplayStore, ValkeyReplayStore

log = logging.getLogger("aip.data_plane")


class InvokeIn(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    version: str | None = None
    channel: str = "stable"


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


async def pull_once(client: httpx.AsyncClient, cache: SnapshotCache, token: str, wait_s: float = 0) -> bool:
    r = await client.get(
        f"/api/v1/snapshots/{cache.kind}",
        params={"after_seq": cache.seq or 0, "wait_s": wait_s},
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 200:
        return cache.load(r.json())
    return False


async def pull_snapshots(app: FastAPI, control_plane_url: str, wait_s: float = 20.0) -> None:
    """长轮询控制面：每类快照一条循环，有变更立即返回（通常 1 秒内生效）；控制面不可用时继续使用已加载的快照。"""
    token = get_settings().internal_service_token

    async def loop(cache: SnapshotCache) -> None:
        backoff = 1.0
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=wait_s + 10) as client:
            while True:
                try:
                    if await pull_once(client, cache, token, wait_s) and cache is app.state.releases:
                        await app.state.policy.sync_policy()
                    backoff = 1.0
                except (httpx.HTTPError, SnapshotRejected, KeyError) as e:
                    log.warning("%s snapshot pull failed: %s", cache.kind, e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    await asyncio.gather(loop(app.state.iam), loop(app.state.releases))


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
    secrets: SecretStore | None = None,
    exec_store: ExecutionStore | None = None,
    resolver: Resolver | None = None,
    runtime_engine: AsyncEngine | None = None,
    llm: LlmClient | None = None,
) -> FastAPI:
    settings = get_settings()
    # 签名用当前密钥；验签信任全部已发布版本（Transit 轮换期间旧令牌与旧快照仍有效）
    trusted = [key] if key is not None else list(platform_keys())
    key = trusted[0]
    data_dir = data_dir or pathlib.Path(settings.data_dir)
    iam = IamCache(trusted, data_dir / "snapshots")
    releases = ReleaseCache(trusted, data_dir / "snapshots")
    opa = OpaClient(opa_url or settings.opa_url, settings.opa_timeout_s)
    audit = AuditSpool(data_dir / "audit-spool", settings.instance_id, settings.audit_spool_max_bytes)
    policy = PolicyService(iam, releases, opa, audit, env=settings.env if settings.env != "dev" else "test")
    ctx = AuthContext(
        settings=settings,
        resolver=PrincipalResolver(iam),
        key=key,
        replay=replay or ValkeyReplayStore(settings.valkey_url),
        verify_keys=trusted,
    )
    if runtime_engine is None and settings.runtime_database_url:
        runtime_engine = create_async_engine(settings.runtime_database_url)
    if exec_store is None:
        exec_store = PgExecutionStore(runtime_engine) if runtime_engine else MemoryExecutionStore()
    forwarder = EventForwarder(control_plane_url, settings.internal_service_token, settings.instance_id)
    ctx.resolver.on_employee_match = forwarder

    def assertion_failed(client_id: str, code: str) -> None:
        found = iam.consumer_by_client_id(client_id)
        audit.append(
            "auth.assertion_failed",
            company_id=found[1]["company_id"] if found else None,
            record={"client_id": client_id, "code": code},
        )

    ctx.on_assertion_failure = assertion_failed
    registry = ConnectorRegistry(
        releases, secrets or OpenBaoSecretStore(settings.openbao_addr, settings.openbao_token), resolver, forwarder
    )
    runtime = ToolRuntime(
        iam=iam,
        releases=releases,
        policy=policy,
        registry=registry,
        store=exec_store,
        audit=audit,
        mode=settings.runtime_mode,
        confirmations=PgConfirmationStore(runtime_engine) if runtime_engine else MemoryConfirmationStore(),
        quota=PgQuotaStore(runtime_engine) if runtime_engine else MemoryQuotaStore(),
    )
    runtime.workflow_sync_wait_s = settings.workflow_sync_wait_s
    # Workflow 需要持久化运行表；没有运行库（纯内存开发模式）时不启用
    workflows = (
        WorkflowEngine(
            runtime_engine,
            runtime,
            audit,
            instance_id=settings.instance_id,
            llm=llm,
            lease_s=settings.workflow_lease_s,
            stale_s=settings.workflow_stale_s,
        )
        if runtime_engine
        else None
    )
    runtime.workflows = workflows

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
            tasks.append(asyncio.create_task(forwarder.run()))
        if settings.audit_database_url:
            tasks.append(asyncio.create_task(ship_audit(app, settings.audit_database_url)))
        if workflows is not None and settings.workflow_worker:
            tasks.append(asyncio.create_task(workflows.run_worker()))
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
    app.state.runtime = runtime
    app.state.events = forwarder
    app.state.workflows = workflows
    store = exec_store
    app.include_router(oauth_router(ctx))
    app.include_router(mcp_router(McpGateway(ctx, runtime)))
    app.include_router(confirm_router(ctx, runtime, workflows))

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

    @app.post("/api/v1/invoke/{capability_id}", tags=["invoke"], operation_id="invoke")
    async def invoke(
        capability_id: str,
        body: InvokeIn,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_user_assertion: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """统一执行链的 REST 出口；业务失败也返回 200 + status=failed（docs/api/conventions.md）。"""
        principal = await ctx.principal_from_request(authorization, x_user_assertion)
        return await runtime.invoke(
            principal,
            capability_id,
            body.args,
            idempotency_key=idempotency_key,
            exposure="rest",
            channel=body.channel,
            version=body.version,
            trace_id=request.state.trace_id,
        )

    @app.get("/api/v1/executions/{id}", tags=["invoke"], operation_id="getExecution")
    async def get_execution(
        id: str,
        authorization: Annotated[str | None, Header()] = None,
        x_user_assertion: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await ctx.principal_from_request(authorization, x_user_assertion)
        rec = await store.get(id)
        if rec is None or str(rec["consumer_id"]) != principal.consumer_id:
            raise PlatformError(ErrorCode.NOT_FOUND, "执行记录不存在")
        return {
            "execution_id": rec["id"],
            "status": rec["status"],
            "data": rec.get("result"),
            "error": {"code": rec["error_code"]} if rec.get("error_code") else None,
            "trace_id": rec["trace_id"],
        }

    @app.get("/api/v1/runs/{id}", tags=["invoke"], operation_id="getRun")
    async def get_run(
        id: str,
        authorization: Annotated[str | None, Header()] = None,
        x_user_assertion: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = await ctx.principal_from_request(authorization, x_user_assertion)
        if workflows is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "Run 不存在")
        view = await workflows.view(id, consumer_id=principal.consumer_id)
        return {
            "run_id": view["run_id"],
            "status": view["status"],
            "result": view["result"],
            "error": view["error"],
            "tainted": view["tainted"],
            "pending_tasks": [{k: t[k] for k in ("id", "kind", "summary", "deadline")} for t in view["pending_tasks"]],
            "steps": view["steps"],
            "trace_id": view["trace_id"],
        }

    return app


app = create_app(control_plane_url=get_settings().control_plane_url or None)
