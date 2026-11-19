"""Small value helpers for the migrated, host-independent state algorithms."""

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json


class StateError(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


def stamp(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("task timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def digest(value):
    def default(item):
        if is_dataclass(item):
            return {field.name: getattr(item, field.name) for field in fields(item) if field.metadata.get("encode", True)}
        if isinstance(item, datetime):
            return stamp(item)
        raise TypeError("unsupported task record value")
    encoded = json.dumps(value, default=default, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
