"""H7：工具选择评测集与跑分结果（architecture.md 7.2、11.2）

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE registry.selection_eval_settings (
          version_id uuid PRIMARY KEY REFERENCES registry.asset_version(id),
          company_id uuid NOT NULL,
          model_profiles text[] NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE registry.selection_eval_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registry.selection_eval_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON registry.selection_eval_settings "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON registry.selection_eval_settings TO aip_app")

    op.execute(
        """
        CREATE TABLE registry.selection_eval_case (
          id uuid PRIMARY KEY,
          version_id uuid NOT NULL REFERENCES registry.asset_version(id),
          company_id uuid NOT NULL,
          name text NOT NULL,
          question text NOT NULL,
          expect_capability_id text,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (version_id, name)
        )
        """
    )
    op.execute("ALTER TABLE registry.selection_eval_case ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registry.selection_eval_case FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON registry.selection_eval_case "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON registry.selection_eval_case TO aip_app")

    op.execute(
        """
        CREATE TABLE registry.selection_eval_run (
          id uuid PRIMARY KEY,
          version_id uuid NOT NULL REFERENCES registry.asset_version(id),
          company_id uuid NOT NULL,
          model_profile text NOT NULL,
          top1_hit_rate double precision NOT NULL,
          false_call_rate double precision NOT NULL,
          passed boolean NOT NULL,
          evidence jsonb NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX selection_eval_run_version_idx ON registry.selection_eval_run (version_id, created_at DESC)"
    )
    op.execute("ALTER TABLE registry.selection_eval_run ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registry.selection_eval_run FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON registry.selection_eval_run "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON registry.selection_eval_run TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS registry.selection_eval_run")
    op.execute("DROP TABLE IF EXISTS registry.selection_eval_case")
    op.execute("DROP TABLE IF EXISTS registry.selection_eval_settings")
