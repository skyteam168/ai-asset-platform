"""Workflow DSL 静态校验（architecture.md 6.3；G0 与 Run 启动时都调用）。

JSON Schema 之外的语义检查：
- 节点 id 唯一，所有跳转目标存在（或为 end），从第一个节点可达，无环（M5 不支持 loop，故图必须无环）；
- condition 表达式可解析，只引用 input 或一定先于它执行的节点；
- llm 节点不能带任何分支字段，输出 Schema 必须在顶层 schemas 中声明（LLM 只产出数据，不能改变流程）；
- tool 引用的版本可解析；写类（write / danger）节点必须声明补偿，或标记 irreversible 且所有到达路径上
  都先经过 confirm / approve 类人工任务；
- human_task 的 assignee 可解析；
- 引擎暂不支持的节点类型（parallel、loop、sub_workflow）直接报错，而不是运行时才失败。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Iterable
from typing import Any

import jsonschema

from aip.libs.common.errors import PlatformError
from aip.libs.workflow import expr

SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[4] / "docs" / "specs" / "workflow-dsl.schema.json"
SUPPORTED = {"tool", "llm", "condition", "human_task", "wait", "end"}
WRITE_RISKS = {"write", "danger"}
ROLES = {"platform_admin", "asset_developer", "data_owner", "business_owner", "company_admin", "auditor"}
EDGE_KEYS = ("next", "then", "else", "on_timeout", "on_reject")

ToolLookup = Callable[[str, str], dict[str, Any] | None]
_validator: jsonschema.Draft202012Validator | None = None


def _schema() -> jsonschema.Draft202012Validator:
    global _validator
    if _validator is None:
        _validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return _validator


def split_ref(ref: str) -> tuple[str, str]:
    cap, _, version = ref.partition("@")
    return cap, version


def edges(n: dict[str, Any]) -> list[str]:
    return [n[k] for k in EDGE_KEYS if isinstance(n.get(k), str)]


def data_paths(value: Any) -> Iterable[str]:
    """参数映射里的数据路径（"$input.x" → "input.x"；"$$" 开头表示字面量 $）。"""
    if isinstance(value, str) and value.startswith("$") and not value.startswith("$$"):
        yield value[1:]
    elif isinstance(value, dict):
        for v in value.values():
            yield from data_paths(v)
    elif isinstance(value, list):
        for v in value:
            yield from data_paths(v)


def validate(dsl: dict[str, Any], lookup: ToolLookup | None = None) -> list[str]:
    """返回问题列表；空列表表示通过。lookup(capability, version) 返回 Tool Spec，None 表示不可解析。"""
    problems = [
        f"{'.'.join(str(p) for p in e.path) or '(根)'}："
        + ("LLM 节点只能用 next，不能带分支字段（LLM 只产出数据，不改变流程）" if e.validator == "not" else e.message)
        for e in sorted(_schema().iter_errors(dsl), key=lambda e: list(e.path))
    ][:20]
    if problems:
        return problems
    nodes: list[dict[str, Any]] = dsl["nodes"]
    ids = [n["id"] for n in nodes]
    by_id = {n["id"]: n for n in nodes}
    if len(set(ids)) != len(ids):
        problems.append("节点 id 重复")
    if "end" in by_id:
        problems.append("end 是保留的跳转目标，不能用作节点 id")
    schemas = dsl.get("schemas") or {}

    for n in nodes:
        nid, kind = n["id"], n["type"]
        if kind not in SUPPORTED:
            problems.append(f"{nid}：M5 引擎暂不支持 {kind} 节点")
            continue
        for target in edges(n):
            if target != "end" and target not in by_id:
                problems.append(f"{nid}：跳转目标 {target} 不存在")
        if kind in ("tool", "llm", "wait", "human_task") and "next" not in n:
            problems.append(f'{nid}：缺少 next（流程结束请写 "end"）')
        if kind == "llm" and n["output_schema"] not in schemas:
            problems.append(f"{nid}：输出 Schema {n['output_schema']} 未在 schemas 中声明")
        if kind == "human_task":
            problems += [f"{nid}：{p}" for p in _assignee_problems(n["assignee"])]
        if kind == "wait" and float(n["seconds"]) > dsl["budget"]["timeout_s"]:
            problems.append(f"{nid}：等待时间超过 Run 总期限")
    if problems:
        return problems

    # 图：可达、无环
    start = nodes[0]["id"]
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(nid: str) -> None:
        if nid == "end":
            return
        if state.get(nid) == 1:
            raise ValueError(nid)
        if state.get(nid) == 2:
            return
        state[nid] = 1
        for t in edges(by_id[nid]):
            visit(t)
        state[nid] = 2
        order.append(nid)

    try:
        visit(start)
    except ValueError as e:
        return [f"{e.args[0]}：流程存在环（M5 不支持循环）"]
    unreachable = [i for i in ids if i not in state]
    if unreachable:
        problems.append(f"从 {start} 不可达的节点：{', '.join(unreachable)}")
    order.reverse()  # 拓扑序

    # 每个节点之前一定执行过的节点（所有到达路径的交集）与“所有路径都经过人工确认”
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for n in nodes:
        for t in edges(n):
            if t != "end":
                preds[t].append(n["id"])
    always_before: dict[str, set[str]] = {}
    gated: dict[str, bool] = {}
    for nid in order:
        ps = preds[nid]
        if not ps:
            always_before[nid], gated[nid] = set(), False
            continue
        always_before[nid] = set.intersection(*({p} | always_before[p] for p in ps))
        gated[nid] = all(gated[p] or _is_gate(by_id[p]) for p in ps)

    for nid in order:
        n = by_id[nid]
        known = {"input"} | always_before[nid]
        if n["type"] == "condition":
            try:
                refs = expr.roots(expr.parse(n["expr"]))
            except PlatformError as e:
                problems.append(f"{nid}：{e.detail}")
                continue
            problems += [f"{nid}：表达式引用了 {r}，它不一定在此之前执行" for r in sorted(refs - known)]
        for path in data_paths({k: n.get(k) for k in ("args", "inputs", "compensation_args", "output")}):
            root = path.split(".")[0].split("[")[0]
            if root not in known and not (n.get("compensation_args") and root == nid):
                problems.append(f"{nid}：参数引用了 {root}，它不一定在此之前执行")
        if n["type"] == "tool":
            problems += _tool_problems(n, lookup, gated[nid])
    return problems


def _is_gate(n: dict[str, Any]) -> bool:
    return n["type"] == "human_task" and n["kind"] in ("confirm", "approve")


def _assignee_problems(assignee: str) -> list[str]:
    if assignee == "initiator" or assignee.startswith("user:"):
        return []
    role, _, where = assignee[5:].partition("@")
    out = [] if role in ROLES else [f"assignee 角色 {role} 不存在（可用：{', '.join(sorted(ROLES))}）"]
    expr_path = assignee_org_path(where)
    if (where.startswith("$") or where.startswith("org_of(")) and (
        expr_path is None or not expr_path.startswith("input.")
    ):
        out.append("assignee 的组织表达式只能引用 input")
    return out


def assignee_org_path(where: str) -> str | None:
    """组织写法：编码或 id（字面量）、$input.x、org_of(input.x)；后两者返回数据路径 input.x。"""
    if where.startswith("$"):
        return where[1:]
    if where.startswith("org_of(") and where.endswith(")"):
        return where[len("org_of(") : -1]
    return None


def _tool_problems(n: dict[str, Any], lookup: ToolLookup | None, gated: bool) -> list[str]:
    nid = n["id"]
    if lookup is None:
        return []
    spec = lookup(*split_ref(n["ref"]))
    if spec is None:
        return [f"{nid}：引用的 {n['ref']} 不存在或不可用"]
    out: list[str] = []
    if n.get("compensation") and lookup(*split_ref(n["compensation"])) is None:
        out.append(f"{nid}：补偿 {n['compensation']} 不存在或不可用")
    if spec.get("risk_level") in WRITE_RISKS:
        if not n.get("compensation") and not n.get("irreversible"):
            out.append(f"{nid}：写操作必须声明 compensation 或标记 irreversible")
        if n.get("irreversible") and not gated:
            out.append(f"{nid}：不可逆写操作之前的每条路径都必须经过 confirm / approve 人工任务")
    return out


def resolve_args(mapping: Any, ctx: dict[str, Any]) -> Any:
    """把参数映射中的数据路径替换为上下文里的值（缺失为 None）；"$$x" 表示字面量 "$x"。"""
    if isinstance(mapping, str) and mapping.startswith("$$"):
        return mapping[1:]
    if isinstance(mapping, str) and mapping.startswith("$"):
        return expr.evaluate(expr.parse(mapping[1:]), ctx)
    if isinstance(mapping, dict):
        return {k: resolve_args(v, ctx) for k, v in mapping.items()}
    if isinstance(mapping, list):
        return [resolve_args(v, ctx) for v in mapping]
    return mapping
