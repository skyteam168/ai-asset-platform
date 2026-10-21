"""LLM Gateway：白名单、预算、脱敏、OpenAI 兼容调用（architecture.md 8.6）。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jsonschema

from aip.data_plane.llm.allowlist import AllowlistStore
from aip.data_plane.llm.budget import BudgetStore
from aip.data_plane.llm.openai_compat import chat_completions, extract_content_and_tokens
from aip.data_plane.llm.redact import redact_value, sha256_text
from aip.data_plane.workflow.llm import LlmResult
from aip.libs.common.errors import ErrorCode, PlatformError

log = logging.getLogger("aip.llm")

_REF = re.compile(r"^([a-z][a-z0-9_.]*)@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")

ProfileResolver = Callable[[str, str], Awaitable[dict[str, Any]] | dict[str, Any]]


class LlmGateway:
    """实现 LlmClient Protocol；选择评测与 Workflow 共用 complete。"""

    def __init__(
        self,
        *,
        allowlist: AllowlistStore,
        budget: BudgetStore,
        resolve_profile: ProfileResolver,
        env: str = "test",
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        audit_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.allowlist = allowlist
        self.budget = budget
        self.resolve_profile = resolve_profile
        self.env = env
        self.api_key = api_key
        self.transport = transport
        self.audit_hook = audit_hook

    async def complete(
        self,
        *,
        model_profile: str,
        prompt: str,
        variables: dict[str, Any],
        output_schema: dict[str, Any],
        company_id: str,
    ) -> LlmResult:
        m = _REF.match(model_profile.strip())
        if not m:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "model_profile 须为 name@semver")
        name, ver = m.group(1), m.group(2)
        try:
            cid = uuid.UUID(company_id)
        except ValueError as e:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "company_id 不合法") from e

        if not await self.allowlist.is_allowed(cid, name):
            raise PlatformError(ErrorCode.PERMISSION_DENIED, "该公司未授权使用此 model_profile")

        profile = await self._profile(name, ver)
        redacted_vars = redact_value(variables)
        user_prompt = _render(prompt, redacted_vars if isinstance(redacted_vars, dict) else {})
        user_prompt = redact_value(user_prompt) if isinstance(user_prompt, str) else str(user_prompt)

        day = utc_today()
        limit = await self.budget.get_daily_limit(cid)
        if limit is None:
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "公司未配置 LLM token 日预算")
        used = await self.budget.get_usage(cid, day)
        if used >= limit:
            raise PlatformError(ErrorCode.RATE_LIMITED, "超过公司日 token 预算")

        system = (
            "You are a structured extraction assistant. "
            "Reply with a single JSON object matching the required schema. No markdown."
        )
        doc = await chat_completions(
            base_url=str(profile["base_url"]),
            api_path=str(profile.get("api_path") or "/v1/chat/completions"),
            model=str(profile["model"]),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=float(profile.get("temperature", 0)),
            max_tokens=int(profile.get("max_tokens", 1024)),
            api_key=self.api_key,
            transport=self.transport,
        )
        content, tokens = extract_content_and_tokens(doc)
        tokens = max(1, tokens)
        await self.budget.reserve_and_add(cid, day, tokens)

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "LLM 输出不是合法 JSON") from e
        if isinstance(data, dict) and isinstance(output_schema.get("properties"), dict):
            data = {k: v for k, v in data.items() if k in output_schema["properties"]}
        errs = list(jsonschema.Draft202012Validator(output_schema).iter_errors(data))
        if errs:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"LLM 输出不符合 schema：{errs[0].message}")

        if self.audit_hook:
            self.audit_hook(
                {
                    "kind": "llm.complete",
                    "company_id": company_id,
                    "model_profile": model_profile,
                    "prompt_sha256": sha256_text(user_prompt),
                    "prompt_preview": user_prompt[:200],
                    "tokens": tokens,
                }
            )
        return LlmResult(data=data, tokens=tokens)

    async def _profile(self, name: str, ver: str) -> dict[str, Any]:
        ref = f"{name}@{ver}"
        resolved = self.resolve_profile(ref, self.env)
        if isinstance(resolved, dict):
            profile = resolved
        else:
            profile = await resolved
        for key in ("model", "base_url"):
            if key not in profile:
                raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"model_profile 缺少 {key}")
        return profile


def utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _render(template: str, variables: dict[str, Any]) -> str:
    """极简 {{ key }} 替换；缺失键留空。"""
    out = template

    def repl(m: re.Match[str]) -> str:
        key = m.group(1).strip()
        val = variables.get(key, "")
        return "" if val is None else str(val)

    return re.sub(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", repl, out)


def profile_from_release_cache(releases: Any, ref: str, env: str) -> dict[str, Any]:
    """从 ReleaseCache 解析 model_profile（name@semver）。"""
    m = _REF.match(ref.strip())
    if not m:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "model_profile 须为 name@semver")
    name, ver = m.group(1), m.group(2)
    entry = releases.resolve(name, env, version=ver)
    at = entry.get("asset_type")
    if at is not None and at != "model_profile":
        raise PlatformError(ErrorCode.NOT_FOUND, f"{ref} 不是 model_profile")
    return dict(entry["spec"])
