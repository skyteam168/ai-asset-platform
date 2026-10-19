"""标识与时间：主键用 UUIDv7（时间有序），时间一律 UTC。"""

from __future__ import annotations

import datetime as dt
import uuid

import uuid_utils


def new_id() -> uuid.UUID:
    return uuid.UUID(str(uuid_utils.uuid7()))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
