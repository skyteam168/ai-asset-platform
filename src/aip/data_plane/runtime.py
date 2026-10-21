"""Tool Runtime（architecture.md 8）：单步安全执行。

认证 → Principal → 入参校验 → PDP → 幂等 → 限流 / 并发 / 熔断 → Connector（期限、重试） → classify →
Result Guard → tool_execution 与审计。重试预算由 Runtime 控制；写操作结果未知进入 unknown 并对账，不盲目重放。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import jsonschema
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from aip.connectors.base import Connector, ExecContext
from aip.connectors.credentials import CredentialManager, SecretStore
from aip.connectors.declarative import DeclarativeConnector, kb_connector
from aip.connectors.mock import MockConnector
from aip.connectors.upstream_mcp import UpstreamMcpConnector
from aip.data_plane.confirmation import (
    ConfirmationStore,
    MemoryConfirmationStore,
    args_digest,
    frozen_args,
    new_confirmation,
    principal_from_json,
)
from aip.data_plane.iam_cache import IamCache
from aip.data_plane.pdp import Decision, PolicyService
from aip.data_plane.principal import Principal
from aip.data_plane.quota import MemoryQuotaStore, QuotaItem, QuotaStore
from aip.data_plane.release_cache import ReleaseCache
from aip.libs.audit.spool import AuditSpool
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import ConnectionConfig, NetworkAllowlist, build_client
from aip.libs.httpclient.config import Resolver
from aip.libs.tokens.keys import canonical_json

log = logging.getLogger("aip.runtime")
RETRYABLE = {ErrorCode.DOWNSTREAM_UNAVAILABLE, ErrorCode.DOWNSTREAM_TIMEOUT}
BREAKER_CODES = RETRYABLE | {
    ErrorCode.TLS_UNTRUSTED,
    ErrorCode.TLS_PIN_MISMATCH,
    ErrorCode.TLS_HOSTNAME_MISMATCH,
    ErrorCode.TLS_EXPIRED,
    ErrorCode.TLS_FINGERPRINT_CHANGED,
}
REASON_TO_CODE = {
    "CLASSIFICATION": ErrorCode.CLASSIFICATION_DENIED,
    "POLICY_UNAVAILABLE": ErrorCode.POLICY_UNAVAILABLE,
    "NOT_FOUND": ErrorCode.NOT_FOUND,
    "UNAUTHENTICATED": ErrorCode.UNAUTHENTICATED,
    "USER_NOT_BOUND": ErrorCode.USER_NOT_BOUND,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------- 保护：令牌桶、熔断 ----------------


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.capacity, self.tokens, self.t = rate, float(burst), float(burst), time.monotonic()

    def take(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens < 1:
            raise PlatformError(ErrorCode.RATE_LIMITED, "超出该系统的调用速率")
        self.tokens -= 1


class CircuitBreaker:
    """连续失败达到阈值后打开；open_s 后半开放行一次，成功即关闭。"""

    def __init__(self, threshold: int = 5, open_s: float = 30) -> None:
        self.threshold, self.open_s = threshold, open_s
        self.failures, self.opened_at = 0, 0.0

    def check(self) -> None:
        if self.failures >= self.threshold and time.monotonic() - self.opened_at < self.open_s:
            raise PlatformError(ErrorCode.CIRCUIT_OPEN, f"下游熔断中，约 {self.open_s:.0f} 秒后重试")

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


@dataclass
class Instance:
    connector: Connector
    connection: ConnectionConfig | None
    semaphore: asyncio.Semaphore
    bucket: TokenBucket
    breaker: CircuitBreaker
    allowlist: NetworkAllowlist
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class ConnectorRegistry:
    """按发布快照中的 connectors 构造 Connector 实例；快照变化时重建。"""

    def __init__(
        self,
        releases: ReleaseCache,
        secrets: SecretStore,
        resolver: Resolver | None = None,
        sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.releases, self.secrets, self.resolver, self.sink = releases, secrets, resolver, sink
        self._built_for: int | None = None
        self._instances: dict[str, Instance] = {}

    def event(self, inst: Instance, kind: str, data: dict[str, Any]) -> None:
        """连接事件：进程内记录，并回传控制面（指纹记录、传输例外到期禁用等）。"""
        inst.events.append((kind, data))
        if self.sink is not None:
            self.sink(kind, data)

    def get(self, name: str) -> Instance:
        if self._built_for != self.releases.seq:
            self._build()
        inst = self._instances.get(name)
        if inst is None:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "Connector 实例未登记或已停用")
        return inst

    def _build(self) -> None:
        data = self.releases.data
        allow = NetworkAllowlist.parse(data.get("network_allowlist", []))
        old = self._instances
        new: dict[str, Instance] = {}
        for c in data.get("connectors", []):
            conn = ConnectionConfig.from_dict(c["connection"]) if c["connection"] else None
            prev_conn = old[c["name"]].connection if c["name"] in old else None
            if conn is not None and prev_conn is not None and not conn.last_seen_fingerprint:
                # 快照重建时保留进程内已记录的 insecure 指纹，避免把变化误判为首次连接
                conn.last_seen_fingerprint = prev_conn.last_seen_fingerprint
            creds = CredentialManager(
                c["config"].get("auth", {}), (c["connection"] or {}).get("credential_ref"), self.secrets
            )
            connector: Connector
            if c["kind"] == "mock":
                connector = MockConnector(c["name"], c["config"])
            elif c["kind"] == "kb":
                connector = kb_connector(c["name"], c["config"], creds)
            elif c["kind"] == "upstream_mcp":
                connector = UpstreamMcpConnector(c["name"], c["config"], creds)
            else:
                connector = DeclarativeConnector(c["name"], c["config"], creds)
            prev = old.get(c["name"])
            new[c["name"]] = Instance(
                connector=connector,
                connection=conn,
                semaphore=prev.semaphore if prev else asyncio.Semaphore(int(c["concurrency"])),
                bucket=prev.bucket if prev else TokenBucket(float(c["rate_per_s"]), int(c["burst"])),
                breaker=prev.breaker if prev else CircuitBreaker(),
                allowlist=allow,
            )
        self._instances, self._built_for = new, self.releases.seq


# ---------------- 执行记录 ----------------


class IdempotencyHit(Exception):
    def __init__(self, existing: dict[str, Any]) -> None:
        self.existing = existing


class ExecutionStore(Protocol):
    async def create(self, rec: dict[str, Any]) -> None: ...

    async def update(self, rec: dict[str, Any]) -> None: ...

    async def get(self, execution_id: str) -> dict[str, Any] | None: ...


class MemoryExecutionStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(self, rec: dict[str, Any]) -> None:
        if rec["idempotency_key"]:
            for r in self.rows.values():
                if (r["consumer_id"], r["capability_id"], r["idempotency_key"]) == (
                    rec["consumer_id"],
                    rec["capability_id"],
                    rec["idempotency_key"],
                ):
                    raise IdempotencyHit(r)
        self.rows[rec["id"]] = dict(rec)

    async def update(self, rec: dict[str, Any]) -> None:
        self.rows[rec["id"]] = dict(rec)

    async def get(self, execution_id: str) -> dict[str, Any] | None:
        return self.rows.get(execution_id)


class PgExecutionStore:
    """exec.tool_execution；唯一约束（consumer + capability + 幂等键）在数据库，Redis 锁只做优化（这里不需要）。"""

    COLS = (
        "id",
        "company_id",
        "capability_id",
        "version",
        "consumer_id",
        "end_user_id",
        "agent_profile_id",
        "idempotency_key",
        "args_sha256",
        "status",
        "result",
        "error_code",
        "retry_count",
        "reconcile_status",
        "transport",
        "tls_verified",
        "trace_id",
        "confirmation_id",
    )

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @staticmethod
    def _params(rec: dict[str, Any]) -> dict[str, Any]:
        p = {k: rec.get(k) for k in PgExecutionStore.COLS}
        p["result"] = json.dumps(rec.get("result"), ensure_ascii=False) if rec.get("result") is not None else None
        return p

    async def create(self, rec: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.company_ids', :c, true)"), {"c": rec["company_id"]})
            try:
                async with conn.begin_nested():
                    cols = ", ".join(self.COLS)
                    vals = ", ".join(f"CAST(:{c} AS jsonb)" if c == "result" else f":{c}" for c in self.COLS)
                    await conn.execute(
                        # 列名来自常量 COLS，值全部走绑定参数
                        text(f"INSERT INTO exec.tool_execution ({cols}) VALUES ({vals})"),  # noqa: S608
                        self._params(rec),
                    )
            except IntegrityError:
                row = (
                    (
                        await conn.execute(
                            text(
                                "SELECT * FROM exec.tool_execution WHERE consumer_id = :c AND capability_id = :cap "
                                "AND idempotency_key = :k"
                            ),
                            {"c": rec["consumer_id"], "cap": rec["capability_id"], "k": rec["idempotency_key"]},
                        )
                    )
                    .mappings()
                    .one()
                )
                raise IdempotencyHit({k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}) from None

    async def update(self, rec: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.company_ids', :c, true)"), {"c": rec["company_id"]})
            await conn.execute(
                text(
                    "UPDATE exec.tool_execution SET status = :status, result = CAST(:result AS jsonb), "
                    "error_code = :error_code, retry_count = :retry_count, reconcile_status = :reconcile_status, "
                    "updated_at = now() WHERE id = :id"
                ),
                self._params(rec),
            )

    async def get(self, execution_id: str) -> dict[str, Any] | None:
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.bypass_rls', 'on', true)"))
            row = (
                (await conn.execute(text("SELECT * FROM exec.tool_execution WHERE id = :id"), {"id": execution_id}))
                .mappings()
                .first()
            )
        return None if row is None else {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}


# ---------------- Result Guard ----------------


def _trim(data: Any, schema: dict[str, Any]) -> Any:
    if isinstance(data, list):
        item = schema.get("items", {}) if schema.get("type") == "array" else schema
        return [_trim(x, item) for x in data]
    props = schema.get("properties")
    if isinstance(data, dict) and isinstance(props, dict):
        return {k: _trim(v, props[k]) for k, v in data.items() if k in props}
    return data


def _mask(data: Any, fields: set[str]) -> Any:
    if isinstance(data, list):
        return [_mask(x, fields) for x in data]
    if isinstance(data, dict):
        return {k: ("***" if k in fields and v is not None else _mask(v, fields)) for k, v in data.items()}
    return data


def result_guard(data: Any, spec: dict[str, Any], principal: Principal) -> tuple[Any, bool]:
    """按 output_schema 裁剪字段、按密级脱敏 PII、按 output_limits 截断（architecture.md 8.6）。"""
    data = _trim(data, spec.get("output_schema") or {})
    pii = {p.split(".")[-1].replace("[*]", "") for p in spec.get("pii_fields", [])}
    if pii and principal.clearance < 3:
        data = _mask(data, pii)
    limits = spec.get("output_limits") or {}
    truncated = False
    if isinstance(data, list):
        max_items = int(limits.get("max_items", 200))
        if len(data) > max_items:
            data, truncated = data[:max_items], True
        max_bytes = int(limits.get("max_bytes", 65536))
        while data and len(canonical_json(data)) > max_bytes:
            data, truncated = data[: len(data) // 2], True
    return data, truncated


# ---------------- Runtime ----------------


def _result(
    rec: dict[str, Any],
    *,
    next_action: dict[str, Any] | None = None,
    next_cursor: str | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    err = None
    if rec.get("error_code"):
        code = ErrorCode(rec["error_code"])
        err = {"code": str(code), "message": rec.get("error_message", ""), "retryable": PlatformError(code).retryable}
    return {
        "execution_id": rec["id"],
        "status": rec["status"],
        "data": rec.get("result"),
        "page": {"next_cursor": next_cursor, "truncated": truncated},
        "error": err,
        "next_action": next_action,
        "trace_id": rec["trace_id"],
    }


def run_result(view: dict[str, Any]) -> dict[str, Any]:
    """Run 视图映射为统一执行结果：execution_id = run_id；未结束时 next_action=wait。"""
    status = view["status"]
    mapped = {"waiting": "running", "rejected": "cancelled"}.get(status, status)
    err = view.get("error") or None
    pending = view.get("pending_tasks") or []
    if mapped == "running":
        nxt: dict[str, Any] | None = {
            "type": "ask_human" if pending else "wait",
            "message": pending[0]["summary"]
            if pending
            else "Workflow 运行中，用 aip.get_run_status、tasks/get 或 GET /api/v1/runs/{id} 查询",
            "retry_after_s": 2,
        }
    else:
        nxt = None
    return {
        "execution_id": view["run_id"],
        "run_id": view["run_id"],
        "status": mapped,
        "data": view.get("result"),
        "page": {"next_cursor": None, "truncated": False},
        "error": {"code": err.get("code"), "message": err.get("message", ""), "retryable": False} if err else None,
        "next_action": nxt,
        "trace_id": view.get("trace_id") or "",
    }


def _confirm_action(rec: dict[str, Any]) -> dict[str, Any] | None:
    if rec.get("status") != "pending_confirm":
        return None
    return {"type": "confirm", "message": "等待确认", "confirmation_id": rec.get("confirmation_id")}


def _next_action(code: ErrorCode) -> dict[str, Any] | None:
    kind = {
        ErrorCode.INVALID_ARGUMENT: "fix_args",
        ErrorCode.PERMISSION_DENIED: "request_permission",
        ErrorCode.OUT_OF_SCOPE: "request_permission",
        ErrorCode.CLASSIFICATION_DENIED: "request_permission",
        ErrorCode.RATE_LIMITED: "wait",
        ErrorCode.CIRCUIT_OPEN: "wait",
        ErrorCode.DOWNSTREAM_UNAVAILABLE: "wait",
        ErrorCode.DOWNSTREAM_TIMEOUT: "wait",
        ErrorCode.OUTCOME_UNKNOWN: "reconcile",
        ErrorCode.DOWNSTREAM_AUTH_EXPIRED: "ask_human",
    }.get(code)
    return {"type": kind, "message": ""} if kind else None


class ToolRuntime:
    def __init__(
        self,
        *,
        iam: IamCache,
        releases: ReleaseCache,
        policy: PolicyService,
        registry: ConnectorRegistry,
        store: ExecutionStore,
        audit: AuditSpool,
        mode: str = "sandbox",
        confirmations: ConfirmationStore | None = None,
        quota: QuotaStore | None = None,
    ) -> None:
        self.iam, self.releases, self.policy, self.registry = iam, releases, policy, registry
        self.store, self.audit, self.mode = store, audit, mode
        self.confirmations: ConfirmationStore = confirmations or MemoryConfirmationStore()
        self.quota: QuotaStore = quota or MemoryQuotaStore()
        # 确认被执行或拒绝后通知 Workflow 引擎唤醒对应 Run（由引擎注入）
        self.on_confirmation_done: Callable[[str], Awaitable[None]] | None = None
        # 启动 Workflow 类能力（由引擎注入；没有引擎时 Workflow 不可调用）
        self.workflows: Any = None
        self.workflow_sync_wait_s = 0.0

    def _deny_code(self, d: Decision) -> ErrorCode:
        for r in d.reasons:
            if r.startswith("OUT_OF_SCOPE"):
                return ErrorCode.OUT_OF_SCOPE
            if r in REASON_TO_CODE:
                return REASON_TO_CODE[r]
        return ErrorCode.PERMISSION_DENIED

    async def invoke(
        self,
        principal: Principal,
        capability_id: str,
        args: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        exposure: str = "rest",
        channel: str = "stable",
        version: str | None = None,
        trace_id: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace

        from aip.libs.otel import current_trace_id_hex, get_tracer

        tracer = get_tracer("aip.runtime")
        span = tracer.start_span(
            "tool.invoke",
            kind=otel_trace.SpanKind.INTERNAL,
            attributes={"aip.capability_id": capability_id, "aip.exposure": exposure},
        )
        token = otel_context.attach(otel_trace.set_span_in_context(span))
        try:
            return await self._invoke_body(
                principal,
                capability_id,
                args,
                idempotency_key=idempotency_key,
                exposure=exposure,
                channel=channel,
                version=version,
                trace_id=trace_id or current_trace_id_hex(),
                run_id=run_id,
            )
        finally:
            span.end()
            otel_context.detach(token)

    async def _invoke_body(
        self,
        principal: Principal,
        capability_id: str,
        args: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        exposure: str = "rest",
        channel: str = "stable",
        version: str | None = None,
        trace_id: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        trace_id = trace_id or uuid.uuid4().hex
        rec: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "company_id": principal.consumer_company_id,
            "capability_id": capability_id,
            "version": version or "",
            "consumer_id": principal.consumer_id,
            "end_user_id": principal.user_id,
            "agent_profile_id": principal.agent_profile_id,
            "idempotency_key": idempotency_key,
            "args_sha256": hashlib.sha256(canonical_json(args)).hexdigest(),
            "status": "running",
            "result": None,
            "error_code": None,
            "retry_count": 0,
            "reconcile_status": None,
            "transport": None,
            "tls_verified": None,
            "trace_id": trace_id,
            "confirmation_id": None,
        }
        try:
            release = self.releases.resolve(capability_id, self.policy.env, channel=channel, version=version)
        except PlatformError as e:
            return await self._finish_without_record(rec, e)
        spec = release["spec"]
        rec["version"] = release["version"]
        errors = sorted(
            jsonschema.Draft202012Validator(spec.get("input_schema") or {}).iter_errors(args),
            key=lambda e: list(e.path),
        )
        if errors:
            return await self._finish_without_record(rec, PlatformError(ErrorCode.INVALID_ARGUMENT, errors[0].message))
        decision = await self.policy.decide(
            principal, capability_id, args, exposure=exposure, channel=channel, version=version
        )
        if decision.result == "deny":
            return await self._finish_without_record(
                rec, PlatformError(self._deny_code(decision), ",".join(decision.reasons))
            )
        if release.get("asset_type") == "workflow":
            return await self._start_workflow(rec, principal, release, args, idempotency_key, decision)
        if decision.result == "require_confirmation":
            return await self._require_confirmation(rec, principal, release, args, decision.kind or "approval", run_id)
        connector_name, op = spec["connector_ref"]["connector"], spec["connector_ref"]["operation"]
        try:
            inst = self.registry.get(connector_name)
        except PlatformError as e:
            return await self._finish_without_record(rec, e)
        write = inst.connector.is_write(op)
        rec["write"] = write
        if write and not idempotency_key:
            return await self._finish_without_record(
                rec, PlatformError(ErrorCode.INVALID_ARGUMENT, "写操作必须带幂等键")
            )
        if inst.connection:
            rec.update(transport=inst.connection.scheme, tls_verified=inst.connection.tls_verified)
        try:
            await self.store.create(rec)
        except IdempotencyHit as hit:
            prev = hit.existing
            if prev["args_sha256"] != rec["args_sha256"]:
                return _result(
                    {
                        **rec,
                        "status": "failed",
                        "error_code": str(ErrorCode.IDEMPOTENCY_CONFLICT),
                        "error_message": "同一幂等键对应不同参数",
                    }
                )
            idempotent = bool((spec.get("idempotency") or {}).get("idempotent"))
            if prev["status"] == "failed" and prev.get("reconcile_status") == "not_executed" and idempotent:
                # 对账确认下游未执行：按幂等策略用同一幂等键重试（architecture.md 8.3）
                retry = {**prev, "status": "running", "error_code": None, "reconcile_status": "retried"}
                await self.store.update(retry)
                return await self._execute(retry, release, inst, op, args, write, principal)
            return _result(
                prev, next_action=_next_action(ErrorCode.OUTCOME_UNKNOWN) if prev["status"] == "unknown" else None
            )
        if (denied := await self._take_quota(principal, release, write)) is not None:
            return await self._reject_quota(rec, denied)
        return await self._execute(rec, release, inst, op, args, write, principal)

    def _quota_items(self, principal: Principal, release: dict[str, Any], write: bool) -> list[QuotaItem]:
        consumer = self.iam.data.get("consumers", {}).get(principal.consumer_id) or {}
        company = principal.consumer_company_id
        cq = consumer.get("quota") or {}
        items: list[QuotaItem] = []
        if cq.get("daily_calls"):
            items.append((f"consumer:{principal.consumer_id}", company, int(cq["daily_calls"])))
        if write and cq.get("daily_writes"):
            items.append((f"consumer_write:{principal.consumer_id}", company, int(cq["daily_writes"])))
        for g in self.iam.data.get("grants", []):
            owner = g["grantee_type"] == "consumer" and g["grantee_id"] == principal.consumer_id
            profile = g["grantee_type"] == "agent_profile" and g["grantee_id"] == principal.agent_profile_id
            limit = (g.get("ceiling_quota") or {}).get("daily_calls")
            if g["asset_id"] == release["asset_id"] and (owner or profile) and limit and g.get("ceiling_id"):
                items.append((f"ceiling:{g['ceiling_id']}", g["company_id"], int(limit)))
                break
        return items

    async def _take_quota(self, principal: Principal, release: dict[str, Any], write: bool) -> str | None:
        items = self._quota_items(principal, release, write)
        return await self.quota.take(_now().date(), items) if items else None

    async def _reject_quota(self, rec: dict[str, Any], key: str) -> dict[str, Any]:
        scope = {"consumer": "消费方每日调用", "consumer_write": "消费方每日写操作", "ceiling": "授权上限每日调用"}
        rec.update(
            status="failed",
            error_code=str(ErrorCode.RATE_LIMITED),
            error_message=f"配额已用尽：{scope.get(key.split(':')[0], key)}（UTC 次日重置）",
        )
        await self.store.update(rec)
        await self.audit.append_async("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
        return _result(rec, next_action={"type": "wait", "message": rec["error_message"], "retry_after_s": None})

    async def _start_workflow(
        self,
        rec: dict[str, Any],
        principal: Principal,
        release: dict[str, Any],
        args: dict[str, Any],
        idempotency_key: str | None,
        decision: Decision,
    ) -> dict[str, Any]:
        """Workflow 类能力：启动 Run，短暂同步等待；未完成则返回 running + run_id（architecture.md 8.5）。"""
        if self.workflows is None:
            return await self._finish_without_record(
                rec, PlatformError(ErrorCode.PRECONDITION_FAILED, "本数据面未启用 Workflow 引擎")
            )
        if decision.result != "allow":
            return await self._finish_without_record(rec, PlatformError(ErrorCode.CONFIRMATION_REQUIRED, "需要确认"))
        try:
            view = await self.workflows.start(
                principal, release, args, trace_id=rec["trace_id"], idempotency_key=idempotency_key
            )
            view = await self.workflows.wait_settled(view["run_id"], self.workflow_sync_wait_s)
        except PlatformError as e:
            return await self._finish_without_record(rec, e)
        return run_result(view)

    async def _require_confirmation(
        self,
        rec: dict[str, Any],
        principal: Principal,
        release: dict[str, Any],
        args: dict[str, Any],
        kind: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        """冻结调用内容并创建 confirmation（architecture.md 4.5）；执行记录以 pending_confirm 落库，
        确认后在同一条记录上执行，消费方用 execution_id 轮询或用同一幂等键重试都能拿到结果。"""
        conf = new_confirmation(
            kind=kind,
            principal=principal,
            spec=release["spec"],
            capability_id=rec["capability_id"],
            version=release["version"],
            args=args,
            execution_id=rec["id"],
            trace_id=rec["trace_id"],
            run_id=run_id,
        )
        rec.update(
            status="pending_confirm", error_code=str(ErrorCode.CONFIRMATION_REQUIRED), confirmation_id=conf["id"]
        )
        try:
            await self.store.create(rec)
        except IdempotencyHit as hit:
            prev = hit.existing
            if prev["args_sha256"] != rec["args_sha256"]:
                return _result(
                    {
                        **rec,
                        "status": "failed",
                        "error_code": str(ErrorCode.IDEMPOTENCY_CONFLICT),
                        "error_message": "同一幂等键对应不同参数",
                    }
                )
            return _result(prev, next_action=_confirm_action(prev))
        await self.confirmations.create(conf)
        await self.audit.append_async(
            "confirmation.created",
            company_id=rec["company_id"],
            record={
                "confirmation_id": conf["id"],
                "kind": kind,
                "execution_id": rec["id"],
                "run_id": run_id,
                "capability_id": rec["capability_id"],
                "version": release["version"],
                "args_sha256": conf["args_sha256"],
                "end_user_id": rec["end_user_id"],
            },
        )
        return _result(rec, next_action={"type": "confirm", "message": conf["summary"], "confirmation_id": conf["id"]})

    async def execute_confirmation(
        self,
        cid: str,
        *,
        by: str,
        via: str,
        comment: str | None = None,
        expect_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """确认后执行冻结的调用：先核对参数（调用方声明的与冻结的一致、冻结内容未被篡改），
        再把 pending 条件更新为 confirmed——并发或重复确认只有一个会执行，其余拿到同一条执行记录。"""
        row = await self.confirmations.get(cid)
        if row is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "确认不存在")
        if expect_args is not None and args_digest(expect_args) != row["args_sha256"]:
            await self._audit_confirmation("confirmation.mismatch", row, by, via)
            raise PlatformError(ErrorCode.CONFIRMATION_MISMATCH, "确认的参数与冻结的调用内容不一致")
        try:
            args = frozen_args(row)
        except PlatformError:
            await self._audit_confirmation("confirmation.tampered", row, by, via)
            raise
        claimed = await self.confirmations.claim(cid, by=by, via=via, comment=comment)
        if claimed is None:
            row = await self.confirmations.get(cid) or row
            if row["status"] in ("confirmed", "executed"):
                prev = await self.store.get(str(row["execution_id"]))
                if prev is not None:
                    return _result(prev)
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"确认已处于 {row['status']} 状态，不能再确认")
        await self._audit_confirmation("confirmation.confirmed", claimed, by, via)
        try:
            return await self._run_confirmed(claimed, args)
        finally:
            await self.confirmations.finish(cid, "executed")
            await self._notify_run(claimed)

    async def _notify_run(self, row: dict[str, Any]) -> None:
        if row.get("run_id") and self.on_confirmation_done is not None:
            await self.on_confirmation_done(str(row["run_id"]))

    async def settle_stale(self, execution_id: str, older_than_s: float) -> str:
        """执行记录停在 running（执行中进程崩溃）且超过陈旧阈值：转为 unknown 等待对账，返回当前状态。"""
        rec = await self.store.get(execution_id)
        if rec is None:
            return "missing"
        if rec["status"] != "running":
            return str(rec["status"])
        updated = rec.get("updated_at") or rec.get("created_at")
        if isinstance(updated, dt.datetime) and (_now() - updated).total_seconds() < older_than_s:
            return "running"
        rec = dict(rec)
        rec.update(status="unknown", error_code=str(ErrorCode.OUTCOME_UNKNOWN), reconcile_status="pending")
        await self.store.update(rec)
        await self.audit.append_async("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
        return "unknown"

    async def _run_confirmed(self, row: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        principal = principal_from_json(row["principal"])
        found = await self.store.get(str(row["execution_id"]))
        if found is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "执行记录不存在")
        rec = dict(found)
        rec.update(status="running", error_code=None)
        try:
            release = self.releases.resolve(row["capability_id"], self.policy.env, version=row["version"])
        except PlatformError as e:
            rec.update(status="failed", error_code=str(e.code), error_message=e.detail)
            await self.store.update(rec)
            return _result(rec)
        # 确认期间授权可能已变：执行前重新判定（确认要求视为已满足）
        decision = await self.policy.decide(
            principal, row["capability_id"], args, exposure="internal", version=row["version"]
        )
        if decision.result == "deny":
            rec.update(
                status="failed", error_code=str(self._deny_code(decision)), error_message=",".join(decision.reasons)
            )
            await self.store.update(rec)
            return _result(rec)
        spec = release["spec"]
        inst = self.registry.get(spec["connector_ref"]["connector"])
        op = spec["connector_ref"]["operation"]
        # 幂等键 = confirmation_id：同一确认无论被触发几次，下游只执行一次
        rec["idempotency_key"] = str(row["id"])
        write = inst.connector.is_write(op)
        rec["write"] = write
        if (denied := await self._take_quota(principal, release, write)) is not None:
            return await self._reject_quota(rec, denied)
        return await self._execute(rec, release, inst, op, args, write, principal)

    async def reject_confirmation(self, cid: str, *, by: str, via: str, comment: str | None = None) -> dict[str, Any]:
        row = await self.confirmations.get(cid)
        if row is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "确认不存在")
        if not await self.confirmations.reject(cid, by=by, via=via, comment=comment):
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, f"确认已处于 {row['status']} 状态")
        await self._audit_confirmation("confirmation.rejected", row, by, via)
        await self._notify_run(row)
        found = await self.store.get(str(row["execution_id"]))
        if found is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "执行记录不存在")
        rec = dict(found)
        rec.update(status="cancelled", error_code=None, error_message="确认被拒绝")
        await self.store.update(rec)
        return _result(rec)

    async def _audit_confirmation(self, kind: str, row: dict[str, Any], by: str, via: str) -> None:
        await self.audit.append_async(
            kind,
            company_id=str(row["company_id"]),
            record={
                "confirmation_id": str(row["id"]),
                "kind": row["kind"],
                "by": by,
                "via": via,
                "execution_id": str(row["execution_id"]),
                "args_sha256": row["args_sha256"],
            },
        )

    async def _finish_without_record(self, rec: dict[str, Any], e: PlatformError) -> dict[str, Any]:
        rec.update(status="failed", error_code=str(e.code), error_message=e.detail)
        await self.audit.append_async("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
        return _result(rec, next_action=_next_action(e.code))

    def _audit(self, rec: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id",
            "capability_id",
            "version",
            "consumer_id",
            "end_user_id",
            "status",
            "error_code",
            "retry_count",
            "args_sha256",
            "transport",
            "tls_verified",
            "trace_id",
            "write",
        )
        return {k: rec.get(k) for k in keys}

    async def _execute(
        self,
        rec: dict[str, Any],
        release: dict[str, Any],
        inst: Instance,
        op: str,
        args: dict[str, Any],
        write: bool,
        principal: Principal,
    ) -> dict[str, Any]:
        spec = release["spec"]
        timeout = spec.get("timeout") or {}
        deadline = time.monotonic() + float(timeout.get("deadline_ms", 8000)) / 1000
        retry_cfg = spec.get("retry_class") or {}
        idempotent = bool((spec.get("idempotency") or {}).get("idempotent"))
        ctx = ExecContext(
            trace_id=rec["trace_id"],
            deadline=deadline,
            mode=self.mode,  # type: ignore[arg-type]
            idempotency_key=rec["idempotency_key"],
            connection_env=_env_of(self.releases, inst),
            external_codes=self.iam.data.get("external_codes", {}),
        )
        if inst.connection:
            conn = inst.connection
            probe_headers = {"X-Platform-Probe": "1"} if principal.extra.get("probe") else None
            ctx.http = lambda: build_client(
                conn,
                allowlist=inst.allowlist,
                resolver=self.registry.resolver,
                on_event=lambda k, d: self.registry.event(inst, k, d),
                trace_id=rec["trace_id"],
                extra_headers=probe_headers,
            )
        attempt = 0
        while True:
            attempt += 1
            try:
                inst.breaker.check()
                inst.bucket.take()
                async with inst.semaphore:
                    left = ctx.require_time()
                    res = await asyncio.wait_for(inst.connector.execute(op, args, ctx), timeout=left)
                inst.breaker.record(True)
                data, truncated = result_guard(res.data, spec, principal)
                rec.update(status="succeeded", result=data, retry_count=attempt - 1)
                await self.store.update(rec)
                await self.audit.append_async("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
                return _result(rec, next_cursor=res.next_cursor, truncated=truncated)
            except (TimeoutError, PlatformError) as e:
                err = e if isinstance(e, PlatformError) else PlatformError(ErrorCode.DOWNSTREAM_TIMEOUT, "下游超时")
                if err.code in BREAKER_CODES:
                    inst.breaker.record(False)
                if write and err.code == ErrorCode.DOWNSTREAM_TIMEOUT:
                    # 写操作结果未知：不重放，进入 unknown 等待对账（architecture.md 8.3）
                    rec.update(
                        status="unknown",
                        error_code=str(ErrorCode.OUTCOME_UNKNOWN),
                        reconcile_status="pending",
                        retry_count=attempt - 1,
                    )
                    await self.store.update(rec)
                    await self.audit.append_async(
                        "tool.execution", company_id=rec["company_id"], record=self._audit(rec)
                    )
                    return _result(rec, next_action=_next_action(ErrorCode.OUTCOME_UNKNOWN))
                rule = retry_cfg.get(
                    str(err.code), {"max_attempts": 2 if (not write or idempotent) else 0, "backoff_ms": 100}
                )
                can_retry = (
                    err.code in RETRYABLE
                    and (not write or idempotent)
                    and attempt <= int(rule.get("max_attempts", 0))
                    and ctx.remaining() > 0.05
                )
                if can_retry:
                    await asyncio.sleep(min(float(rule.get("backoff_ms", 100)) / 1000 * attempt, ctx.remaining()))
                    continue
                rec.update(status="failed", error_code=str(err.code), error_message=err.detail, retry_count=attempt - 1)
                await self.store.update(rec)
                await self.audit.append_async("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
                return _result(rec, next_action=_next_action(err.code))

    async def reconcile(self, execution_id: str) -> dict[str, Any]:
        """对写操作 unknown 的执行做只读对账：已成功补记结果；确认未执行标记 failed（可安全重试）；否则保持 unknown。"""
        rec = await self.store.get(execution_id)
        if rec is None:
            raise PlatformError(ErrorCode.NOT_FOUND, "执行记录不存在")
        if rec["status"] != "unknown":
            return _result(rec)
        release = self.releases.resolve(rec["capability_id"], self.policy.env, version=rec["version"])
        spec = release["spec"]
        inst = self.registry.get(spec["connector_ref"]["connector"])
        ctx = ExecContext(
            trace_id=rec["trace_id"],
            deadline=time.monotonic() + 5,
            mode=self.mode,  # type: ignore[arg-type]
            idempotency_key=rec["idempotency_key"],
            connection_env=_env_of(self.releases, inst),
        )
        if inst.connection:
            conn = inst.connection
            ctx.http = lambda: build_client(conn, allowlist=inst.allowlist, resolver=self.registry.resolver)
        outcome = await inst.connector.reconcile(spec["connector_ref"]["operation"], rec["idempotency_key"], ctx)
        if outcome.outcome == "succeeded":
            rec.update(status="succeeded", result=outcome.data, error_code=None, reconcile_status="confirmed")
        elif outcome.outcome == "not_executed":
            rec.update(status="failed", error_code=str(ErrorCode.DOWNSTREAM_TIMEOUT), reconcile_status="not_executed")
        else:
            rec.update(reconcile_status="escalated")
        await self.store.update(rec)
        await self.audit.append_async("tool.reconcile", company_id=rec["company_id"], record=self._audit(rec))
        return _result(rec)


def _env_of(releases: ReleaseCache, inst: Instance) -> str | None:
    for c in releases.data.get("connectors", []):
        if c["connection"] and inst.connection and c["connection"]["id"] == inst.connection.id:
            return str(c["connection"]["env"])
    return None
