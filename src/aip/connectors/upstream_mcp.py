"""上游 MCP Connector（architecture.md 9.1、ADR-0014）。

不可信来源：调用前核对 tools/list 的内容哈希；结果由 Tool Spec 的 untrusted_output 标记。
出站必须经 ctx.http()（libs/httpclient）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from aip.connectors.base import ConnectorResult, ExecContext, ReconcileResult, http_status_error
from aip.connectors.credentials import CredentialManager
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import map_error
from aip.libs.tokens.keys import canonical_json


def tools_manifest_hash(tools: list[dict[str, Any]]) -> str:
    """对 tools/list 结果做确定性哈希（按 name 排序，只取契约字段）。"""
    slim = []
    for t in sorted(tools, key=lambda x: str(x.get("name", ""))):
        slim.append(
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "inputSchema": t.get("inputSchema") or t.get("input_schema") or {},
            }
        )
    return hashlib.sha256(canonical_json(slim)).hexdigest()


class UpstreamMcpConnector:
    """config:
    - mcp_path: 上游 MCP 路径，默认 /mcp
    - tools_manifest_hash: 钉住的 tools/list 哈希（必填）
    - verify_on_call: 每次调用前复核哈希，默认 true
    - operations: { local_op: { tool: upstream_name, write?: bool } }
    - auth: 交给 CredentialManager（可选）
    """

    def __init__(self, name: str, config: dict[str, Any], credentials: CredentialManager | None = None) -> None:
        self.name = name
        self.mcp_path = str(config.get("mcp_path") or "/mcp")
        self.expected_hash = str(config.get("tools_manifest_hash") or "")
        if not self.expected_hash:
            raise PlatformError(ErrorCode.INTERNAL, f"上游 MCP Connector {name} 缺少 tools_manifest_hash")
        self.verify_on_call = bool(config.get("verify_on_call", True))
        self.ops: dict[str, dict[str, Any]] = dict(config.get("operations") or {})
        self.credentials = credentials
        self._rid = 0

    def capabilities(self) -> list[str]:
        return sorted(self.ops)

    def is_write(self, op: str) -> bool:
        return bool(self.ops.get(op, {}).get("write"))

    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    async def _rpc(self, ctx: ExecContext, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert ctx.http is not None
        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params or {}}
        auth = await self.credentials.headers(ctx.http) if self.credentials else {}
        try:
            async with ctx.http() as client:
                r = await client.post(self.mcp_path, json=body, headers=auth)
        except Exception as e:
            raise map_error(e) from e
        err = http_status_error(r.status_code)
        if err is not None:
            raise err
        try:
            payload = r.json()
        except ValueError as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 MCP 返回非 JSON") from e
        if not isinstance(payload, dict):
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 MCP 响应格式错误")
        if "error" in payload:
            err_obj = payload["error"] if isinstance(payload["error"], dict) else {}
            raise PlatformError(
                ErrorCode.DOWNSTREAM_UNAVAILABLE,
                f"上游 MCP 错误：{err_obj.get('message', err_obj)}",
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 MCP 缺少 result")
        return result

    async def verify_manifest(self, ctx: ExecContext) -> list[dict[str, Any]]:
        """拉取 tools/list 并与钉住的哈希比对；不一致视为 rug pull。"""
        result = await self._rpc(ctx, "tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "上游 tools/list 无效")
        digest = tools_manifest_hash([t for t in tools if isinstance(t, dict)])
        if digest != self.expected_hash:
            raise PlatformError(
                ErrorCode.PRECONDITION_FAILED,
                "上游 MCP 工具清单与钉住的内容哈希不一致（疑似 rug pull）",
            )
        return [t for t in tools if isinstance(t, dict)]

    async def execute(self, op: str, params: dict[str, Any], ctx: ExecContext) -> ConnectorResult:
        mapping = self.ops.get(op)
        if mapping is None:
            raise PlatformError(ErrorCode.INTERNAL, f"Connector {self.name} 没有操作 {op}")
        if self.is_write(op):
            ctx.guard_write()
        if self.verify_on_call:
            await self.verify_manifest(ctx)
        tool = mapping.get("tool") or op
        result = await self._rpc(ctx, "tools/call", {"name": tool, "arguments": params})
        if result.get("isError"):
            text = ""
            for c in result.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = str(c.get("text") or "")
                    break
            raise PlatformError(ErrorCode.BUSINESS_RULE_VIOLATION, text or "上游工具返回 isError")
        data = result.get("structuredContent")
        if data is None:
            # 回退：拼 text content
            parts = [
                str(c.get("text"))
                for c in (result.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            data = {"text": "\n".join(parts)} if parts else {}
        return ConnectorResult(data=data)

    async def health(self, ctx: ExecContext) -> bool:
        try:
            await self.verify_manifest(ctx)
            return True
        except PlatformError:
            return False

    async def reconcile(self, op: str, idempotency_key: str, ctx: ExecContext) -> ReconcileResult:
        # 上游 MCP 一般无法对账写结果
        return ReconcileResult("unknown")


def pin_from_tools_list(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """从 tools/list 结果生成可写入 Connector config 的钉扎片段。"""
    clean = [t for t in tools if isinstance(t, dict) and t.get("name")]
    return {
        "tools_manifest_hash": tools_manifest_hash(clean),
        "pinned_tools": [
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "inputSchema": t.get("inputSchema") or {},
            }
            for t in sorted(clean, key=lambda x: str(x.get("name")))
        ],
    }
