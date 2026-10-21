"""H25：human_task.data_ref 信封（architecture.md 12.6）

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for schema in ("exec", "exec_sandbox"):
        op.execute(f"ALTER TABLE {schema}.human_task ADD COLUMN IF NOT EXISTS data_ref jsonb")


def downgrade() -> None:
    for schema in ("exec", "exec_sandbox"):
        op.execute(f"ALTER TABLE {schema}.human_task DROP COLUMN IF EXISTS data_ref")
