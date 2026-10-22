"""生产清单本地校验（H42）：文档章节 + Settings 环境变量名对照。"""

from __future__ import annotations

import re
from pathlib import Path

from aip.libs.common.config import Settings

# production-checklist.md 必须出现的二级标题（与文档一致）
REQUIRED_SECTIONS = (
    "Secrets",
    "迁移 Job",
    "kustomize prod",
    "CNPG",
    "OpenBao / Valkey / OPA / SeaweedFS",
    "OTel",
    "审计 WORM",
    "Notifier 环境变量",
    "SLO / 故障演练",
    "服务间认证",
    "GenAI / Langfuse",
    "Object Lock / WORM 全量",
    "CNPG / Patroni / PITR 实装",
)

# 清单中引用的 AIP_* 名须能映射到 Settings 字段（或已知别名）
REQUIRED_ENV_NAMES = (
    "AIP_ENV",
    "AIP_DATABASE_URL",
    "AIP_APP_DATABASE_URL",
    "AIP_VALKEY_URL",
    "AIP_OPA_URL",
    "AIP_RUNTIME_MODE",
    "AIP_OTEL_EXPORTER",
    "AIP_WORM_ENABLED",
    "AIP_WORM_BUCKET",
    "AIP_NOTIFY_IM_WEBHOOK_URL",
    "AIP_NOTIFY_FLUSH_INTERVAL_S",
    "AIP_SERVICE_AUTH_MODE",
    "AIP_SERVICE_TOKEN_TTL_S",
    "AIP_SERVICE_MTLS_CA_PEM",
    "AIP_LANGFUSE_HOST",
)


def _headings(md: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", md, re.MULTILINE)}


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for field_name in Settings.model_fields:
        names.add(f"AIP_{field_name.upper()}")
    return names


def run_prod_check(root: Path) -> list[str]:
    problems: list[str] = []
    path = root / "docs" / "ops" / "production-checklist.md"
    if not path.is_file():
        return [f"missing {path}"]
    text = path.read_text(encoding="utf-8")
    found = _headings(text)
    for sec in REQUIRED_SECTIONS:
        if sec not in found:
            problems.append(f"checklist missing section: ## {sec}")
    settings_envs = _settings_env_names()
    for env in REQUIRED_ENV_NAMES:
        if env not in settings_envs:
            problems.append(f"Settings missing field for {env}")
        if env not in text:
            problems.append(f"checklist does not mention {env}")
    return problems
