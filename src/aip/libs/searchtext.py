"""发现检索文本：NFD 折叠、CJK 双字、快照物化 search_text（architecture.md 10.3）。

数据面发现模式在本地快照上检索（控制面宕机仍可用）；默认 alpine PG 无 zhparser，
中文靠物化字段 + 双字匹配。生产可选 zhparser 见 docs/ops/search-index.md。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def fold(text: str) -> str:
    """大小写折叠 + 去掉结合音标（越文 unaccent 等价）。"""
    norm = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")


def cjk_bigrams(segment: str) -> list[str]:
    """CJK 连续段 → 非重叠双字；奇数末字单独保留。"""
    if not segment:
        return []
    if len(segment) == 1:
        return [segment]
    out: list[str] = []
    i = 0
    while i + 1 < len(segment):
        out.append(segment[i : i + 2])
        i += 2
    if i < len(segment):
        out.append(segment[i])
    return out


def query_tokens(query: str) -> list[str]:
    """查询分词：空白分词；CJK 段拆非重叠双字。"""
    q = fold(query).strip()
    if not q:
        return []
    tokens: list[str] = []
    pos = 0
    for m in _CJK_RE.finditer(q):
        latin = q[pos : m.start()].split()
        tokens.extend(t for t in latin if t)
        tokens.extend(cjk_bigrams(m.group(0)))
        pos = m.end()
    tokens.extend(t for t in q[pos:].split() if t)
    return tokens


def build_search_text(release: dict[str, Any]) -> str:
    """从 release + spec 生成已 fold 的检索文本（写入快照 search_text）。"""
    spec = release.get("spec") or {}
    parts = [
        str(release.get("capability_id") or ""),
        str(spec.get("title") or ""),
        str(spec.get("name") or release.get("name") or ""),
        str(spec.get("description") or ""),
        str(spec.get("usage_notes") or ""),
        str(spec.get("domain") or ""),
    ]
    return fold(" ".join(parts))


def release_search_blob(release: dict[str, Any]) -> str:
    """优先用物化 search_text；旧快照回退现算。"""
    cached = release.get("search_text")
    if isinstance(cached, str) and cached.strip():
        return cached if cached == fold(cached) else fold(cached)
    return build_search_text(release)


def score_release(release: dict[str, Any], query: str) -> int:
    """命中分：id 前缀 > title > 全文；0 表示未命中。"""
    q = fold(query).strip()
    if not q:
        return 0
    cap = fold(str(release.get("capability_id") or ""))
    spec = release.get("spec") or {}
    title = fold(str(spec.get("title") or spec.get("name") or release.get("name") or ""))
    blob = release_search_blob(release)
    tokens = query_tokens(q)
    if not tokens:
        return 0
    if not (q in blob or all(t in blob for t in tokens)):
        return 0
    score = 10
    if cap.startswith(q) or any(cap.startswith(t) for t in tokens):
        score += 100
    if q in title or any(t in title for t in tokens):
        score += 40
    if q in cap or any(t in cap for t in tokens):
        score += 20
    return score
