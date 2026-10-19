"""registry（资产、版本、发布、快照）、grants（授权上限与授权）、audit（分区审计表与锚点）

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE registry.asset (
  id uuid PRIMARY KEY,
  type text NOT NULL CHECK (type IN ('tool','skill','workflow','bundle','prompt','model_profile','connector_package')),
  name text NOT NULL UNIQUE,
  owner_org_id uuid NOT NULL,
  owner_user_id uuid,
  company_id uuid NOT NULL,
  classification smallint NOT NULL CHECK (classification BETWEEN 1 AND 4),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry.asset_version (
  id uuid PRIMARY KEY,
  asset_id uuid NOT NULL REFERENCES registry.asset(id),
  company_id uuid NOT NULL,
  semver text NOT NULL CHECK (semver ~ '^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?$'),
  content_hash text NOT NULL,
  spec jsonb NOT NULL,
  dep_lock jsonb NOT NULL DEFAULT '{}',
  state text NOT NULL DEFAULT 'draft' CHECK (state IN
    ('draft','testing','in_review','approved','canary','active','deprecated','retired','suspended')),
  suspended_from text,
  submitted_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, semver)
);

-- 版本不可变（architecture.md 6、13）：离开 draft 后内容字段不能改，也不能删除；数据库与应用双重保护
CREATE FUNCTION registry.asset_version_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.state <> 'draft' THEN
      RAISE EXCEPTION 'asset_version % is immutable (state=%)', OLD.id, OLD.state USING ERRCODE = 'P0001';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.state <> 'draft' AND (
       NEW.spec IS DISTINCT FROM OLD.spec OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
    OR NEW.dep_lock IS DISTINCT FROM OLD.dep_lock OR NEW.semver IS DISTINCT FROM OLD.semver
    OR NEW.asset_id IS DISTINCT FROM OLD.asset_id) THEN
    RAISE EXCEPTION 'asset_version % is immutable (state=%)', OLD.id, OLD.state USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER asset_version_immutable BEFORE UPDATE OR DELETE ON registry.asset_version
  FOR EACH ROW EXECUTE FUNCTION registry.asset_version_immutable();

CREATE TABLE registry.state_transition (
  id bigserial PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES registry.asset_version(id),
  company_id uuid NOT NULL,
  from_state text NOT NULL, to_state text NOT NULL,
  reason text NOT NULL, actor_id uuid,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry.release (
  id uuid PRIMARY KEY,
  version_id uuid NOT NULL REFERENCES registry.asset_version(id),
  asset_id uuid NOT NULL REFERENCES registry.asset(id),
  company_id uuid NOT NULL,
  env text NOT NULL CHECK (env IN ('test','staging','prod')),
  channel text NOT NULL CHECK (channel IN ('beta','stable')),
  scope jsonb NOT NULL,
  exposure text[] NOT NULL DEFAULT '{rest}',
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','rolled_back','suspended')),
  prev_release_id uuid REFERENCES registry.release(id),
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX one_active_release ON registry.release (asset_id, env, channel) WHERE status = 'active';

CREATE TABLE registry.domain_event (
  seq bigserial PRIMARY KEY,
  company_id uuid NOT NULL,
  kind text NOT NULL,
  subject_id uuid,
  actor_id uuid,
  payload jsonb NOT NULL DEFAULT '{}',
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry.release_snapshot (
  seq bigint PRIMARY KEY,
  event_seq bigint NOT NULL,
  sha256 text NOT NULL,
  signature text NOT NULL,
  policy_version text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE grants.grant_ceiling (
  id uuid PRIMARY KEY,
  company_id uuid NOT NULL,
  asset_ids uuid[] NOT NULL DEFAULT '{}',
  domains text[] NOT NULL DEFAULT '{}',
  max_classification smallint NOT NULL CHECK (max_classification BETWEEN 1 AND 4),
  quota jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE grants.grant (
  id uuid PRIMARY KEY,
  asset_id uuid NOT NULL REFERENCES registry.asset(id),
  grantee_type text NOT NULL CHECK (grantee_type IN ('consumer','agent_profile','org_unit','user')),
  grantee_id uuid NOT NULL,
  env text NOT NULL CHECK (env IN ('test','staging','prod')),
  company_id uuid NOT NULL,
  ceiling_id uuid REFERENCES grants.grant_ceiling(id),
  grantor uuid,
  expires_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  last_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, grantee_type, grantee_id, env)
);

CREATE TABLE audit.audit_log (
  shard_id text NOT NULL,
  seq bigint NOT NULL,
  occurred_at timestamptz NOT NULL,
  company_id uuid,
  kind text NOT NULL,
  record jsonb NOT NULL,
  prev_hash text NOT NULL,
  hash text NOT NULL,
  PRIMARY KEY (shard_id, seq, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE audit.audit_anchor (
  shard_id text NOT NULL,
  up_to_seq bigint NOT NULL,
  head_hash text NOT NULL,
  signature text NOT NULL,
  key_id text NOT NULL,
  anchored_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shard_id, up_to_seq)
);

-- 按月建分区：调用方在写入前确保分区存在（审计写入进程启动时与每月初调用）
CREATE FUNCTION audit.ensure_partition(month date) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  start_ts date := date_trunc('month', month)::date;
  end_ts date := (date_trunc('month', month) + interval '1 month')::date;
  part text := format('audit_log_%s', to_char(start_ts, 'YYYYMM'));
BEGIN
  EXECUTE format('CREATE TABLE IF NOT EXISTS audit.%I PARTITION OF audit.audit_log FOR VALUES FROM (%L) TO (%L)',
                 part, start_ts, end_ts);
END $$;

SELECT audit.ensure_partition(date_trunc('month', now())::date);

SELECT audit.ensure_partition((date_trunc('month', now()) + interval '1 month')::date);

-- 仅追加：任何人（含 owner）UPDATE / DELETE 都被拒；只能由超级用户关闭触发器篡改，而这会被哈希链校验发现
CREATE FUNCTION audit.append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit tables are append-only' USING ERRCODE = 'P0001';
END $$;

CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit.audit_log
  FOR EACH ROW EXECUTE FUNCTION audit.append_only();

CREATE TRIGGER audit_anchor_append_only BEFORE UPDATE OR DELETE ON audit.audit_anchor
  FOR EACH ROW EXECUTE FUNCTION audit.append_only()
"""

RLS = {
    "registry": ["asset", "asset_version", "state_transition", "release", "domain_event"],
    "grants": ["grant_ceiling", "grant"],
}


def upgrade() -> None:
    # 语句之间以空行分隔（函数体内部有分号，不能按分号切分）
    for stmt in DDL.split(";\n\n"):
        if stmt.strip():
            op.execute(stmt)
    for schema, tables in RLS.items():
        for tb in tables:
            op.execute(f'ALTER TABLE {schema}."{tb}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE {schema}."{tb}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f'CREATE POLICY company_isolation ON {schema}."{tb}" '
                "USING (app_bypass_rls() OR company_id = ANY (app_company_ids())) "
                "WITH CHECK (app_bypass_rls() OR company_id = ANY (app_company_ids()))"
            )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO aip_app")
        op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO aip_app")
    # 审计：应用角色只能 INSERT / SELECT，并可调用建分区函数
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA audit FROM aip_app")
    op.execute("GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA audit TO aip_app")
    op.execute("ALTER FUNCTION audit.ensure_partition(date) SECURITY DEFINER")
    op.execute("GRANT EXECUTE ON FUNCTION audit.ensure_partition(date) TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.audit_anchor, audit.audit_log CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit.ensure_partition(date), audit.append_only()")
    op.execute('DROP TABLE IF EXISTS grants."grant", grants.grant_ceiling CASCADE')
    op.execute(
        "DROP TABLE IF EXISTS registry.release_snapshot, registry.domain_event, registry.release, "
        "registry.state_transition, registry.asset_version, registry.asset CASCADE"
    )
    op.execute("DROP FUNCTION IF EXISTS registry.asset_version_immutable()")
