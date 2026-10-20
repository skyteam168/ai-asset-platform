"""iam schema 的 SQLAlchemy Core 表定义（DDL 见 migrations/versions/*_0002_iam.py）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.types import UserDefinedType

md = MetaData(schema="iam")


class Ltree(UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "ltree"


def _id(name: str = "id", pk: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), primary_key=pk)


def _ts(name: str) -> Column[Any]:
    return Column(name, DateTime(timezone=True))


org_unit = Table(
    "org_unit",
    md,
    _id(pk=True),
    Column("type", Text),
    Column("code", Text),
    Column("name", Text),
    _id("parent_id"),
    Column("path", Ltree()),
    _id("company_id"),
    Column("default_clearance", SmallInteger),
    Column("timezone", Text),
    Column("locale", Text),
    Column("status", Text),
    _ts("valid_from"),
    _ts("valid_to"),
    _ts("updated_at"),
)
org_closure = Table(
    "org_closure",
    md,
    _id("ancestor_id", pk=True),
    _id("descendant_id", pk=True),
    Column("depth", SmallInteger),
    _id("company_id"),
)
org_external_code = Table(
    "org_external_code",
    md,
    _id("org_id"),
    Column("system_key", Text, primary_key=True),
    Column("external_code", Text, primary_key=True),
    _id("company_id"),
)
iam_user = Table(
    "iam_user",
    md,
    _id(pk=True),
    Column("login_name", Text),
    Column("display_name", Text),
    Column("employee_no", Text),
    Column("email", Text),
    _id("home_org_id"),
    _id("company_id"),
    Column("login_enabled", Boolean),
    Column("password_hash", Text),
    Column("totp_secret_enc", Text),
    Column("failed_logins", SmallInteger),
    _ts("locked_until"),
    Column("timezone", Text),
    Column("locale", Text),
    Column("status", Text),
    Column("clearance_override", SmallInteger),
    _ts("clearance_expires_at"),
    Column("is_break_glass", Boolean),
    _ts("last_seen_at"),
    _id("import_batch_id"),
    _ts("updated_at"),
)
user_org_membership = Table(
    "user_org_membership",
    md,
    _id("user_id", pk=True),
    _id("org_id", pk=True),
    Column("kind", Text),
    _id("company_id"),
)
user_role = Table(
    "user_role",
    md,
    _id(pk=True),
    _id("user_id"),
    Column("role", Text),
    _id("scope_org_id"),
    _id("company_id"),
    _id("granted_by"),
    _ts("expires_at"),
)
user_data_scope = Table(
    "user_data_scope",
    md,
    _id("user_id", pk=True),
    _id("org_id", pk=True),
    _id("company_id"),
    _id("granted_by"),
    _ts("expires_at"),
)
clearance_request = Table(
    "clearance_request",
    md,
    _id(pk=True),
    _id("user_id"),
    _id("company_id"),
    Column("requested_level", SmallInteger),
    Column("reason", Text),
    _ts("expires_at"),
    Column("status", Text),
    _id("requested_by"),
    _id("approver"),
    _ts("decided_at"),
    Column("comment", Text),
)
console_session = Table(
    "console_session",
    md,
    Column("token_hash", Text, primary_key=True),
    _id("user_id"),
    _id("company_id"),
    _ts("last_used_at"),
    _ts("expires_at"),
    _ts("absolute_expires_at"),
    Column("revoked", Boolean),
)
consumer = Table(
    "consumer",
    md,
    _id(pk=True),
    Column("client_id", Text),
    _id("company_id"),
    Column("clearance_cap", SmallInteger),
    Column("bind_policy", Text),
    Column("status", Text),
    Column("contact", Text),
    Column("quota", JSONB),
)
consumer_credential = Table(
    "consumer_credential",
    md,
    _id(pk=True),
    _id("consumer_id"),
    _id("company_id"),
    Column("kind", Text),
    Column("token_hash", Text),
    Column("ip_binding", ARRAY(Text)),
    _ts("expires_at"),
    Column("status", Text),
)
consumer_jwk = Table(
    "consumer_jwk",
    md,
    _id("consumer_id", pk=True),
    Column("kid", Text, primary_key=True),
    _id("company_id"),
    Column("jwk", JSONB),
    Column("status", Text),
    _ts("expires_at"),
)
user_binding = Table(
    "user_binding",
    md,
    _id(pk=True),
    _id("consumer_id"),
    Column("external_subject", Text),
    _id("user_id"),
    _id("company_id"),
    _id("created_by"),
    _ts("expires_at"),
)
agent_profile = Table(
    "agent_profile",
    md,
    _id(pk=True),
    _id("consumer_id"),
    _id("company_id"),
    Column("name", Text),
    Column("domain_ids", ARRAY(Text)),
)
domain_event = Table(
    "domain_event",
    md,
    Column("seq", BigInteger, primary_key=True, autoincrement=True),
    _id("company_id"),
    Column("kind", Text),
    Column("priority", Text),
    _id("subject_id"),
    _id("actor_id"),
    Column("payload", JSONB),
    _ts("occurred_at"),
)
iam_snapshot = Table(
    "iam_snapshot",
    md,
    Column("seq", BigInteger, primary_key=True),
    Column("event_seq", BigInteger),
    Column("sha256", Text),
    Column("signature", Text),
    Column("payload", JSONB),
    _ts("created_at"),
)
