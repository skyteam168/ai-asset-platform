"""H4：按 OpenAPI 自动拉起 Prism Mock。"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from aip.control_plane.app import create_app as create_cp
from aip.control_plane.db import Database
from aip.control_plane.testlab import mock_service as svc
from aip.control_plane.testlab import prism as prism_lib
from aip.libs.common.errors import ErrorCode, PlatformError
from iam_seed import PASSWORD, Seed, actor_for, app_url

ROOT = Path(__file__).resolve().parents[1]
MES = (ROOT / "tests" / "contracts" / "mes-vn01.openapi.yaml").read_text(encoding="utf-8")

pytestmark = pytest.mark.infra


async def _login(cp: httpx.AsyncClient, seed: Seed, name: str = "admin") -> dict[str, str]:
    r = await cp.post(
        "/api/v1/auth/login",
        json={"login_name": name, "password": PASSWORD, "totp": seed.code(name)},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


@pytest.fixture(autouse=True)
def _clean_mocks() -> None:
    yield
    svc.reset_for_tests()


def test_start_mock_mes_vn01() -> None:
    if not prism_lib.docker_available():
        pytest.skip("docker not available")
    session = prism_lib.start_mock(MES)
    try:
        r = httpx.get(f"{session.base_url}/api/v2/workorders?plant=F001", timeout=5)
        assert r.status_code == 200
        assert r.json().get("code") == 0
    finally:
        prism_lib.stop_mock(session)


@pytest.mark.asyncio
async def test_create_and_delete_via_service(db: Database, seeded: Seed) -> None:
    if not prism_lib.docker_available():
        pytest.skip("docker not available")
    dev = await actor_for(db, seeded, "admin")
    created = svc.create_mock(dev, document=MES, ttl_seconds=600)
    assert created["base_url"].startswith("http://127.0.0.1:")
    got = svc.get_mock(dev, uuid.UUID(created["id"]))
    assert got["id"] == created["id"]
    async with httpx.AsyncClient() as client:
        probe = await client.get(f"{created['base_url']}/api/v2/workorders?plant=F001", timeout=5)
        assert probe.status_code == 200
    svc.delete_mock(dev, uuid.UUID(created["id"]))
    with pytest.raises(PlatformError) as ei:
        svc.get_mock(dev, uuid.UUID(created["id"]))
    assert ei.value.code == ErrorCode.NOT_FOUND
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPError):
            await client.get(f"{created['base_url']}/api/v2/workorders?plant=F001", timeout=1)


def test_invalid_document() -> None:
    with pytest.raises(PlatformError) as ei:
        prism_lib.start_mock("{")
    assert ei.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(PlatformError) as ei2:
        prism_lib.start_mock("openapi: '2.0'\npaths: {}")
    assert ei2.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_prism_mock_http_routes(db: Database, seeded: Seed) -> None:
    if not prism_lib.docker_available():
        pytest.skip("docker not available")
    app = create_cp(db=Database(app_url()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cp") as cp:
        headers = await _login(cp, seeded, "admin")
        r = await cp.post(
            "/api/v1/prism-mocks",
            headers=headers,
            json={"document": MES, "ttl_seconds": 600},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        mid = body["id"]
        g = await cp.get(f"/api/v1/prism-mocks/{mid}", headers=headers)
        assert g.status_code == 200 and g.json()["base_url"] == body["base_url"]
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"{body['base_url']}/api/v2/workorders?plant=F001", timeout=5)).status_code == 200
        d = await cp.delete(f"/api/v1/prism-mocks/{mid}", headers=headers)
        assert d.status_code == 204
        missing = await cp.get(f"/api/v1/prism-mocks/{mid}", headers=headers)
        assert missing.status_code == 404


def test_no_docker_precondition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prism_lib, "docker_available", lambda: False)
    with pytest.raises(PlatformError) as ei:
        prism_lib.start_mock(MES)
    assert ei.value.code == ErrorCode.PRECONDITION_FAILED
