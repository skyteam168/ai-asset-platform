"""Skill 包对象存储：SeaweedFS（S3 兼容）双写 / 双读（architecture.md 13、10.4）。"""

from __future__ import annotations

from aip.libs.common.config import Settings, get_settings
from aip.libs.storage.s3 import S3Client


def object_key_for(company_id: str, version_id: str, content_sha256: str) -> str:
    return f"skills/{company_id}/{version_id}/{content_sha256}.zip"


def s3_client(settings: Settings | None = None) -> S3Client:
    s = settings or get_settings()
    return S3Client(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, region=s.s3_region)


async def put_skill_zip(
    body: bytes,
    *,
    company_id: str,
    version_id: str,
    content_sha256: str,
    settings: Settings | None = None,
) -> str | None:
    """启用对象存储时写入桶并返回 object_key；未启用返回 None。"""
    s = settings or get_settings()
    if not s.skill_object_storage:
        return None
    key = object_key_for(company_id, version_id, content_sha256)
    client = s3_client(s)
    try:
        await client.ensure_bucket(s.skill_bucket)
        await client.put(s.skill_bucket, key, body, content_type="application/zip")
    finally:
        await client.aclose()
    return key


async def get_skill_zip(object_key: str, *, settings: Settings | None = None) -> bytes:
    s = settings or get_settings()
    client = s3_client(s)
    try:
        return await client.get(s.skill_bucket, object_key)
    finally:
        await client.aclose()
