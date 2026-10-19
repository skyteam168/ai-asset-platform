"""iam：组织、用户、角色、密级、消费方、凭据、公钥、绑定、会话、领域事件，含 RLS

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE iam.org_unit (
  id uuid PRIMARY KEY,
  type text NOT NULL CHECK (type IN ('group','company','department','team')),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  parent_id uuid REFERENCES iam.org_unit(id),
  path ltree NOT NULL,
  company_id uuid NOT NULL,
  default_clearance smallint CHECK (default_clearance BETWEEN 1 AND 4),
  timezone text, locale text, work_calendar_id text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  valid_from timestamptz NOT NULL DEFAULT now(), valid_to timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((type = 'group') = (parent_id IS NULL))
);
CREATE UNIQUE INDEX org_unit_single_group ON iam.org_unit ((type)) WHERE type = 'group';
CREATE INDEX org_unit_path_gist ON iam.org_unit USING gist (path);

CREATE TABLE iam.org_closure (
  ancestor_id uuid NOT NULL REFERENCES iam.org_unit(id),
  descendant_id uuid NOT NULL REFERENCES iam.org_unit(id),
  depth smallint NOT NULL,
  company_id uuid NOT NULL,
  PRIMARY KEY (ancestor_id, descendant_id)
);
CREATE INDEX org_closure_desc ON iam.org_closure (descendant_id);

CREATE TABLE iam.org_external_code (
  org_id uuid NOT NULL REFERENCES iam.org_unit(id),
  system_key text NOT NULL, external_code text NOT NULL, company_id uuid NOT NULL,
  PRIMARY KEY (system_key, external_code)
);

CREATE TABLE iam.iam_user (
  id uuid PRIMARY KEY,
  login_name text UNIQUE,
  display_name text NOT NULL,
  employee_no text UNIQUE,
  email text,
  home_org_id uuid NOT NULL REFERENCES iam.org_unit(id),
  company_id uuid NOT NULL,
  login_enabled boolean NOT NULL DEFAULT false,
  password_hash text,
  totp_secret_enc text,
  failed_logins smallint NOT NULL DEFAULT 0,
  locked_until timestamptz,
  timezone text, locale text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','left')),
  clearance_override smallint CHECK (clearance_override BETWEEN 1 AND 4),
  clearance_expires_at timestamptz,
  is_break_glass boolean NOT NULL DEFAULT false,
  last_seen_at timestamptz,
  valid_from timestamptz NOT NULL DEFAULT now(), valid_to timestamptz,
  import_batch_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT login_enabled OR (login_name IS NOT NULL AND password_hash IS NOT NULL))
);

CREATE TABLE iam.user_org_membership (
  user_id uuid NOT NULL REFERENCES iam.iam_user(id),
  org_id uuid NOT NULL REFERENCES iam.org_unit(id),
  kind text NOT NULL CHECK (kind IN ('primary','secondary')),
  company_id uuid NOT NULL,
  PRIMARY KEY (user_id, org_id)
);
CREATE UNIQUE INDEX one_primary_membership ON iam.user_org_membership (user_id) WHERE kind = 'primary';

CREATE TABLE iam.user_role (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES iam.iam_user(id),
  role text NOT NULL CHECK (role IN ('platform_admin','asset_developer','data_owner','business_owner','company_admin','auditor')),
  scope_org_id uuid NOT NULL REFERENCES iam.org_unit(id),
  company_id uuid NOT NULL,
  granted_by uuid, expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, role, scope_org_id)
);

CREATE TABLE iam.user_data_scope (
  user_id uuid NOT NULL REFERENCES iam.iam_user(id),
  org_id uuid NOT NULL REFERENCES iam.org_unit(id),
  company_id uuid NOT NULL,
  granted_by uuid, expires_at timestamptz,
  PRIMARY KEY (user_id, org_id)
);

CREATE TABLE iam.clearance_request (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES iam.iam_user(id),
  company_id uuid NOT NULL,
  requested_level smallint NOT NULL CHECK (requested_level BETWEEN 1 AND 4),
  reason text NOT NULL,
  expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  requested_by uuid NOT NULL, approver uuid, decided_at timestamptz, comment text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE iam.console_session (
  token_hash text PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES iam.iam_user(id),
  company_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  absolute_expires_at timestamptz NOT NULL,
  revoked boolean NOT NULL DEFAULT false
);

CREATE TABLE iam.consumer (
  id uuid PRIMARY KEY,
  client_id text NOT NULL UNIQUE,
  company_id uuid NOT NULL REFERENCES iam.org_unit(id),
  clearance_cap smallint NOT NULL CHECK (clearance_cap BETWEEN 1 AND 4),
  bind_policy text NOT NULL DEFAULT 'strict' CHECK (bind_policy IN ('strict','by_employee_no','app_only')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  contact text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE iam.consumer_credential (
  id uuid PRIMARY KEY,
  consumer_id uuid NOT NULL REFERENCES iam.consumer(id),
  company_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('static_token','private_key_jwt')),
  token_hash text UNIQUE,
  ip_binding text[],
  expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE iam.consumer_jwk (
  consumer_id uuid NOT NULL REFERENCES iam.consumer(id),
  kid text NOT NULL,
  company_id uuid NOT NULL,
  jwk jsonb NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer_id, kid)
);

CREATE TABLE iam.user_binding (
  id uuid PRIMARY KEY,
  consumer_id uuid NOT NULL REFERENCES iam.consumer(id),
  external_subject text NOT NULL,
  user_id uuid NOT NULL REFERENCES iam.iam_user(id),
  company_id uuid NOT NULL,
  created_by uuid,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (consumer_id, external_subject)
);

CREATE TABLE iam.agent_profile (
  id uuid PRIMARY KEY,
  consumer_id uuid NOT NULL REFERENCES iam.consumer(id),
  company_id uuid NOT NULL,
  name text NOT NULL,
  domain_ids text[] NOT NULL DEFAULT '{}',
  UNIQUE (consumer_id, name)
);

CREATE TABLE iam.domain_event (
  seq bigserial PRIMARY KEY,
  company_id uuid NOT NULL,
  kind text NOT NULL,
  priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal','urgent')),
  subject_id uuid,
  actor_id uuid,
  payload jsonb NOT NULL DEFAULT '{}',
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE iam.iam_snapshot (
  seq bigint PRIMARY KEY,
  event_seq bigint NOT NULL,
  sha256 text NOT NULL,
  signature text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""

RLS_TABLES = [
    "org_unit",
    "org_closure",
    "org_external_code",
    "iam_user",
    "user_org_membership",
    "user_role",
    "user_data_scope",
    "clearance_request",
    "console_session",
    "consumer",
    "consumer_credential",
    "consumer_jwk",
    "user_binding",
    "agent_profile",
    "domain_event",
]


def upgrade() -> None:
    for stmt in DDL.split(";\n"):
        if stmt.strip():
            op.execute(stmt)
    for t in RLS_TABLES:
        op.execute(f"ALTER TABLE iam.{t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE iam.{t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON iam.{t} "
            "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
            "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
        )
    # 快照只由系统上下文读写，不按公司切分
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA iam TO aip_app")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA iam TO aip_app")


def downgrade() -> None:
    for t in [
        "iam_snapshot",
        "domain_event",
        "agent_profile",
        "user_binding",
        "consumer_jwk",
        "consumer_credential",
        "consumer",
        "console_session",
        "clearance_request",
        "user_data_scope",
        "user_role",
        "user_org_membership",
        "iam_user",
        "org_external_code",
        "org_closure",
        "org_unit",
    ]:
        op.execute(f"DROP TABLE IF EXISTS iam.{t} CASCADE")
