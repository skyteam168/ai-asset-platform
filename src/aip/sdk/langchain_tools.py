"""将数据面目录注册为 LangChain StructuredTool（可选依赖 langchain-core）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, create_model

if TYPE_CHECKING:
    from aip.sdk.client import Client


def _require_langchain() -> Any:
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as e:
        raise ImportError(
            "LangChain 适配需要安装可选依赖组 sdk：`uv sync --group sdk` 或 `pip install 'aip[sdk]'`（langchain-core）"
        ) from e
    return StructuredTool


def _json_type(t: Any) -> type[Any]:
    if t == "string":
        return str
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        return list
    if t == "object":
        return dict
    return Any


class _ExtraAllow(BaseModel):
    model_config = ConfigDict(extra="allow")


def args_schema_from_input(capability_id: str, input_schema: dict[str, Any] | None) -> type[BaseModel]:
    """从 Tool Spec 的 input_schema 粗建 Pydantic 模型；无 properties 时允许任意字段透传。"""
    name = "AipArgs_" + "".join(c if c.isalnum() else "_" for c in capability_id)
    schema = input_schema or {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else None
    if not props:
        return create_model(name, __base__=_ExtraAllow)
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, prop in props.items():
        if not isinstance(prop, dict):
            prop = {}
        typ = _json_type(prop.get("type"))
        desc = prop.get("description")
        if key in required:
            fields[key] = (typ, Field(..., description=desc) if desc else ...)
        else:
            fields[key] = (typ | None, Field(None, description=desc) if desc else None)
    return create_model(name, __base__=BaseModel, **fields)


async def build_langchain_tools(
    client: Client,
    *,
    names: set[str] | list[str] | None = None,
    user_assertion: str | None = None,
) -> list[Any]:
    StructuredTool = _require_langchain()
    catalog = await client.catalog(user_assertion=user_assertion)
    items = catalog.get("items") or []
    allow = set(names) if names is not None else None
    tools: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cap = item.get("capability_id")
        if not isinstance(cap, str):
            continue
        if allow is not None and cap not in allow:
            continue
        description = str(item.get("description") or item.get("title") or item.get("name") or cap)
        schema = item.get("input_schema") if isinstance(item.get("input_schema"), dict) else None
        args_model = args_schema_from_input(cap, schema)

        def _make(cap_id: str) -> Any:
            async def _run(**kwargs: Any) -> Any:
                return await client.invoke(cap_id, dict(kwargs), user_assertion=user_assertion)

            return _run

        tools.append(
            StructuredTool.from_function(
                coroutine=_make(cap),
                name=cap,
                description=description,
                args_schema=args_model,
            )
        )
    return tools
