"""H31：带脚本 Skill 白名单（architecture.md 10.4）

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE registry.skill_script_allowlist (
          id uuid PRIMARY KEY,
          company_id uuid NOT NULL,
          version_id uuid NOT NULL REFERENCES registry.asset_version(id) ON DELETE CASCADE,
          path_glob text NOT NULL,
          status text NOT NULL,
          requested_by uuid NOT NULL,
          approved_by uuid,
          reason text,
          expires_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT skill_script_allowlist_status_chk
            CHECK (status IN ('pending', 'approved', 'rejected')),
          CONSTRAINT skill_script_allowlist_glob_chk
            CHECK (char_length(path_glob) BETWEEN 1 AND 256)
        )
        """
    )
    op.execute(
        "CREATE INDEX skill_script_allowlist_version_idx "
        "ON registry.skill_script_allowlist (version_id, status)"
    )
    op.execute("ALTER TABLE registry.skill_script_allowlist ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registry.skill_script_allowlist FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON registry.skill_script_allowlist "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON registry.skill_script_allowlist TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS registry.skill_script_allowlist")
