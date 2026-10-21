"""从 Tool Spec 推断可能的 pii_fields（建议，不强制写入）。"""

from __future__ import annotations

import re
from typing import Any

# 字段名启发式（中 / 英 / 越常见）
_NAME_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(email|e_mail|mail)"), "EMAIL"),
    (re.compile(r"(?i)(phone|mobile|tel|cellphone|sdt|dien_thoai)"), "PHONE"),
    (re.compile(r"(?i)(id_card|national_id|citizen_id|cccd|cmnd|身份证|身份证号)"), "ID_CARD"),
    (re.compile(r"(?i)(passport|护照)"), "PASSPORT"),
    (re.compile(r"(?i)(bank_account|iban|卡号)"), "FINANCIAL"),
    (re.compile(r"(?i)(password|secret|token|api_key)"), "SECRET"),
]

_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_CN = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
_PHONE_VN = re.compile(r"(?<!\d)(?:\+?84|0)(?:3|5|7|8|9)\d{8}(?!\d)")
_ID_CN = re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")


def _walk_props(schema: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """返回 (json_pointer_ish_field, description_or_name)。"""
    out: list[tuple[str, str]] = []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return out
    for name, sub in props.items():
        path = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(sub, dict):
            desc = str(sub.get("description") or name)
            out.append((path, desc))
            out.extend(_walk_props(sub, path))
    return out


def _rule_scan_text(text: str) -> list[str]:
    kinds: list[str] = []
    if _EMAIL.search(text):
        kinds.append("EMAIL")
    if _PHONE_CN.search(text) or _PHONE_VN.search(text):
        kinds.append("PHONE")
    if _ID_CN.search(text):
        kinds.append("ID_CARD")
    return kinds


def _presidio_scan(text: str) -> list[str]:
    """可选 Presidio；未安装或失败则空。"""
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        engine = AnalyzerEngine()
        results = engine.analyze(text=text, language="en")
        mapping = {
            "EMAIL_ADDRESS": "EMAIL",
            "PHONE_NUMBER": "PHONE",
            "PERSON": "PERSON",
            "LOCATION": "LOCATION",
            "NRP": "ID_CARD",
        }
        kinds: list[str] = []
        for r in results:
            ent = mapping.get(str(r.entity_type))
            if ent:
                kinds.append(ent)
        return kinds
    except Exception:
        return []


def suggest_pii_fields(spec: dict[str, Any]) -> list[dict[str, str]]:
    """
    返回建议列表：[{field, kind, source}]。
    source: name_hint | text_rule | presidio
    已在 spec.pii_fields 中的字段不再建议。
    """
    declared = {str(x) for x in (spec.get("pii_fields") or [])}
    found: dict[str, dict[str, str]] = {}

    def add(field: str, kind: str, source: str) -> None:
        if field in declared:
            return
        key = f"{field}:{kind}"
        if key not in found:
            found[key] = {"field": field, "kind": kind, "source": source}

    for path, desc in _walk_props(spec.get("input_schema") or {}):
        for pat, kind in _NAME_HINTS:
            if pat.search(path) or pat.search(desc):
                add(path, kind, "name_hint")
        for kind in _rule_scan_text(desc):
            add(path, kind, "text_rule")
        for kind in _presidio_scan(desc):
            add(path, kind, "presidio")

    blob = " ".join(
        [
            str(spec.get("description") or ""),
            str(spec.get("usage_notes") or ""),
            " ".join(str(x) for x in (spec.get("not_for") or [])),
        ]
    )
    for kind in _rule_scan_text(blob):
        add("_description", kind, "text_rule")
    for kind in _presidio_scan(blob[:4000]):
        add("_description", kind, "presidio")

    return sorted(found.values(), key=lambda x: (x["field"], x["kind"]))
