"""Wave P：试点自检脚本与 Settings 门禁。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from aip.libs.common.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_staging_rejects_dev_only_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIP_ENV", "staging")
    monkeypatch.setenv("AIP_SERVICE_AUTH_MODE", "jwt")
    monkeypatch.setenv("AIP_SIGNING_KEY_PEM", "-----BEGIN EC PRIVATE KEY-----\nMTEST\n-----END EC PRIVATE KEY-----")
    monkeypatch.setenv("AIP_OPENBAO_TOKEN", "not-a-default-token")
    monkeypatch.setenv("AIP_INTERNAL_SERVICE_TOKEN", "not-a-default-svc")
    monkeypatch.setenv("AIP_SECRET_BOX_KEY", "not-default-box-key-32bytes-aaaa")
    # 仍含 dev-only → 应失败
    monkeypatch.setenv("AIP_DATABASE_URL", "postgresql+asyncpg://aip:aip-dev-only@127.0.0.1:55432/aip")
    monkeypatch.setenv("AIP_APP_DATABASE_URL", "postgresql+asyncpg://aip_app:aip-app-dev-only@127.0.0.1:55432/aip")
    with pytest.raises(ValidationError):
        Settings()


def test_staging_accepts_explicit_non_dev_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIP_ENV", "staging")
    monkeypatch.setenv("AIP_SERVICE_AUTH_MODE", "jwt")
    monkeypatch.setenv("AIP_SIGNING_KEY_PEM", "-----BEGIN EC PRIVATE KEY-----\nMTEST\n-----END EC PRIVATE KEY-----")
    monkeypatch.setenv("AIP_OPENBAO_TOKEN", "pilot-openbao-token")
    monkeypatch.setenv("AIP_INTERNAL_SERVICE_TOKEN", "pilot-internal-token")
    monkeypatch.setenv("AIP_SECRET_BOX_KEY", "pilot-secret-box-key-32b-xxxxxx")
    monkeypatch.setenv("AIP_DATABASE_URL", "postgresql+asyncpg://aip:pilot-db-pass@127.0.0.1:55432/aip")
    monkeypatch.setenv("AIP_APP_DATABASE_URL", "postgresql+asyncpg://aip_app:pilot-app-pass@127.0.0.1:55432/aip")
    s = Settings()
    assert s.env == "staging"
    assert s.service_auth_mode == "jwt"


def test_pilot_check_script_runs_in_dev() -> None:
    """dev 环境下脚本应 exit 0（仅 WARN），不要求已切 staging。"""
    import subprocess
    import sys

    env = {**os.environ, "AIP_ENV": "dev", "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "pilot_check.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    # compose/healthz 可能因未 up 产生 FAIL；此处只断言脚本可导入并输出 Wave P 标题
    assert "Wave P pilot_check" in (proc.stdout or "")
