#!/usr/bin/env python3
"""用用户断言换 access_token 的最小示例（jwt-bearer）。

环境变量：
  AIP_BASE_URL          控制面根 URL，如 http://127.0.0.1:18081
  AIP_CLIENT_ID         已登记的 client_id
  AIP_SUBJECT           外部主体（工号）
  AIP_AUDIENCE          通常为 mcp_resource（模式 A 可用 token_endpoint_aud）
  AIP_PRIVATE_PEM_PATH  消费方私钥 PEM 路径
  AIP_KID               与平台登记的 JWK kid 一致
  AIP_STATIC_TOKEN      可选；若设置则用静态令牌作 client_assertion 换令牌

安全：禁止把完整 access_token / 断言写入日志；本脚本仅打印前缀。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# 允许直接运行而未 pip install
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aip_consumer_kit import sign_client_assertion, sign_user_assertion  # noqa: E402


def main() -> None:
    base = os.environ["AIP_BASE_URL"].rstrip("/")
    client_id = os.environ["AIP_CLIENT_ID"]
    subject = os.environ["AIP_SUBJECT"]
    audience = os.environ["AIP_AUDIENCE"]
    kid = os.environ["AIP_KID"]
    pem = Path(os.environ["AIP_PRIVATE_PEM_PATH"]).read_text(encoding="utf-8")

    assertion = sign_user_assertion(
        pem, client_id=client_id, subject=subject, audience=audience, kid=kid
    )
    data: dict[str, str] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
        "client_id": client_id,
    }
    static = os.environ.get("AIP_STATIC_TOKEN")
    if static:
        data["client_assertion"] = static
        data["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    else:
        token_aud = os.environ.get("AIP_TOKEN_ENDPOINT_AUD", f"{base}/oauth/token")
        data["client_assertion"] = sign_client_assertion(
            pem, client_id=client_id, audience=token_aud, kid=kid
        )
        data["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"{base}/oauth/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — 联调脚本，URL 来自环境变量
        payload = json.loads(resp.read().decode())
    token = payload["access_token"]
    print(f"access_token prefix: {token[:12]}… (len={len(token)})")
    print("勿将完整令牌写入日志或提交到版本库。")


if __name__ == "__main__":
    main()
