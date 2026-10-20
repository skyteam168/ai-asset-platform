"""确认对象（architecture.md 4.5）。

PDP 返回 require_confirmation 时 Runtime 不执行 Tool，而是冻结调用内容并创建 confirmation：
参数加密存放（seal），同时存规范化 JSON 的 SHA-256；执行前重新解密并核对哈希，任何篡改都拒绝执行。
状态机 pending → confirmed → executed（或 rejected / expired）；“同一 confirmation 只执行一次”靠
pending → confirmed 的条件更新保证，并发确认只有一个能赢，其余拿到已有的执行结果。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any, Protocol

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aip.data_plane.principal import Principal
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.keys import canonical_json
from aip.libs.tokens.secrets_util import seal, unseal

USER_CONFIRM_TTL_S = 600
APPROVAL_TTL_S = 86_400
TERMINAL = ("executed", "rejected", "expired")


def args_digest(args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(args)).hexdigest()


def render_summary(spec: dict[str, Any], args: dict[str, Any], version: str) -> str:
    """确认文案只由 Tool Spec 的 confirm_template 渲染（沙箱、严格未定义），不由模型生成。"""
    template = spec.get("confirm_template")
    if template:
        try:
            env = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
            return env.from_string(template).render(args=args)[:500]
        except TemplateError:
            pass
    cap = spec.get("capability_id")
    return f"执行 {spec.get('name', cap)}（{cap}@{version}），参数摘要 {args_digest(args)[:12]}"


def principal_to_json(p: Principal) -> dict[str, Any]:
    return {**p.as_dict(), "extra": p.extra}


def principal_from_json(d: dict[str, Any]) -> Principal:
    return Principal(**d)


def new_confirmation(
    *,
    kind: str,
    principal: Principal,
    spec: dict[str, Any],
    capability_id: str,
    version: str,
    args: dict[str, Any],
    execution_id: str,
    trace_id: str,
    run_id: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    ttl = USER_CONFIRM_TTL_S if kind == "user_confirm" else APPROVAL_TTL_S
    return {
        "id": str(uuid.uuid4()),
        "company_id": principal.consumer_company_id,
        "kind": kind,
        "capability_id": capability_id,
        "version": version,
        "consumer_id": principal.consumer_id,
        "end_user_id": principal.user_id,
        "agent_profile_id": principal.agent_profile_id,
        "principal": principal_to_json(principal),
        "args_enc": seal(json.dumps(args, ensure_ascii=False, sort_keys=True)),
        "args_sha256": args_digest(args),
        "execution_id": execution_id,
        "summary": render_summary(spec, args, version),
        "approver_role": spec.get("approver_role", "data_owner") if kind == "approval" else None,
        "status": "pending",
        "expires_at": now + dt.timedelta(seconds=ttl),
        "decided_by": None,
        "decided_via": None,
        "decided_at": None,
        "comment": None,
        "run_id": run_id,
        "trace_id": trace_id,
        "created_at": now,
    }


def frozen_args(row: dict[str, Any]) -> dict[str, Any]:
    """解密冻结参数并核对哈希；不一致说明存储被篡改，拒绝执行。"""
    try:
        args: dict[str, Any] = json.loads(unseal(row["args_enc"]))
    except Exception as e:  # 解密失败与哈希不符同样处理
        raise PlatformError(ErrorCode.CONFIRMATION_MISMATCH, "冻结参数无法解密，拒绝执行") from e
    if args_digest(args) != row["args_sha256"]:
        raise PlatformError(ErrorCode.CONFIRMATION_MISMATCH, "冻结参数与哈希不一致，拒绝执行")
    return args


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """对外（控制台待办、API）只给摘要，不给参数原文。"""
    keys = (
        "id", "company_id", "kind", "capability_id", "version", "consumer_id", "end_user_id", "summary",
        "approver_role", "status", "expires_at", "decided_by", "decided_via", "decided_at", "execution_id",
        "run_id", "args_sha256", "created_at",
    )  # fmt: skip
    return {k: (v.isoformat() if isinstance(v, dt.datetime) else (str(v) if isinstance(v, uuid.UUID) else v))
            for k, v in row.items() if k in keys}  # fmt: skip


class ConfirmationStore(Protocol):
    async def create(self, row: dict[str, Any]) -> None: ...
    async def get(self, cid: str) -> dict[str, Any] | None: ...
    async def claim(self, cid: str, *, by: str, via: str, comment: str | None) -> dict[str, Any] | None: ...
    async def finish(self, cid: str, status: str) -> None: ...
    async def reject(self, cid: str, *, by: str, via: str, comment: str | None) -> bool: ...
    async def pending(self, company_ids: list[str] | None) -> list[dict[str, Any]]: ...


class MemoryConfirmationStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(self, row: dict[str, Any]) -> None:
        self.rows[row["id"]] = dict(row)

    async def get(self, cid: str) -> dict[str, Any] | None:
        r = self.rows.get(cid)
        return dict(r) if r else None

    async def claim(self, cid: str, *, by: str, via: str, comment: str | None) -> dict[str, Any] | None:
        r = self.rows.get(cid)
        now = dt.datetime.now(dt.UTC)
        if r is None or r["status"] != "pending":
            return None
        if r["expires_at"] <= now:
            r["status"] = "expired"
            return None
        r.update(status="confirmed", decided_by=by, decided_via=via, decided_at=now, comment=comment)
        return dict(r)

    async def finish(self, cid: str, status: str) -> None:
        self.rows[cid]["status"] = status

    async def reject(self, cid: str, *, by: str, via: str, comment: str | None) -> bool:
        r = self.rows.get(cid)
        if r is None or r["status"] != "pending":
            return False
        r.update(status="rejected", decided_by=by, decided_via=via, decided_at=dt.datetime.now(dt.UTC), comment=comment)
        return True

    async def pending(self, company_ids: list[str] | None) -> list[dict[str, Any]]:
        now = dt.datetime.now(dt.UTC)
        return [
            dict(r)
            for r in self.rows.values()
            if r["status"] == "pending"
            and r["expires_at"] > now
            and (company_ids is None or r["company_id"] in company_ids)
        ]


class PgConfirmationStore:
    COLS = (
        "id", "company_id", "kind", "capability_id", "version", "consumer_id", "end_user_id", "agent_profile_id",
        "principal", "args_enc", "args_sha256", "execution_id", "summary", "approver_role", "status", "expires_at",
        "run_id", "trace_id", "created_at",
    )  # fmt: skip

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @staticmethod
    def _row(m: Any) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in dict(m).items()}

    async def create(self, row: dict[str, Any]) -> None:
        params = {k: row.get(k) for k in self.COLS}
        params["principal"] = json.dumps(row["principal"], ensure_ascii=False)
        cols = ", ".join(self.COLS)
        vals = ", ".join("CAST(:principal AS jsonb)" if c == "principal" else f":{c}" for c in self.COLS)
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.company_ids', :c, true)"), {"c": row["company_id"]})
            # 列名来自常量 COLS，值全部走绑定参数
            await conn.execute(text(f"INSERT INTO exec.confirmation ({cols}) VALUES ({vals})"), params)  # noqa: S608

    async def get(self, cid: str) -> dict[str, Any] | None:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            m = (
                (await conn.execute(text("SELECT * FROM exec.confirmation WHERE id = :id"), {"id": cid}))
                .mappings()
                .first()
            )
        return self._row(m) if m else None

    async def claim(self, cid: str, *, by: str, via: str, comment: str | None) -> dict[str, Any] | None:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text(
                    "UPDATE exec.confirmation SET status = 'expired' "
                    "WHERE id = :id AND status = 'pending' AND expires_at <= now()"
                ),
                {"id": cid},
            )
            m = (
                (
                    await conn.execute(
                        text(
                            "UPDATE exec.confirmation SET status = 'confirmed', decided_by = :by, decided_via = :via, "
                            "decided_at = now(), comment = :comment WHERE id = :id AND status = 'pending' RETURNING *"
                        ),
                        {"id": cid, "by": by, "via": via, "comment": comment},
                    )
                )
                .mappings()
                .first()
            )
        return self._row(m) if m else None

    async def finish(self, cid: str, status: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            await conn.execute(
                text("UPDATE exec.confirmation SET status = :s WHERE id = :id"), {"id": cid, "s": status}
            )

    async def reject(self, cid: str, *, by: str, via: str, comment: str | None) -> bool:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            r = await conn.execute(
                text(
                    "UPDATE exec.confirmation SET status = 'rejected', decided_by = :by, decided_via = :via, "
                    "decided_at = now(), comment = :comment WHERE id = :id AND status = 'pending' RETURNING id"
                ),
                {"id": cid, "by": by, "via": via, "comment": comment},
            )
            return r.first() is not None

    async def pending(self, company_ids: list[str] | None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM exec.confirmation WHERE status = 'pending' AND expires_at > now()"
        params: dict[str, Any] = {}
        if company_ids is not None:
            sql += " AND company_id = ANY(CAST(:c AS uuid[]))"
            params["c"] = company_ids
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            rows = (await conn.execute(text(sql + " ORDER BY created_at LIMIT 500"), params)).mappings().all()
        return [self._row(m) for m in rows]
