"""H35：Skill 包 object_key（SeaweedFS）；保留 zip_bytes 双读回落

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE registry.skill_package ADD COLUMN IF NOT EXISTS object_key text")


def downgrade() -> None:
    op.execute("ALTER TABLE registry.skill_package DROP COLUMN IF EXISTS object_key")
