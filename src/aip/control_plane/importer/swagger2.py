"""Swagger 2.0 → OpenAPI 3.0.3（Importer 用最小转换；architecture 导入入口兼容）。"""

from __future__ import annotations

import copy
from typing import Any

from aip.libs.common.errors import ErrorCode, PlatformError


def upgrade_swagger2(doc: dict[str, Any]) -> dict[str, Any]:
    """将 Swagger 2.0 文档升级为 OpenAPI 3.0.3 dict。"""
    ver = str(doc.get("swagger") or "")
    if not ver.startswith("2."):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"无法升级：swagger 版本为 {ver!r}")
    src = copy.deepcopy(doc)
    out: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": src.get("info") if isinstance(src.get("info"), dict) else {"title": "imported", "version": "0.0.0"},
    }
    servers = _servers(src)
    if servers:
        out["servers"] = servers
    if isinstance(src.get("tags"), list):
        out["tags"] = src["tags"]
    components: dict[str, Any] = {}
    defs = src.get("definitions")
    if isinstance(defs, dict) and defs:
        components["schemas"] = _rewrite_refs(defs)
    sec_defs = src.get("securityDefinitions")
    if isinstance(sec_defs, dict) and sec_defs:
        components["securitySchemes"] = _security_schemes(sec_defs)
    if components:
        out["components"] = components
    if isinstance(src.get("security"), list):
        out["security"] = src["security"]
    paths = src.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "Swagger 缺少 paths")
    out["paths"] = _paths(src, paths)
    return out


def _servers(src: dict[str, Any]) -> list[dict[str, str]]:
    host = str(src.get("host") or "").strip()
    base = str(src.get("basePath") or "").rstrip("/")
    raw_schemes = src.get("schemes")
    schemes: list[Any] = list(raw_schemes) if isinstance(raw_schemes, list) else ["https"]
    if not host:
        return [{"url": base or "/"}] if base else []
    urls: list[dict[str, str]] = []
    for sch in schemes:
        s = str(sch).lower()
        if s not in ("http", "https"):
            continue
        urls.append({"url": f"{s}://{host}{base}"})
    return urls or [{"url": f"https://{host}{base}"}]


def _security_schemes(defs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in defs.items():
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "")
        if t == "apiKey":
            out[name] = {
                "type": "apiKey",
                "name": raw.get("name") or name,
                "in": raw.get("in") or "header",
            }
        elif t == "basic":
            out[name] = {"type": "http", "scheme": "basic"}
        elif t == "oauth2":
            # 简化：Importer 只保留标记，不完整还原 flow
            out[name] = {"type": "oauth2", "flows": {}}
        else:
            out[name] = {"type": "apiKey", "name": name, "in": "header"}
    return out


def _rewrite_refs(node: Any) -> Any:
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if ref.startswith("#/definitions/"):
                return {**node, "$ref": "#/components/schemas/" + ref[len("#/definitions/") :]}
            return node
        return {k: _rewrite_refs(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_refs(x) for x in node]
    return node


def _paths(src: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    global_consumes = _media_list(src.get("consumes"), default=["application/json"])
    global_produces = _media_list(src.get("produces"), default=["application/json"])
    out: dict[str, Any] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        path_item: dict[str, Any] = {}
        path_params = [p for p in (item.get("parameters") or []) if isinstance(p, dict)]
        for method, op in item.items():
            if method == "parameters" or not isinstance(op, dict):
                continue
            if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            path_item[method] = _operation(op, path_params, global_consumes, global_produces)
        if path_item:
            out[path] = path_item
    return out


def _media_list(raw: Any, *, default: list[str]) -> list[str]:
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw]
    return list(default)


def _operation(
    op: dict[str, Any],
    path_params: list[dict[str, Any]],
    global_consumes: list[str],
    global_produces: list[str],
) -> dict[str, Any]:
    consumes = _media_list(op.get("consumes"), default=global_consumes)
    produces = _media_list(op.get("produces"), default=global_produces)
    params_in = [p for p in path_params + list(op.get("parameters") or []) if isinstance(p, dict)]
    params_out: list[dict[str, Any]] = []
    body_param: dict[str, Any] | None = None
    form_props: dict[str, Any] = {}
    form_required: list[str] = []
    for p in params_in:
        loc = str(p.get("in") or "")
        if loc == "body":
            body_param = p
            continue
        if loc == "formData":
            name = str(p.get("name") or "field")
            schema = _param_schema(p)
            form_props[name] = schema
            if p.get("required"):
                form_required.append(name)
            continue
        params_out.append(_param_v3(p))
    out: dict[str, Any] = {
        k: v
        for k, v in op.items()
        if k
        not in (
            "parameters",
            "consumes",
            "produces",
            "responses",
        )
    }
    if params_out:
        out["parameters"] = params_out
    if body_param is not None:
        schema = _rewrite_refs(body_param.get("schema") or {"type": "object"})
        content = {mt: {"schema": schema} for mt in consumes}
        out["requestBody"] = {
            "required": bool(body_param.get("required", True)),
            "content": content,
        }
    elif form_props:
        form_schema: dict[str, Any] = {"type": "object", "properties": form_props}
        if form_required:
            form_schema["required"] = form_required
        mt = "application/x-www-form-urlencoded"
        if any(str((form_props.get(n) or {}).get("format")) == "binary" for n in form_props):
            mt = "multipart/form-data"
        out["requestBody"] = {"content": {mt: {"schema": form_schema}}}
    out["responses"] = _responses(op.get("responses") or {}, produces)
    return out


def _param_schema(p: dict[str, Any]) -> dict[str, Any]:
    if p.get("type") == "file":
        return {"type": "string", "format": "binary"}
    schema: dict[str, Any] = {"type": p.get("type") or "string"}
    for k in ("format", "enum", "items", "minimum", "maximum", "default", "description"):
        if k in p:
            schema[k] = p[k]
    rewritten = _rewrite_refs(schema)
    return rewritten if isinstance(rewritten, dict) else schema


def _param_v3(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": p.get("name"),
        "in": p.get("in"),
        "required": bool(p.get("required")) if p.get("in") == "path" else bool(p.get("required", False)),
        "schema": _param_schema(p),
    }
    if "description" in p:
        out["description"] = p["description"]
    return out


def _responses(responses: dict[str, Any], produces: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for code, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        item: dict[str, Any] = {"description": str(resp.get("description") or "response")}
        schema = resp.get("schema")
        if schema is not None:
            item["content"] = {mt: {"schema": _rewrite_refs(schema)} for mt in produces}
        out[str(code)] = item
    if not out:
        out["200"] = {"description": "ok"}
    return out
