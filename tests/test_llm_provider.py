"""LLM 提供方：保存 base_url、OpenBao 密钥、同步 /models、创建 model_profile。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from aip.connectors.credentials import MemorySecretStore
from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.control_plane.llm import provider as llm_provider
from aip.data_plane.llm.openai_compat import list_models
from aip.libs.common.config import get_settings
from iam_seed import PASSWORD, Seed


@pytest.mark.asyncio
async def test_list_models_openai_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers.get("Authorization") == "Bearer sk-test"
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "deepseek-v4.1-flash", "owned_by": "maas"}]},
        )

    transport = httpx.MockTransport(handler)
    items = await list_models(
        base_url="https://example.internal/compatible-mode/v1",
        api_key="sk-test",
        transport=transport,
    )
    assert items[0]["id"] == "deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_provider_save_sync_and_create_profile(
    tmp_path: Path, db: Database, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.data_dir == str(tmp_path)

    store = MemorySecretStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "deepseek-v4.1-flash-1017", "owned_by": "aliyun"}]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    app = create_cp(db)
    app.state.secrets = store
    app.state.llm_transport = transport

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cp:
        login = await cp.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": PASSWORD, "totp": seeded.code("admin")},
        )
        assert login.status_code == 200, login.text
        h = {"Authorization": f"Bearer {login.json()['session_token']}"}

        put = await cp.put(
            "/api/v1/llm/provider",
            headers=h,
            json={
                "base_url": "https://ws-example.maas.aliyuncs.com/compatible-mode/v1",
                "api_key": "sk-live-demo-key",
                "models_path": "/models",
            },
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["base_url"].endswith("/compatible-mode/v1")
        assert body["has_api_key"] is True
        assert "sk-" in (body.get("api_key_hint") or "")

        # 同步走 app.state.llm_transport
        sync = await cp.post("/api/v1/llm/provider/sync-models", headers=h)
        assert sync.status_code == 200, sync.text
        items = sync.json()["items"]
        assert items[0]["id"] == "deepseek-v4.1-flash-1017"
        assert items[0]["suggested_profile"] == "deepseek_v4.1_flash_1017"

        created = await cp.post(
            "/api/v1/llm/model-profiles",
            headers=h,
            json={
                "model_id": "deepseek-v4.1-flash-1017",
                "owner_org_id": str(seeded.hq),
                "add_to_allowlist_company_id": str(seeded.hq),
                "version": "1.0.0",
            },
        )
        assert created.status_code == 201, created.text
        ref = created.json()["ref"]
        assert ref.startswith("deepseek_v4.1_flash_1017@")
        assert created.json()["spec"]["api_path"] == "/chat/completions"
        assert created.json()["spec"]["credential_ref"] == llm_provider.DEFAULT_CREDENTIAL_REF

        listed = await cp.get("/api/v1/llm/model-profiles", headers=h)
        assert listed.status_code == 200
        assert any(r["name"] == "deepseek_v4.1_flash_1017" for r in listed.json()["items"])

        allow = await cp.get(f"/api/v1/companies/{seeded.hq}/llm-allowlist", headers=h)
        assert "deepseek_v4.1_flash_1017" in allow.json()["names"]

    get_settings.cache_clear()


def test_profile_name_sanitize() -> None:
    assert llm_provider.profile_name_from_model("deepseek-v4.1-flash") == "deepseek_v4.1_flash"
    assert llm_provider.profile_name_from_model("9bad").startswith("m_")
