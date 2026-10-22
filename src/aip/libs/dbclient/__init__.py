"""数据库只读出站（architecture.md 9.5、ADR-0026）。HTTP 仍只用 libs.httpclient。"""

from aip.libs.dbclient.pool import DbEndpoint, open_connection, parse_db_url

__all__ = ["DbEndpoint", "open_connection", "parse_db_url"]
