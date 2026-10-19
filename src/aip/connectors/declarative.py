"""声明式 REST Connector（architecture.md 9.1）：YAML / JSON 映射，无代码覆盖大多数 REST 接口。

- 模板：沙箱化 Jinja2（不可变沙箱，只开放白名单过滤器），值统一做 URL / 头部编码；
- 响应：JSONPath 取数与字段映射；success_when 判定业务成功（HTTP 200 但业务码失败 → 失败）；errors 映射业务码。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from aip.connectors.base import ConnectorResult, ExecContext, ReconcileResult, http_status_error
from aip.connectors.credentials import CredentialManager
from aip.connectors.jsonpath import find, first
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import map_error

_EXPR = re.compile(r"^\s*(\$[^\s=!]*)\s*(==|!=)\s*(.+?)\s*$")


def _env(ctx: ExecContext) -> ImmutableSandboxedEnvironment:
    env = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    env.filters.clear()
    env.filters.update(
        {
            "default": lambda v, d="": d if isinstance(v, Undefined) or v is None or v == "" else v,
            "max": lambda v, m: min(int(v), int(m)),  # “不超过 m”
            "min": lambda v, m: max(int(v), int(m)),
            "upper": lambda v: str(v).upper(),
            "external_code": lambda org_id, system: ctx.external_codes.get(system, {}).get(str(org_id), ""),
        }
    )
    env.globals.clear()
    return env


def _render(env: ImmutableSandboxedEnvironment, template: str, variables: dict[str, Any]) -> str:
    try:
        return env.from_string(template).render(**variables)
    except TemplateError as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "参数映射失败（缺少必需参数）") from e


def evaluate(expr: str, doc: Any) -> bool:
    """mini 表达式：`<jsonpath> == <JSON 字面量>` 或 `!=`。"""
    m = _EXPR.match(expr)
    if not m:
        raise PlatformError(ErrorCode.INTERNAL, "Connector 映射中的表达式不合法")
    path, op, literal = m.groups()
    value = first(path, doc)
    expected = json.loads(literal)
    return bool(value == expected) if op == "==" else bool(value != expected)


class DeclarativeConnector:
    def __init__(self, name: str, config: dict[str, Any], credentials: CredentialManager | None = None) -> None:
        self.name = name
        self.ops: dict[str, dict[str, Any]] = {o["operation"]: o for o in config.get("operations", [])}
        self.credentials = credentials

    def capabilities(self) -> list[str]:
        return sorted(self.ops)

    def is_write(self, op: str) -> bool:
        return bool(self.ops[op].get("mode", "read") == "write")

    async def execute(self, op: str, params: dict[str, Any], ctx: ExecContext) -> ConnectorResult:
        mapping = self.ops.get(op)
        if mapping is None:
            raise PlatformError(ErrorCode.INTERNAL, f"Connector {self.name} 没有操作 {op}")
        if self.is_write(op):
            ctx.guard_write()
        assert ctx.http is not None
        env = _env(ctx)
        variables = {"args": params}
        req = mapping["request"]
        path = _render(env, req["path"], variables)
        query = {k: _render(env, v, variables) for k, v in (req.get("query") or {}).items()}
        query = {k: v for k, v in query.items() if v != ""}
        headers = {k: _render(env, v, variables) for k, v in (req.get("headers") or {}).items()}
        body = None
        if "body" in req:
            body = json.loads(_render(env, json.dumps(req["body"], ensure_ascii=False), variables))
        if ctx.idempotency_key:
            headers["Idempotency-Key"] = ctx.idempotency_key
        for attempt in (1, 2):  # 401 刷新凭据后最多重试一次
            auth = await self.credentials.headers(ctx.http) if self.credentials else {}
            try:
                async with ctx.http() as client:
                    r = await client.request(
                        req["method"],
                        path,
                        params=query,
                        headers={**headers, **auth},
                        json=body,
                        timeout=max(ctx.require_time(), 0.05),
                    )
            except Exception as e:
                raise map_error(e) from e
            if r.status_code == 401 and self.credentials and attempt == 1:
                self.credentials.invalidate(next(iter(auth.values()), None))
                continue
            break
        err = http_status_error(r.status_code)
        if err is not None:
            raise err
        return self._classify(mapping, r)

    def _classify(self, mapping: dict[str, Any], r: httpx.Response) -> ConnectorResult:
        try:
            doc = r.json()
        except ValueError as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "下游返回的不是 JSON") from e
        for rule in mapping.get("errors", []):
            if evaluate(rule["when"], doc):
                raise PlatformError(ErrorCode(rule["map"]), "下游业务规则拒绝")
        resp = mapping["response"]
        if "success_when" in resp and not evaluate(resp["success_when"], doc):
            raise PlatformError(ErrorCode.BUSINESS_RULE_VIOLATION, "下游返回业务失败")
        values = find(resp["data"], doc)
        fields = resp.get("fields")
        if fields:
            values = [{k: first(p, v) for k, p in fields.items()} for v in values]
        data: Any = values if "[*]" in resp["data"] else (values[0] if values else None)
        cursor = None
        if resp.get("next_cursor"):
            nxt = first(resp["next_cursor"], doc)
            cursor = str(nxt) if nxt is not None else None
        return ConnectorResult(data=data, next_cursor=cursor, status_code=r.status_code)

    async def health(self, ctx: ExecContext) -> bool:
        assert ctx.http is not None
        try:
            async with ctx.http() as client:
                return (await client.get("/")).status_code < 500
        except Exception:
            return False

    async def reconcile(self, op: str, idempotency_key: str, ctx: ExecContext) -> ReconcileResult:
        """写操作对账：mapping.reconcile 指向一个只读操作，用幂等键查询是否已执行。"""
        spec = self.ops[op].get("reconcile")
        if not spec:
            return ReconcileResult("unknown")
        try:
            res = await self.execute(spec["operation"], {"idempotency_key": idempotency_key}, ctx)
        except PlatformError:
            return ReconcileResult("unknown")
        found = bool(res.data.get(spec.get("found_field", "found"))) if isinstance(res.data, dict) else bool(res.data)
        return ReconcileResult("succeeded" if found else "not_executed", res.data)


def kb_connector(name: str, config: dict[str, Any], credentials: CredentialManager | None) -> DeclarativeConnector:
    """知识库 Connector（architecture.md 9.4）：只读，search 与 get_document 两个预置操作，路径可配置。"""
    paths = {
        "search": "/api/search",
        "get_document": "/api/documents/{{ args.document_id }}",
        **config.get("paths", {}),
    }
    ops = [
        {
            "operation": "search",
            "mode": "read",
            "request": {
                "method": "GET",
                "path": paths["search"],
                "query": {"q": "{{ args.query }}", "limit": "{{ args.limit | default(5) | max(20) }}"},
            },
            "response": {
                "data": "$.items[*]",
                "fields": {"document_id": "$.id", "title": "$.title", "url": "$.url", "snippet": "$.snippet"},
            },
        },
        {
            "operation": "get_document",
            "mode": "read",
            "request": {"method": "GET", "path": paths["get_document"]},
            "response": {
                "data": "$",
                "fields": {"document_id": "$.id", "title": "$.title", "url": "$.url", "content": "$.content"},
            },
        },
    ]
    return DeclarativeConnector(name, {"operations": ops}, credentials)
