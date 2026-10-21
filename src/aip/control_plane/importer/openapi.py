"""解析 OpenAPI 3.x 文档为中间 operation 列表。"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from aip.libs.common.errors import ErrorCode, PlatformError

_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_PATH_PARAM = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def load_document(raw: str) -> dict[str, Any]:
    """YAML 或 JSON → dict。"""
    text = raw.strip()
    if not text:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "OpenAPI 文档为空")
    try:
        if text[0] in "{[":
            doc = json.loads(text)
        else:
            doc = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"无法解析 OpenAPI：{e}") from e
    if not isinstance(doc, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "OpenAPI 根节点须为对象")
    if not str(doc.get("openapi") or "").startswith("3."):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 OpenAPI 3.x")
    if not isinstance(doc.get("paths"), dict) or not doc["paths"]:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "OpenAPI 缺少 paths")
    return doc


def _resolve_ref(doc: dict[str, Any], node: Any) -> Any:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = str(node["$ref"])
    if not ref.startswith("#/"):
        return node
    cur: Any = doc
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            return node
        cur = cur[part]
    return cur


def _schema_props(doc: dict[str, Any], schema: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    if not schema:
        return {}, []
    schema = _resolve_ref(doc, schema)
    if not isinstance(schema, dict):
        return {}, []
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    out: dict[str, Any] = {}
    for name, prop in props.items():
        prop = _resolve_ref(doc, prop) if isinstance(prop, dict) else prop
        if not isinstance(prop, dict):
            out[name] = {"type": "string"}
            continue
        t = prop.get("type", "string")
        item: dict[str, Any] = {"type": t} if isinstance(t, str) else {"type": "string"}
        if "enum" in prop:
            item["enum"] = prop["enum"]
        out[name] = item
    return out, required


def to_snake(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return s or "op"


def op_name(operation_id: str | None, method: str, path: str) -> str:
    if operation_id:
        return to_snake(operation_id)
    slug = to_snake(path.replace("/", "_").strip("_") or "root")
    return f"{method}_{slug}"


def parse_operations(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (operations, warnings)。每个 operation 含 method/path/params/response_schema 等。"""
    ops: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method not in _METHODS or not isinstance(op, dict):
                continue
            responses = op.get("responses") or {}
            if not responses:
                warnings.append(f"{method.upper()} {path}: 无 responses，已跳过")
                continue
            success = None
            for code in ("200", "201", "default"):
                if code in responses:
                    success = _resolve_ref(doc, responses[code])
                    break
            if success is None:
                # 取第一个 2xx
                for code, resp in responses.items():
                    if str(code).startswith("2"):
                        success = _resolve_ref(doc, resp)
                        break
            if not isinstance(success, dict):
                warnings.append(f"{method.upper()} {path}: 无成功响应，已跳过")
                continue
            content = success.get("content") or {}
            json_body = content.get("application/json") or content.get("application/problem+json")
            if not json_body:
                warnings.append(f"{method.upper()} {path}: 非 JSON 响应，已跳过")
                continue
            schema = _resolve_ref(doc, (json_body or {}).get("schema") or {})
            params: list[dict[str, Any]] = []
            for p in list(item.get("parameters") or []) + list(op.get("parameters") or []):
                p = _resolve_ref(doc, p)
                if isinstance(p, dict):
                    params.append(p)
            body_schema = None
            if "requestBody" in op:
                rb = _resolve_ref(doc, op["requestBody"])
                if isinstance(rb, dict):
                    rc = rb.get("content") or {}
                    jb = rc.get("application/json")
                    if isinstance(jb, dict):
                        body_schema = _resolve_ref(doc, jb.get("schema") or {})
            name = op_name(op.get("operationId"), method, path)
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                name = f"op_{abs(hash(name)) % 10_000:04d}"
            ops.append(
                {
                    "operation": name,
                    "operation_id": op.get("operationId") or name,
                    "method": method.upper(),
                    "path": path,
                    "summary": str(op.get("summary") or op.get("description") or name),
                    "description": str(
                        op.get("description") or op.get("summary") or f"Imported {method.upper()} {path}"
                    ),
                    "parameters": params,
                    "body_schema": body_schema if isinstance(body_schema, dict) else None,
                    "response_schema": schema if isinstance(schema, dict) else {},
                }
            )
    if not ops:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "未从 OpenAPI 解析出任何可用 operation")
    return ops, warnings
