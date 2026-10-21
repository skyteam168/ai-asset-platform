"""控制面 Skill Registry 客户端（architecture.md 10.4 / 10.5）：目录、清单、下载、安装前检查。

与数据面 `Client`（invoke/MCP）分离：Skill API 使用控制面会话令牌（或具备权限的 Bearer）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from aip.sdk.errors import SdkError


class SkillRegistryClient:
    """异步 Skill 目录客户端（httpx），base_url 指向控制面根（如 http://127.0.0.1:18081）。"""

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> SkillRegistryClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        r = await self._http.request(method, path, headers=self._headers(), json=json, params=params)
        if r.status_code >= 400:
            raise _to_sdk_error(r)
        if r.status_code == 204 or not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype or r.content[:1] in (b"{", b"["):
            return r.json()
        return r.content

    async def list_skills(
        self,
        *,
        q: str | None = None,
        channel: str = "stable",
        include_retracted: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"channel": channel, "include_retracted": include_retracted}
        if q:
            params["q"] = q
        body = await self._request("GET", "/api/v1/skills", params=params)
        assert isinstance(body, dict)
        return body

    async def get_manifest(self, name: str, version: str) -> dict[str, Any]:
        path = f"/api/v1/skills/{quote(name, safe='')}/versions/{quote(version, safe='')}/manifest"
        body = await self._request("GET", path)
        assert isinstance(body, dict)
        return body

    async def download(self, name: str, version: str) -> bytes:
        path = f"/api/v1/skills/{quote(name, safe='')}/versions/{quote(version, safe='')}/download"
        body = await self._request("GET", path)
        if not isinstance(body, (bytes, bytearray)):
            raise SdkError("INVALID_ARGUMENT", "下载响应不是 zip 字节", status=0)
        return bytes(body)

    async def install_check(
        self,
        *,
        consumer_id: str,
        name: str,
        version: str,
        env: str = "test",
    ) -> dict[str, Any]:
        body = await self._request(
            "POST",
            "/api/v1/skills/install-check",
            json={"consumer_id": consumer_id, "name": name, "version": version, "env": env},
        )
        assert isinstance(body, dict)
        return body

    async def download_to(
        self,
        name: str,
        version: str,
        dest_dir: str | Path,
        *,
        filename: str | None = None,
    ) -> Path:
        """下载 zip 到目录，返回文件路径。"""
        raw = await self.download(name, version)
        out = Path(dest_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / (filename or f"{name}-{version}.zip")
        path.write_bytes(raw)
        return path


def _to_sdk_error(r: httpx.Response) -> SdkError:
    code, detail = f"HTTP_{r.status_code}", r.text[:500]
    body: dict[str, Any] = {}
    try:
        parsed = r.json()
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        body = parsed
        code = str(parsed.get("code") or code)
        detail = str(parsed.get("detail") or detail)
    return SdkError(code, detail, status=r.status_code, body=body)
