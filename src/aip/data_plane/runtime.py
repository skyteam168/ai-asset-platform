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
from aip.data_plane.iam_cache import IamCache
from aip.data_plane.pdp import Decision, PolicyService
from aip.data_plane.principal import Principal
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

    def __init__(self, releases: ReleaseCache, secrets: SecretStore, resolver: Resolver | None = None) -> None:
        self.releases, self.secrets, self.resolver = releases, secrets, resolver
        self._built_for: int | None = None
        self._instances: dict[str, Instance] = {}

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
    ) -> None:
        self.iam, self.releases, self.policy, self.registry = iam, releases, policy, registry
        self.store, self.audit, self.mode = store, audit, mode

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
        }
        try:
            release = self.releases.resolve(capability_id, self.policy.env, channel=channel, version=version)
        except PlatformError as e:
            return self._finish_without_record(rec, e)
        spec = release["spec"]
        rec["version"] = release["version"]
        errors = sorted(
            jsonschema.Draft202012Validator(spec.get("input_schema") or {}).iter_errors(args),
            key=lambda e: list(e.path),
        )
        if errors:
            return self._finish_without_record(rec, PlatformError(ErrorCode.INVALID_ARGUMENT, errors[0].message))
        decision = await self.policy.decide(
            principal, capability_id, args, exposure=exposure, channel=channel, version=version
        )
        if decision.result == "deny":
            return self._finish_without_record(
                rec, PlatformError(self._deny_code(decision), ",".join(decision.reasons))
            )
        if decision.result == "require_confirmation":
            rec.update(status="pending_confirm", error_code=str(ErrorCode.CONFIRMATION_REQUIRED))
            return _result(
                rec, next_action={"type": "confirm", "message": f"需要{decision.kind}", "confirmation_id": None}
            )
        connector_name, op = spec["connector_ref"]["connector"], spec["connector_ref"]["operation"]
        try:
            inst = self.registry.get(connector_name)
        except PlatformError as e:
            return self._finish_without_record(rec, e)
        write = inst.connector.is_write(op)
        if write and not idempotency_key:
            return self._finish_without_record(rec, PlatformError(ErrorCode.INVALID_ARGUMENT, "写操作必须带幂等键"))
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
            return _result(
                prev, next_action=_next_action(ErrorCode.OUTCOME_UNKNOWN) if prev["status"] == "unknown" else None
            )
        return await self._execute(rec, release, inst, op, args, write, principal)

    def _finish_without_record(self, rec: dict[str, Any], e: PlatformError) -> dict[str, Any]:
        rec.update(status="failed", error_code=str(e.code), error_message=e.detail)
        self.audit.append("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
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
            ctx.http = lambda: build_client(
                conn,
                allowlist=inst.allowlist,
                resolver=self.registry.resolver,
                on_event=lambda k, d: inst.events.append((k, d)),
                trace_id=rec["trace_id"],
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
                self.audit.append("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
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
                    self.audit.append("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
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
                self.audit.append("tool.execution", company_id=rec["company_id"], record=self._audit(rec))
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
        self.audit.append("tool.reconcile", company_id=rec["company_id"], record=self._audit(rec))
        return _result(rec)


def _env_of(releases: ReleaseCache, inst: Instance) -> str | None:
    for c in releases.data.get("connectors", []):
        if c["connection"] and inst.connection and c["connection"]["id"] == inst.connection.id:
            return str(c["connection"]["env"])
    return None
