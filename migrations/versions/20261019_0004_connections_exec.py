"""conn（系统连接、内网白名单、连接测试记录、Connector 实例）与 exec（tool_execution）

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE conn.system_connection (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  system_key text NOT NULL,
  env text NOT NULL CHECK (env IN ('sandbox','test','staging','prod')),
  base_url text NOT NULL,
  scheme text NOT NULL CHECK (scheme IN ('https','http')),
  tls_mode text CHECK (tls_mode IN ('system_ca','custom_ca','pinned','insecure')),
  ca_pem text,
  pins jsonb NOT NULL DEFAULT '[]',
  tls_check_hostname boolean NOT NULL DEFAULT true,
  pin_enforce_expiry boolean NOT NULL DEFAULT false,
  block_on_fingerprint_change boolean NOT NULL DEFAULT false,
  client_cert_ref text,
  credential_ref text,
  connect_timeout_s real NOT NULL DEFAULT 3,
  read_timeout_s real NOT NULL DEFAULT 10,
  transport_exception_reason text,
  transport_exception_expires_at timestamptz,
  transport_approved_by uuid[] NOT NULL DEFAULT '{}',
  tls_last_seen_fingerprint text,
  owner_user_id uuid,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('pending_approval','active','disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (system_key, env, company_id),
  CHECK ((scheme = 'http') = (tls_mode IS NULL)),
  CHECK (NOT (scheme = 'http' OR tls_mode = 'insecure')
         OR (transport_exception_reason IS NOT NULL AND transport_exception_expires_at IS NOT NULL)),
  CHECK (tls_mode <> 'custom_ca' OR ca_pem IS NOT NULL),
  CHECK (tls_mode <> 'pinned' OR jsonb_array_length(pins) > 0)
);

CREATE TABLE conn.network_allowlist (
  cidr cidr PRIMARY KEY,
  description text NOT NULL
);

INSERT INTO conn.network_allowlist (cidr, description) VALUES
  ('10.0.0.0/8', 'RFC1918'), ('172.16.0.0/12', 'RFC1918'), ('192.168.0.0/16', 'RFC1918'),
  ('127.0.0.0/8', 'loopback（仅开发与测试环境）');

CREATE TABLE conn.connection_test_run (
  id uuid PRIMARY KEY,
  connection_id uuid NOT NULL REFERENCES conn.system_connection(id),
  company_id uuid NOT NULL,
  run_at timestamptz NOT NULL DEFAULT now(),
  verdict text NOT NULL,
  error_code text,
  result jsonb NOT NULL,
  operator uuid
);

CREATE TABLE conn.connector_instance (
  id uuid PRIMARY KEY,
  name text NOT NULL UNIQUE,
  company_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('declarative','kb','mock')),
  system_connection_id uuid REFERENCES conn.system_connection(id),
  config jsonb NOT NULL DEFAULT '{}',
  concurrency integer NOT NULL DEFAULT 8 CHECK (concurrency > 0),
  rate_per_s real NOT NULL DEFAULT 20 CHECK (rate_per_s > 0),
  burst integer NOT NULL DEFAULT 40 CHECK (burst > 0),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE exec.tool_execution (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  capability_id text NOT NULL,
  version text NOT NULL,
  consumer_id uuid NOT NULL,
  end_user_id uuid,
  agent_profile_id uuid,
  idempotency_key text,
  args_sha256 text NOT NULL,
  status text NOT NULL CHECK (status IN ('succeeded','failed','unknown','pending_confirm','running','cancelled','compensated')),
  result jsonb,
  error_code text,
  retry_count integer NOT NULL DEFAULT 0,
  reconcile_status text,
  deadline timestamptz,
  transport text,
  tls_verified boolean,
  trace_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX tool_execution_idem ON exec.tool_execution (consumer_id, capability_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL
"""

RLS = {"conn": ["system_connection", "connection_test_run", "connector_instance"], "exec": ["tool_execution"]}


def upgrade() -> None:
    for stmt in DDL.split(";\n\n"):
        if stmt.strip():
            op.execute(stmt)
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
    op.execute("REVOKE INSERT, UPDATE, DELETE ON conn.network_allowlist FROM aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS exec.tool_execution CASCADE")
    op.execute(
        "DROP TABLE IF EXISTS conn.connector_instance, conn.connection_test_run, conn.network_allowlist, "
        "conn.system_connection CASCADE"
    )
