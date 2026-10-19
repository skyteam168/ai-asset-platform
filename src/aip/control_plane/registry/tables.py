"""registry 与 grants schema 的 Core 表定义（DDL 见 migrations/versions/*_0003_*.py）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ARRAY, BigInteger, Column, DateTime, MetaData, SmallInteger, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

md = MetaData()


def _id(name: str = "id", pk: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), primary_key=pk)


def _ts(name: str) -> Column[Any]:
    return Column(name, DateTime(timezone=True))


asset = Table(
    "asset",
    md,
    _id(pk=True),
    Column("type", Text),
    Column("name", Text),
    _id("owner_org_id"),
    _id("owner_user_id"),
    _id("company_id"),
    Column("classification", SmallInteger),
    _ts("created_at"),
    schema="registry",
)
asset_version = Table(
    "asset_version",
    md,
    _id(pk=True),
    _id("asset_id"),
    _id("company_id"),
    Column("semver", Text),
    Column("content_hash", Text),
    Column("spec", JSONB),
    Column("dep_lock", JSONB),
    Column("state", Text),
    Column("suspended_from", Text),
    _id("submitted_by"),
    _ts("created_at"),
    _ts("updated_at"),
    schema="registry",
)
state_transition = Table(
    "state_transition",
    md,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _id("version_id"),
    _id("company_id"),
    Column("from_state", Text),
    Column("to_state", Text),
    Column("reason", Text),
    _id("actor_id"),
    _ts("occurred_at"),
    schema="registry",
)
release = Table(
    "release",
    md,
    _id(pk=True),
    _id("version_id"),
    _id("asset_id"),
    _id("company_id"),
    Column("env", Text),
    Column("channel", Text),
    Column("scope", JSONB),
    Column("exposure", ARRAY(Text)),
    Column("status", Text),
    _id("prev_release_id"),
    _id("created_by"),
    _ts("created_at"),
    schema="registry",
)
domain_event = Table(
    "domain_event",
    md,
    Column("seq", BigInteger, primary_key=True, autoincrement=True),
    _id("company_id"),
    Column("kind", Text),
    _id("subject_id"),
    _id("actor_id"),
    Column("payload", JSONB),
    _ts("occurred_at"),
    schema="registry",
)
release_snapshot = Table(
    "release_snapshot",
    md,
    Column("seq", BigInteger, primary_key=True),
    Column("event_seq", BigInteger),
    Column("sha256", Text),
    Column("signature", Text),
    Column("policy_version", Text),
    Column("payload", JSONB),
    _ts("created_at"),
    schema="registry",
)
grant_ceiling = Table(
    "grant_ceiling",
    md,
    _id(pk=True),
    _id("company_id"),
    Column("asset_ids", ARRAY(UUID(as_uuid=True))),
    Column("domains", ARRAY(Text)),
    Column("max_classification", SmallInteger),
    Column("quota", JSONB),
    Column("status", Text),
    _id("created_by"),
    _ts("created_at"),
    schema="grants",
)
grant = Table(
    "grant",
    md,
    _id(pk=True),
    _id("asset_id"),
    Column("grantee_type", Text),
    _id("grantee_id"),
    Column("env", Text),
    _id("company_id"),
    _id("ceiling_id"),
    _id("grantor"),
    _ts("expires_at"),
    Column("status", Text),
    _ts("last_used_at"),
    _ts("created_at"),
    schema="grants",
)
