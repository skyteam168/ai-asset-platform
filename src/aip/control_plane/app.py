"""控制面应用：IAM、Registry、Release、Grant 等模块的路由在各自里程碑挂载。"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from aip.control_plane.conn.api import router as conn_router
from aip.control_plane.console_api import router as console_router
from aip.control_plane.db import Database
from aip.control_plane.gates.api import router as gates_router
from aip.control_plane.iam.api import router as iam_router
from aip.control_plane.importer.api import router as importer_router
from aip.control_plane.internal_api import router as internal_router
from aip.control_plane.registry.api import router as registry_router
from aip.control_plane.risk.api import router as risk_router
from aip.control_plane.risk.api import scan_forever
from aip.control_plane.skill.api import router as skill_router
from aip.control_plane.snapshot_feed import SnapshotFeed
from aip.control_plane.todo_api import router as todo_router
from aip.control_plane.webhook.api import deliver_forever, wire_risk_notifier
from aip.control_plane.webhook.api import router as webhook_router
from aip.libs.common import web
from aip.libs.common.config import get_settings

CONSOLE_DIST = pathlib.Path(__file__).resolve().parents[3] / "console" / "dist"


def create_app(db: Database | None = None) -> FastAPI:
    settings = get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        wire_risk_notifier(app.state.db)
        scanner = (
            asyncio.create_task(scan_forever(app.state.db, settings.risk_scan_interval_s))
            if settings.risk_scan_interval_s > 0
            else None
        )
        delivery = asyncio.create_task(deliver_forever(app.state.db, 5.0))
        yield
        delivery.cancel()
        if scanner is not None:
            scanner.cancel()
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
    app.include_router(console_router())
    app.include_router(todo_router())
    app.include_router(risk_router())
    app.include_router(webhook_router())
    app.include_router(skill_router())
    app.include_router(importer_router())
    # 控制台静态文件（console/dist，构建后存在时才挂载）；同源访问，无需 CORS
    console_dist = pathlib.Path(settings.console_dist) if settings.console_dist else CONSOLE_DIST
    if console_dist.is_dir():
        app.mount("/console", StaticFiles(directory=console_dist, html=True), name="console")

    @app.get("/healthz", response_model=web.Health, tags=["ops"], operation_id="cpHealth")
    async def healthz() -> web.Health:
        return web.Health(status="ok", service="control-plane", version=settings.version)

    return app


app = create_app()
