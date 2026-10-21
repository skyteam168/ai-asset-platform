"""规则输入脱敏（architecture.md 8.6；Presidio 另片）。"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
_SK = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{20,}")


def redact_text(text: str) -> str:
    s = _EMAIL.sub("[REDACTED_EMAIL]", text)
    s = _PHONE.sub("[REDACTED_PHONE]", s)
    s = _ID_CARD.sub("[REDACTED_ID]", s)
    s = _SK.sub("[REDACTED_SECRET]", s)
    s = _BEARER.sub("Bearer [REDACTED_TOKEN]", s)
    return s


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
