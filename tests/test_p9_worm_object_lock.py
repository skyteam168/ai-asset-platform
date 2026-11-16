"""P9：WORM Object Lock 保留期头接线。"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aip.libs.common.config import Settings
from aip.libs.storage.s3 import S3Client
from aip.libs.storage.worm import S3WormStore, worm_store_from_settings


@pytest.mark.asyncio
async def test_s3_put_includes_object_lock_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = S3Client("http://127.0.0.1:58333", "aip", "secret")
    captured: dict[str, Any] = {}

    async def fake_put(url: str, *, content: bytes, headers: dict[str, str]) -> MagicMock:
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = content
        r = MagicMock()
        r.status_code = 200
        r.text = ""
        return r

    monkeypatch.setattr(client._http, "put", fake_put)
    until = dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    await client.put(
        "worm",
        "a/b.json",
        b"{}",
        content_type="application/json",
        object_lock_mode="COMPLIANCE",
        object_lock_retain_until=until,
    )
    assert captured["headers"]["x-amz-object-lock-mode"] == "COMPLIANCE"
    assert captured["headers"]["x-amz-object-lock-retain-until-date"] == "2030-01-02T03:04:05Z"
    assert "Authorization" in captured["headers"]
    await client.aclose()


def test_worm_store_from_settings_passes_lock_mode() -> None:
    s = Settings(
        worm_enabled=True,
        worm_bucket="aip-audit-worm",
        worm_object_lock_mode="GOVERNANCE",
        worm_object_lock_days=90,
        s3_endpoint="http://127.0.0.1:58333",
    )
    store = worm_store_from_settings(s)
    assert isinstance(store, S3WormStore)
    assert store.object_lock_mode == "GOVERNANCE"
    assert store.object_lock_days == 90
    assert store._lock_kwargs()["object_lock_mode"] == "GOVERNANCE"


@pytest.mark.asyncio
async def test_s3_worm_store_forwards_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.ensure_bucket = AsyncMock()
    client.put_if_absent = AsyncMock(return_value="s3://b/k")
    store = S3WormStore(client, "b", object_lock_mode="COMPLIANCE", object_lock_days=30)
    await store.put_if_absent("k", b"x")
    kwargs = client.put_if_absent.await_args.kwargs
    assert kwargs["object_lock_mode"] == "COMPLIANCE"
    assert isinstance(kwargs["object_lock_retain_until"], dt.datetime)
