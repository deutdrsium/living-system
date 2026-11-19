"""Target v1 wire digests; deliberately independent of legacy task digests."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json


def wire(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("contract timestamp requires a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("contract keys must be strings")
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ValueError("unsupported contract value")


def plain(value):
    return json.loads(json.dumps(wire(value), ensure_ascii=False, allow_nan=False))


def params_digest(namespace, request):
    document = wire(request)
    document["scope"] = {key: document["scope"][key] for key in ("owner_key", "context_key")}
    document.pop("params_digest", None)
    document.pop({"model_task": "task_id", "platform_action": "operation_id",
                  "publish_asset": "operation_id", "authorized_intent": "operation_id"}.get(namespace, "params_digest"), None)
    encoded = json.dumps({"namespace": namespace, "request": document}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
