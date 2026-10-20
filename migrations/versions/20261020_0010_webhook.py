"""Webhook 订阅与投递（architecture.md 10.5、P10）

- notify.subscription：消费方订阅的事件种类（catalog.changed / run.finished / risk.triggered）
- notify.webhook_endpoint：回调 URL 与加密存放的 HMAC 密钥；轮换后旧密钥立即失效
- notify.webhook_delivery：每次投递记录（签名头、重试、结果）

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS notify")
    op.execute(
        """CREATE TABLE notify.subscription (
             id uuid PRIMARY KEY,
             company_id uuid NOT NULL,
             consumer_id uuid NOT NULL REFERENCES iam.consumer(id),
             events text[] NOT NULL,
             created_at timestamptz NOT NULL DEFAULT now(),
             created_by uuid
           )"""
    )
    op.execute(
        """CREATE TABLE notify.webhook_endpoint (
             id uuid PRIMARY KEY,
             company_id uuid NOT NULL,
             subscription_id uuid NOT NULL REFERENCES notify.subscription(id) ON DELETE CASCADE,
             url text NOT NULL,
             secret_sealed text NOT NULL,
             status text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'disabled')),
             created_at timestamptz NOT NULL DEFAULT now(),
             updated_at timestamptz NOT NULL DEFAULT now()
           )"""
    )
    op.execute(
        """CREATE TABLE notify.webhook_delivery (
             id uuid PRIMARY KEY,
             company_id uuid NOT NULL,
             endpoint_id uuid NOT NULL REFERENCES notify.webhook_endpoint(id) ON DELETE CASCADE,
             event_kind text NOT NULL,
             event_id text,
             body jsonb NOT NULL,
             status text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'delivered', 'failed', 'dead')),
             attempts integer NOT NULL DEFAULT 0,
             next_retry_at timestamptz,
             last_status_code integer,
             last_error text,
             created_at timestamptz NOT NULL DEFAULT now(),
             delivered_at timestamptz
           )"""
    )
    op.execute(
        "CREATE INDEX webhook_delivery_pending ON notify.webhook_delivery "
        "(status, next_retry_at) WHERE status IN ('pending', 'failed')"
    )
    for tb in ("subscription", "webhook_endpoint", "webhook_delivery"):
        op.execute(f"ALTER TABLE notify.{tb} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE notify.{tb} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON notify.{tb} "
            f"USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
            f"WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
        )
    op.execute("GRANT USAGE ON SCHEMA notify TO aip_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "notify.subscription, notify.webhook_endpoint, notify.webhook_delivery TO aip_app"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS notify CASCADE")
