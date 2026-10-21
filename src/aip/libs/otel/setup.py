"""OpenTelemetry TracerProvider 装配（architecture.md 12.4）。"""

from __future__ import annotations

import atexit
import logging
from typing import Any, Literal

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util._once import Once

log = logging.getLogger("aip.otel")

ExporterKind = Literal["none", "console", "otlp", "memory"]

_PROVIDER: TracerProvider | None = None
_MEMORY_EXPORTER: Any = None


def memory_exporter() -> Any:
    """测试用：返回当前 InMemorySpanExporter（若已以 memory 模式初始化）。"""
    return _MEMORY_EXPORTER


def setup_tracing(
    *,
    service_name: str,
    exporter: ExporterKind = "none",
    otlp_endpoint: str = "",
    force: bool = False,
) -> TracerProvider:
    """初始化全局 TracerProvider；重复调用默认忽略（force=True 可重置，供测试）。"""
    global _PROVIDER, _MEMORY_EXPORTER
    if _PROVIDER is not None and not force:
        return _PROVIDER
    if force and _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception as e:
            log.debug("otel provider shutdown: %s", e)
        _PROVIDER = None
        _MEMORY_EXPORTER = None
        # SDK 默认只允许 set_tracer_provider 一次
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE = Once()

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = otlp_endpoint or "http://127.0.0.1:4318/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    elif exporter == "memory":
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        _MEMORY_EXPORTER = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_MEMORY_EXPORTER))
    # none：仍注册 Provider，便于进程内 get_current_span，但不导出

    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    _PROVIDER = provider
    def _atexit() -> None:
        _safe_shutdown(provider)

    atexit.register(_atexit)
    log.info("otel tracing ready service=%s exporter=%s", service_name, exporter)
    return provider


def _safe_shutdown(provider: TracerProvider) -> None:
    try:
        provider.shutdown()
    except Exception as e:
        log.debug("otel shutdown: %s", e)


def get_tracer(name: str = "aip") -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id_hex() -> str:
    """当前 span 的 trace_id（32 hex）；无活跃 span 时返回空串。"""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return ""
    return format(ctx.trace_id, "032x")


def extract_context(carrier: dict[str, str]) -> otel_context.Context:
    return propagate.extract(carrier)


def inject_headers(headers: dict[str, str]) -> dict[str, str]:
    """把当前上下文写入 headers（W3C traceparent）。"""
    out = dict(headers)
    propagate.inject(out)
    return out
