"""证书工具：指纹、SAN 主机名匹配、有效期（用于 pinned、insecure 与连接测试）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def fp_cert(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def fp_spki(der: bytes) -> str:
    pub = x509.load_der_x509_certificate(der).public_key()
    return hashlib.sha256(
        pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).hexdigest()


def hostname_matches(der: bytes, host: str) -> bool:
    """按 SAN 匹配（DNS 名支持单级通配符；IP 精确匹配）。不回退到 CN。"""
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    try:
        return ipaddress.ip_address(host) in san.get_values_for_type(x509.IPAddress)
    except ValueError:
        pass
    h = host.lower().rstrip(".")
    for name in san.get_values_for_type(x509.DNSName):
        n = name.lower().rstrip(".")
        if n == h:
            return True
        if n.startswith("*.") and h.endswith(n[1:]) and h.count(".") == n.count("."):
            return True
    return False


@dataclass(frozen=True)
class CertInfo:
    subject: str
    issuer: str
    not_before: dt.datetime
    not_after: dt.datetime
    sans: list[str]
    fingerprint_cert_sha256: str
    fingerprint_spki_sha256: str
    self_signed: bool

    def expired(self, now: dt.datetime) -> bool:
        return now > self.not_after or now < self.not_before


def describe(der: bytes) -> CertInfo:
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(v) for v in san.get_values_for_type(x509.DNSName)] + [
            str(v) for v in san.get_values_for_type(x509.IPAddress)
        ]
    except x509.ExtensionNotFound:
        sans = []
    return CertInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        sans=sans,
        fingerprint_cert_sha256=fp_cert(der),
        fingerprint_spki_sha256=fp_spki(der),
        self_signed=cert.issuer == cert.subject,
    )
