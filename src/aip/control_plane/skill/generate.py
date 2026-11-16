"""Skill AI 草稿（勾选 Tool → LLM Gateway 生成 SKILL.md 候选）。

只产指令型内容；结果须人工修改后再保存 / 走门禁（architecture.md 6.2、8.6）。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt
from aip.libs.common.errors import ErrorCode, PlatformError

_TOOL_REF = re.compile(r"^[a-z0-9_.]+@\d+\.\d+\.\d+$")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_PROFILE = re.compile(r"^[\w.-]+@\d+\.\d+\.\d+$")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 64},
        "description": {"type": "string", "minLength": 10, "maxLength": 1024},
        "body": {"type": "string", "minLength": 40, "maxLength": 20000},
    },
    "required": ["name", "description", "body"],
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


def _sanitize_name(raw: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    if not s or not s[0].isalpha():
        s = f"skill-{s}" if s else "ops-skill"
    return s[:64]


def _tool_brief(spec: dict[str, Any], *, ref: str) -> dict[str, Any]:
    props = list((spec.get("input_schema") or {}).get("properties") or {})
    return {
        "ref": ref,
        "capability_id": spec.get("capability_id"),
        "name": spec.get("name"),
        "title": spec.get("title"),
        "description": (spec.get("description") or "")[:500],
        "risk_level": spec.get("risk_level"),
        "mode": spec.get("mode"),
        "input_fields": props[:15],
    }


def build_prompt(*, tools: list[dict[str, Any]], scenario: str, hint_name: str) -> str:
    return (
        "你是企业 Agent Skill 文档助手。根据下列已发布 Tool 与运维场景说明，"
        "生成一份指令型 Skill 草稿（不要脚本、不要可执行代码块）。\n"
        "要求：\n"
        "- name：小写英文，用连字符，2–64 字符（如 repair-data-guide）\n"
        "- description：单行中文，10–1024 字，说明何时启用\n"
        "- body：Markdown 中文，含「何时使用」「步骤」「注意事项」；步骤须引用给出的 Tool ref\n"
        "- 不要编造未列出的 Tool；不要诱导越权或绕过权限\n"
        "只输出 JSON。\n\n"
        f"建议 name 提示：{hint_name or '（无）'}\n"
        f"场景说明：{scenario or '（无补充，请仅根据 Tool 推断合理用法）'}\n"
        f"工具列表：{json.dumps(tools, ensure_ascii=False)}"
    )


async def load_tool_briefs(conn: AsyncConnection, tool_refs: list[str]) -> list[dict[str, Any]]:
    briefs: list[dict[str, Any]] = []
    for ref in tool_refs:
        if not _TOOL_REF.match(ref):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"Tool 引用格式须为 capability_id@x.y.z：{ref}")
        cap, _, ver = ref.partition("@")
        row = (
            await conn.execute(
                select(rt.asset_version.c.spec)
                .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
                .where(rt.asset.c.type == "tool")
                .where(rt.asset_version.c.semver == ver)
                .where(
                    or_(
                        rt.asset_version.c.spec["capability_id"].astext == cap,
                        rt.asset.c.name == cap,
                    )
                )
                .limit(1)
            )
        ).mappings().first()
        if row is None:
            raise PlatformError(ErrorCode.NOT_FOUND, f"找不到 Tool：{ref}")
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        briefs.append(_tool_brief(spec, ref=ref))
    return briefs


async def generate_draft(
    conn: AsyncConnection,
    actor: Any,
    *,
    version_id: uuid.UUID,
    tool_refs: list[str],
    scenario: str = "",
    model_profile: str,
    hint_name: str = "",
    llm: LlmComplete | None = None,
) -> dict[str, Any]:
    """返回候选 name/description/body/tools；不落库。"""
    actor.require(
        actor.has("platform_admin") or actor.has("asset_developer"),
        "需要平台管理员或资产开发者",
    )
    refs = [r.strip() for r in tool_refs if r and r.strip()]
    if not refs:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "请至少勾选一个 Tool")
    if len(refs) > 20:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "一次最多引用 20 个 Tool")
    if not _PROFILE.match((model_profile or "").strip()):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "model_profile 须为 name@semver")
    scen = (scenario or "").strip()
    if len(scen) > 4000:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "场景说明过长（上限 4000）")

    ver = await registry.version_row(conn, version_id)
    asset = await registry.asset_row(conn, ver["asset_id"])
    if asset["type"] != "skill":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "仅 Skill 版本可生成 AI 草稿")
    if ver["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只能在 draft 状态生成 AI 草稿")

    briefs = await load_tool_briefs(conn, refs)
    if llm is None:
        from aip.control_plane.gates.service import _default_selection_llm

        llm = await _default_selection_llm(conn)

    prompt = build_prompt(tools=briefs, scenario=scen, hint_name=hint_name or str(asset.get("name") or ""))
    res = await llm.complete(
        model_profile=model_profile.strip(),
        prompt=prompt,
        variables={},
        output_schema=OUTPUT_SCHEMA,
        company_id=str(asset["company_id"]),
    )
    data = res.data if hasattr(res, "data") else res
    if not isinstance(data, dict):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "LLM 输出非对象")

    name = _sanitize_name(str(data.get("name") or hint_name or asset.get("name") or "ops-skill"))
    if not _NAME.match(name):
        name = _sanitize_name(name)
    description = str(data.get("description") or "").strip().replace("\n", " ")
    if len(description) < 10:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "AI 生成的 description 过短，请重试或手工填写")
    body = str(data.get("body") or "").strip()
    # 硬拒脚本痕迹（先于长度，避免短脚本被「过短」掩盖）
    low = body.lower()
    if any(x in low for x in ("```python", "```bash", "```sh", "#!/", ".exe")):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "AI 草稿含脚本痕迹，已拒绝；请改场景说明后重试")
    if len(body) < 40:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "AI 生成的正文过短，请重试或手工填写")

    warnings = [
        "AI 生成的 Skill 仅为候选草稿，请人工核对 Tool 引用与步骤后再保存并走门禁。",
        f"已按勾选锁定 tools：{', '.join(refs)}",
    ]
    return {
        "name": name[:64],
        "description": description[:1024],
        "body": body[:20000],
        "tools": refs,
        "allowed_tools": [],
        "warnings": warnings,
    }
