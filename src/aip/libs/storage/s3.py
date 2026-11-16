"""最小 S3 兼容客户端（httpx + SigV4），用于 SeaweedFS 等。"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from urllib.parse import quote

import httpx


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _sig_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), date)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


class S3Client:
    """Path-style：{endpoint}/{bucket}/{key}。"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        region: str = "us-east-1",
        timeout: float = 10.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _url(self, bucket: str, key: str = "") -> str:
        path = f"/{bucket}" + (f"/{quote(key, safe='/')}" if key else "")
        return self.endpoint + path

    def _headers(
        self,
        method: str,
        bucket: str,
        key: str,
        payload: bytes,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        now = dt.datetime.now(dt.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        host = self.endpoint.removeprefix("http://").removeprefix("https://")
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            **(extra or {}),
        }
        signed_headers = ";".join(sorted(k.lower() for k in headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers, key=str.lower))
        path = f"/{bucket}" + (f"/{quote(key, safe='/')}" if key else "")
        canonical = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        )
        signing_key = _sig_key(self.secret_key, date_stamp, self.region, "s3")
        sig = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256)
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig.hexdigest()}"
        )
        return headers

    async def ensure_bucket(self, bucket: str) -> None:
        r = await self._http.head(self._url(bucket), headers=self._headers("HEAD", bucket, "", b""))
        if r.status_code == 404:
            body = b""
            r = await self._http.put(self._url(bucket), content=body, headers=self._headers("PUT", bucket, "", body))
            if r.status_code not in (200, 201, 204):
                raise RuntimeError(f"create bucket {bucket}: HTTP {r.status_code} {r.text[:200]}")
        elif r.status_code not in (200, 204):
            # 部分实现 HEAD 未授权桶返回 403；尝试 PUT 幂等
            body = b""
            r2 = await self._http.put(self._url(bucket), content=body, headers=self._headers("PUT", bucket, "", body))
            if r2.status_code not in (200, 201, 204, 409):
                raise RuntimeError(f"ensure bucket {bucket}: HEAD {r.status_code} PUT {r2.status_code}")

    async def head(self, bucket: str, key: str) -> int:
        r = await self._http.head(self._url(bucket, key), headers=self._headers("HEAD", bucket, key, b""))
        return r.status_code

    async def get(self, bucket: str, key: str) -> bytes:
        r = await self._http.get(self._url(bucket, key), headers=self._headers("GET", bucket, key, b""))
        if r.status_code != 200:
            raise FileNotFoundError(f"s3://{bucket}/{key} HTTP {r.status_code}")
        return r.content

    async def put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        object_lock_mode: str | None = None,
        object_lock_retain_until: dt.datetime | None = None,
    ) -> None:
        extra: dict[str, str] = {"content-type": content_type}
        if object_lock_mode and object_lock_retain_until:
            # AWS S3 Object Lock 保留期头（合规桶；兼容实现可忽略或拒绝）
            until = object_lock_retain_until.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            extra["x-amz-object-lock-mode"] = object_lock_mode
            extra["x-amz-object-lock-retain-until-date"] = until
        headers = self._headers("PUT", bucket, key, body, extra)
        r = await self._http.put(self._url(bucket, key), content=body, headers=headers)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"put s3://{bucket}/{key}: HTTP {r.status_code} {r.text[:200]}")

    async def put_if_absent(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/json",
        object_lock_mode: str | None = None,
        object_lock_retain_until: dt.datetime | None = None,
    ) -> str:
        """WORM 语义：不存在则写入；已存在且内容相同则成功；内容不同则拒绝。"""
        code = await self.head(bucket, key)
        if code == 200:
            existing = await self.get(bucket, key)
            if existing != body:
                raise RuntimeError(f"WORM 冲突：s3://{bucket}/{key} 已存在且内容不同")
            return f"s3://{bucket}/{key}"
        if code not in (404, 403, 405):
            # 405/403：部分网关对 HEAD 支持差，直接 PUT 并接受 409/412
            pass
        else:
            await self.put(
                bucket,
                key,
                body,
                content_type=content_type,
                object_lock_mode=object_lock_mode,
                object_lock_retain_until=object_lock_retain_until,
            )
            return f"s3://{bucket}/{key}"
        # HEAD 异常时尝试 PUT
        try:
            await self.put(
                bucket,
                key,
                body,
                content_type=content_type,
                object_lock_mode=object_lock_mode,
                object_lock_retain_until=object_lock_retain_until,
            )
        except RuntimeError:
            existing = await self.get(bucket, key)
            if existing != body:
                raise
        return f"s3://{bucket}/{key}"
