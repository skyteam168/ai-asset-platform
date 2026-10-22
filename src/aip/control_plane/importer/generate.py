"""AI 生成候选 OpenAPI（O3b / architecture.md 8.6「AI 生成 Importer」）。

只经 LLM Gateway；输出为候选文档，须人工预览 / 点测后再落库。
extras 仅承载表格或截图转写的纯文本，不解析图片二进制。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.iam.authz import Actor
from aip.control_plane.importer import openapi as oa
from aip.control_plane.importer.service import _company_for_enrich, _require_dev
from aip.libs.common.errors import ErrorCode, PlatformError

_MAX_DESC = 20_000
_MAX_EXTRA = 12_000
_MAX_EXTRAS = 8
_MAX_DOC_CHARS = 400_000
_PROFILE = re.compile(r"^[\w.-]+@\d+\.\d+\.\d+$")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_json": {"type": "string", "minLength": 40, "maxLength": _MAX_DOC_CHARS},
        "title": {"type": "string", "maxLength": 200},
        "notes": {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
            "maxItems": 20,
        },
    },
    "required": ["document_json"],
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


def _looks_binary(text: str) -> bool:
    if "\x00" in text:
        return True
    sample = text[:4000]
    if not sample:
        return False
    bad = sum(1 for c in sample if ord(c) < 9 or (13 < ord(c) < 32 and c not in "\t\n\r"))
    return bad / max(len(sample), 1) > 0.05


def _clip(text: str, limit: int) -> str:
    t = text.strip()
    if len(t) <= limit:
        return t
    return t[: limit - 20] + "\n…[truncated]"


def build_prompt(
    *,
    description: str,
    extras: list[str],
    title_hint: str | None,
    domain_hint: str | None,
    system_hint: str | None,
) -> str:
    parts = [
        "你是企业 API 文档助手。根据下列业务说明与附录文本，生成一份候选 OpenAPI 3.0.3 文档。",
        "要求：",
        "1. document_json 必须是合法 JSON 字符串，内容为完整 OpenAPI 对象（含 openapi、info、paths）。",
        "2. 每个 path+method 尽量带 operationId、parameters 或 requestBody、200 响应 schema。",
        "3. 不要编造真实凭据；不确定的字段用 string 并加 description 说明。",
        "4. 只输出符合 schema 的 JSON 对象，不要 markdown 代码块。",
        "",
        f"业务说明：\n{_clip(description, _MAX_DESC)}",
    ]
    if title_hint:
        parts.append(f"建议标题：{title_hint[:200]}")
    if domain_hint:
        parts.append(f"业务域提示：{domain_hint}")
    if system_hint:
        parts.append(f"系统提示：{system_hint}")
    for i, ex in enumerate(extras, 1):
        parts.append(f"附录文本 #{i}（表格或截图转写，非图片）：\n{_clip(ex, _MAX_EXTRA)}")
    return "\n\n".join(parts)


def _parse_document(document_json: str) -> tuple[str, int, list[str]]:
    """返回 (序列化后的文档字符串, operation 数, warnings)。"""
    warnings: list[str] = []
    raw = document_json.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json|yaml|yml)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        warnings.append("已剥离模型输出中的 markdown 代码围栏")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"模型输出的 document_json 不是合法 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "document_json 须为 OpenAPI 对象")
    serialized = json.dumps(obj, ensure_ascii=False)
    doc = oa.load_document(serialized)
    ops, parse_warn = oa.parse_operations(doc)
    warnings.extend(parse_warn)
    if not ops:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "生成的 OpenAPI 未解析出任何 operation")
    return json.dumps(doc, ensure_ascii=False, indent=2), len(ops), warnings


async def generate_openapi(
    conn: AsyncConnection,
    actor: Actor,
    *,
    description: str,
    model_profile: str,
    extras: list[str] | None = None,
    title_hint: str | None = None,
    domain_hint: str | None = None,
    system_hint: str | None = None,
    connection_id: uuid.UUID | None = None,
    owner_org_id: uuid.UUID | None = None,
    llm: LlmComplete | None = None,
) -> dict[str, Any]:
    _require_dev(actor)
    desc = (description or "").strip()
    if len(desc) < 20:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "业务说明至少 20 字")
    if len(desc) > _MAX_DESC:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"业务说明过长（上限 {_MAX_DESC}）")
    if _looks_binary(desc):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "业务说明须为纯文本（不接受二进制/图片）")
    if not model_profile or not _PROFILE.match(model_profile.strip()):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "model_profile 须为 name@semver")

    clean_extras: list[str] = []
    for i, ex in enumerate(extras or []):
        if i >= _MAX_EXTRAS:
            break
        t = (ex or "").strip()
        if not t:
            continue
        if _looks_binary(t):
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT,
                f"附录 #{i + 1} 须为纯文本（表格/截图转写），不解析图片",
            )
        if len(t) > _MAX_EXTRA:
            t = _clip(t, _MAX_EXTRA)
        clean_extras.append(t)

    company_id = await _company_for_enrich(
        conn, actor, connection_id=connection_id, owner_org_id=owner_org_id
    )
    if llm is None:
        from aip.control_plane.gates.service import _default_selection_llm

        llm = await _default_selection_llm(conn)

    prompt = build_prompt(
        description=desc,
        extras=clean_extras,
        title_hint=title_hint,
        domain_hint=domain_hint,
        system_hint=system_hint,
    )
    res = await llm.complete(
        model_profile=model_profile.strip(),
        prompt=prompt,
        variables={},
        output_schema=OUTPUT_SCHEMA,
        company_id=str(company_id),
    )
    data = res.data if hasattr(res, "data") else res
    if not isinstance(data, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "LLM 输出非对象")
    doc_json = str(data.get("document_json") or "")
    document, op_count, warnings = _parse_document(doc_json)
    title = str(data.get("title") or title_hint or "").strip() or None
    notes = data.get("notes")
    if isinstance(notes, list):
        for n in notes[:20]:
            if str(n).strip():
                warnings.append(str(n).strip()[:300])
    warnings.insert(0, "AI 生成的 OpenAPI 仅为候选，请预览与真连接点测后再落库")
    return {
        "document": document,
        "format": "openapi",
        "title": title,
        "warnings": warnings,
        "operation_count": op_count,
    }
