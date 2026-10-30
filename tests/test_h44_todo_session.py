"""回归：数据面服务凭据失败不得把控制台会话踢掉（/todo 不得回用户 401）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.control_plane.todo_api import DataPlane
from aip.libs.common.config import get_settings
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.serviceauth import bearer_for_outbound
from iam_seed import PASSWORD, Seed, app_url

pytestmark = pytest.mark.infra
V = "/api/v1"


@pytest.fixture
async def cp() -> httpx.AsyncClient:
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.aip.internal"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_dataplane_call_maps_service_401_to_downstream() -> None:
    dp = DataPlane("http://dp.test", "tok")
    resp = httpx.Response(
        401,
        json={"code": "UNAUTHENTICATED", "detail": "需要服务凭据"},
        request=httpx.Request("POST", "http://dp.test/api/v1/internal/todo"),
    )
    dp.http.request = AsyncMock(return_value=resp)  # type: ignore[method-assign]
    with pytest.raises(PlatformError) as ei:
        await dp.call("POST", "/todo", {"company_ids": None})
    assert ei.value.code == ErrorCode.DOWNSTREAM_UNAVAILABLE


async def test_todo_does_not_401_logged_in_console_user(cp: httpx.AsyncClient, seeded: Seed) -> None:
    login = await cp.post(
        f"{V}/auth/login",
        json={"login_name": "admin", "password": PASSWORD, "totp": seeded.code("admin")},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['session_token']}"}
    # both 模式无共享签名密钥时出站用静态令牌，避免 JWT 双进程互验失败
    s = get_settings()
    if not s.signing_key_pem and s.service_auth_mode == "both":
        assert bearer_for_outbound() == s.internal_service_token
    r = await cp.get(f"{V}/todo", headers=headers)
    assert r.status_code != 401, r.text
    assert r.status_code in (200, 503)
    me = await cp.get(f"{V}/me", headers=headers)
    assert me.status_code == 200, me.text
    if r.status_code == 200:
        body: dict[str, Any] = r.json()
        assert "confirmations" in body and "tasks" in body
