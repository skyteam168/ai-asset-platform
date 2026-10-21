"""H18：wf_run.ctx_ref / wf_step.output_ref 信封（architecture.md 12.6）

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for schema in ("exec", "exec_sandbox"):
        # 沙箱补齐 H11 列（0018 仅改了 exec）
        op.execute(f"ALTER TABLE {schema}.wf_run ADD COLUMN IF NOT EXISTS input_ref jsonb")
        op.execute(f"ALTER TABLE {schema}.wf_run ADD COLUMN IF NOT EXISTS result_ref jsonb")
        op.execute(f"ALTER TABLE {schema}.wf_run ADD COLUMN IF NOT EXISTS ctx_ref jsonb")
        op.execute(f"ALTER TABLE {schema}.wf_step ADD COLUMN IF NOT EXISTS output_ref jsonb")


def downgrade() -> None:
    for schema in ("exec", "exec_sandbox"):
        op.execute(f"ALTER TABLE {schema}.wf_step DROP COLUMN IF EXISTS output_ref")
        op.execute(f"ALTER TABLE {schema}.wf_run DROP COLUMN IF EXISTS ctx_ref")
