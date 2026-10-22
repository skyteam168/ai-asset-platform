"""H51：external_login（OIDC/SAML 外部主体 → 内部用户映射）

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE iam.external_login (
          provider text NOT NULL,
          external_id text NOT NULL,
          user_id uuid NOT NULL REFERENCES iam.iam_user(id),
          company_id uuid NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          created_by uuid REFERENCES iam.iam_user(id),
          PRIMARY KEY (provider, external_id),
          CONSTRAINT external_login_provider_fmt CHECK (provider ~ '^[a-z][a-z0-9_]{1,31}$')
        )
        """
    )
    op.execute("CREATE INDEX external_login_user_idx ON iam.external_login (user_id)")
    op.execute("ALTER TABLE iam.external_login ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.external_login FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON iam.external_login "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON iam.external_login TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS iam.external_login")
