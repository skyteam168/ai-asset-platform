"""控制面应用：IAM、Registry、Release、Grant 等模块的路由在各自里程碑挂载。"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from aip.control_plane.conn.api import router as conn_router
from aip.control_plane.db import Database
from aip.control_plane.gates.api import router as gates_router
from aip.control_plane.iam.api import router as iam_router
from aip.control_plane.internal_api import router as internal_router
from aip.control_plane.registry.api import router as registry_router
from aip.control_plane.snapshot_feed import SnapshotFeed
from aip.libs.common import web
from aip.libs.common.config import get_settings


def create_app(db: Database | None = None) -> FastAPI:
    settings = get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.snapshot_feed.close()
        await app.state.db.dispose()

    app = FastAPI(title="AIP Control Plane API", version=settings.version, lifespan=lifespan)
    app.state.db = db or Database()
    app.state.snapshot_feed = SnapshotFeed(app.state.db)
    web.install(app)
    app.include_router(iam_router())
    app.include_router(registry_router())
    app.include_router(conn_router())
    app.include_router(gates_router())
    app.include_router(internal_router())

    @app.get("/healthz", response_model=web.Health, tags=["ops"], operation_id="cpHealth")
    async def healthz() -> web.Health:
        return web.Health(status="ok", service="control-plane", version=settings.version)

    return app


app = create_app()
