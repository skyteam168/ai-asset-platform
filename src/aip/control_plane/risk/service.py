"""风险规则引擎（architecture.md 12.4、P9）。

扫描审计库（数据面写入的 pdp.decision、tool.execution、auth.* 等）与控制面领域事件，按规则在时间窗口内聚合：
- 阈值规则：分组计数 ≥ threshold；
- 比例规则：分组内满足 ratio.where 的比例 ≥ threshold，且总数 ≥ min_count；
- 非工作时段规则：按公司时区筛出工作时间以外的事件后再计数。
每条规则在每个分组、每个窗口最多产生一条事件（唯一约束保证多实例并发扫描也不重复处置）。
动作：alert 只告警；suspend_asset 把该能力当前对外的版本置为 Suspended（PDP 随快照拒绝调用），
恢复必须发起复核审批（kind=resume），批准后版本回到进入 Suspended 之前的状态。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aip.control_plane.approval import service as approval
from aip.control_plane.iam.authz import Actor
from aip.control_plane.registry import service as registry
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.common.ids import new_id, utcnow

log = logging.getLogger(__name__)
FIELD = re.compile(r"^[a-z_][a-z0-9_]*$")
SUSPENDABLE = ("canary", "active", "deprecated")
SOURCES = {
    "audit": ("audit.audit_log", "record"),
    "domain": ("registry.domain_event", "(payload || jsonb_build_object('subject_id', subject_id::text))"),
}

# 告警出口（通知插件、webhook）在进程启动时注册；规则命中只依赖这个接口
Notifier = Callable[[dict[str, Any]], Awaitable[None]]
_notifiers: list[Notifier] = []


def register_notifier(fn: Notifier) -> None:
    _notifiers.append(fn)


def _fields(names: list[str]) -> list[str]:
    for n in names:
        if not FIELD.match(n):
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"字段名不合法：{n}")
    return names


def validate_params(params: dict[str, Any]) -> None:
    if not isinstance(params.get("event"), str) or not params["event"]:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "规则缺少 event")
    _fields(list(params.get("group_by") or []))
    _fields(list((params.get("where") or {}).keys()))
    _fields(list((params.get("contains") or {}).keys()))
    _fields(list(((params.get("ratio") or {}).get("where") or {}).keys()))
    if int(params.get("window_s", 0)) <= 0 or float(params.get("threshold", 0)) <= 0:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "window_s 与 threshold 必须为正数")
    if "off_hours" in params:
        oh = params["off_hours"]
        for tz in [oh.get("default_tz", "UTC"), *(oh.get("tz_by_company_code") or {}).values()]:
            ZoneInfo(tz)


def _filters(params: dict[str, Any], rec: str, binds: dict[str, Any], prefix: str = "w") -> list[str]:
    out = []
    for i, (f, values) in enumerate((params.get("where") or {}).items()):
        binds[f"{prefix}{i}"] = [str(v) for v in values]
        out.append(f"{rec}->>'{f}' = ANY(:{prefix}{i})")
    for i, (f, value) in enumerate((params.get("contains") or {}).items()):
        binds[f"c{prefix}{i}"] = str(value)
        out.append(f"jsonb_exists(COALESCE({rec}->'{f}', '[]'::jsonb), :c{prefix}{i})")
    return out


async def _groups(
    conn: AsyncConnection, rule: dict[str, Any], since: dt.datetime, until: dt.datetime
) -> list[dict[str, Any]]:
    """返回超过阈值的分组：{key, company_id, observed}。字段名经白名单校验后才拼进 SQL，取值全部绑定。"""
    params = rule["params"]
    table, rec = SOURCES[rule["source"]]
    group = _fields(list(params.get("group_by") or []))
    binds: dict[str, Any] = {"event": params["event"], "since": since, "until": until}
    conds = ["kind = :event", "occurred_at >= :since", "occurred_at < :until", *_filters(params, rec, binds)]
    keys = [f"{rec}->>'{f}' AS g{i}" for i, f in enumerate(group)] or ["'*' AS g0"]
    gcols = ", ".join(f"g{i}" for i in range(max(len(group), 1)))
    base = f"SELECT occurred_at, company_id, {', '.join(keys)} FROM {table} WHERE {' AND '.join(conds)}"  # noqa: S608
    if "off_hours" in params:
        return await _off_hours(conn, base, binds, params, len(group) or 1)
    if "ratio" in params:
        ratio_where = _filters(params["ratio"], rec, binds, "r") or ["true"]
        base = base.replace(" FROM ", f", ({' AND '.join(ratio_where)}) AS bad FROM ", 1)
        sql = (
            f"SELECT {gcols}, max(company_id::text) AS company, count(*) AS total, count(*) FILTER (WHERE bad) AS bad "  # noqa: S608
            f"FROM ({base}) b GROUP BY {gcols} "
            "HAVING count(*) >= :min_count AND count(*) FILTER (WHERE bad)::numeric / count(*) >= :threshold"
        )
        binds.update(min_count=int(params.get("min_count", 1)), threshold=float(params["threshold"]))
        rows = (await conn.execute(text(sql), binds)).mappings().all()
        return [
            {
                "key": _key(group, r),
                "company_id": r["company"],
                "observed": round(r["bad"] / r["total"], 4),
                "detail": {"total": r["total"], "matched": r["bad"]},
            }
            for r in rows
        ]
    sql = (
        f"SELECT {gcols}, max(company_id::text) AS company, count(*) AS n FROM ({base}) b "  # noqa: S608
        f"GROUP BY {gcols} HAVING count(*) >= :threshold"
    )
    binds["threshold"] = float(params["threshold"])
    rows = (await conn.execute(text(sql), binds)).mappings().all()
    return [{"key": _key(group, r), "company_id": r["company"], "observed": r["n"], "detail": {}} for r in rows]


def _key(group: list[str], row: Any) -> dict[str, Any]:
    return {f: row[f"g{i}"] for i, f in enumerate(group)} if group else {"*": "*"}


async def _off_hours(
    conn: AsyncConnection, base: str, binds: dict[str, Any], params: dict[str, Any], ngroup: int
) -> list[dict[str, Any]]:
    oh = params["off_hours"]
    start, end = (dt.time.fromisoformat(oh.get(k, d)) for k, d in (("start", "07:00"), ("end", "20:00")))
    workdays = set(oh.get("workdays", [1, 2, 3, 4, 5]))
    codes = dict(
        (str(cid), code)
        for cid, code in await conn.execute(text("SELECT id, code FROM iam.org_unit WHERE type = 'company'"))
    )
    tz_map = oh.get("tz_by_company_code") or {}
    counts: dict[tuple[Any, ...], int] = defaultdict(int)
    company: dict[tuple[Any, ...], str | None] = {}
    for r in (await conn.execute(text(base), binds)).mappings():
        cid = str(r["company_id"]) if r["company_id"] else None
        tz = ZoneInfo(tz_map.get(codes.get(cid or "", ""), oh.get("default_tz", "UTC")))
        local = r["occurred_at"].astimezone(tz)
        if local.isoweekday() in workdays and start <= local.time() < end:
            continue
        k = tuple(r[f"g{i}"] for i in range(ngroup))
        counts[k] += 1
        company[k] = cid
    group = list(params.get("group_by") or [])
    return [
        {
            "key": {f: k[i] for i, f in enumerate(group)} or {"*": "*"},
            "company_id": company[k],
            "observed": n,
            "detail": {},
        }
        for k, n in counts.items()
        if n >= float(params["threshold"])
    ]


async def scan(conn: AsyncConnection, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """评估全部启用的规则；返回本次新产生的事件（已执行动作）。"""
    now = now or utcnow()
    created: list[dict[str, Any]] = []
    rules = (await conn.execute(text("SELECT * FROM risk.rule WHERE enabled ORDER BY code"))).mappings().all()
    for rule in rules:
        window = int(rule["params"]["window_s"])
        bucket = dt.datetime.fromtimestamp(int(now.timestamp()) // window * window, dt.UTC)
        for g in await _groups(conn, dict(rule), now - dt.timedelta(seconds=window), now):
            eid = new_id()
            inserted = (
                await conn.execute(
                    text(
                        """INSERT INTO risk.event (id, rule_code, company_id, group_key, window_start, window_s,
                             observed, threshold, severity, action)
                           VALUES (:id, :rule, :company, CAST(:key AS jsonb), :ws, :window, :observed, :threshold,
                             :severity, :action)
                           ON CONFLICT (rule_code, group_key, window_start) DO NOTHING RETURNING id"""
                    ),
                    {
                        "id": eid,
                        "rule": rule["code"],
                        "company": g["company_id"],
                        "key": json.dumps(g["key"]),
                        "ws": bucket,
                        "window": window,
                        "observed": g["observed"],
                        "threshold": rule["params"]["threshold"],
                        "severity": rule["severity"],
                        "action": rule["action"],
                    },
                )
            ).first()
            if inserted is None:
                continue  # 同一窗口已处置过
            result = await _act(conn, dict(rule), g)
            await conn.execute(
                text("UPDATE risk.event SET action_result = CAST(:r AS jsonb) WHERE id = :id"),
                {"id": eid, "r": json.dumps({**result, **g["detail"]}, ensure_ascii=False)},
            )
            event = {
                "id": str(eid),
                "rule_code": rule["code"],
                "rule_name": rule["name"],
                "severity": rule["severity"],
                "action": rule["action"],
                "group_key": g["key"],
                "observed": g["observed"],
                "threshold": rule["params"]["threshold"],
                "company_id": g["company_id"],
                "action_result": result,
            }
            await conn.execute(
                text(
                    "INSERT INTO registry.domain_event (company_id, kind, subject_id, payload) "
                    "VALUES (:c, 'risk.triggered', :id, CAST(:p AS jsonb))"
                ),
                {"c": g["company_id"], "id": eid, "p": json.dumps(event, ensure_ascii=False, default=str)},
            )
            log.warning("risk rule %s triggered: %s", rule["code"], g["key"])
            created.append(event)
    for event in created:
        for notify in _notifiers:
            try:
                await notify(event)
            except Exception:  # 通知失败不影响处置
                log.exception("risk notifier failed")
    return created


async def _act(conn: AsyncConnection, rule: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    if rule["action"] != "suspend_asset":
        return {"alerted": True}
    cap = g["key"].get("capability_id")
    if not cap:
        return {"alerted": True, "skipped": "分组中没有 capability_id，无法 Suspended"}
    rows = (
        await conn.execute(
            text(
                "SELECT v.id, v.state, v.semver FROM registry.asset_version v "
                "JOIN registry.asset a ON a.id = v.asset_id "
                "WHERE v.spec->>'capability_id' = :cap AND v.state = ANY(:states)"
            ),
            {"cap": cap, "states": list(SUSPENDABLE)},
        )
    ).all()
    system = Actor.system_actor()
    suspended = []
    for vid, state, semver in rows:
        await registry.transition(conn, system, vid, "suspended", f"风险规则 {rule['code']} 自动熔断")
        suspended.append({"version_id": str(vid), "semver": semver, "from": state})
    return {"capability_id": cap, "suspended": suspended}


# ---------------------------------------------------------------- 规则与事件管理


def _out(row: Any) -> dict[str, Any]:
    return {
        k: (str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, dt.datetime) else v)
        for k, v in dict(row).items()
    }


async def list_rules(conn: AsyncConnection) -> list[dict[str, Any]]:
    return [_out(r) for r in (await conn.execute(text("SELECT * FROM risk.rule ORDER BY code"))).mappings()]


async def update_rule(conn: AsyncConnection, actor: Actor, code: str, patch: dict[str, Any]) -> dict[str, Any]:
    actor.require(actor.has("platform_admin"), "风险规则由平台管理员维护")
    row = (await conn.execute(text("SELECT * FROM risk.rule WHERE code = :c"), {"c": code})).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "规则不存在")
    params = {**row["params"], **(patch.get("params") or {})}
    validate_params(params)
    action = patch.get("action", row["action"])
    if action == "suspend_asset" and "capability_id" not in (params.get("group_by") or []):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "自动 Suspended 的规则必须按 capability_id 分组")
    await conn.execute(
        text(
            "UPDATE risk.rule SET params = CAST(:p AS jsonb), enabled = :e, action = :a, severity = :s, "
            "updated_by = :by, updated_at = now() WHERE code = :c"
        ),
        {
            "c": code,
            "p": json.dumps(params, ensure_ascii=False),
            "e": patch.get("enabled", row["enabled"]),
            "a": action,
            "s": patch.get("severity", row["severity"]),
            "by": actor.user_id,
        },
    )
    return _out((await conn.execute(text("SELECT * FROM risk.rule WHERE code = :c"), {"c": code})).mappings().one())


async def list_events(conn: AsyncConnection, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT e.*, r.name AS rule_name FROM risk.event e JOIN risk.rule r ON r.code = e.rule_code"
    params: dict[str, Any] = {"limit": min(max(limit, 1), 500)}
    if status:
        sql += " WHERE e.status = :status"
        params["status"] = status
    rows = (await conn.execute(text(sql + " ORDER BY e.created_at DESC LIMIT :limit"), params)).mappings()
    return [_out(r) for r in rows]


async def _event(conn: AsyncConnection, event_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(text("SELECT * FROM risk.event WHERE id = :id"), {"id": event_id})).mappings().first()
    if row is None:
        raise PlatformError(ErrorCode.NOT_FOUND, "风险事件不存在或不可见")
    return dict(row)


def _can_handle(actor: Actor) -> bool:
    return actor.has("platform_admin") or actor.has("data_owner")


async def acknowledge(conn: AsyncConnection, actor: Actor, event_id: uuid.UUID, note: str) -> dict[str, Any]:
    """告警类事件：确认已处理即关闭。Suspended 类事件不能直接关闭，必须走复核恢复。"""
    actor.require(_can_handle(actor), "需要平台管理员或数据 Owner")
    ev = await _event(conn, event_id)
    status = "resolved" if ev["action"] == "alert" else "acknowledged"
    await conn.execute(
        text(
            "UPDATE risk.event SET status = :s, note = :n, resolved_by = :by, "
            "resolved_at = CASE WHEN :s = 'resolved' THEN now() END "
            "WHERE id = :id AND status IN ('open','acknowledged')"
        ),
        {"id": event_id, "s": status, "n": note, "by": actor.user_id},
    )
    return _out(await _event(conn, event_id))


async def request_resume(conn: AsyncConnection, actor: Actor, event_id: uuid.UUID, note: str) -> dict[str, Any]:
    """自动 Suspended 的恢复：发起人写明修复情况，由另一位数据 Owner 复核批准。

    批准后版本回到进入 Suspended 之前的状态（architecture.md 第 7 章）。"""
    actor.require(_can_handle(actor), "需要平台管理员或数据 Owner")
    ev = await _event(conn, event_id)
    if ev["action"] != "suspend_asset" or ev["status"] not in ("open", "acknowledged"):
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "只有处于打开状态的熔断事件可以申请恢复")
    if len(note.strip()) < 4:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "请写明修复情况")
    company = ev["company_id"]
    if company is None:
        raise PlatformError(ErrorCode.PRECONDITION_FAILED, "事件没有归属公司，无法发起复核")
    rid = await approval.create(
        conn,
        kind="resume",
        subject_id=event_id,
        company_id=company,
        submitter=actor.user_id,
        approver_role="data_owner",
        payload={"rule_code": ev["rule_code"], "note": note, "suspended": ev["action_result"].get("suspended", [])},
    )
    await conn.execute(
        text("UPDATE risk.event SET status = 'resume_requested', note = :n WHERE id = :id"), {"id": event_id, "n": note}
    )
    return {"event_id": str(event_id), "approval_id": str(rid), "status": "resume_requested"}


async def decide_resume(
    conn: AsyncConnection, actor: Actor, request_id: uuid.UUID, approve: bool, comment: str, on_behalf_of: Any = None
) -> str:
    req = await approval.request_row(conn, request_id)
    status = await approval.decide(conn, actor, request_id, approve, comment, on_behalf_of)
    event_id = req["subject_id"]
    if status == "approved":
        ev = await _event(conn, event_id)
        for s in ev["action_result"].get("suspended", []):
            v = await registry.version_row(conn, uuid.UUID(s["version_id"]))
            if v["state"] == "suspended":
                await registry.transition(conn, Actor.system_actor(), v["id"], v["suspended_from"], "复核通过，恢复")
        await conn.execute(
            text("UPDATE risk.event SET status = 'resolved', resolved_by = :by, resolved_at = now() WHERE id = :id"),
            {"id": event_id, "by": actor.user_id},
        )
    elif status == "rejected":
        await conn.execute(text("UPDATE risk.event SET status = 'open' WHERE id = :id"), {"id": event_id})
    return status
