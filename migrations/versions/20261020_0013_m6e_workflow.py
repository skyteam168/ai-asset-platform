"""M6e：子流程父子 Run、沙箱运行表、通知投递箱

- exec.wf_run.parent_run_id：sub_workflow 挂起父 Run，子结束时唤醒
- exec_sandbox：与 exec 同结构的隔离 schema，供 Test Lab 沙箱回放（独立 Worker）
- notify.channel_outbox：Notifier（邮件 / IM）待投递

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXEC_TABLES = """
CREATE TABLE {schema}.wf_run (
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
  taint_sources text[] NOT NULL DEFAULT '{{}}',
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
  parent_run_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX wf_run_claim ON {schema}.wf_run (status, lease_until, wake_at);
CREATE INDEX wf_run_parent ON {schema}.wf_run (parent_run_id) WHERE parent_run_id IS NOT NULL;

CREATE TABLE {schema}.wf_step (
  run_id uuid NOT NULL REFERENCES {schema}.wf_run(id) ON DELETE CASCADE,
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

CREATE TABLE {schema}.human_task (
  id uuid PRIMARY KEY,
  run_id uuid REFERENCES {schema}.wf_run(id) ON DELETE CASCADE,
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
);
"""


def _rls(schema: str, tables: list[str]) -> None:
    for tb in tables:
        op.execute(f"ALTER TABLE {schema}.{tb} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {schema}.{tb} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON {schema}.{tb} "
            f"USING (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
        )


def upgrade() -> None:
    op.execute("ALTER TABLE exec.wf_run ADD COLUMN IF NOT EXISTS parent_run_id uuid")
    op.execute(
        "CREATE INDEX IF NOT EXISTS wf_run_parent ON exec.wf_run (parent_run_id) "
        "WHERE parent_run_id IS NOT NULL"
    )

    op.execute("CREATE SCHEMA IF NOT EXISTS exec_sandbox")
    # 沙箱表与生产同结构（含 parent_run_id），避免被生产 Worker 认领
    for stmt in EXEC_TABLES.format(schema="exec_sandbox").split(";"):
        s = stmt.strip()
        if s:
            op.execute(s)
    _rls("exec_sandbox", ["wf_run", "wf_step", "human_task"])
    op.execute("GRANT USAGE ON SCHEMA exec_sandbox TO aip_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "exec_sandbox.wf_run, exec_sandbox.wf_step, exec_sandbox.human_task TO aip_app"
    )

    op.execute(
        """CREATE TABLE notify.channel_outbox (
             id uuid PRIMARY KEY,
             company_id uuid NOT NULL,
             channel text NOT NULL CHECK (channel IN ('email', 'im')),
             recipient text NOT NULL,
             subject text NOT NULL,
             body text NOT NULL,
             kind text NOT NULL,
             ref jsonb NOT NULL DEFAULT '{}',
             status text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'sent', 'failed', 'dead')),
             attempts integer NOT NULL DEFAULT 0,
             next_attempt_at timestamptz NOT NULL DEFAULT now(),
             last_error text,
             created_at timestamptz NOT NULL DEFAULT now(),
             sent_at timestamptz
           )"""
    )
    op.execute(
        "CREATE INDEX channel_outbox_pending ON notify.channel_outbox "
        "(status, next_attempt_at) WHERE status = 'pending'"
    )
    _rls("notify", ["channel_outbox"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON notify.channel_outbox TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notify.channel_outbox CASCADE")
    op.execute("DROP SCHEMA IF EXISTS exec_sandbox CASCADE")
    op.execute("DROP INDEX IF EXISTS exec.wf_run_parent")
    op.execute("ALTER TABLE exec.wf_run DROP COLUMN IF EXISTS parent_run_id")
