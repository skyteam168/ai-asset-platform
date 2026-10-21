"""官方 Python SDK：封装数据面 REST（令牌、目录、调用、确认、Run / Execution 状态）。"""

from __future__ import annotations

from typing import Any

import httpx

from aip.sdk.errors import SdkError

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class Client:
    """异步消费方客户端（httpx）。不经平台 libs/httpclient——那是平台连下游的约束。"""

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        static_token: str | None = None,
        client_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.static_token = static_token
        self.client_id = client_id
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def _bearer(self) -> str:
        tok = self.access_token or self.static_token
        if not tok:
            raise SdkError("UNAUTHENTICATED", "未设置 access_token 或 static_token", status=0)
        return tok

    def _auth_headers(self, *, user_assertion: str | None = None, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._bearer()}"}
        if user_assertion:
            h["X-User-Assertion"] = user_assertion
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> Any:
        r = await self._http.request(method, path, headers=headers, json=json, data=data)
        if r.status_code >= 400:
            raise _to_sdk_error(r)
        if r.status_code == 204 or not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype or r.content[:1] in (b"{", b"["):
            return r.json()
        return r.text

    async def exchange_token(
        self,
        assertion: str,
        *,
        client_assertion: str | None = None,
        resource: str | None = None,
        grant_type: str = JWT_BEARER,
    ) -> dict[str, Any]:
        """RFC 7523 jwt-bearer 换用户访问令牌；客户端凭据为 static_token 或 client_assertion。"""
        form: dict[str, str] = {"grant_type": grant_type, "assertion": assertion}
        if resource:
            form["resource"] = resource
        headers: dict[str, str] = {}
        if client_assertion:
            if not self.client_id:
                raise SdkError("INVALID_ARGUMENT", "使用 client_assertion 时需要 client_id", status=0)
            form["client_id"] = self.client_id
            form["client_assertion_type"] = CLIENT_ASSERTION_TYPE
            form["client_assertion"] = client_assertion
        elif self.static_token:
            headers["Authorization"] = f"Bearer {self.static_token}"
        else:
            raise SdkError("UNAUTHENTICATED", "需要 static_token 或 client_assertion", status=0)
        body = await self._request("POST", "/oauth/token", headers=headers or None, data=form)
        assert isinstance(body, dict)
        tok = body.get("access_token")
        if isinstance(tok, str):
            self.access_token = tok
        return body

    async def catalog(self, *, user_assertion: str | None = None) -> dict[str, Any]:
        body = await self._request(
            "GET", "/api/v1/catalog", headers=self._auth_headers(user_assertion=user_assertion)
        )
        assert isinstance(body, dict)
        return body

    async def invoke(
        self,
        capability_id: str,
        args: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        version: str | None = None,
        channel: str = "stable",
        user_assertion: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"args": args or {}, "channel": channel}
        if version is not None:
            payload["version"] = version
        body = await self._request(
            "POST",
            f"/api/v1/invoke/{capability_id}",
            headers=self._auth_headers(user_assertion=user_assertion, idempotency_key=idempotency_key),
            json=payload,
        )
        assert isinstance(body, dict)
        return body

    async def get_execution(self, execution_id: str, *, user_assertion: str | None = None) -> dict[str, Any]:
        body = await self._request(
            "GET",
            f"/api/v1/executions/{execution_id}",
            headers=self._auth_headers(user_assertion=user_assertion),
        )
        assert isinstance(body, dict)
        return body

    async def get_run(self, run_id: str, *, user_assertion: str | None = None) -> dict[str, Any]:
        body = await self._request(
            "GET",
            f"/api/v1/runs/{run_id}",
            headers=self._auth_headers(user_assertion=user_assertion),
        )
        assert isinstance(body, dict)
        return body

    async def confirm(
        self,
        confirmation_id: str,
        assertion: str,
        *,
        comment: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"assertion": assertion}
        if comment is not None:
            payload["comment"] = comment
        if args is not None:
            payload["args"] = args
        body = await self._request(
            "POST",
            f"/api/v1/confirmations/{confirmation_id}/confirm",
            headers=self._auth_headers(),
            json=payload,
        )
        assert isinstance(body, dict)
        return body

    async def reject(
        self,
        confirmation_id: str,
        *,
        assertion: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if assertion is not None:
            payload["assertion"] = assertion
        if comment is not None:
            payload["comment"] = comment
        body = await self._request(
            "POST",
            f"/api/v1/confirmations/{confirmation_id}/reject",
            headers=self._auth_headers(),
            json=payload,
        )
        assert isinstance(body, dict)
        return body

    async def as_langchain_tools(
        self,
        *,
        names: set[str] | list[str] | None = None,
        user_assertion: str | None = None,
    ) -> list[Any]:
        from aip.sdk.langchain_tools import build_langchain_tools

        return await build_langchain_tools(self, names=names, user_assertion=user_assertion)


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
