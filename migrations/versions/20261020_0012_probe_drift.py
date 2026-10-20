"""合成探针结果与契约漂移（architecture.md 7.3、11.1 probe_result）

- registry.probe_result：每次探针结果
- registry.contract_drift：版本漂移标记（不改 asset_version，发布后不可变）
- risk.rule：漂移劣化（领域事件 contract.drift）

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 上线后漂移触发的 G1 回归记录（不改版本状态）
    op.execute("ALTER TABLE registry.gate_result DROP CONSTRAINT IF EXISTS gate_result_gate_check")
    op.execute(
        "ALTER TABLE registry.gate_result ADD CONSTRAINT gate_result_gate_check "
        "CHECK (gate IN ('G0','G1','G2','G3','G4','G5','G6','G7','G8','G1_regression'))"
    )
    op.execute(
        """CREATE TABLE registry.probe_result (
             id uuid PRIMARY KEY,
             company_id uuid NOT NULL,
             version_id uuid NOT NULL REFERENCES registry.asset_version(id),
             capability_id text NOT NULL,
             run_at timestamptz NOT NULL,
             outcome text NOT NULL CHECK (outcome IN ('ok', 'failed', 'schema_invalid')),
             latency_ms integer,
             schema_valid boolean NOT NULL,
             detail jsonb NOT NULL DEFAULT '{}',
             created_at timestamptz NOT NULL DEFAULT now()
           )"""
    )
    op.execute(
        "CREATE INDEX probe_result_version_run ON registry.probe_result (version_id, run_at DESC)"
    )
    op.execute(
        """CREATE TABLE registry.contract_drift (
             version_id uuid PRIMARY KEY REFERENCES registry.asset_version(id),
             company_id uuid NOT NULL,
             capability_id text NOT NULL,
             marked_at timestamptz NOT NULL,
             reason text NOT NULL,
             consecutive_failures integer NOT NULL,
             last_g1_at timestamptz,
             cleared_at timestamptz,
             created_at timestamptz NOT NULL DEFAULT now()
           )"""
    )
    for tb in ("probe_result", "contract_drift"):
        op.execute(f"ALTER TABLE registry.{tb} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE registry.{tb} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON registry.{tb} "
            "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
            "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON registry.{tb} TO aip_app")

    params = {
        "event": "contract.drift",
        "group_by": ["capability_id"],
        "window_s": 86400,
        "threshold": 1,
    }
    op.execute(
        text(
            "INSERT INTO risk.rule (code, name, description, source, params, action, severity) "
            "VALUES (:code, :name, :desc, :source, CAST(:params AS jsonb), :action, :severity) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(
            code="drift_degradation",
            name="契约漂移劣化",
            desc="探针连续失败或输出不符合契约后标记漂移；告警负责人，不自动 Suspended",
            source="domain",
            params=json.dumps(params, ensure_ascii=False),
            action="alert",
            severity="high",
        )
    )


def downgrade() -> None:
    op.execute("DELETE FROM risk.rule WHERE code = 'drift_degradation'")
    op.execute("DROP TABLE IF EXISTS registry.contract_drift")
    op.execute("DROP TABLE IF EXISTS registry.probe_result")
    op.execute("ALTER TABLE registry.gate_result DROP CONSTRAINT IF EXISTS gate_result_gate_check")
    op.execute(
        "ALTER TABLE registry.gate_result ADD CONSTRAINT gate_result_gate_check "
        "CHECK (gate IN ('G0','G1','G2','G3','G4','G5','G6','G7','G8'))"
    )
