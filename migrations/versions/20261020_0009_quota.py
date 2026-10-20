"""配额与计量（architecture.md 4.1、12.4、P9）

- iam.consumer.quota：消费方每日调用 / 写操作上限（{"daily_calls": N, "daily_writes": N}，空对象表示不限）。
- grants.grant_ceiling.quota 已存在：同一上限下全公司共享的每日调用上限（{"daily_calls": N}）。
- exec.usage_counter：数据面按 UTC 日计数；条件自增（未超限才加一），多副本共享。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE iam.consumer ADD COLUMN quota jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute(
        """CREATE TABLE exec.usage_counter (
             day date NOT NULL,
             key text NOT NULL,
             company_id uuid NOT NULL,
             count integer NOT NULL DEFAULT 0,
             updated_at timestamptz NOT NULL DEFAULT now(),
             PRIMARY KEY (day, key)
           )"""
    )
    op.execute("ALTER TABLE exec.usage_counter ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE exec.usage_counter FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON exec.usage_counter "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON exec.usage_counter TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS exec.usage_counter")
    op.execute("ALTER TABLE iam.consumer DROP COLUMN IF EXISTS quota")
