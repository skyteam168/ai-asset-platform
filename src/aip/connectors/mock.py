"""MockConnector：联调与 Runtime 测试用，可注入延迟、失败与“写入后超时”。"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from aip.connectors.base import ConnectorResult, ExecContext, ReconcileResult
from aip.libs.common.errors import ErrorCode, PlatformError


class MockConnector:
    """config = {"operations": {"op": {"response": ..., "write": bool, "delay_s": 0, "fail_times": 0,
    "fail_code": "DOWNSTREAM_UNAVAILABLE", "commit_then_hang": bool}}}"""

    # 跨实例共享：模拟下游系统里“已经执行过的写操作”，供对账查询
    executed: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.ops: dict[str, dict[str, Any]] = config.get("operations", {})
        self.calls: dict[str, int] = {}

    def capabilities(self) -> list[str]:
        return sorted(self.ops)

    def is_write(self, op: str) -> bool:
        return bool(self.ops[op].get("write"))

    async def execute(self, op: str, params: dict[str, Any], ctx: ExecContext) -> ConnectorResult:
        o = self.ops[op]
        if self.is_write(op):
            ctx.guard_write()
        n = self.calls[op] = self.calls.get(op, 0) + 1
        if n <= int(o.get("fail_times", 0)):
            raise PlatformError(ErrorCode(o.get("fail_code", "DOWNSTREAM_UNAVAILABLE")), "注入的失败")
        if o.get("commit_then_hang") and ctx.idempotency_key:
            MockConnector.executed[f"{self.name}:{ctx.idempotency_key}"] = {"params": params}
            await asyncio.sleep(max(ctx.remaining(), 0) + 1)
        if o.get("delay_s"):
            await asyncio.sleep(float(o["delay_s"]))
        if self.is_write(op) and ctx.idempotency_key:
            MockConnector.executed[f"{self.name}:{ctx.idempotency_key}"] = {"params": params}
        return ConnectorResult(data=o.get("response"))

    async def health(self, ctx: ExecContext) -> bool:
        return True

    async def reconcile(self, op: str, idempotency_key: str, ctx: ExecContext) -> ReconcileResult:
        hit = MockConnector.executed.get(f"{self.name}:{idempotency_key}")
        return ReconcileResult("succeeded", self.ops[op].get("response")) if hit else ReconcileResult("not_executed")
