"""OpenTelemetry 追踪（architecture.md 12.4）。"""

from aip.libs.otel.setup import (
    current_trace_id_hex,
    extract_context,
    get_tracer,
    inject_headers,
    memory_exporter,
    setup_tracing,
)

__all__ = [
    "current_trace_id_hex",
    "extract_context",
    "get_tracer",
    "inject_headers",
    "memory_exporter",
    "setup_tracing",
]
