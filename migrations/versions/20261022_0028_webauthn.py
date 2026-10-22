"""H53：WebAuthn 凭证与挑战（可选第二因子骨架）

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE iam.webauthn_credential (
          id uuid PRIMARY KEY,
          user_id uuid NOT NULL REFERENCES iam.iam_user(id),
          company_id uuid NOT NULL,
          credential_id text NOT NULL,
          public_key_cose bytea NOT NULL,
          sign_count bigint NOT NULL DEFAULT 0,
          transports text[] NOT NULL DEFAULT '{}',
          nickname text NOT NULL DEFAULT '',
          created_at timestamptz NOT NULL DEFAULT now(),
          last_used_at timestamptz,
          UNIQUE (credential_id)
        )
        """
    )
    op.execute("CREATE INDEX webauthn_credential_user_idx ON iam.webauthn_credential (user_id)")
    op.execute(
        """
        CREATE TABLE iam.webauthn_challenge (
          id uuid PRIMARY KEY,
          user_id uuid REFERENCES iam.iam_user(id),
          company_id uuid,
          kind text NOT NULL CHECK (kind IN ('register','authenticate')),
          challenge text NOT NULL,
          expires_at timestamptz NOT NULL,
          consumed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX webauthn_challenge_chal_idx ON iam.webauthn_challenge (challenge)")
    for t in ("webauthn_credential", "webauthn_challenge"):
        op.execute(f"ALTER TABLE iam.{t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE iam.{t} FORCE ROW LEVEL SECURITY")
        # challenge 在注册前可能尚无 company；用 app_bypass 或 company 匹配；NULL company 仅 system 可见
        op.execute(
            f"CREATE POLICY company_isolation ON iam.{t} "
            f"USING (app_bypass_rls() OR company_id IS NULL OR company_id = ANY (app_company_ids())) "
            f"WITH CHECK (app_bypass_rls() OR company_id IS NULL OR company_id = ANY (app_company_ids()))"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON iam.{t} TO aip_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS iam.webauthn_challenge")
    op.execute("DROP TABLE IF EXISTS iam.webauthn_credential")
