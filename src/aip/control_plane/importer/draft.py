"""中间 operation → 声明式 Connector config 与 Tool Spec 草稿。"""

from __future__ import annotations

import re
from typing import Any

from aip.control_plane.importer.openapi import _PATH_PARAM, _schema_props, to_snake


def _jinja_path(path: str) -> str:
    return _PATH_PARAM.sub(r"{{ args.\1 }}", path)


def _query_template(param: dict[str, Any]) -> str:
    name = param["name"]
    schema = param.get("schema") or {}
    required = bool(param.get("required"))
    expr = f"{{{{ args.{name}"
    if schema.get("type") == "integer":
        default = schema.get("default", 50)
        maximum = schema.get("maximum")
        expr = f"{{{{ args.{name} | default({default})"
        if maximum is not None:
            expr += f" | max({int(maximum)})"
        expr += " }}"
        return expr
    if required:
        return f"{{{{ args.{name} }}}}"
    return f"{{{{ args.{name} | default('') }}}}"


def _item_fields(items: dict[str, Any]) -> dict[str, str]:
    if items.get("type") == "array":
        items = items.get("items") or {}
    props = items.get("properties") or {}
    return {to_snake(k): f"$.{k}" for k in props} if props else {}


def _response_mapping(schema: dict[str, Any]) -> dict[str, Any]:
    """启发式：code+data 包络；数组 items 字段 camelCase→snake。"""
    props = schema.get("properties") or {}
    resp: dict[str, Any] = {}
    if "code" in props and "data" in props:
        resp["success_when"] = "$.code == 0"
        data_schema = props["data"]
        if isinstance(data_schema, dict) and "$ref" not in data_schema:
            dprops = data_schema.get("properties") or {}
            if "items" in dprops:
                resp["data"] = "$.data.items[*]"
                fields = _item_fields(dprops["items"] if isinstance(dprops["items"], dict) else {})
                if fields:
                    resp["fields"] = fields
                if "nextPage" in dprops or "next_page" in dprops:
                    key = "nextPage" if "nextPage" in dprops else "next_page"
                    resp["next_cursor"] = f"$.data.{key}"
            else:
                resp["data"] = "$.data"
        else:
            resp["data"] = "$.data"
        return resp
    if schema.get("type") == "array":
        resp["data"] = "$[*]"
        items = schema.get("items") or {}
        if isinstance(items, dict) and items.get("properties"):
            resp["fields"] = {to_snake(k): f"$.{k}" for k in items["properties"]}
        return resp
    resp["data"] = "$"
    if props:
        resp["fields"] = {to_snake(k): f"$.{k}" for k in props}
    return resp


def _input_schema(doc: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in op["parameters"]:
        name = p["name"]
        schema = p.get("schema") or {"type": "string"}
        prop: dict[str, Any] = {"type": schema.get("type", "string")}
        if "enum" in schema:
            prop["enum"] = schema["enum"]
        properties[name] = prop
        if p.get("required"):
            required.append(name)
    if op.get("body_schema"):
        body_props, body_req = _schema_props(doc, op["body_schema"])
        for k, v in body_props.items():
            properties[k] = v
        required.extend(body_req)
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = sorted(set(required))
    return out


def _output_schema(doc: dict[str, Any], schema: dict[str, Any], resp: dict[str, Any]) -> dict[str, Any]:
    fields = resp.get("fields") or {}
    if fields:
        props = {k: {"type": "string"} for k in fields}
        # 粗略：qty/planQty 类为 integer
        for k in list(props):
            if k in ("qty", "plan_qty", "count", "limit") or k.endswith("_qty"):
                props[k] = {"type": "integer"}
        return {"type": "array", "items": {"type": "object", "properties": props, "required": list(props)}}
    if schema.get("type") == "array":
        return {"type": "array", "items": schema.get("items") or {"type": "object"}}
    props, req = _schema_props(doc, schema)
    if props:
        return {"type": "object", "properties": props, "required": req}
    return {"type": "object"}


def build_drafts(
    doc: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    connector_name: str,
    domain: str,
    system: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """返回 (connector_config, tool_specs)。"""
    conn_ops: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    for op in operations:
        query: dict[str, str] = {}
        headers: dict[str, str] = {}
        for p in op["parameters"]:
            loc = p.get("in")
            if loc == "query":
                query[p["name"]] = _query_template(p)
            elif loc == "header":
                headers[p["name"]] = f"{{{{ args.{p['name']} }}}}"
        req: dict[str, Any] = {
            "method": op["method"],
            "path": _jinja_path(op["path"]),
        }
        if query:
            req["query"] = query
        if headers:
            req["headers"] = headers
        resp = _response_mapping(op["response_schema"])
        mode = "read" if op["method"] == "GET" else "write"
        mapping: dict[str, Any] = {
            "operation": op["operation"],
            "mode": mode,
            "request": req,
            "response": resp,
        }
        conn_ops.append(mapping)

        dom = domain if re.match(r"^[a-z][a-z0-9_]*$", domain) else "imported"
        sys = system if re.match(r"^[a-z][a-z0-9_]*$", system) else to_snake(system)
        if not re.match(r"^[a-z][a-z0-9_]*$", sys):
            sys = "imported"
        cap = f"{dom}.{sys}.{op['operation']}"
        desc = op["description"].strip()
        if len(desc) < 20:
            desc = f"{desc}（从 OpenAPI 导入的操作草稿）".strip()
        tool: dict[str, Any] = {
            "capability_id": cap,
            "domain": dom,
            "name": op["operation"][:64],
            "description": desc[:2000],
            "input_schema": _input_schema(doc, op),
            "output_schema": _output_schema(doc, op["response_schema"], resp),
            "connector_ref": {"connector": connector_name, "operation": op["operation"]},
            "risk_level": "read" if mode == "read" else "write",
            "timeout": {"connect_ms": 2000, "read_ms": 5000, "deadline_ms": 8000},
            "data_classification": 2,
            "identity_mode": "service_account",
            "confirm_policy": "auto",
        }
        if tool["risk_level"] == "write":
            tool["idempotency"] = {"idempotent": True}
            tool["recovery"] = "manual"
            tool["reconcile"] = {"operation": op["operation"], "by": "idempotency_key"}
        tools.append(tool)
    return {"operations": conn_ops}, tools
