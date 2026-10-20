"""连接测试（architecture.md 9.6“连接测试”）：真实握手，不携带任何凭据。

返回解析出的 IP 与白名单判定、TLS 版本、证书、两种指纹、SAN 匹配，以及按当前 tls_mode 的判定与明确失败原因。
说明：Python 3.12 的 ssl 只能取到服务端叶子证书；完整证书链展示需要 3.13 的 get_unverified_chain（升级后补齐）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
from typing import Any

import httpx

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient.certs import describe, hostname_matches
from aip.libs.httpclient.config import DEFAULT_ALLOWLIST, ConnectionConfig, NetworkAllowlist, Resolver
from aip.libs.httpclient.factory import _ssl_context, check_connection_usable, system_resolver

_REASON = {
    ErrorCode.TLS_UNTRUSTED: "证书不受信任",
    ErrorCode.TLS_EXPIRED: "证书已过期",
    ErrorCode.TLS_HOSTNAME_MISMATCH: "主机名与证书不符",
    ErrorCode.TLS_PIN_MISMATCH: "证书指纹与登记值不符",
    ErrorCode.TRANSPORT_NOT_ALLOWLISTED: "目标地址不在内网白名单",
    ErrorCode.TRANSPORT_EXCEPTION_EXPIRED: "传输例外未审批或已过期",
    ErrorCode.SCHEME_MISMATCH: "base_url 协议与连接配置不一致",
    ErrorCode.DOWNSTREAM_UNAVAILABLE: "无法建立连接",
    ErrorCode.DOWNSTREAM_TIMEOUT: "连接超时",
}


def _result(conn: ConnectionConfig, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connection_id": conn.id,
        "scheme": conn.scheme,
        "tls_mode": conn.tls_mode,
        "resolved_ip": None,
        "allowlisted": None,
        "tls_version": None,
        "chain": [],
        "fingerprint_cert_sha256": None,
        "fingerprint_spki_sha256": None,
        "hostname_match": None,
        "verdict": "fail",
        "error_code": None,
        "detail": None,
    }
    base.update(kw)
    if base["error_code"] and not base["detail"]:
        base["detail"] = _REASON.get(ErrorCode(base["error_code"]), base["error_code"])
    return base


async def _handshake(ip: str, port: int, host: str, ctx: ssl.SSLContext, limit_s: float) -> tuple[bytes, str]:
    _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port, ssl=ctx, server_hostname=host), limit_s)
    try:
        obj = writer.get_extra_info("ssl_object")
        return obj.getpeercert(binary_form=True), obj.version()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ssl.SSLError, OSError):
            pass


async def probe(
    conn: ConnectionConfig,
    *,
    allowlist: NetworkAllowlist = DEFAULT_ALLOWLIST,
    resolver: Resolver | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    try:
        check_connection_usable(conn, now)
    except PlatformError as e:
        return _result(conn, error_code=str(e.code))
    url = httpx.URL(conn.base_url)
    host, port = url.host, url.port or (443 if conn.scheme == "https" else 80)
    try:
        ips = await (resolver or system_resolver)(host, port)
    except OSError:
        return _result(conn, error_code="DOWNSTREAM_UNAVAILABLE", detail="域名解析失败")
    allowed = bool(ips) and all(allowlist.allows(ip) for ip in ips)
    common = {"resolved_ip": ips[0] if ips else None, "allowlisted": allowed}
    if conn.is_transport_exception and not allowed:
        return _result(conn, **common, error_code="TRANSPORT_NOT_ALLOWLISTED")
    timeout = conn.connect_timeout_s
    if conn.scheme == "http":
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ips[0], port), timeout)
            w.close()
        except (OSError, TimeoutError):
            return _result(conn, **common, error_code="DOWNSTREAM_UNAVAILABLE")
        return _result(conn, **common, verdict="pass", detail="明文 HTTP 可达（传输例外：须经数据 Owner 审批生效）")

    # 先用不校验的握手取证书（仅用于展示与比对），再按配置模式判定
    raw = ssl.create_default_context()
    raw.check_hostname = False
    raw.verify_mode = ssl.CERT_NONE
    try:
        der, version = await _handshake(ips[0], port, host, raw, timeout)
    except (OSError, TimeoutError, ssl.SSLError):
        return _result(conn, **common, error_code="DOWNSTREAM_UNAVAILABLE")
    info = describe(der)
    shown = {
        **common,
        "tls_version": version,
        "fingerprint_cert_sha256": info.fingerprint_cert_sha256,
        "fingerprint_spki_sha256": info.fingerprint_spki_sha256,
        "hostname_match": hostname_matches(der, host),
        "chain": [
            {
                "subject": info.subject,
                "issuer": info.issuer,
                "not_before": info.not_before.isoformat(),
                "not_after": info.not_after.isoformat(),
                "self_signed": info.self_signed,
                "sans": info.sans,
            }
        ],
    }
    mode = conn.tls_mode
    if mode == "pinned":
        ok = any(
            (p.kind == "cert" and p.sha256 == info.fingerprint_cert_sha256)
            or (p.kind == "spki" and p.sha256 == info.fingerprint_spki_sha256)
            for p in conn.pins
        )
        if not ok:
            return _result(conn, **shown, error_code="TLS_PIN_MISMATCH")
        if info.expired(now) and conn.pin_enforce_expiry:
            return _result(conn, **shown, error_code="TLS_EXPIRED")
        return _result(
            conn, **shown, verdict="pass", detail="指纹匹配" + ("（证书已过期，仅告警）" if info.expired(now) else "")
        )
    if mode == "insecure":
        if conn.tls_check_hostname and not shown["hostname_match"]:
            return _result(conn, **shown, error_code="TLS_HOSTNAME_MISMATCH")
        return _result(
            conn, **shown, verdict="pass", detail="握手成功但未校验证书（传输例外：须经数据 Owner 审批生效）"
        )
    try:
        await _handshake(ips[0], port, host, _ssl_context(conn), timeout)
    except ssl.SSLCertVerificationError as e:
        code = {10: "TLS_EXPIRED", 62: "TLS_HOSTNAME_MISMATCH"}.get(e.verify_code, "TLS_UNTRUSTED")
        return _result(conn, **shown, error_code=code)
    except (OSError, TimeoutError, ssl.SSLError):
        return _result(conn, **shown, error_code="DOWNSTREAM_UNAVAILABLE")
    return _result(conn, **shown, verdict="pass", detail="证书链校验通过")


def trust_draft(result: dict[str, Any], kind: str) -> dict[str, Any]:
    """“固定此证书 / 信任此 CA”：根据连接测试结果生成配置草稿，经审批后生效。"""
    if kind == "pinned":
        if not result.get("fingerprint_spki_sha256"):
            raise PlatformError(ErrorCode.PRECONDITION_FAILED, "连接测试未取得证书")
        return {
            "tls_mode": "pinned",
            "pins": [
                {"kind": "spki", "sha256": result["fingerprint_spki_sha256"]},
                {"kind": "cert", "sha256": result["fingerprint_cert_sha256"]},
            ],
        }
    raise PlatformError(ErrorCode.INVALID_ARGUMENT, "custom_ca 草稿需要上传 CA 证书")
