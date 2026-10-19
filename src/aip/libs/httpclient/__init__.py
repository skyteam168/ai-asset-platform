"""出站 HTTP 客户端工厂（architecture.md 9.6）。

全仓库唯一允许出现 ssl.CERT_NONE 的位置。Connector 只能通过 build_client 出站。
"""

from aip.libs.httpclient.config import DEFAULT_ALLOWLIST, ConnectionConfig, NetworkAllowlist, Pin
from aip.libs.httpclient.factory import build_client, check_connection_usable, map_error
from aip.libs.httpclient.probe import probe, trust_draft

__all__ = [
    "DEFAULT_ALLOWLIST",
    "ConnectionConfig",
    "NetworkAllowlist",
    "Pin",
    "build_client",
    "check_connection_usable",
    "map_error",
    "probe",
    "trust_draft",
]
