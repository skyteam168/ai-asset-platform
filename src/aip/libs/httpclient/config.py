"""出站连接配置（architecture.md 9.6、11.3 system_connection）。"""

from __future__ import annotations

import datetime as dt
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Scheme = Literal["https", "http"]
TlsMode = Literal["system_ca", "custom_ca", "pinned", "insecure"]
EventSink = Callable[[str, dict[str, Any]], None]
Resolver = Callable[[str, int], Awaitable[list[str]]]


@dataclass(frozen=True)
class Pin:
    kind: Literal["cert", "spki"]
    sha256: str


@dataclass
class ConnectionConfig:
    """一个下游连接。last_seen_fingerprint 是可变状态，由 insecure 模式首次连接写入并经事件回写控制面。"""

    id: str
    base_url: str
    scheme: Scheme
    tls_mode: TlsMode | None = None
    ca_pem: str | None = None
    pins: tuple[Pin, ...] = ()
    pin_enforce_expiry: bool = False
    tls_check_hostname: bool = True
    block_on_fingerprint_change: bool = False
    last_seen_fingerprint: str | None = None
    client_cert_pem: str | None = None
    client_key_pem: str | None = None
    transport_exception_reason: str | None = None
    transport_exception_expires_at: dt.datetime | None = None
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 10.0
    status: Literal["active", "pending_approval", "disabled"] = "active"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_transport_exception(self) -> bool:
        """http 与 https + insecure 属于传输例外：需审批、限内网、带到期日（architecture.md 9.6）。"""
        return self.scheme == "http" or self.tls_mode == "insecure"

    @property
    def tls_verified(self) -> bool:
        return self.scheme == "https" and self.tls_mode in ("system_ca", "custom_ca", "pinned")

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ConnectionConfig:
        exp = d.get("transport_exception_expires_at")
        return ConnectionConfig(
            id=str(d["id"]),
            base_url=d["base_url"],
            scheme=d["scheme"],
            tls_mode=d.get("tls_mode"),
            ca_pem=d.get("ca_pem"),
            pins=tuple(Pin(p["kind"], p["sha256"].lower()) for p in d.get("pins") or []),
            pin_enforce_expiry=bool(d.get("pin_enforce_expiry", False)),
            tls_check_hostname=bool(d.get("tls_check_hostname", True)),
            block_on_fingerprint_change=bool(d.get("block_on_fingerprint_change", False)),
            last_seen_fingerprint=d.get("tls_last_seen_fingerprint"),
            client_cert_pem=d.get("client_cert_pem"),
            client_key_pem=d.get("client_key_pem"),
            transport_exception_reason=d.get("transport_exception_reason"),
            transport_exception_expires_at=dt.datetime.fromisoformat(exp) if isinstance(exp, str) else exp,
            connect_timeout_s=float(d.get("connect_timeout_s", 3.0)),
            read_timeout_s=float(d.get("read_timeout_s", 10.0)),
            status=d.get("status", "active"),
        )


@dataclass(frozen=True)
class NetworkAllowlist:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @staticmethod
    def parse(cidrs: list[str]) -> NetworkAllowlist:
        return NetworkAllowlist(tuple(ipaddress.ip_network(c, strict=False) for c in cidrs))

    def allows(self, ip: str) -> bool:
        addr = ipaddress.ip_address(ip)
        return any(addr in n for n in self.networks)


# 默认内网网段（RFC 1918、回环、IPv6 ULA）；生产按 network_allowlist 表配置
DEFAULT_ALLOWLIST = NetworkAllowlist.parse(
    ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7"]
)
