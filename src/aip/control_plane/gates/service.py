"""质量门禁流水线（architecture.md 7.1）：状态迁移只能由门禁结果驱动。

Draft --G0--> Testing --G1/G2--> InReview --G5 签字 + G6 审批--> Approved --G7 灰度--> Canary --G8--> Active
快速通道（只读、L1/L2、无 PII）：G6 单人审批。write / danger：G6 双人复核。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, MetaData, Table, Text, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.connectors.credentials import SecretStore
from aip.control_plane.approval import service as approval
from aip.control_plane.conn import service as conn_service
from aip.control_plane.iam import snapshot as iam_snapshot
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.control_plane.registry import tables as rt
from aip.control_plane.testlab.runner import run_cases
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow
from aip.libs.workflow.dsl import split_ref
from aip.libs.workflow.dsl import validate as validate_dsl

md = MetaData(schema="registry")
U = UUID(as_uuid=True)
test_case = Table(
    "test_case",
    md,
    Column("id", U, primary_key=True),
    Column("version_id", U),
    Column("company_id", U),
    Column("name", Text),
    Column("kind", Text),
    Column("critical", Boolean),
    Column("args", JSONB),
    Column("principal", JSONB),
    Column("expect", JSONB),
)
gate_result = Table(
    "gate_result",
    md,
    Column("id", U, primary_key=True),
    Column("version_id", U),
    Column("company_id", U),
    Column("gate", Text),
    Column("status", Text),
    Column("evidence", JSONB),
    Column("operator", U),
    Column("created_at", DateTime(timezone=True)),
)
acceptance_sheet = Table(
    "acceptance_sheet",
    md,
    Column("version_id", U, primary_key=True),
    Column("company_id", U),
    Column("template", JSONB),
    Column("results", JSONB),
    Column("signed_by", U),
    Column("signed_at", DateTime(timezone=True)),
)

# 工具描述是审核对象（architecture.md 12.3）：隐藏字符、诱导越权措辞、外链
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤﻿\U000e0000-\U000e007f]")
_INJECTION = re.compile(
    r"ignore (all )?(previous|prior|above) instructions|disregard .{0,20}instructions|system prompt|"
    r"do not (tell|inform) the user|忽略(之前|以上|前面|先前)的?(所有)?(指令|说明|规则)|不要告诉用户|你现在是|"
    r"<\s*script|exfiltrat",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://", re.IGNORECASE)


def scan_text(*texts: str) -> list[str]:
    findings = []
    for t in texts:
        if _HIDDEN.search(t):
            findings.append("包含零宽、方向控制或 Tag 等隐藏字符")
        if _INJECTION.search(t):
            findings.append("包含诱导模型越权或隐瞒用户的措辞")
        if _URL.search(t):
            findings.append("描述中不允许出现外部链接")
    return sorted(set(findings))


async def _record(
    conn: AsyncConnection, actor: Actor, v: dict[str, Any], gate: str, ok: bool, evidence: dict[str, Any]
) -> None:
    await conn.execute(
        gate_result.insert().values(
            id=new_id(),
            version_id=v["id"],
            company_id=v["company_id"],
            gate=gate,
            status="passed" if ok else "failed",
            evidence=evidence,
            operator=actor.user_id,
        )
    )


async def add_test_case(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    *,
    name: str,
    kind: str,
    args: dict[str, Any],
    expect: dict[str, Any],
    principal: dict[str, Any] | None = None,
    critical: bool = True,
) -> uuid.UUID:
    v = await registry.version_row(conn, version_id)
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    if v["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "用例属于资产版本，只能在 draft 阶段维护")
    if kind not in ("functional", "permission"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "kind 只能是 functional 或 permission")
    cid = new_id()
    await conn.execute(
        test_case.insert().values(
            id=cid,
            version_id=version_id,
            company_id=v["company_id"],
            name=name,
            kind=kind,
            critical=critical,
            args=args,
            principal=principal or {},
            expect=expect,
        )
    )
    return cid


async def set_acceptance(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID, template: dict[str, Any]) -> None:
    """验收单模板在 Draft 阶段写入：目标场景、准确率、完成率、耗时、人工介入率（architecture.md 7.1）。"""
    v = await registry.version_row(conn, version_id)
    actor.require(actor.has("asset_developer") or actor.has("platform_admin") or actor.has("business_owner"))
    if not template.get("scenarios"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "验收单至少需要一个目标场景")
    stmt = text(
        "INSERT INTO registry.acceptance_sheet (version_id, company_id, template) VALUES (:v, :c, "
        "CAST(:t AS jsonb)) ON CONFLICT (version_id) DO UPDATE SET template = EXCLUDED.template"
    )
    import json

    await conn.execute(stmt, {"v": version_id, "c": v["company_id"], "t": json.dumps(template, ensure_ascii=False)})


async def submit(conn: AsyncConnection, actor: Actor, version_id: uuid.UUID) -> dict[str, Any]:
    """G0 静态检查，通过后 draft → testing。"""
    v = await registry.version_row(conn, version_id)
    actor.require(actor.user_id == v["submitted_by"] or actor.has("platform_admin"), "只有提交人可以提交")
    if v["state"] != "draft":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"版本状态 {v['state']} 不能提交")
    a = await registry.asset_row(conn, v["asset_id"])
    problems: list[str] = []
    try:
        registry.validate_spec(a["type"], v["spec"])
    except PlatformError as e:
        problems.append(e.detail)
    spec = v["spec"]
    problems += scan_text(spec.get("description", ""), spec.get("usage_notes", ""), *spec.get("not_for", []))
    if a["type"] == "workflow" and not problems:
        # DSL 静态校验：无环、可达、表达式、引用的 Tool 版本可解析、写节点有补偿或前置人工确认（6.3）
        tools = await _tool_versions(conn, _workflow_refs(spec))
        problems += validate_dsl(spec, lambda cap, ver: (tools.get((cap, ver)) or {}).get("spec"))
    if a["type"] == "bundle" and not problems:
        # 套件成员须可解析；组合约束（L3+ 读 ∩ external）在 G6 显式批准，G0 只记录不阻断
        refs = list(spec.get("tools") or [])
        tools = await _tool_versions(conn, refs)
        for ref in refs:
            if split_ref(ref) not in tools:
                problems.append(f"依赖不可解析：Tool {ref}")
    ref = spec.get("connector_ref", {}).get("connector")
    if (
        ref
        and (
            await conn.execute(
                select(conn_service.connector_instance.c.id).where(conn_service.connector_instance.c.name == ref)
            )
        ).first()
        is None
    ):
        problems.append(f"依赖不可解析：Connector 实例 {ref} 不存在")
    kinds = {r[0] for r in await conn.execute(select(test_case.c.kind).where(test_case.c.version_id == version_id))}
    if a["type"] not in ("workflow", "bundle") and kinds != {"functional", "permission"}:
        problems.append("至少需要一个功能用例和一个权限用例")
    if (
        await conn.execute(select(acceptance_sheet.c.version_id).where(acceptance_sheet.c.version_id == version_id))
    ).first() is None:
        problems.append("缺少验收单模板")
    ok = not problems
    await _record(conn, actor, v, "G0", ok, {"problems": problems})
    if ok:
        await registry.transition(conn, actor, version_id, "testing", "G0 passed")
    return {"gate": "G0", "passed": ok, "problems": problems}


def _fast_track(spec: dict[str, Any], classification: int) -> bool:
    return spec.get("risk_level") == "read" and classification <= 2 and not spec.get("pii_fields")


async def run_lab(
    conn: AsyncConnection, actor: Actor, version_id: uuid.UUID, *, secrets: SecretStore, resolver: Any = None
) -> dict[str, Any]:
    """G1 契约 + G2 功能与权限；通过则 testing → in_review 并发起 G5 / G6 审批，失败退回 draft。"""
    v = await registry.version_row(conn, version_id)
    actor.require(actor.has("asset_developer") or actor.has("platform_admin"))
    if v["state"] != "testing":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只有 testing 状态可以运行门禁测试")
    a = await registry.asset_row(conn, v["asset_id"])
    cases = [
        dict(r) for r in (await conn.execute(select(test_case).where(test_case.c.version_id == version_id))).mappings()
    ]
    if a["type"] in ("workflow", "bundle"):
        return await _composition_lab(conn, actor, v, a)
    iam = await iam_snapshot.build_payload(conn)
    conns = await conn_service.snapshot_part(conn)
    report = await run_cases(
        spec=v["spec"],
        classification=a["classification"],
        cases=cases,
        iam_payload=iam,
        connectors=conns["connectors"],
        allowlist=conns["network_allowlist"],
        opa_url=get_settings().opa_url,
        secrets=secrets,
        resolver=resolver,
        repeats=get_settings().g3_repeats,
    )
    await _record(conn, actor, v, "G1", report.g1_passed, report.evidence())
    await _record(conn, actor, v, "G2", report.g2_passed, report.evidence())
    await _record(conn, actor, v, "G3", report.g3_passed, {"stability": report.stability})
    if not (report.g1_passed and report.g2_passed and report.g3_passed):
        await registry.transition(conn, actor, version_id, "draft", "G1/G2/G3 failed")
        return {"passed": False, **report.evidence()}
    await registry.transition(conn, actor, version_id, "in_review", "G1/G2/G3 passed")
    return {"passed": True, **(await _open_reviews(conn, v, a)), **report.evidence()}


async def _open_reviews(conn: AsyncConnection, v: dict[str, Any], a: dict[str, Any]) -> dict[str, str]:
    version_id = v["id"]
    submitter = v["submitted_by"]
    g5 = await approval.create(
        conn,
        kind="G5",
        subject_id=version_id,
        company_id=v["company_id"],
        submitter=submitter,
        approver_role="business_owner",
        sla_business_days=3,
    )
    # 套件没有自身的 risk_level；组合风险时强制双人 + 显式批准（architecture.md 8.7）
    risk = await _bundle_composition_risk(conn, a["type"], v["spec"])
    required = 1 if not risk and _fast_track(v["spec"], a["classification"]) else 2
    g6_payload: dict[str, Any] = {"fast_track": required == 1}
    if risk:
        g6_payload["composition_risk"] = True
        g6_payload["composition_detail"] = risk
    g6 = await approval.create(
        conn,
        kind="G6",
        subject_id=version_id,
        company_id=v["company_id"],
        submitter=submitter,
        approver_role="data_owner",
        required=required,
        sla_business_days=2,
        payload=g6_payload,
    )
    return {"g5_approval": str(g5), "g6_approval": str(g6)}


def _workflow_refs(spec: dict[str, Any]) -> list[str]:
    return [
        n[k] for n in spec.get("nodes", []) for k in ("ref", "compensation") if n.get("type") == "tool" and n.get(k)
    ]


def _bundle_refs(spec: dict[str, Any]) -> list[str]:
    return list(spec.get("tools") or [])


def composition_risk_of(tool_specs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同一集合同时含 L3+ 读 Tool 与 egress_class=external → 需 G6 显式批准（8.7）。"""
    l3_read = [
        s.get("capability_id", "")
        for s in tool_specs
        if s.get("risk_level") == "read" and int(s.get("data_classification") or 0) >= 3
    ]
    external = [s.get("capability_id", "") for s in tool_specs if s.get("egress_class") == "external"]
    if l3_read and external:
        return {"l3_read": l3_read, "external_egress": external}
    return None


async def _bundle_composition_risk(
    conn: AsyncConnection, asset_type: str, spec: dict[str, Any]
) -> dict[str, Any] | None:
    if asset_type != "bundle":
        return None
    tools = await _tool_versions(conn, _bundle_refs(spec))
    return composition_risk_of([t["spec"] for t in tools.values()])


async def _tool_versions(conn: AsyncConnection, refs: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Workflow 引用的 Tool 版本：(capability_id, semver) → {spec, state}。"""
    wanted = {split_ref(r) for r in refs}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not wanted:
        return out
    rows = await conn.execute(
        select(rt.asset_version.c.spec, rt.asset_version.c.semver, rt.asset_version.c.state)
        .select_from(rt.asset_version.join(rt.asset, rt.asset.c.id == rt.asset_version.c.asset_id))
        .where(rt.asset.c.type == "tool")
        .where(rt.asset_version.c.semver.in_([ver for _, ver in wanted]))
    )
    for spec, semver, state in rows:
        key = (spec.get("capability_id", ""), semver)
        if key in wanted:
            out[key] = {"spec": spec, "state": state}
    return out


async def _composition_lab(
    conn: AsyncConnection, actor: Actor, v: dict[str, Any], a: dict[str, Any]
) -> dict[str, Any]:
    """Workflow / 套件的 G1–G3：引用的每个 Tool 版本必须已通过自身门禁（approved 及以后状态）。
    Workflow 在沙箱中的整体回放需要隔离的运行表，推迟到 M6e。"""
    refs = _workflow_refs(v["spec"]) if a["type"] == "workflow" else _bundle_refs(v["spec"])
    tools = await _tool_versions(conn, refs)
    states = {r: (tools.get(split_ref(r)) or {}).get("state", "missing") for r in refs}
    ok = all(s in registry.RELEASABLE for s in states.values())
    risk = composition_risk_of([t["spec"] for t in tools.values()]) if a["type"] == "bundle" else None
    evidence: dict[str, Any] = {"mode": "composition", "tool_versions": states}
    if risk:
        evidence["composition_risk"] = risk
    for gate in ("G1", "G2", "G3"):
        await _record(conn, actor, v, gate, ok, evidence)
    if not ok:
        await registry.transition(conn, actor, v["id"], "draft", "referenced tools not released")
        return {"passed": False, **evidence}
    await registry.transition(conn, actor, v["id"], "in_review", "composition gates passed")
    return {"passed": True, **(await _open_reviews(conn, v, a)), **evidence}


async def decide(
    conn: AsyncConnection,
    actor: Actor,
    request_id: uuid.UUID,
    approve: bool,
    comment: str = "",
    results: dict[str, Any] | None = None,
    on_behalf_of: uuid.UUID | None = None,
) -> str:
    """G5 / G6 审批。验收人与安全审核人不能是同一人；两者都通过后 in_review → approved。"""
    req = await approval.request_row(conn, request_id)
    if req["kind"] not in ("G5", "G6"):
        return await approval.decide(conn, actor, request_id, approve, comment, on_behalf_of)
    sibling_kind = "G6" if req["kind"] == "G5" else "G5"
    sibling = (
        (
            await conn.execute(
                select(approval.approval_request)
                .where(
                    and_(
                        approval.approval_request.c.subject_id == req["subject_id"],
                        approval.approval_request.c.kind == sibling_kind,
                        approval.approval_request.c.status != "cancelled",
                    )
                )
                .order_by(approval.approval_request.c.created_at.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    who = on_behalf_of or actor.user_id
    if sibling is not None and who in await approval.approvers_of(conn, sibling["id"]):
        raise PlatformError(ErrorCode.PERMISSION_DENIED, "同一人不能既做业务验收又做安全审核")
    # 套件组合约束：含 L3+ 读与对外发送时，G6 批准必须显式带 composition_override 与理由（8.7）
    if req["kind"] == "G6" and approve and (req.get("payload") or {}).get("composition_risk"):
        override = (results or {}).get("composition_override") is True
        reason = str((results or {}).get("reason") or comment or "").strip()
        if not override or not reason:
            raise PlatformError(
                ErrorCode.INVALID_ARGUMENT,
                "该套件同时含 L3+ 读与对外发送 Tool，G6 须显式批准（composition_override=true）并写明理由",
            )
        # 把理由写入审批 payload，供审计追溯
        merged = {**(req.get("payload") or {}), "composition_override": True, "composition_reason": reason}
        await conn.execute(
            update(approval.approval_request).where(approval.approval_request.c.id == request_id).values(payload=merged)
        )
    status = await approval.decide(conn, actor, request_id, approve, comment, on_behalf_of)
    v = await registry.version_row(conn, req["subject_id"])
    if req["kind"] == "G5" and status == "approved":
        await conn.execute(
            update(acceptance_sheet)
            .where(acceptance_sheet.c.version_id == v["id"])
            .values(results=results or {}, signed_by=who, signed_at=utcnow())
        )
    if status in ("approved", "rejected"):
        await _record(
            conn, actor, v, req["kind"], status == "approved", {"approval_id": str(request_id), "comment": comment}
        )
    if status == "rejected" and v["state"] == "in_review":
        if sibling is not None and sibling["status"] == "pending":
            await conn.execute(
                update(approval.approval_request)
                .where(approval.approval_request.c.id == sibling["id"])
                .values(status="cancelled")
            )
        await registry.transition(conn, actor, v["id"], "draft", f"{req['kind']} rejected")
    if status == "approved" and sibling is not None and sibling["status"] == "approved" and v["state"] == "in_review":
        await registry.transition(conn, actor, v["id"], "approved", "G5 and G6 approved")
    return status


async def start_canary(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    *,
    env: str,
    canary_scope: dict[str, Any],
    exposure: list[str],
) -> uuid.UUID:
    """G7：小范围发布（approved → canary）。"""
    rid = await registry.create_release(
        conn, actor, version_id, env=env, channel="stable", scope=canary_scope, exposure=exposure
    )
    await registry.transition(conn, actor, version_id, "canary", "G7 canary started")
    return rid


async def promote(
    conn: AsyncConnection,
    actor: Actor,
    version_id: uuid.UUID,
    *,
    env: str,
    scope: dict[str, Any],
    exposure: list[str],
    min_calls: int = 20,
    max_error_rate: float = 0.05,
) -> dict[str, Any]:
    """G7 指标达标后全量（canary → active，G8）。指标取自灰度期间的执行记录。"""
    actor.require(actor.has("platform_admin"))
    v = await registry.version_row(conn, version_id)
    if v["state"] != "canary":
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只有 canary 状态可以全量")
    cap = v["spec"].get("capability_id")
    row = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) AS calls, count(*) FILTER (WHERE status = 'failed' AND error_code NOT IN "
                    "('PERMISSION_DENIED','OUT_OF_SCOPE','CLASSIFICATION_DENIED','INVALID_ARGUMENT')) AS errors "
                    "FROM exec.tool_execution WHERE capability_id = :c AND version = :v"
                ),
                {"c": cap, "v": v["semver"]},
            )
        )
        .mappings()
        .one()
    )
    calls, errors = int(row["calls"]), int(row["errors"])
    rate = errors / calls if calls else 0.0
    ok = calls >= min_calls and rate <= max_error_rate
    evidence = {
        "calls": calls,
        "errors": errors,
        "error_rate": round(rate, 4),
        "min_calls": min_calls,
        "max_error_rate": max_error_rate,
    }
    await _record(conn, actor, v, "G7", ok, evidence)
    if not ok:
        return {"passed": False, **evidence}
    await registry.create_release(conn, actor, version_id, env=env, channel="stable", scope=scope, exposure=exposure)
    await registry.transition(conn, actor, version_id, "active", "G8 full rollout")
    await _record(conn, actor, v, "G8", True, {"scope": scope})
    return {"passed": True, **evidence}


async def gate_history(conn: AsyncConnection, version_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(gate_result).where(gate_result.c.version_id == version_id).order_by(gate_result.c.created_at)
    )
    return [
        {
            k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in r.items()
        }
        for r in rows.mappings()
    ]


async def count_cases(conn: AsyncConnection, version_id: uuid.UUID) -> int:
    return int(
        (
            await conn.execute(select(func.count()).select_from(test_case).where(test_case.c.version_id == version_id))
        ).scalar_one()
    )
