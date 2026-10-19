"""数据面应用：Gateway（MCP + REST + OAuth）、Runtime 等在各自里程碑挂载。"""

from __future__ import annotations

from fastapi import FastAPI, Response

from aip.libs.common import web
from aip.libs.common.config import get_settings


class Readiness:
    """就绪条件：IAM 快照、发布快照与策略 bundle 均已加载（architecture.md 4.4，缺失即拒绝服务）。"""

    def __init__(self) -> None:
        self.iam_snapshot_seq: int | None = None
        self.release_snapshot_seq: int | None = None
        self.policy_bundle: str | None = None

    def checks(self) -> dict[str, str]:
        return {
            "iam_snapshot": "loaded" if self.iam_snapshot_seq is not None else "missing",
            "release_snapshot": "loaded" if self.release_snapshot_seq is not None else "missing",
            "policy_bundle": "loaded" if self.policy_bundle is not None else "missing",
        }

    @property
    def ready(self) -> bool:
        return all(v == "loaded" for v in self.checks().values())


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AIP Data Plane API", version=settings.version)
    web.install(app)
    readiness = Readiness()
    app.state.readiness = readiness

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

    return app


app = create_app()
