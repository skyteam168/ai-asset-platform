"""M7i：故障演练（architecture.md 12.2）。

对照 docs/ops/fault-drills.md：控制面不可达仍可调用、Valkey 退化、OpenBao 宕机用缓存凭据。
审计 spool 背压 / 控制面快照恢复见 test_m2_*。
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx
import pytest

from aip.connectors.credentials import CredentialManager
from aip.libs.common.errors import ErrorCode, PlatformError
from aip.libs.tokens.replay import ValkeyReplayStore
from m3_world import World, connector, tool

pytestmark = pytest.mark.infra

CAP = "production.mes.query_work_orders"
ARGS = {"factory_id": "F001", "status": "open"}
STATIC = "aipk_m7i_fault"


async def test_invoke_without_control_plane(tmp_path: pathlib.Path) -> None:
    """控制面不可达：数据面已加载快照时 invoke 仍成功（12.2）。"""
    # World 不挂控制面长轮询：等价于“控制面已不可达、仅靠已装载快照服务”
    w = World(
        tmp_path,
        [tool(CAP, "mes", "q")],
        [connector("mes", "mock", {"operations": {"q": {"response": [{"wo_no": "W1"}]}}})],
        static_token=STATIC,
        exposure=["rest", "mcp"],
    )
    await w.ready()
    r = await w.runtime.invoke(w.principal(), CAP, ARGS)
    assert r["status"] == "succeeded", r
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://dp") as c:
        ready = await c.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["release_snapshot"] == "loaded"
        cat = await c.get("/api/v1/catalog", headers={"Authorization": f"Bearer {STATIC}"})
        assert cat.status_code == 200
        assert CAP in {i["capability_id"] for i in cat.json()["items"]}


async def test_valkey_down_sets_degraded() -> None:
    """Valkey 不可用：ReplayStore 置 degraded，并退化为进程内 first_use。"""
    store = ValkeyReplayStore("redis://127.0.0.1:1/0")  # 必定连不上
    assert await store.first_use("jti-a", 30) is True
    assert store.degraded is True
    assert await store.first_use("jti-a", 30) is False  # 进程内防重放仍生效
    assert await store.first_use("jti-b", 30) is True
    await store.aclose()


async def test_openbao_down_uses_cached_credential(tmp_path: pathlib.Path) -> None:
    """OpenBao 不可用：已缓存的 login token 到期前继续；新取凭据失败。"""

    class BoomStore:
        def __init__(self) -> None:
            self.fail = False
            self.calls = 0

        async def get(self, ref: str) -> dict[str, str]:
            self.calls += 1
            if self.fail:
                raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "读取下游凭据失败")
            return {"username": "u", "password": "p"}

    store = BoomStore()
    mgr = CredentialManager(
        {"mode": "login", "path": "/login", "token_path": "$.token", "ttl_s": 1800},
        "kv/data/x",
        store,  # type: ignore[arg-type]
    )

    def factory() -> httpx.AsyncClient:
        # 简易 ASGI：固定返回 token（client_factory 为同步工厂）
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def login(_req: Any) -> JSONResponse:
            return JSONResponse({"token": "cached-tok"})

        app = Starlette(routes=[Route("/login", login, methods=["POST"])])
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://down")

    h1 = await mgr.headers(factory)
    assert h1["Authorization"].endswith("cached-tok")
    assert store.calls == 1

    store.fail = True  # OpenBao 宕机
    h2 = await mgr.headers(factory)
    assert h2 == h1  # 缓存命中，不再读 store
    assert store.calls == 1

    mgr._expires = 0.0  # 强制过期
    with pytest.raises(PlatformError) as ei:
        await mgr.headers(factory)
    assert ei.value.code == ErrorCode.DOWNSTREAM_AUTH_EXPIRED
