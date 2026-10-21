"""解析 Postman Collection v2.x → 与 openapi.parse 同形的中间 operations。"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from aip.control_plane.importer.openapi import op_name
from aip.libs.common.errors import ErrorCode, PlatformError

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_SKIP_HEADERS = frozenset({"authorization", "content-type"})
_VAR = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")
_COLON = re.compile(r":([a-zA-Z0-9_]+)")
_SCHEMA_OK = re.compile(r"collection/v2", re.I)


def load_collection(raw: str) -> dict[str, Any]:
    """JSON Collection → dict。"""
    text = raw.strip()
    if not text:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Postman Collection 为空")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"无法解析 Postman Collection：{e}") from e
    if not isinstance(doc, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Collection 根节点须为对象")
    if not isinstance(doc.get("info"), dict) or not isinstance(doc.get("item"), list):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "须为 Postman Collection（含 info 与 item）")
    schema = str((doc.get("info") or {}).get("schema") or "")
    if schema and not _SCHEMA_OK.search(schema):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "仅支持 Postman Collection v2.x")
    if not schema:
        # 无 schema 字段时仍接受（部分导出省略），靠 info+item 结构
        pass
    return doc


def _vars(doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in doc.get("variable") or []:
        if isinstance(v, dict) and v.get("key") is not None:
            out[str(v["key"])] = str(v.get("value") or "")
    return out


def _subst(text: str, variables: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return variables[key] if key in variables else m.group(0)

    return _VAR.sub(repl, text)


def _schema_from_example(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        if not value:
            return {"type": "array", "items": {"type": "object"}}
        return {"type": "array", "items": _schema_from_example(value[0])}
    if isinstance(value, dict):
        props = {k: _schema_from_example(v) for k, v in value.items()}
        return {"type": "object", "properties": props, "required": list(props)}
    return {"type": "string"}


def _path_from_url(url: Any, variables: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    """返回 (openapi 形 path, query parameters)。"""
    query_params: list[dict[str, Any]] = []
    if isinstance(url, str):
        url = {"raw": url}
    if not isinstance(url, dict):
        return "/", query_params

    path_parts: list[str] = []
    if isinstance(url.get("path"), list) and url["path"]:
        for p in url["path"]:
            s = _subst(str(p), variables).strip("/")
            if not s:
                continue
            s = _COLON.sub(r"{\1}", s)
            s = _VAR.sub(r"{\1}", s)
            path_parts.append(s)
        path = "/" + "/".join(path_parts)
    else:
        raw = _subst(str(url.get("raw") or ""), variables)
        parsed = urlparse(raw if "://" in raw else f"https://placeholder{raw if raw.startswith('/') else '/' + raw}")
        path = parsed.path or "/"
        path = _COLON.sub(r"{\1}", path)
        path = _VAR.sub(r"{\1}", path)

    for q in url.get("query") or []:
        if not isinstance(q, dict) or q.get("disabled"):
            continue
        name = str(q.get("key") or "").strip()
        if not name:
            continue
        val = q.get("value")
        schema: dict[str, Any] = {"type": "string"}
        required = val is not None and str(val) != ""
        if val is not None and str(val).isdigit():
            schema = {"type": "integer", "default": int(val)}
            # 常见分页上限启发式
            if name.lower() in ("pagesize", "page_size", "limit", "size"):
                schema["maximum"] = 200
                schema["default"] = min(int(val), 200) if int(val) else 50
            required = False
        elif val is None or str(val) == "":
            required = False
        query_params.append({"name": name, "in": "query", "required": required, "schema": schema})
    return path or "/", query_params


def _body_schema(request: dict[str, Any]) -> dict[str, Any] | None:
    body = request.get("body")
    if not isinstance(body, dict):
        return None
    mode = body.get("mode")
    if mode and mode not in ("raw",):
        return None  # formdata / file / graphql 由调用方跳过
    raw = body.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return _schema_from_example(data)
    return None


def _response_schema(responses: list[Any]) -> tuple[dict[str, Any], bool]:
    for resp in responses:
        if not isinstance(resp, dict):
            continue
        code = resp.get("code")
        if code is None:
            try:
                code = int(str(resp.get("status") or "").split()[0])
            except (ValueError, IndexError):
                continue
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            continue
        if not 200 <= code_i < 300:
            continue
        body = resp.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        return _schema_from_example(data), True
    return {}, False


def _unique_name(base: str, used: set[str]) -> str:
    name = base if re.match(r"^[a-z][a-z0-9_]*$", base) else f"op_{abs(hash(base)) % 10_000:04d}"
    if name not in used:
        used.add(name)
        return name
    n = 2
    while f"{name}_{n}" in used:
        n += 1
    out = f"{name}_{n}"
    used.add(out)
    return out


def _walk_items(
    items: list[Any],
    *,
    variables: dict[str, str],
    used: set[str],
    ops: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("item"), list):
            _walk_items(item["item"], variables=variables, used=used, ops=ops, warnings=warnings)
            continue
        req = item.get("request")
        if req is None:
            continue
        if isinstance(req, str):
            warnings.append(f"{item.get('name')}: 请求仅为 URL 字符串，已跳过")
            continue
        if not isinstance(req, dict):
            continue
        method = str(req.get("method") or "GET").upper()
        if method not in _METHODS:
            warnings.append(f"{item.get('name')}: 不支持的方法 {method}，已跳过")
            continue
        body = req.get("body")
        if isinstance(body, dict) and body.get("mode") in ("formdata", "file", "graphql"):
            warnings.append(f"{item.get('name')}: {body.get('mode')} 请求已跳过")
            continue
        path, query_params = _path_from_url(req.get("url"), variables)
        headers: list[dict[str, Any]] = []
        for h in req.get("header") or []:
            if not isinstance(h, dict) or h.get("disabled"):
                continue
            key = str(h.get("key") or "").strip()
            if not key or key.lower() in _SKIP_HEADERS:
                continue
            headers.append(
                {"name": key, "in": "header", "required": True, "schema": {"type": "string"}}
            )
        params = query_params + headers
        body_schema = _body_schema(req)
        resp_schema, has_example = _response_schema(list(item.get("response") or []))
        name_src = str(item.get("name") or "")
        base = op_name(name_src or None, method.lower(), path)
        name = _unique_name(base, used)
        desc = str(req.get("description") or item.get("name") or f"Imported {method} {path}")
        if not has_example:
            warnings.append(f"{method} {path}: 无示例响应，response 映射为 $")
        ops.append(
            {
                "operation": name,
                "operation_id": name_src or name,
                "method": method,
                "path": path,
                "summary": str(item.get("name") or name),
                "description": desc,
                "parameters": params,
                "body_schema": body_schema,
                "response_schema": resp_schema,
            }
        )


def parse_operations(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (operations, warnings)。"""
    variables = _vars(doc)
    ops: list[dict[str, Any]] = []
    warnings: list[str] = []
    used: set[str] = set()
    _walk_items(list(doc.get("item") or []), variables=variables, used=used, ops=ops, warnings=warnings)
    if not ops:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "未从 Postman Collection 解析出任何可用 operation")
    return ops, warnings
