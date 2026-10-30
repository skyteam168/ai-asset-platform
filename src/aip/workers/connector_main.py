"""独立进程入口：`python -m aip.workers.connector_main`。"""

from __future__ import annotations

import os

import uvicorn

from aip.workers.connector_app import create_connector_worker_app


def main() -> None:
    host = os.environ.get("AIP_CONNECTOR_WORKER_HOST", "0.0.0.0")  # noqa: S104 — 集群内 Service 需全接口
    port = int(os.environ.get("AIP_CONNECTOR_WORKER_PORT", "18090"))
    app = create_connector_worker_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
