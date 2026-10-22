"""数据库只读 Connector（architecture.md 9.5、ADR-0026）。"""

from __future__ import annotations

import re
from typing import Any

from aip.connectors.base import ConnectorResult, ExecContext, ReconcileResult
from aip.connectors.credentials import SecretStore
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.dbclient.pool import open_connection

_SELECT_OK = re.compile(r"^\s*(WITH\b[\s\S]+\)\s*)?SELECT\b", re.IGNORECASE)
_MULTI = re.compile(r";\s*\S")
_NAMED = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def assert_readonly_sql(statement: str) -> str:
    text = statement.strip().rstrip(";").strip()
    if not text:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "SQL 模板为空")
    if _MULTI.search(statement):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "禁止多语句 SQL")
    if not _SELECT_OK.match(text):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "只读模板仅允许 SELECT / WITH…SELECT")
    return text


def bind_named(
    statement: str, params: dict[str, Any], *, style: str = "postgres"
) -> tuple[str, list[Any]]:
    """`:name` → `$n`（postgres）或 `%s`（mysql）；缺少参数则失败。"""
    order: list[str] = []
    mysql_args: list[Any] = []

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"缺少参数 {name}")
        if style == "mysql":
            mysql_args.append(params[name])
            return "%s"
        if name not in order:
            order.append(name)
        return f"${order.index(name) + 1}"

    sql = _NAMED.sub(repl, statement)
    if style == "mysql":
        return sql, mysql_args
    return sql, [params[n] for n in order]


class SqlReadonlyConnector:
    def __init__(self, name: str, config: dict[str, Any], secrets: SecretStore) -> None:
        self.name = name
        self.config = config
        self.secrets = secrets
        self._templates: dict[str, dict[str, Any]] = {
            str(k): v for k, v in (config.get("templates") or {}).items() if isinstance(v, dict)
        }

    def capabilities(self) -> list[str]:
        return sorted(self._templates)

    def is_write(self, op: str) -> bool:
        return False

    async def health(self, ctx: ExecContext) -> bool:
        return True

    async def reconcile(self, op: str, idempotency_key: str, ctx: ExecContext) -> ReconcileResult:
        return ReconcileResult(outcome="not_executed")

    async def execute(self, op: str, params: dict[str, Any], ctx: ExecContext) -> ConnectorResult:
        tpl = self._templates.get(op)
        if tpl is None:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"未知 SQL 模板 {op}")
        statement = assert_readonly_sql(str(tpl["statement"]))
        max_rows = min(int(tpl.get("max_rows") or 200), 1000)
        timeout_ms = int(tpl.get("statement_timeout_ms") or 5000)
        whitelist = [str(x) for x in (tpl.get("output_field_whitelist") or [])]
        # 强制 LIMIT：若语句无 LIMIT 则追加
        if not re.search(r"\bLIMIT\b", statement, re.IGNORECASE):
            statement = f"{statement} LIMIT {max_rows}"
        conn_meta = self.config.get("connection") or {}
        scheme = str(conn_meta.get("scheme") or tpl.get("engine") or "postgres")
        style = "mysql" if scheme == "mysql" else "postgres"
        sql, args = bind_named(statement, params, style=style)
        ref = conn_meta.get("credential_ref")
        if not ref:
            raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "数据库连接未配置 credential_ref")
        secret = await self.secrets.get(str(ref))
        user = secret.get("username") or secret.get("user")
        password = secret.get("password") or ""
        if not user:
            raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "凭据缺少 username")
        raw = None
        try:
            raw = await open_connection(
                scheme=scheme,
                base_url=str(conn_meta["base_url"]),
                tls_mode=conn_meta.get("tls_mode"),
                ca_pem=conn_meta.get("ca_pem"),
                username=user,
                password=password,
                timeout_s=float(conn_meta.get("connect_timeout_s") or 3),
            )
            fetch = _fetch_mysql if style == "mysql" else _fetch_postgres
            out = await fetch(
                raw, sql, args, max_rows=max_rows, timeout_ms=timeout_ms, whitelist=whitelist
            )
            return ConnectorResult(data=out)
        except PlatformError:
            raise
        except Exception as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"数据库查询失败：{type(e).__name__}") from e
        finally:
            if raw is not None:
                await raw.close()


async def _fetch_postgres(
    raw: Any,
    sql: str,
    args: list[Any],
    *,
    max_rows: int,
    timeout_ms: int,
    whitelist: list[str],
) -> list[dict[str, Any]]:
    await raw.execute(f"SET statement_timeout = {int(timeout_ms)}")
    await raw.execute("SET default_transaction_read_only = on")
    rows = await raw.fetch(sql, *args)
    out: list[dict[str, Any]] = []
    for r in rows[:max_rows]:
        d = dict(r)
        if whitelist:
            d = {k: d[k] for k in whitelist if k in d}
        out.append(d)
    return out


async def _fetch_mysql(
    raw: Any,
    sql: str,
    args: list[Any],
    *,
    max_rows: int,
    timeout_ms: int,
    whitelist: list[str],
) -> list[dict[str, Any]]:
    async with raw.cursor() as cur:
        # 会话级只读 + 语句超时（毫秒，MySQL 5.7.8+）
        await cur.execute("SET SESSION TRANSACTION READ ONLY")
        await cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}")
        await cur.execute("START TRANSACTION READ ONLY")
        try:
            await cur.execute(sql, args)
            cols = [d[0] for d in (cur.description or [])]
            rows = await cur.fetchmany(max_rows)
        finally:
            await cur.execute("ROLLBACK")
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(zip(cols, r, strict=False))
        if whitelist:
            d = {k: d[k] for k in whitelist if k in d}
        out.append(d)
    return out
