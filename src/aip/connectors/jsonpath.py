"""jsonpath-ng 的类型化包装（该库没有类型注解）。表达式编译结果缓存。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from jsonpath_ng.ext import parse as _parse  # type: ignore[attr-defined]


@lru_cache(maxsize=1024)
def _compiled(path: str) -> Any:
    return _parse(path)  # type: ignore[no-untyped-call]


def find(path: str, doc: Any) -> list[Any]:
    return [m.value for m in _compiled(path).find(doc)]


def first(path: str, doc: Any) -> Any:
    found = find(path, doc)
    return found[0] if found else None
