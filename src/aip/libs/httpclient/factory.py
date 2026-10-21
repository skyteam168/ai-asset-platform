"""出站 HTTP 客户端工厂（architecture.md 9.6 实现约束、附录 B / C）。

全仓库唯一允许出现 ssl.CERT_NONE 的位置。要点：
1. pinned / insecure 的指纹与主机名检查在 TLS 握手之后、写入任何 HTTP 字节之前完成（httpcore 网络后端的 start_tls）。
2. http 与 insecure 连接只能到达内网白名单地址：按连接时解析出的 IP 判断，并直接连接已检查的 IP（防 DNS 重绑定）。
3. 只能访问登记的 base_url 主机；不跟随重定向，降级（https→http）或跨主机的重定向直接报 REDIRECT_BLOCKED。
4. 所有错误沿异常链映射为明确的平台错误码。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import socket
import ssl
import tempfile
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient.certs import describe, fp_cert, fp_spki, hostname_matches
from aip.libs.httpclient.config import DEFAULT_ALLOWLIST, ConnectionConfig, EventSink, NetworkAllowlist, Resolver

log = logging.getLogger("aip.outbound")
EXPIRY_WARNING = dt.timedelta(days=30)


class TransportPolicyError(httpcore.ConnectError):
    """在网络后端内抛出；httpx 会把它包装成 httpx.ConnectError，map_error 沿 __cause__ 取回错误码。"""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _noop(kind: str, data: dict[str, Any]) -> None:
    log.info("connection event %s %s", kind, data)


class _CheckedStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner: httpcore.AsyncNetworkStream, conn: ConnectionConfig, host: str, sink: EventSink) -> None:
        self._s, self._c, self._host, self._sink = inner, conn, host, sink
        self.peer_fingerprint: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._s.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._s.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._s.aclose()

    def get_extra_info(self, info: str) -> Any:
        if info == "aip_peer_fingerprint":
            return self.peer_fingerprint
        return self._s.get_extra_info(info)

    async def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._s.start_tls(ssl_context, server_hostname, timeout)
        obj = stream.get_extra_info("ssl_object")
        der = obj.getpeercert(binary_form=True) if obj is not None else None
        try:
            if not der:
                raise TransportPolicyError(ErrorCode.TLS_UNTRUSTED, "未取得服务端证书")
            self._check(der)
        except BaseException:
            await stream.aclose()
            raise
        checked = _CheckedStream(stream, self._c, self._host, self._sink)
        checked.peer_fingerprint = fp_cert(der)
        return checked

    def _check(self, der: bytes) -> None:
        c = self._c
        now = dt.datetime.now(dt.UTC)
        if c.tls_mode == "pinned":
            cert_fp, spki_fp = fp_cert(der), fp_spki(der)
            if not any(
                (p.kind == "cert" and p.sha256 == cert_fp) or (p.kind == "spki" and p.sha256 == spki_fp) for p in c.pins
            ):
                raise TransportPolicyError(ErrorCode.TLS_PIN_MISMATCH, f"cert={cert_fp} spki={spki_fp}")
            info = describe(der)
            if info.expired(now):
                if c.pin_enforce_expiry:
                    raise TransportPolicyError(ErrorCode.TLS_EXPIRED, f"not_after={info.not_after.isoformat()}")
                self._sink("tls.cert_expired", {"connection_id": c.id, "not_after": info.not_after.isoformat()})
            elif info.not_after - now < EXPIRY_WARNING:
                self._sink("tls.cert_expiring", {"connection_id": c.id, "not_after": info.not_after.isoformat()})
        elif c.tls_mode == "insecure":
            if c.tls_check_hostname and not hostname_matches(der, self._host):
                raise TransportPolicyError(ErrorCode.TLS_HOSTNAME_MISMATCH, self._host)
            seen = fp_cert(der)
            if c.last_seen_fingerprint and c.last_seen_fingerprint != seen:
                self._sink(
                    "tls.fingerprint_changed",
                    {"connection_id": c.id, "previous": c.last_seen_fingerprint, "current": seen},
                )
                if c.block_on_fingerprint_change:
                    raise TransportPolicyError(ErrorCode.TLS_FINGERPRINT_CHANGED, seen)
            if c.last_seen_fingerprint != seen:
                c.last_seen_fingerprint = seen
                self._sink("tls.fingerprint_seen", {"connection_id": c.id, "fingerprint": seen})


class _Backend(httpcore.AsyncNetworkBackend):
    def __init__(
        self, conn: ConnectionConfig, allowlist: NetworkAllowlist, resolver: Resolver, sink: EventSink
    ) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._c, self._allow, self._resolve, self._sink = conn, allowlist, resolver, sink

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            ips = await self._resolve(host, port)
        except OSError as e:
            raise TransportPolicyError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "域名解析失败") from e
        if not ips:
            raise TransportPolicyError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "域名解析无结果")
        if self._c.is_transport_exception and not all(self._allow.allows(ip) for ip in ips):
            raise TransportPolicyError(ErrorCode.TRANSPORT_NOT_ALLOWLISTED, f"{host} -> {ips}")
        # 连接已解析（并已检查）的 IP，避免检查与连接之间被重新解析；SNI 与主机名校验仍用原主机名
        stream = await self._inner.connect_tcp(ips[0], port, timeout, local_address, socket_options)
        return _CheckedStream(stream, self._c, host, self._sink)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, pool: httpcore.AsyncConnectionPool) -> None:
        self._t = httpx.AsyncHTTPTransport()
        self._t._pool = pool  # 复用 httpx 的请求 / 响应映射，只替换连接池

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._t.handle_async_request(request)

    async def aclose(self) -> None:
        await self._t.aclose()


def _ssl_context(conn: ConnectionConfig) -> ssl.SSLContext:
    if conn.tls_mode == "custom_ca":
        if not conn.ca_pem:
            raise PlatformError(ErrorCode.INVALID_ARGUMENT, "custom_ca 连接缺少 CA 证书")
        ctx = ssl.create_default_context(cadata=conn.ca_pem)  # 只信任上传的 CA，不叠加系统信任库
    else:
        ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if conn.tls_mode in ("pinned", "insecure"):
        ctx.check_hostname = False  # 必须先关主机名检查才能设 CERT_NONE；身份由指纹 / 手动 SAN 检查保证
        ctx.verify_mode = ssl.CERT_NONE
    if conn.client_cert_pem and conn.client_key_pem:
        # ssl 只能从文件加载客户端证书：写入仅当前用户可读的临时文件，加载后立即删除
        fd, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(conn.client_cert_pem + "\n" + conn.client_key_pem)
            ctx.load_cert_chain(path)
        finally:
            os.unlink(path)
    return ctx


def check_connection_usable(conn: ConnectionConfig, now: dt.datetime | None = None) -> None:
    """构造客户端前的静态检查：协议一致、连接未停用、传输例外有效。"""
    url = httpx.URL(conn.base_url)
    if url.scheme != conn.scheme or (conn.scheme == "https") != (conn.tls_mode is not None):
        raise PlatformError(ErrorCode.SCHEME_MISMATCH, "base_url 协议与连接配置不一致")
    if conn.status != "active":
        raise PlatformError(ErrorCode.TRANSPORT_EXCEPTION_EXPIRED, "连接已停用")
    if conn.is_transport_exception:
        now = now or dt.datetime.now(dt.UTC)
        if not conn.transport_exception_reason or conn.transport_exception_expires_at is None:
            raise PlatformError(ErrorCode.TRANSPORT_EXCEPTION_EXPIRED, "传输例外未经审批")
        if conn.transport_exception_expires_at <= now:
            raise PlatformError(ErrorCode.TRANSPORT_EXCEPTION_EXPIRED, "传输例外已过期，连接已禁用")


def build_client(
    conn: ConnectionConfig,
    *,
    allowlist: NetworkAllowlist = DEFAULT_ALLOWLIST,
    resolver: Resolver | None = None,
    on_event: EventSink | None = None,
    trace_id: str = "",
    extra_headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """按连接的 scheme 与 tls_mode 构造出站客户端。所有 Connector 必须经此出站。"""
    sink = on_event if on_event is not None else _noop
    try:
        check_connection_usable(conn)
    except PlatformError as e:
        if e.code == ErrorCode.TRANSPORT_EXCEPTION_EXPIRED and conn.status == "active":
            sink("transport_exception.expired", {"connection_id": conn.id})
        raise
    base = httpx.URL(conn.base_url)
    pool = httpcore.AsyncConnectionPool(
        ssl_context=_ssl_context(conn) if conn.scheme == "https" else None,
        network_backend=_Backend(conn, allowlist, resolver or system_resolver, sink),
    )

    async def guard_request(request: httpx.Request) -> None:
        if (request.url.scheme, request.url.host, request.url.port) != (base.scheme, base.host, base.port):
            raise PlatformError(ErrorCode.REDIRECT_BLOCKED, "只能访问连接登记的主机")

    async def guard_response(response: httpx.Response) -> None:
        stream = response.extensions.get("network_stream")
        log.info(
            "outbound",
            extra={
                "outbound": {
                    "connection_id": conn.id,
                    "transport": conn.scheme,
                    "tls_mode": conn.tls_mode,
                    "tls_verified": conn.tls_verified,
                    "method": response.request.method,
                    "path": response.request.url.path,
                    "status": response.status_code,
                    "trace_id": trace_id,
                    "peer_fingerprint": stream.get_extra_info("aip_peer_fingerprint") if stream else None,
                }
            },
        )
        if response.is_redirect:
            target = response.request.url.join(response.headers.get("location", ""))
            if target.scheme != base.scheme or target.host != base.host:
                raise PlatformError(ErrorCode.REDIRECT_BLOCKED, "下游重定向到其他协议或主机，已拒绝")

    from aip.libs.otel import inject_headers

    headers = inject_headers(dict(extra_headers or {}))
    # 无活跃 span 时回退到调用方传入的 trace_id（兼容旧测试）
    if not any(k.lower() == "traceparent" for k in headers) and len(trace_id) == 32:
        headers["traceparent"] = f"00-{trace_id}-{'0' * 15}1-01"
    return httpx.AsyncClient(
        base_url=conn.base_url,
        transport=_Transport(pool),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(conn.read_timeout_s, connect=conn.connect_timeout_s),
        headers=headers or None,
        event_hooks={"request": [guard_request], "response": [guard_response]},
    )


_SSL_VERIFY_CODES = {10: ErrorCode.TLS_EXPIRED, 62: ErrorCode.TLS_HOSTNAME_MISMATCH}


def map_error(exc: BaseException) -> PlatformError:
    """沿异常链找到真正原因，映射为平台错误码；不把下游主机、堆栈带给调用方。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, PlatformError):
            return cur
        if isinstance(cur, TransportPolicyError):
            return PlatformError(cur.code, "出站传输策略拒绝")
        if isinstance(cur, ssl.SSLCertVerificationError):
            return PlatformError(_SSL_VERIFY_CODES.get(cur.verify_code, ErrorCode.TLS_UNTRUSTED), "证书校验失败")
        if isinstance(cur, httpx.TimeoutException | httpcore.TimeoutException | TimeoutError):
            return PlatformError(ErrorCode.DOWNSTREAM_TIMEOUT, "下游超时")
        cur = cur.__cause__ or cur.__context__
    return PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "下游不可用")
