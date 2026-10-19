"""附录 B 测试夹具：现场生成证书，起 HTTP / HTTPS 测试服务器并统计收到的请求。"""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SANS_DNS = ("legacy.local",)
SANS_IP = ("127.0.0.1",)


@dataclass
class Cert:
    pem: str
    key_pem: str
    der: bytes
    key: ec.EllipticCurvePrivateKey

    def files(self) -> tuple[str, str]:
        d = Path(tempfile.mkdtemp())
        (d / "c.pem").write_text(self.pem, encoding="utf-8")
        (d / "k.pem").write_text(self.key_pem, encoding="utf-8")
        return str(d / "c.pem"), str(d / "k.pem")


def make_cert(
    *,
    cn: str = "legacy.local",
    key: ec.EllipticCurvePrivateKey | None = None,
    days: int = 365,
    expired: bool = False,
    issuer: Cert | None = None,
    ca: bool = False,
    client: bool = False,
    dns: tuple[str, ...] = SANS_DNS,
    ips: tuple[str, ...] = SANS_IP,
) -> Cert:
    key = key or ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    start, end = (
        (now - dt.timedelta(days=30), now - dt.timedelta(days=1))
        if expired
        else (now - dt.timedelta(days=1), now + dt.timedelta(days=days))
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    b = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(x509.load_pem_x509_certificate(issuer.pem.encode()).subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
    )
    if ca:
        b = b.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        alt = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        b = b.add_extension(x509.SubjectAlternativeName(alt), critical=False)
        b = b.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    cert = b.sign(issuer.key if issuer else key, hashes.SHA256())
    return Cert(
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        cert.public_bytes(serialization.Encoding.DER),
        key,
    )


@dataclass
class Server:
    """线程化测试服务器：hits 统计收到的 HTTP 请求，auth_headers 记录 Authorization 头。"""

    cert: Cert | None = None
    redirect_to: str | None = None
    client_ca: Cert | None = None
    hits: int = 0
    auth_headers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits += 1
                outer.auth_headers.append(self.headers.get("Authorization", ""))
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                    return
                body = b'{"code":0,"data":{"items":[{"woNo":"WO-1","planQty":10,"status":"open"}]}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        if self.cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*self.cert.files())
            if self.client_ca:
                ctx.verify_mode = ssl.CERT_REQUIRED
                ctx.load_verify_locations(cadata=self.client_ca.pem)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, host: str = "127.0.0.1") -> str:
        return f"{'https' if self.cert else 'http'}://{host}:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
