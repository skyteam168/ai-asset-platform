"""工具选择评测（architecture.md 7.2）：经 LLM Gateway 跑 top-1 / 误调用率。"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from aip.libs.common.errors import ErrorCode, PlatformError

MIN_CASES = 20
MAX_MODELS = 2
TOP1_THRESHOLD = 0.90
FALSE_CALL_THRESHOLD = 0.05

_REF = re.compile(r"^([a-z][a-z0-9_.]*)@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capability_id": {
            "type": ["string", "null"],
            "description": "应调用的 Tool capability_id；若不应调用任何工具则为 null",
        }
    },
    "required": ["capability_id"],
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


@dataclass
class EvalCase:
    name: str
    question: str
    expect_capability_id: str | None


@dataclass
class CaseOutcome:
    name: str
    expect: str | None
    selected: str | None
    top1_hit: bool
    false_call: bool
    error: str | None = None


@dataclass
class ModelRunResult:
    model_profile: str
    top1_hit_rate: float
    false_call_rate: float
    passed: bool
    outcomes: list[CaseOutcome] = field(default_factory=list)
    reason: str | None = None


def validate_settings(model_profiles: list[str], cases: list[EvalCase], *, require_min_cases: bool = True) -> list[str]:
    problems: list[str] = []
    if not (1 <= len(model_profiles) <= MAX_MODELS):
        problems.append(f"model_profiles 须为 1–{MAX_MODELS} 个")
    for mp in model_profiles:
        if not _REF.match(mp.strip()):
            problems.append(f"model_profile 须为 name@semver：{mp}")
    if require_min_cases and len(cases) < MIN_CASES:
        problems.append(f"评测集至少 {MIN_CASES} 条，当前 {len(cases)}")
    names = [c.name for c in cases]
    if len(names) != len(set(names)):
        problems.append("评测用例 name 不可重复")
    for c in cases:
        if not c.name.strip():
            problems.append("用例 name 为空")
        if not c.question.strip():
            problems.append(f"用例 {c.name} 的 question 为空")
    return problems


def _catalog_block(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for t in candidates:
        cap = t.get("capability_id") or ""
        desc = (t.get("description") or "")[:500]
        notes = (t.get("usage_notes") or "")[:300]
        not_for = t.get("not_for") or []
        lines.append(f"- {cap}: {desc}")
        if notes:
            lines.append(f"  usage_notes: {notes}")
        if not_for:
            lines.append(f"  not_for: {', '.join(str(x) for x in not_for[:5])}")
    return "\n".join(lines)


def build_prompt(question: str, candidates: list[dict[str, Any]]) -> str:
    catalog = _catalog_block(candidates)
    return (
        "你是企业工具路由器。根据用户问题，从下列候选 Tool 中选出最合适的一个 capability_id；"
        "若问题不适合调用任何工具，返回 capability_id=null。\n"
        "只输出 JSON 对象，勿输出其它文字。\n\n"
        f"候选工具：\n{catalog}\n\n"
        f"用户问题：{question}"
    )


def score_outcomes(outcomes: list[CaseOutcome]) -> tuple[float, float]:
    positive = [o for o in outcomes if o.expect is not None and o.error is None]
    top1_hits = sum(1 for o in positive if o.top1_hit)
    top1_rate = 1.0 if not positive else top1_hits / len(positive)
    scored = [o for o in outcomes if o.error is None]
    false_calls = sum(1 for o in scored if o.false_call)
    false_rate = 0.0 if not scored else false_calls / len(scored)
    return top1_rate, false_rate


def classify(expect: str | None, selected: str | None) -> tuple[bool, bool]:
    """返回 (top1_hit, false_call)。"""
    if expect is not None:
        top1 = selected == expect
        false = selected is not None and selected != expect
        return top1, false
    # 期望不调用
    return False, selected is not None


async def run_for_model(
    llm: LlmComplete,
    *,
    model_profile: str,
    company_id: str,
    cases: list[EvalCase],
    candidates: list[dict[str, Any]],
) -> ModelRunResult:
    allowed = {str(t.get("capability_id")) for t in candidates if t.get("capability_id")}
    outcomes: list[CaseOutcome] = []
    for case in cases:
        try:
            res = await llm.complete(
                model_profile=model_profile,
                prompt=build_prompt(case.question, candidates),
                variables={},
                output_schema=OUTPUT_SCHEMA,
                company_id=company_id,
            )
            data = res.data if hasattr(res, "data") else res
            selected = data.get("capability_id") if isinstance(data, dict) else None
            if selected is not None:
                selected = str(selected)
                if selected not in allowed:
                    # 选了目录外 → 视为误调用
                    top1, false = False, True
                    if case.expect_capability_id is not None:
                        top1 = False
                    outcomes.append(
                        CaseOutcome(
                            name=case.name,
                            expect=case.expect_capability_id,
                            selected=selected,
                            top1_hit=top1,
                            false_call=false,
                        )
                    )
                    continue
            top1, false = classify(case.expect_capability_id, selected)
            outcomes.append(
                CaseOutcome(
                    name=case.name,
                    expect=case.expect_capability_id,
                    selected=selected,
                    top1_hit=top1,
                    false_call=false,
                )
            )
        except PlatformError as e:
            outcomes.append(
                CaseOutcome(
                    name=case.name,
                    expect=case.expect_capability_id,
                    selected=None,
                    top1_hit=False,
                    false_call=False,
                    error=f"{e.code.value}:{e.detail}",
                )
            )
        except Exception as e:
            outcomes.append(
                CaseOutcome(
                    name=case.name,
                    expect=case.expect_capability_id,
                    selected=None,
                    top1_hit=False,
                    false_call=False,
                    error=type(e).__name__,
                )
            )
    # 任一条 Gateway/平台错误 → 本模型轮次不通过
    if any(o.error for o in outcomes):
        top1_rate, false_rate = score_outcomes(outcomes)
        return ModelRunResult(
            model_profile=model_profile,
            top1_hit_rate=top1_rate,
            false_call_rate=false_rate,
            passed=False,
            outcomes=outcomes,
            reason="case_errors",
        )
    top1_rate, false_rate = score_outcomes(outcomes)
    passed = top1_rate >= TOP1_THRESHOLD and false_rate <= FALSE_CALL_THRESHOLD
    reason = None
    if not passed:
        reason = (
            f"top1={top1_rate:.3f} (need>={TOP1_THRESHOLD}) "
            f"false_call={false_rate:.3f} (need<={FALSE_CALL_THRESHOLD})"
        )
    return ModelRunResult(
        model_profile=model_profile,
        top1_hit_rate=top1_rate,
        false_call_rate=false_rate,
        passed=passed,
        outcomes=outcomes,
        reason=reason,
    )


async def run_selection_eval(
    llm: LlmComplete,
    *,
    company_id: uuid.UUID | str,
    model_profiles: list[str],
    cases: list[EvalCase],
    candidates: list[dict[str, Any]],
) -> tuple[bool, list[ModelRunResult], list[str]]:
    """返回 (全部模型通过, 各模型结果, 前置问题)。"""
    problems = validate_settings(model_profiles, cases)
    if not candidates:
        problems.append("套件无候选 Tool")
    if problems:
        return False, [], problems
    cid = str(company_id)
    results: list[ModelRunResult] = []
    for mp in model_profiles:
        results.append(
            await run_for_model(llm, model_profile=mp, company_id=cid, cases=cases, candidates=candidates)
        )
    return all(r.passed for r in results), results, []


def evidence_from_results(results: list[ModelRunResult], problems: list[str]) -> dict[str, Any]:
    return {
        "kind": "selection_eval",
        "problems": problems,
        "min_cases": MIN_CASES,
        "thresholds": {"top1": TOP1_THRESHOLD, "false_call": FALSE_CALL_THRESHOLD},
        "models": [
            {
                "model_profile": r.model_profile,
                "top1_hit_rate": r.top1_hit_rate,
                "false_call_rate": r.false_call_rate,
                "passed": r.passed,
                "reason": r.reason,
                "outcomes": [
                    {
                        "name": o.name,
                        "expect": o.expect,
                        "selected": o.selected,
                        "top1_hit": o.top1_hit,
                        "false_call": o.false_call,
                        "error": o.error,
                    }
                    for o in r.outcomes
                ],
            }
            for r in results
        ],
    }


def parse_ref(ref: str) -> tuple[str, str]:
    m = _REF.match(ref.strip())
    if not m:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "model_profile 须为 name@semver")
    return m.group(1), m.group(2)
