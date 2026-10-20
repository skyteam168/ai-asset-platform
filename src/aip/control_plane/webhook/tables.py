"""notify schema 表定义。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

md = MetaData(schema="notify")
U = UUID(as_uuid=True)

subscription = Table(
    "subscription",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("consumer_id", U, nullable=False),
    Column("events", ARRAY(Text), nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("created_by", U),
)

webhook_endpoint = Table(
    "webhook_endpoint",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("subscription_id", U, nullable=False),
    Column("url", Text, nullable=False),
    Column("secret_sealed", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

webhook_delivery = Table(
    "webhook_delivery",
    md,
    Column("id", U, primary_key=True),
    Column("company_id", U, nullable=False),
    Column("endpoint_id", U, nullable=False),
    Column("event_kind", Text, nullable=False),
    Column("event_id", Text),
    Column("body", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_retry_at", DateTime(timezone=True)),
    Column("last_status_code", Integer),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True)),
    Column("delivered_at", DateTime(timezone=True)),
)
