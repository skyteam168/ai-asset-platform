"""Importer AI 候选描述（architecture.md 4.2 / 8.6）：经 LLM Gateway 润色 Tool Spec。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from aip.libs.common.errors import ErrorCode, PlatformError

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "minLength": 20, "maxLength": 2000},
        "usage_notes": {"type": "string", "maxLength": 1000},
        "not_for": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 8},
    },
    "required": ["description"],
    "additionalProperties": False,
}


class LlmComplete(Protocol):
    async def complete(
        self,
        *,
        model_profile: str,
        prompt: str,
        variables: dict[str, Any],
        output_schema: dict[str, Any],
        company_id: str,
    ) -> Any: ...


def _tool_brief(tool: dict[str, Any]) -> str:
    props = list((tool.get("input_schema") or {}).get("properties") or {})
    return json.dumps(
        {
            "capability_id": tool.get("capability_id"),
            "name": tool.get("name"),
            "risk_level": tool.get("risk_level"),
            "current_description": tool.get("description"),
            "input_fields": props[:20],
            "connector": (tool.get("connector_ref") or {}).get("connector"),
            "operation": (tool.get("connector_ref") or {}).get("operation"),
        },
        ensure_ascii=False,
    )


def build_prompt(tool: dict[str, Any]) -> str:
    return (
        "你是企业 AI 工具文档助手。根据下列 Tool 草稿信息，生成面向消费方 Agent 的中文说明。\n"
        "要求：description 至少 20 字，说明用途与返回内容；可附 usage_notes（前置条件）与 not_for（不适用场景）。\n"
        "只输出 JSON。\n\n"
        f"工具草稿：{_tool_brief(tool)}"
    )


async def enrich_tools(
    llm: LlmComplete,
    *,
    tools: list[dict[str, Any]],
    model_profile: str,
    company_id: uuid.UUID | str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """就地润色 tools；返回 (tools, warnings)。单条失败保留原文。"""
    warnings: list[str] = []
    cid = str(company_id)
    out: list[dict[str, Any]] = []
    for tool in tools:
        cap = str(tool.get("capability_id") or "?")
        try:
            res = await llm.complete(
                model_profile=model_profile,
                prompt=build_prompt(tool),
                variables={},
                output_schema=OUTPUT_SCHEMA,
                company_id=cid,
            )
            data = res.data if hasattr(res, "data") else res
            if not isinstance(data, dict):
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "LLM 输出非对象")
            desc = str(data.get("description") or "").strip()
            if len(desc) < 20:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, "description 过短")
            merged = dict(tool)
            merged["description"] = desc[:2000]
            merged["description_lang"] = "zh"
            notes = data.get("usage_notes")
            if isinstance(notes, str) and notes.strip():
                merged["usage_notes"] = notes.strip()[:1000]
            nf = data.get("not_for")
            if isinstance(nf, list) and nf:
                merged["not_for"] = [str(x)[:200] for x in nf if str(x).strip()][:8]
            out.append(merged)
        except PlatformError as e:
            warnings.append(f"{cap}: AI 描述失败（{e.code}），保留原文")
            out.append(tool)
        except Exception as e:
            warnings.append(f"{cap}: AI 描述失败（{type(e).__name__}），保留原文")
            out.append(tool)
    return out, warnings
