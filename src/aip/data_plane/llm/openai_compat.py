"""OpenAI 兼容 chat.completions 客户端（architecture.md 8.6）。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

import httpx

from aip.libs.common.errors import ErrorCode, PlatformError


def _join(base_url: str, api_path: str) -> str:
    root = base_url.rstrip("/") + "/"
    path = api_path if api_path.startswith("/") else f"/{api_path}"
    return urljoin(root, path.lstrip("/"))


def _auth_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def list_models(
    *,
    base_url: str,
    api_key: str | None = None,
    models_path: str = "/models",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """GET {base}/models（OpenAI 兼容）；返回 data 数组。"""
    url = _join(base_url, models_path)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout_s,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            r = await client.get(url, headers=_auth_headers(api_key))
        except httpx.TimeoutException as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_TIMEOUT, "LLM 上游超时") from e
        except httpx.HTTPError as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游不可用") from e
    if r.status_code >= 500:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游 5xx")
    if r.status_code == 401 or r.status_code == 403:
        raise PlatformError(ErrorCode.DOWNSTREAM_AUTH_EXPIRED, "LLM 上游鉴权失败（检查 API Key）")
    if r.status_code >= 400:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"LLM 上游拒绝：HTTP {r.status_code}")
    try:
        doc: Any = r.json()
    except json.JSONDecodeError as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游返回非 JSON") from e
    if isinstance(doc, dict):
        data = doc.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]
    raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM /models 响应格式无效")


async def chat_completions(
    *,
    base_url: str,
    api_path: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """调用上游；verify 使用系统 CA，禁止关闭校验。"""
    url = _join(base_url, api_path)
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout_s,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            r = await client.post(url, headers=_auth_headers(api_key), json=body)
        except httpx.TimeoutException as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_TIMEOUT, "LLM 上游超时") from e
        except httpx.HTTPError as e:
            raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游不可用") from e
    if r.status_code >= 500:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游 5xx")
    if r.status_code == 429:
        raise PlatformError(ErrorCode.RATE_LIMITED, "LLM 上游限流")
    if r.status_code >= 400:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, f"LLM 上游拒绝：HTTP {r.status_code}")
    try:
        data: dict[str, Any] = r.json()
    except json.JSONDecodeError as e:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游返回非 JSON") from e
    if not isinstance(data, dict):
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游返回非对象")
    return data


def extract_content_and_tokens(doc: dict[str, Any]) -> tuple[str, int]:
    choices = doc.get("choices") or []
    if not choices:
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游无 choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PlatformError(ErrorCode.DOWNSTREAM_UNAVAILABLE, "LLM 上游无 content")
    usage = doc.get("usage") or {}
    total = usage.get("total_tokens")
    if isinstance(total, int) and total >= 0:
        return content, total
    # 启发式：约 4 字符 / token
    return content, max(1, len(content) // 4)
