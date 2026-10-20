"""确认对象与 Workflow 运行表（architecture.md 4.5、8.5、10 数据模型；ADR-0003 自研 step journal）

- exec.confirmation：冻结的调用内容（参数加密存放 + 参数哈希）、状态机 pending → confirmed → executed；
  同一 confirmation 只执行一次靠条件更新保证。
- exec.tool_execution 加 confirmation_id。
- exec.wf_run / exec.wf_step / exec.human_task：Run 钉住 DSL 与版本；Worker 用租约认领；
  定时器（wait、人工任务超时、Run 总期限、对账退避）统一落在 wf_run.wake_at。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE exec.confirmation (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('user_confirm','approval')),
  capability_id text NOT NULL,
  version text NOT NULL,
  consumer_id uuid NOT NULL,
  end_user_id uuid,
  agent_profile_id uuid,
  principal jsonb NOT NULL,
  args_enc text NOT NULL,
  args_sha256 text NOT NULL,
  execution_id uuid NOT NULL,
  summary text NOT NULL,
  approver_role text,
  status text NOT NULL CHECK (status IN ('pending','confirmed','rejected','expired','executed')),
  expires_at timestamptz NOT NULL,
  decided_by text,
  decided_via text CHECK (decided_via IN ('callback','console','workflow')),
  decided_at timestamptz,
  comment text,
  run_id uuid,
  trace_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX confirmation_pending ON exec.confirmation (company_id, status, expires_at);

ALTER TABLE exec.tool_execution ADD COLUMN confirmation_id uuid;

CREATE TABLE exec.wf_run (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  consumer_id uuid NOT NULL,
  end_user_id uuid,
  capability_id text NOT NULL,
  version text NOT NULL,
  release_id text NOT NULL,
  dsl jsonb NOT NULL,
  principal jsonb NOT NULL,
  input jsonb NOT NULL,
  ctx jsonb NOT NULL,
  current text NOT NULL,
  status text NOT NULL CHECK (status IN ('running','waiting','succeeded','failed','compensated','rejected','cancelled')),
  tainted boolean NOT NULL DEFAULT false,
  taint_sources text[] NOT NULL DEFAULT '{}',
  steps_used integer NOT NULL DEFAULT 0,
  llm_calls integer NOT NULL DEFAULT 0,
  tokens_used integer NOT NULL DEFAULT 0,
  deadline timestamptz NOT NULL,
  lease_owner text,
  lease_until timestamptz,
  wake_at timestamptz,
  error jsonb,
  result jsonb,
  trace_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX wf_run_claim ON exec.wf_run (status, lease_until, wake_at);

CREATE TABLE exec.wf_step (
  run_id uuid NOT NULL REFERENCES exec.wf_run(id) ON DELETE CASCADE,
  node_id text NOT NULL,
  seq integer NOT NULL,
  company_id uuid NOT NULL,
  kind text NOT NULL,
  status text NOT NULL CHECK (status IN ('started','done','failed','waiting','compensated','compensation_failed')),
  execution_id uuid,
  output jsonb,
  attempts integer NOT NULL DEFAULT 0,
  started_at timestamptz,
  finished_at timestamptz,
  executor text,
  PRIMARY KEY (run_id, node_id)
);

CREATE TABLE exec.human_task (
  id uuid PRIMARY KEY,
  run_id uuid REFERENCES exec.wf_run(id) ON DELETE CASCADE,
  node_id text,
  company_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('confirm','approve','input')),
  assignee text NOT NULL,
  assignee_user_id uuid,
  assignee_role text,
  assignee_org_id uuid,
  confirmation_id uuid,
  summary text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending','approved','rejected','completed','timeout','cancelled')),
  deadline timestamptz NOT NULL,
  decided_by text,
  decided_at timestamptz,
  data jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, node_id)
)
"""

TABLES = ["confirmation", "wf_run", "wf_step", "human_task"]


def upgrade() -> None:
    for stmt in DDL.split(";\n\n"):
        if stmt.strip():
            op.execute(stmt)
    for tb in TABLES:
        op.execute(f"ALTER TABLE exec.{tb} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE exec.{tb} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON exec.{tb} "
            "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
            "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON exec.{tb} TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS exec.human_task, exec.wf_step, exec.wf_run, exec.confirmation CASCADE")
    op.execute("ALTER TABLE exec.tool_execution DROP COLUMN IF EXISTS confirmation_id")
