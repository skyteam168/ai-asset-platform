"""按 system_connection 构造短生命周期只读连接（不用 HTTP 工厂）。"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import asyncpg

from aip.libs.common.errors import ErrorCode, PlatformError

Engine = Literal["postgres", "mysql"]


@dataclass(frozen=True)
class DbEndpoint:
    engine: Engine
    host: str
    port: int
    database: str


def parse_db_url(base_url: str, scheme: str) -> DbEndpoint:
    if scheme not in ("postgres", "mysql"):
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, f"非数据库 scheme：{scheme}")
    u = urlparse(base_url)
    if u.scheme != scheme:
        raise PlatformError(ErrorCode.SCHEME_MISMATCH, "base_url 协议与连接配置不一致")
    if not u.hostname:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "数据库连接缺少主机")
    db = unquote((u.path or "/").lstrip("/"))
    if not db:
        raise PlatformError(ErrorCode.INVALID_ARGUMENT, "数据库连接缺少库名")
    default_port = 5432 if scheme == "postgres" else 3306
    return DbEndpoint(
        engine="postgres" if scheme == "postgres" else "mysql",
        host=u.hostname,
        port=int(u.port or default_port),
        database=db,
    )


def _ssl_ctx(tls_mode: str | None, ca_pem: str | None) -> ssl.SSLContext | bool:
    if tls_mode is None or tls_mode == "insecure":
        # asyncpg：True=默认校验；False=不校验。insecure → 不校验。
        return False
    ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    return ctx


async def open_connection(
    *,
    scheme: str,
    base_url: str,
    tls_mode: str | None,
    ca_pem: str | None,
    username: str,
    password: str,
    timeout_s: float = 3.0,
) -> Any:
    """返回引擎原生连接；调用方负责关闭。"""
    ep = parse_db_url(base_url, scheme)
    if ep.engine == "postgres":
        return await asyncpg.connect(
            host=ep.host,
            port=ep.port,
            user=username,
            password=password,
            database=ep.database,
            timeout=timeout_s,
            ssl=_ssl_ctx(tls_mode, ca_pem),
        )
    # mysql：H39f（optional-dependencies mysql）
    try:
        import asyncmy
    except ImportError as e:
        raise PlatformError(ErrorCode.INTERNAL, "未安装 asyncmy，无法连接 MySQL") from e
    return await asyncmy.connect(
        host=ep.host,
        port=ep.port,
        user=username,
        password=password,
        db=ep.database,
        connect_timeout=int(timeout_s),
        ssl=_ssl_ctx(tls_mode, ca_pem) if tls_mode and tls_mode != "insecure" else None,
    )
