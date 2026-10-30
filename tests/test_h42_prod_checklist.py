"""H42：生产清单文档与 prod-check。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aip.libs.common.config import Settings
from aip.libs.prod_check import REQUIRED_SECTIONS, check_service_auth_settings, run_prod_check

ROOT = Path(__file__).resolve().parents[1]

_PROD_OK = {
    "env": "prod",
    "database_url": "postgresql+asyncpg://owner:secret@db/aip",
    "app_database_url": "postgresql+asyncpg://aip_app:secret@db/aip",
    "openbao_token": "prod-openbao-token",
    "secret_box_key": "cHJvZC1vbmx5LXNlY3JldC1ib3gta2V5LTMyYnl0ZXMh",
    "internal_service_token": "prod-internal-service-token",
    "signing_key_pem": "-----BEGIN PRIVATE KEY-----\nMEw=\n-----END PRIVATE KEY-----",
    "service_auth_mode": "jwt",
}


def test_checklist_sections_present() -> None:
    problems = run_prod_check(ROOT)
    assert problems == [], problems


def test_required_sections_constant_matches_doc() -> None:
    md = (ROOT / "docs" / "ops" / "production-checklist.md").read_text(encoding="utf-8")
    for sec in REQUIRED_SECTIONS:
        assert f"## {sec}" in md


def test_prod_check_cli_exits_zero() -> None:
    # 无 kubectl 时 k8s-check 可能失败；prod-check 内含 k8s-check
    # 此处只验文档/Settings 部分逻辑（run_prod_check）已由上例覆盖；
    # CLI 完整路径在有 kubectl 的环境跑 tools/dev.py prod-check。
    rc = subprocess.call(
        [sys.executable, "-c", "from aip.libs.prod_check import run_prod_check; "
         "from pathlib import Path; "
         "p=run_prod_check(Path('.').resolve()); "
         "raise SystemExit(1 if p else 0)"],
        cwd=ROOT,
    )
    assert rc == 0


def test_prod_rejects_static_service_auth() -> None:
    """G2.2：AIP_ENV=prod 且 service_auth_mode=static → Settings 校验失败。"""
    with pytest.raises(ValidationError) as ei:
        Settings(**{**_PROD_OK, "service_auth_mode": "static"})  # type: ignore[arg-type]
    assert "service_auth_mode" in str(ei.value)


def test_check_service_auth_settings_flags_static_and_missing_pem() -> None:
    ok = Settings(**_PROD_OK)  # type: ignore[arg-type]
    assert check_service_auth_settings(ok) == []
    static = ok.model_copy(update={"service_auth_mode": "static"})
    assert any("static" in p for p in check_service_auth_settings(static))
    no_pem = ok.model_copy(update={"signing_key_pem": "", "signing_backend": "local"})
    assert any("SIGNING_KEY_PEM" in p or "signing_key" in p.lower() for p in check_service_auth_settings(no_pem))
    transit = ok.model_copy(update={"signing_backend": "transit", "signing_key_pem": "", "transit_signing_key": ""})
    assert any("TRANSIT" in p for p in check_service_auth_settings(transit))
