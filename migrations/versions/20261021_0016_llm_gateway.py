"""H6：LLM Gateway — 公司模型白名单与日 token 预算（architecture.md 8.6）

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS llm")
    op.execute("GRANT USAGE ON SCHEMA llm TO aip_app")
    op.execute(
        """
        CREATE TABLE llm.company_model_allowlist (
          company_id uuid NOT NULL,
          model_profile_name text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (company_id, model_profile_name)
        )
        """
    )
    op.execute("ALTER TABLE llm.company_model_allowlist ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm.company_model_allowlist FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON llm.company_model_allowlist "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON llm.company_model_allowlist TO aip_app")

    op.execute(
        """
        CREATE TABLE llm.company_token_budget (
          company_id uuid PRIMARY KEY,
          daily_tokens integer NOT NULL CHECK (daily_tokens >= 0),
          updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE llm.company_token_budget ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm.company_token_budget FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON llm.company_token_budget "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON llm.company_token_budget TO aip_app")

    op.execute(
        """
        CREATE TABLE llm.company_token_usage (
          company_id uuid NOT NULL,
          usage_date date NOT NULL,
          tokens_used bigint NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (company_id, usage_date)
        )
        """
    )
    op.execute("ALTER TABLE llm.company_token_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm.company_token_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON llm.company_token_usage "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON llm.company_token_usage TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm.company_token_usage")
    op.execute("DROP TABLE IF EXISTS llm.company_token_budget")
    op.execute("DROP TABLE IF EXISTS llm.company_model_allowlist")
    op.execute("DROP SCHEMA IF EXISTS llm")
