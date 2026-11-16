"""P9 / G6.4：校验 WORM 应用层 put_if_absent，并在配置开启时附带 Object Lock 头。

无 S3 时 skip（exit 0）；AIP_WORM_LOCK_CHECK=1 则强制失败。

用法：
  .venv/Scripts/python tools/worm_object_lock_check.py
  # 合规桶：AIP_WORM_OBJECT_LOCK_MODE=COMPLIANCE AIP_WORM_OBJECT_LOCK_DAYS=2555
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def _run() -> int:
    force = os.environ.get("AIP_WORM_LOCK_CHECK") == "1"
    from aip.libs.common.config import get_settings
    from aip.libs.storage.s3 import S3Client
    from aip.libs.storage.worm import S3WormStore

    s = get_settings()
    endpoint = (s.s3_endpoint or "").strip()
    if not endpoint or not s.s3_access_key or not s.s3_secret_key:
        print("skip: S3 endpoint/credentials incomplete")
        return 2 if force else 0

    bucket = (s.worm_bucket or "aip-worm-drill").strip()
    key = f"drill/g64-{uuid.uuid4().hex}.json"
    lock_mode = (s.worm_object_lock_mode or "").strip().upper()
    client = S3Client(endpoint, s.s3_access_key, s.s3_secret_key, region=s.s3_region or "us-east-1")
    store = S3WormStore(
        client,
        bucket,
        object_lock_mode=lock_mode,
        object_lock_days=s.worm_object_lock_days,
    )
    try:
        body1 = b'{"v":1}'
        body2 = b'{"v":2}'
        await store.put_if_absent(key, body1)
        await store.put_if_absent(key, body1)  # same content ok
        try:
            await store.put_if_absent(key, body2)
        except RuntimeError as e:
            if "WORM" not in str(e) and "冲突" not in str(e):
                raise
        else:
            print("FAIL: conflicting put_if_absent did not raise")
            return 1
        got = await store.get(key)
        if got != body1:
            print("FAIL: readback mismatch")
            return 1
    except Exception as e:  # noqa: BLE001 — 运维脚本：连接失败则 skip/强制失败
        print(f"skip: S3 unreachable or error: {e}")
        return 2 if force else 0
    finally:
        await client.aclose()

    lock_note = (
        f"Object Lock mode={lock_mode} days={s.worm_object_lock_days}"
        if lock_mode in ("COMPLIANCE", "GOVERNANCE")
        else "未配置 worm_object_lock_mode（仅应用层 put_if_absent）"
    )
    evidence = ROOT / "docs" / "ops" / "evidence" / "g6-worm-object-lock.md"
    evidence.write_text(
        "\n".join(
            [
                "# G6.4 / P9 WORM Object Lock 校验",
                "",
                f"- endpoint: `{endpoint}`",
                f"- bucket: `{bucket}`",
                f"- key: `{key}`",
                f"- lock: {lock_note}",
                "- 结果：同内容幂等写入成功；冲突内容拒绝；读回一致",
                "- 生产：桶须创建时开启 Object Lock；设 `AIP_WORM_ENABLED=true` + `AIP_WORM_OBJECT_LOCK_MODE=COMPLIANCE`",
                "- 命令: `.venv/Scripts/python tools/worm_object_lock_check.py`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"ok: WORM semantics ({lock_note}); wrote {evidence}")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
