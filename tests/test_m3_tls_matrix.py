"""M3 / P3：附录 B TLS 与传输测试矩阵。每个用例 ID 与 architecture.md 附录 B 一一对应。

三种必过的连接测试：T-HTTP-01（明文 HTTP）、T-PIN-01（自签 HTTPS + pinned）、T-INS-01（自签 HTTPS + insecure）。
所有请求都带测试用 Authorization 头；反向用例同时断言服务端收到的请求数为 0（凭据没有发出）。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.httpclient import ConnectionConfig, Pin, build_client, map_error, probe, trust_draft
from aip.libs.httpclient.certs import fp_cert, fp_spki
from tls_fixtures import Cert, Server, make_cert

SECRET = "Bearer test-downstream-secret"
FUTURE = dt.datetime.now(dt.UTC) + dt.timedelta(days=60)
EXC = {"transport_exception_reason": "旧系统只支持自签证书 / HTTP", "transport_exception_expires_at": FUTURE}


class Events(list[tuple[str, dict[str, Any]]]):
    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        self.append((kind, data))

    def kinds(self) -> list[str]:
        return [k for k, _ in self]


def resolver(mapping: dict[str, list[str]] | None = None) -> Any:
    calls: dict[str, int] = {}

    async def resolve(host: str, port: int) -> list[str]:
        calls[host] = calls.get(host, 0) + 1
        table = mapping or {}
        if host in table:
            value = table[host]
            return (
                [value[min(calls[host], len(value)) - 1]]
                if isinstance(value[0], str) and len(value) > 1 and host.startswith("rebind")
                else value
            )
        return ["127.0.0.1"] if host in ("127.0.0.1", "legacy.local", "localhost") else []

    return resolve


async def call(conn: ConnectionConfig, events: Events | None = None, res: Any = None, path: str = "/ping") -> Any:
    async with build_client(conn, on_event=events, resolver=res or resolver()) as c:
        try:
            r = await c.get(path, headers={"Authorization": SECRET})
            return r.status_code
        except Exception as e:
            return str(map_error(e).code)


@pytest.fixture(scope="module")
def certs() -> dict[str, Cert]:
    key = make_cert().key
    return {
        "self": make_cert(key=key),
        "reissued": make_cert(key=key),  # 同一密钥续签：SPKI 指纹不变
        "other": make_cert(),
        "expired": make_cert(expired=True),
    }


@pytest.fixture(scope="module")
def https(certs: dict[str, Cert]) -> Iterator[Server]:
    s = Server(certs["self"])
    yield s
    s.stop()


@pytest.fixture(scope="module")
def http() -> Iterator[Server]:
    s = Server()
    yield s
    s.stop()


def conn(url: str, **kw: Any) -> ConnectionConfig:
    scheme = "https" if url.startswith("https") else "http"
    return ConnectionConfig(id="c1", base_url=url, scheme=scheme, **kw)  # type: ignore[arg-type]


# ===================== 三种必过的连接测试 =====================


async def test_T_HTTP_01_plain_http(http: Server, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="aip.outbound")
    assert await call(conn(http.url(), **EXC)) == 200
    out = [r.outbound for r in caplog.records if hasattr(r, "outbound")][-1]  # type: ignore[attr-defined]
    assert out["transport"] == "http" and out["tls_verified"] is False
    report = await probe(conn(http.url(), **EXC), resolver=resolver())
    assert report["verdict"] == "pass" and "明文 HTTP" in report["detail"]


async def test_T_PIN_01_self_signed_pinned_cert(https: Server, certs: dict[str, Cert]) -> None:
    c = conn(https.url(), tls_mode="pinned", pins=(Pin("cert", fp_cert(certs["self"].der)),))
    assert await call(c) == 200
    assert c.tls_verified is True
    report = await probe(c, resolver=resolver())
    assert report["verdict"] == "pass"
    assert report["fingerprint_cert_sha256"] == fp_cert(certs["self"].der)
    assert report["fingerprint_spki_sha256"] == fp_spki(certs["self"].der)


async def test_T_INS_01_self_signed_insecure(https: Server, certs: dict[str, Cert]) -> None:
    ev = Events()
    c = conn(https.url(), tls_mode="insecure", tls_check_hostname=False, **EXC)
    assert await call(c, ev) == 200
    assert c.tls_verified is False
    assert c.last_seen_fingerprint == fp_cert(certs["self"].der)
    assert ev.kinds() == ["tls.fingerprint_seen"]
    assert (await probe(c, resolver=resolver()))["verdict"] == "pass"


# ===================== 正向扩展用例 =====================


async def test_T_PIN_02_spki_pin(https: Server, certs: dict[str, Cert]) -> None:
    assert await call(conn(https.url(), tls_mode="pinned", pins=(Pin("spki", fp_spki(certs["self"].der)),))) == 200


async def test_T_PIN_03_rotation_with_two_pins(certs: dict[str, Cert]) -> None:
    new = Server(certs["other"])
    try:
        pins = (Pin("cert", fp_cert(certs["self"].der)), Pin("cert", fp_cert(certs["other"].der)))
        assert await call(conn(new.url(), tls_mode="pinned", pins=pins)) == 200
    finally:
        new.stop()


async def test_T_PIN_03b_spki_pin_survives_reissue(certs: dict[str, Cert]) -> None:
    s = Server(certs["reissued"])
    try:
        assert await call(conn(s.url(), tls_mode="pinned", pins=(Pin("spki", fp_spki(certs["self"].der)),))) == 200
    finally:
        s.stop()


async def test_T_PIN_04_expired_pinned_cert_warns_only(certs: dict[str, Cert]) -> None:
    s = Server(certs["expired"])
    ev = Events()
    try:
        c = conn(s.url(), tls_mode="pinned", pins=(Pin("cert", fp_cert(certs["expired"].der)),))
        assert await call(c, ev) == 200
        assert "tls.cert_expired" in ev.kinds()
    finally:
        s.stop()


async def test_T_INS_02_insecure_hostname_check_by_ip(https: Server) -> None:
    assert await call(conn(https.url(), tls_mode="insecure", tls_check_hostname=True, **EXC)) == 200


async def test_T_CA_01_self_signed_as_custom_ca(https: Server, certs: dict[str, Cert]) -> None:
    c = conn(https.url("legacy.local"), tls_mode="custom_ca", ca_pem=certs["self"].pem)
    assert await call(c) == 200
    assert (await probe(c, resolver=resolver()))["verdict"] == "pass"


async def test_mtls_client_certificate() -> None:
    ca = make_cert(cn="corp-internal-ca", ca=True)
    server_cert = make_cert(issuer=ca)
    client = make_cert(cn="aip-gateway", issuer=ca, client=True)
    s = Server(server_cert, client_ca=ca)
    try:
        base = {"tls_mode": "custom_ca", "ca_pem": ca.pem}
        assert (
            await call(conn(s.url("legacy.local"), **base, client_cert_pem=client.pem, client_key_pem=client.key_pem))
            == 200
        )
        assert await call(conn(s.url("legacy.local"), **base)) in ("DOWNSTREAM_UNAVAILABLE", "TLS_UNTRUSTED")
    finally:
        s.stop()


# ===================== 反向用例 =====================


async def test_T_PIN_N1_wrong_pin_sends_nothing(https: Server) -> None:
    before = https.hits
    assert await call(conn(https.url(), tls_mode="pinned", pins=(Pin("cert", "00" * 32),))) == "TLS_PIN_MISMATCH"
    assert https.hits == before, "指纹错误时不得发出任何 HTTP 请求"
    report = await probe(conn(https.url(), tls_mode="pinned", pins=(Pin("cert", "00" * 32),)), resolver=resolver())
    assert report["error_code"] == "TLS_PIN_MISMATCH" and report["detail"] == "证书指纹与登记值不符"


async def test_T_PIN_N2_expired_with_enforcement(certs: dict[str, Cert]) -> None:
    s = Server(certs["expired"])
    try:
        c = conn(
            s.url(), tls_mode="pinned", pins=(Pin("cert", fp_cert(certs["expired"].der)),), pin_enforce_expiry=True
        )
        assert await call(c) == "TLS_EXPIRED"
        assert s.hits == 0
    finally:
        s.stop()


async def test_T_INS_N1_hostname_not_in_san(https: Server) -> None:
    before = https.hits
    c = conn(https.url("localhost"), tls_mode="insecure", tls_check_hostname=True, **EXC)
    assert await call(c) == "TLS_HOSTNAME_MISMATCH"
    assert https.hits == before
    assert (await probe(c, resolver=resolver()))["error_code"] == "TLS_HOSTNAME_MISMATCH"


async def test_T_INS_N2_fingerprint_change_alerts(https: Server) -> None:
    ev = Events()
    c = conn(https.url(), tls_mode="insecure", tls_check_hostname=False, last_seen_fingerprint="ab" * 32, **EXC)
    assert await call(c, ev) == 200
    assert ev.kinds()[0] == "tls.fingerprint_changed"
    assert ev[0][1]["previous"] == "ab" * 32


async def test_T_INS_N3_fingerprint_change_blocks(https: Server) -> None:
    before = https.hits
    c = conn(
        https.url(),
        tls_mode="insecure",
        tls_check_hostname=False,
        last_seen_fingerprint="ab" * 32,
        block_on_fingerprint_change=True,
        **EXC,
    )
    assert await call(c) == "TLS_FINGERPRINT_CHANGED"
    assert https.hits == before


async def test_T_SYS_N1_system_ca_rejects_self_signed(https: Server) -> None:
    c = conn(https.url(), tls_mode="system_ca")
    assert await call(c) == "TLS_UNTRUSTED"
    assert (await probe(c, resolver=resolver()))["error_code"] == "TLS_UNTRUSTED"


async def test_T_SYS_N1b_custom_ca_hostname_mismatch(https: Server, certs: dict[str, Cert]) -> None:
    c = conn(https.url("localhost"), tls_mode="custom_ca", ca_pem=certs["self"].pem)
    assert await call(c) == "TLS_HOSTNAME_MISMATCH"


async def test_T_GEN_N1_scheme_mismatch(http: Server, https: Server) -> None:
    for c in (
        ConnectionConfig(id="x", base_url=http.url(), scheme="https", tls_mode="system_ca"),
        ConnectionConfig(id="x", base_url=https.url(), scheme="http", **EXC),
        ConnectionConfig(id="x", base_url=https.url(), scheme="https", tls_mode=None),
    ):
        with pytest.raises(PlatformError) as e:
            build_client(c)
        assert e.value.code == ErrorCode.SCHEME_MISMATCH


async def test_T_GEN_N2_https_to_http_redirect_not_followed(http: Server, certs: dict[str, Cert]) -> None:
    s = Server(certs["self"], redirect_to=http.url() + "/ping")
    before = http.hits
    try:
        c = conn(s.url(), tls_mode="pinned", pins=(Pin("cert", fp_cert(certs["self"].der)),))
        assert await call(c) == "REDIRECT_BLOCKED"
        assert http.hits == before
    finally:
        s.stop()


async def test_absolute_url_to_other_host_blocked(https: Server, http: Server, certs: dict[str, Cert]) -> None:
    c = conn(https.url(), tls_mode="pinned", pins=(Pin("cert", fp_cert(certs["self"].der)),))
    before = http.hits
    async with build_client(c, resolver=resolver()) as client:
        with pytest.raises(PlatformError) as e:
            await client.get(http.url() + "/ping", headers={"Authorization": SECRET})
    assert e.value.code == ErrorCode.REDIRECT_BLOCKED and http.hits == before


async def test_T_GEN_N3_public_address_rejected(http: Server) -> None:
    c = conn(f"http://mes.example.com:{http.port}", **EXC)
    assert await call(c, res=resolver({"mes.example.com": ["8.8.8.8"]})) == "TRANSPORT_NOT_ALLOWLISTED"
    report = await probe(c, resolver=resolver({"mes.example.com": ["8.8.8.8"]}))
    assert report["allowlisted"] is False and report["error_code"] == "TRANSPORT_NOT_ALLOWLISTED"


async def test_T_GEN_N4_dns_rebinding_rejected(http: Server) -> None:
    """首次解析为内网、第二次解析为公网：第二次新建连接被拒（每次新建连接都按解析结果检查）。"""
    res = resolver({"rebind.local": ["127.0.0.1", "8.8.8.8"]})
    c = conn(f"http://rebind.local:{http.port}", **EXC)
    assert await call(c, res=res) == 200
    assert await call(c, res=res) == "TRANSPORT_NOT_ALLOWLISTED"


async def test_T_GEN_N5_expired_exception_disables_connection(http: Server) -> None:
    ev = Events()
    c = conn(
        http.url(),
        transport_exception_reason="x",
        transport_exception_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
    )
    with pytest.raises(PlatformError) as e:
        build_client(c, on_event=ev)
    assert e.value.code == ErrorCode.TRANSPORT_EXCEPTION_EXPIRED
    assert ev.kinds() == ["transport_exception.expired"]
    unapproved = conn(http.url())
    with pytest.raises(PlatformError, match="未经审批"):
        build_client(unapproved)
    assert (await probe(c, resolver=resolver()))["error_code"] == "TRANSPORT_EXCEPTION_EXPIRED"


async def test_T_LOG_01_outbound_log_fields_without_secrets(
    https: Server, certs: dict[str, Cert], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    c = conn(https.url(), tls_mode="pinned", pins=(Pin("cert", fp_cert(certs["self"].der)),))
    async with build_client(c, resolver=resolver(), trace_id="4bf92f3577b34da6a3ce929d0e0e4736") as client:
        await client.get("/ping", headers={"Authorization": SECRET})
    out = [r.outbound for r in caplog.records if hasattr(r, "outbound")][-1]  # type: ignore[attr-defined]
    assert {"transport", "tls_mode", "tls_verified", "peer_fingerprint", "trace_id"} <= set(out)
    assert out["peer_fingerprint"] == fp_cert(certs["self"].der) and out["tls_verified"] is True
    assert out["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert "test-downstream-secret" not in caplog.text
    assert https.auth_headers[-1] == SECRET  # 正向用例确实携带了凭据


async def test_T_ERR_01_errors_map_to_tls_codes(https: Server) -> None:
    """TLS 失败不能被笼统映射成 DOWNSTREAM_UNAVAILABLE。"""
    codes = {
        await call(conn(https.url(), tls_mode="pinned", pins=(Pin("cert", "00" * 32),))),
        await call(conn(https.url(), tls_mode="system_ca")),
        await call(conn(https.url("localhost"), tls_mode="insecure", tls_check_hostname=True, **EXC)),
    }
    assert codes == {"TLS_PIN_MISMATCH", "TLS_UNTRUSTED", "TLS_HOSTNAME_MISMATCH"}
    assert await call(conn("http://127.0.0.1:9", **EXC)) == "DOWNSTREAM_UNAVAILABLE"


async def test_trust_draft_from_probe_produces_working_pins(https: Server) -> None:
    report = await probe(conn(https.url(), tls_mode="system_ca"), resolver=resolver())
    assert report["verdict"] == "fail" and report["chain"][0]["self_signed"] is True
    draft = trust_draft(report, "pinned")
    pins = tuple(Pin(p["kind"], p["sha256"]) for p in draft["pins"])
    assert await call(conn(https.url(), tls_mode="pinned", pins=pins)) == 200
