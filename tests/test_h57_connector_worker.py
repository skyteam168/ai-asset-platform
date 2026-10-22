"""H57：Connector worker 模式骨架。"""

from __future__ import annotations

import pytest

from aip.libs.common.config import Settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.connector_worker import (
    InlineConnectorWorker,
    RpcConnectorWorker,
    WorkerJob,
    build_connector_worker,
)


@pytest.mark.asyncio
async def test_inline_echo() -> None:
    w = InlineConnectorWorker()
    r = await w.execute(
        WorkerJob(
            connector_kind="rest",
            op="ping",
            params={},
            connection_id="c1",
            idempotency_key="k1",
        )
    )
    assert r.ok and r.data["mode"] == "inline"


@pytest.mark.asyncio
async def test_rpc_not_wired() -> None:
    w = RpcConnectorWorker("http://workers.internal")
    with pytest.raises(PlatformError) as ei:
        await w.execute(
            WorkerJob("rest", "ping", {}, "c1", "k1"),
        )
    assert ei.value.code == ErrorCode.PRECONDITION_FAILED


def test_build_from_settings() -> None:
    assert build_connector_worker(Settings(connector_worker_mode="inline")).mode == "inline"
    assert isinstance(
        build_connector_worker(Settings(connector_worker_mode="rpc", connector_worker_url="http://x")),
        RpcConnectorWorker,
    )


def test_connector_worker_doc_exists() -> None:
    from pathlib import Path

    assert (Path(__file__).resolve().parents[1] / "docs" / "ops" / "connector-worker.md").is_file()
