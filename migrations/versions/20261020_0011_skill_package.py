"""Skill 包存储（architecture.md 10.4、P10）

- registry.skill_package：版本对应的 zip 字节、文件清单（path→sha256）、平台签名与内容摘要。
  首期存库（bytea）；对象存储（SeaweedFS）可后续把 zip_bytes 换成 object_key。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE registry.skill_package (
             version_id uuid PRIMARY KEY REFERENCES registry.asset_version(id) ON DELETE CASCADE,
             company_id uuid NOT NULL,
             zip_bytes bytea NOT NULL,
             manifest jsonb NOT NULL,
             signature text NOT NULL,
             content_sha256 text NOT NULL,
             created_at timestamptz NOT NULL DEFAULT now()
           )"""
    )
    op.execute("ALTER TABLE registry.skill_package ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registry.skill_package FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY company_isolation ON registry.skill_package "
        "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
        "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON registry.skill_package TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS registry.skill_package")
