"""门禁（gate_result、test_case、acceptance_sheet）、审批引擎、使用申请、复核活动、快照变更通知

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE SCHEMA IF NOT EXISTS approval;

CREATE TABLE registry.test_case (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES registry.asset_version(id),
  company_id uuid NOT NULL,
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('functional','permission')),
  critical boolean NOT NULL DEFAULT true,
  args jsonb NOT NULL,
  principal jsonb NOT NULL DEFAULT '{}',
  expect jsonb NOT NULL,
  UNIQUE (version_id, name)
);

CREATE TABLE registry.gate_result (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES registry.asset_version(id),
  company_id uuid NOT NULL,
  gate text NOT NULL CHECK (gate IN ('G0','G1','G2','G3','G4','G5','G6','G7','G8')),
  status text NOT NULL CHECK (status IN ('passed','failed','pending')),
  evidence jsonb NOT NULL DEFAULT '{}',
  operator uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry.acceptance_sheet (
  version_id uuid PRIMARY KEY REFERENCES registry.asset_version(id),
  company_id uuid NOT NULL,
  template jsonb NOT NULL,
  results jsonb,
  signed_by uuid,
  signed_at timestamptz
);

CREATE TABLE approval.approval_request (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  kind text NOT NULL,
  subject_id uuid NOT NULL,
  submitter uuid,
  approver_role text NOT NULL,
  approver_scope_org_id uuid,
  required_approvals smallint NOT NULL DEFAULT 1 CHECK (required_approvals BETWEEN 1 AND 3),
  excluded_users uuid[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
  payload jsonb NOT NULL DEFAULT '{}',
  due_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz
);

CREATE INDEX approval_request_pending ON approval.approval_request (approver_role, status);

CREATE TABLE approval.approval_decision (
  id uuid PRIMARY KEY,
  request_id uuid NOT NULL REFERENCES approval.approval_request(id),
  company_id uuid NOT NULL,
  approver uuid NOT NULL,
  on_behalf_of uuid,
  decision text NOT NULL CHECK (decision IN ('approve','reject')),
  comment text,
  decided_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (request_id, approver)
);

CREATE TABLE approval.delegate (
  user_id uuid NOT NULL,
  delegate_user_id uuid NOT NULL,
  company_id uuid NOT NULL,
  valid_from timestamptz NOT NULL,
  valid_to timestamptz NOT NULL,
  PRIMARY KEY (user_id, delegate_user_id, valid_from),
  CHECK (user_id <> delegate_user_id AND valid_to > valid_from)
);

CREATE TABLE grants.usage_request (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  consumer_id uuid NOT NULL,
  asset_id uuid NOT NULL REFERENCES registry.asset(id),
  env text NOT NULL,
  reason text NOT NULL,
  requested_by uuid NOT NULL,
  approval_id uuid REFERENCES approval.approval_request(id),
  grant_id uuid,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE grants.access_review_campaign (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  due_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE grants.access_review_item (
  campaign_id uuid NOT NULL REFERENCES grants.access_review_campaign(id),
  grant_id uuid NOT NULL,
  company_id uuid NOT NULL,
  reason text NOT NULL,
  decision text CHECK (decision IN ('keep','revoke')),
  decided_by uuid,
  PRIMARY KEY (campaign_id, grant_id)
);

-- 快照变更通知：数据面经控制面长轮询接口近实时拿到新快照（architecture.md 4.4、ADR-0019）
CREATE FUNCTION public.notify_iam_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('aip_snapshot', 'iam:' || NEW.priority);
  RETURN NEW;
END $$;

CREATE FUNCTION public.notify_registry_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('aip_snapshot', 'release:normal');
  RETURN NEW;
END $$;

CREATE TRIGGER iam_event_notify AFTER INSERT ON iam.domain_event FOR EACH ROW EXECUTE FUNCTION public.notify_iam_event();

CREATE TRIGGER registry_event_notify AFTER INSERT ON registry.domain_event FOR EACH ROW
  EXECUTE FUNCTION public.notify_registry_event()
"""

RLS = {
    "registry": ["test_case", "gate_result", "acceptance_sheet"],
    "approval": ["approval_request", "approval_decision", "delegate"],
    "grants": ["usage_request", "access_review_campaign", "access_review_item"],
}


def upgrade() -> None:
    for stmt in DDL.split(";\n\n"):
        if stmt.strip():
            op.execute(stmt)
    op.execute("GRANT USAGE ON SCHEMA approval TO aip_app")
    for schema, tables in RLS.items():
        for tb in tables:
            op.execute(f"ALTER TABLE {schema}.{tb} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {schema}.{tb} FORCE ROW LEVEL SECURITY")
            op.execute(
                f"CREATE POLICY company_isolation ON {schema}.{tb} "
                "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
                "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
            )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO aip_app")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS registry_event_notify ON registry.domain_event")
    op.execute("DROP TRIGGER IF EXISTS iam_event_notify ON iam.domain_event")
    op.execute("DROP FUNCTION IF EXISTS public.notify_registry_event(), public.notify_iam_event()")
    op.execute(
        "DROP TABLE IF EXISTS grants.access_review_item, grants.access_review_campaign, grants.usage_request, "
        "approval.delegate, approval.approval_decision, approval.approval_request, registry.acceptance_sheet, "
        "registry.gate_result, registry.test_case CASCADE"
    )
    op.execute("DROP SCHEMA IF EXISTS approval")
