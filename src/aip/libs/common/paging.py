"""列表分页响应（与 OpenAPI page() 形状一致）。"""

from __future__ import annotations

from pydantic import BaseModel


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
