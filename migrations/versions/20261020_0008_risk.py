"""风控：规则、风险事件（architecture.md 12.4、P9）

- risk.rule：可配置的阈值规则（数据来源 audit / domain、过滤条件、分组、窗口、阈值或比例），动作为告警或自动 Suspended。
- risk.event：每条规则在每个分组、每个窗口最多一条事件（唯一约束），记录触发计数、动作与处置状态；
  自动 Suspended 的恢复走审批（kind=resume），批准后版本回到 suspended_from。
- 内置规则覆盖 12.4 列出的风险：异常频率、批量写、越权尝试、密级异常、断言验签失败激增、指纹变化、
  错误率熔断、非工作时段写操作。阈值是起始值，平台管理员可在控制台调整。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE SCHEMA IF NOT EXISTS risk;

CREATE TABLE risk.rule (
  code text PRIMARY KEY,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  source text NOT NULL CHECK (source IN ('audit','domain')),
  params jsonb NOT NULL,
  action text NOT NULL CHECK (action IN ('alert','suspend_asset')),
  severity text NOT NULL CHECK (severity IN ('low','medium','high')),
  enabled boolean NOT NULL DEFAULT true,
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE risk.event (
  id uuid PRIMARY KEY,
  rule_code text NOT NULL REFERENCES risk.rule(code),
  company_id uuid,
  group_key jsonb NOT NULL,
  window_start timestamptz NOT NULL,
  window_s integer NOT NULL,
  observed numeric NOT NULL,
  threshold numeric NOT NULL,
  severity text NOT NULL,
  action text NOT NULL,
  action_result jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resume_requested','resolved')),
  resolved_by uuid,
  resolved_at timestamptz,
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (rule_code, group_key, window_start)
);

CREATE INDEX risk_event_open ON risk.event (status, created_at DESC);

CREATE TABLE risk.scan_cursor (
  id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  scanned_at timestamptz NOT NULL
)
"""

RULES = [
    ("call_burst", "调用频率异常", "单个消费方对单个能力的调用次数在窗口内超过阈值", "audit",
     {"event": "tool.execution", "group_by": ["consumer_id", "capability_id"], "window_s": 60, "threshold": 600},
     "alert", "medium"),
    ("bulk_write", "批量写", "单个消费方对单个能力的写操作次数在窗口内超过阈值：自动 Suspended 该能力", "audit",
     {"event": "tool.execution", "where": {"write": ["true"]}, "group_by": ["consumer_id", "capability_id"],
      "window_s": 300, "threshold": 100},
     "suspend_asset", "high"),
    ("denied_attempts", "越权尝试", "同一调用方在窗口内被策略拒绝的次数超过阈值", "audit",
     {"event": "pdp.decision", "where": {"decision": ["deny"]}, "group_by": ["client_id", "end_user_id"],
      "window_s": 600, "threshold": 30},
     "alert", "medium"),
    ("clearance_probe", "密级异常", "同一用户在窗口内因密级不足被拒的次数超过阈值", "audit",
     {"event": "pdp.decision", "contains": {"reasons": "CLASSIFICATION_DENIED"},
      "group_by": ["client_id", "end_user_id"], "window_s": 3600, "threshold": 10},
     "alert", "medium"),
    ("assertion_failures", "断言验签失败激增", "同一消费方在窗口内的用户断言验签失败次数超过阈值", "audit",
     {"event": "auth.assertion_failed", "group_by": ["client_id"], "window_s": 300, "threshold": 50},
     "alert", "high"),
    ("fingerprint_changed", "证书指纹变化", "不校验证书的连接上出现了新的证书指纹", "domain",
     {"event": "tls.fingerprint_changed", "group_by": ["subject_id"], "window_s": 3600, "threshold": 1},
     "alert", "high"),
    ("error_spike", "错误率熔断", "能力在窗口内的失败比例超过阈值（且调用数达到下限）：自动 Suspended", "audit",
     {"event": "tool.execution", "group_by": ["capability_id"], "window_s": 300, "min_count": 30,
      "ratio": {"where": {"status": ["failed", "unknown"]}}, "threshold": 0.5},
     "suspend_asset", "high"),
    ("off_hours_write", "非工作时段写操作", "按公司时区，在工作时间以外发生的写操作", "audit",
     {"event": "tool.execution", "where": {"write": ["true"], "status": ["succeeded"]},
      "group_by": ["consumer_id", "capability_id"], "window_s": 3600, "threshold": 1,
      "off_hours": {"start": "07:00", "end": "20:00", "workdays": [1, 2, 3, 4, 5, 6],
                    "default_tz": "Asia/Shanghai", "tz_by_company_code": {}}},
     "alert", "low"),
]  # fmt: skip


def upgrade() -> None:
    for stmt in DDL.split(";\n\n"):
        if stmt.strip():
            op.execute(stmt)
    for tb in ("event",):
        op.execute(f"ALTER TABLE risk.{tb} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE risk.{tb} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY company_isolation ON risk.{tb} "
            "USING (app_bypass_rls() OR company_id IS NULL OR company_id = ANY (app_company_ids())) "
            "WITH CHECK (app_bypass_rls() OR company_id IS NULL OR company_id = ANY (app_company_ids()))"
        )
    op.execute("GRANT USAGE ON SCHEMA risk TO aip_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA risk TO aip_app")
    for code, name, desc, source, params, action, severity in RULES:
        op.execute(
            text(
                "INSERT INTO risk.rule (code, name, description, source, params, action, severity) "
                "VALUES (:code, :name, :desc, :source, CAST(:params AS jsonb), :action, :severity) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(
                code=code,
                name=name,
                desc=desc,
                source=source,
                params=json.dumps(params, ensure_ascii=False),
                action=action,
                severity=severity,
            )
        )


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS risk CASCADE")
