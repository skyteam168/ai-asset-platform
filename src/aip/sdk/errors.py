"""消费方 SDK 错误（解析数据面 RFC 9457 Problem Details）。"""

from __future__ import annotations

from typing import Any


class SdkError(Exception):
    def __init__(self, code: str, detail: str = "", *, status: int = 0, body: dict[str, Any] | None = None) -> None:
        self.code = code
        self.detail = detail
        self.status = status
        self.body = body or {}
        super().__init__(f"{code}: {detail}" if detail else code)
