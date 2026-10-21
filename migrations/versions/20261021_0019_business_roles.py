"""H12：公司业务角色（workflow human_task 分派，非控制面 RBAC）

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 原 CHECK 仅允许 6 个平台角色；业务角色编码写入同一列
    op.execute("ALTER TABLE iam.user_role DROP CONSTRAINT IF EXISTS user_role_role_check")
    op.execute(
        """
        ALTER TABLE iam.user_role ADD CONSTRAINT user_role_role_check CHECK (
          role IN ('platform_admin','asset_developer','data_owner','business_owner','company_admin','auditor')
          OR role ~ '^[a-z][a-z0-9_]{1,63}$'
        )
        """
    )
    op.execute(
        """
        CREATE TABLE iam.business_role (
          company_id uuid NOT NULL REFERENCES iam.org_unit(id),
          code text NOT NULL,
          name text NOT NULL,
          description text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (company_id, code),
          CONSTRAINT business_role_code_fmt CHECK (code ~ '^[a-z][a-z0-9_]{1,63}$')
        )
        """
    )
    op.execute("ALTER TABLE iam.business_role ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE iam.business_role FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON iam.business_role "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON iam.business_role TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS iam.business_role")
    op.execute("ALTER TABLE iam.user_role DROP CONSTRAINT IF EXISTS user_role_role_check")
    op.execute(
        """
        ALTER TABLE iam.user_role ADD CONSTRAINT user_role_role_check CHECK (
          role IN ('platform_admin','asset_developer','data_owner','business_owner','company_admin','auditor')
        )
        """
    )
