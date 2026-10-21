"""H5：OpenTelemetry 追踪贯通 Gateway → Runtime → 出站。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from opentelemetry import propagate
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.libs.otel import current_trace_id_hex, get_tracer, inject_headers, memory_exporter, setup_tracing
from iam_seed import app_url
from m3_world import World, connector, tool


@pytest.fixture(autouse=True)
def _otel_memory() -> Any:
    setup_tracing(service_name="aip-test", exporter="memory", force=True)
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    exp = memory_exporter()
    assert exp is not None
    exp.clear()
    yield exp
    exp.clear()


@pytest.mark.asyncio
async def test_http_middleware_creates_server_span(_otel_memory: Any) -> None:
    # create_app 会 setup none，再强制 memory
    app = create_cp(db=Database(app_url()))
    setup_tracing(service_name="aip-control-plane", exporter="memory", force=True)
    exp = memory_exporter()
    assert exp is not None
    exp.clear()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://cp") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200
        assert len(r.headers.get("X-Trace-Id", "")) == 32

    names = {s.name for s in exp.get_finished_spans()}
    assert any(n.startswith("GET ") for n in names)


def test_inject_headers_from_current_span(_otel_memory: Any) -> None:
    tracer = get_tracer("t")
    with tracer.start_as_current_span("parent"):
        tid = current_trace_id_hex()
        headers = inject_headers({})
        assert "traceparent" in headers
        assert tid in headers["traceparent"]


@pytest.mark.asyncio
async def test_runtime_invoke_span(tmp_path: Any, _otel_memory: Any) -> None:
    setup_tracing(service_name="aip-data-plane", exporter="memory", force=True)
    exp = memory_exporter()
    assert exp is not None
    exp.clear()
    cap = "production.mes.query_work_orders"
    w = World(
        tmp_path,
        [tool(cap, "mes", "query")],
        [connector("mes", "mock", {"operations": {"query": {"response": [{"wo": "1"}]}}})],
    )
    await w.ready()
    r = await w.runtime.invoke(w.principal(), cap, {"factory_id": "F001"})
    assert r["status"] == "succeeded", r
    assert any(s.name == "tool.invoke" for s in exp.get_finished_spans())
    inv = next(s for s in exp.get_finished_spans() if s.name == "tool.invoke")
    assert inv.attributes.get("aip.capability_id") == cap


def test_traceparent_propagation_parent_child(_otel_memory: Any) -> None:
    tracer = get_tracer("t")
    with tracer.start_as_current_span("root") as root:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        ctx = propagate.extract(carrier)
        with tracer.start_as_current_span("child", context=ctx) as child:
            assert child.get_span_context().trace_id == root.get_span_context().trace_id
            assert child.parent is not None
