"""H50：生产深度清单章节与 Settings 登记。"""

from __future__ import annotations

from pathlib import Path

from aip.libs.common.config import Settings
from aip.libs.prod_check import REQUIRED_SECTIONS, run_prod_check

ROOT = Path(__file__).resolve().parents[1]


def test_prod_depth_sections() -> None:
    for sec in (
        "服务间认证",
        "GenAI / Langfuse",
        "Object Lock / WORM 全量",
        "CNPG / Patroni / PITR 实装",
    ):
        assert sec in REQUIRED_SECTIONS
    assert run_prod_check(ROOT) == []


def test_langfuse_and_service_auth_settings_fields() -> None:
    fields = Settings.model_fields
    for name in (
        "service_auth_mode",
        "service_token_ttl_s",
        "service_mtls_ca_pem",
        "langfuse_host",
        "langfuse_public_key",
        "langfuse_secret_key",
    ):
        assert name in fields
