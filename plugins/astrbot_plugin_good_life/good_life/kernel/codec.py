"""Strict contract-v1 wire codec for the opt-in kernel, never legacy records."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
import hashlib
import json
import math
from types import MappingProxyType, UnionType
from typing import Union, get_args, get_origin, get_type_hints

from ..contracts.models import ContractError, ReasonCode


class KernelError(ValueError):
    def __init__(self, reason_code, message, *, instance_id=None, operation_id=None):
        super().__init__(message)
        self.error = ContractError(reason_code, message, instance_id, operation_id)
        self.code = reason_code.value


def require(condition, message, code=ReasonCode.INVALID_CONFIG):
    if not condition:
        raise KernelError(code, message)


def name(value):
    require(type(value) is str and bool(value.strip()) and value == value.strip() and len(value) <= 256,
            "identifiers must be nonempty strings of at most 256 characters")
    return value


def moment(value):
    require(isinstance(value, datetime) and value.utcoffset() is not None, "time must be aware")
    return value.astimezone(timezone.utc)


def integer(value, minimum=0):
    require(type(value) is int and value >= minimum, "invalid integer or unit")
    return value


def wire(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return moment(value).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        require(all(type(key) is str for key in value), "JSON keys must be strings")
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [wire(item) for item in value]
    require(value is None or type(value) in (bool, int, float, str), "unsupported JSON value")
    require(type(value) is not float or math.isfinite(value), "nonfinite JSON number")
    return value


def encode(value):
    return json.dumps(wire(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def freeze(value):
    if isinstance(value, Mapping):
        require(all(type(key) is str for key in value), "JSON keys must be strings")
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    wire(value)
    return value


@lru_cache(maxsize=None)
def _hints(record_type):
    return get_type_hints(record_type)


def restore(record_type, value):
    if hasattr(record_type, "__supertype__"):
        return record_type(restore(record_type.__supertype__, value))
    if record_type is datetime:
        require(type(value) is str, "invalid timestamp")
        try:
            return moment(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as error:
            raise KernelError(ReasonCode.INVALID_CONFIG, "invalid timestamp") from error
    if isinstance(record_type, type) and issubclass(record_type, Enum):
        return record_type(value)
    if is_dataclass(record_type):
        require(type(value) is dict and set(value) == {field.name for field in fields(record_type)},
                "record fields do not match contract")
        hints = _hints(record_type)
        return record_type(**{key: restore(hints[key], item) for key, item in value.items()})
    origin, args = get_origin(record_type), get_args(record_type)
    if origin in (Union, UnionType):
        if value is None and type(None) in args:
            return None
        nonnull = [arg for arg in args if arg is not type(None)]
        if len(nonnull) == 1:
            return restore(nonnull[0], value)
        return freeze(value)  # Only JSONValue uses a non-optional union here.
    if origin is tuple:
        require(type(value) is list, "expected array")
        return tuple(restore(args[0], item) for item in value)
    if origin is Mapping:
        require(type(value) is dict, "expected object")
        return MappingProxyType({key: restore(args[1], item) if args else freeze(item) for key, item in value.items()})
    if record_type in (str, int, float, bool):
        require(type(value) is record_type, "invalid primitive type")
    return freeze(value)


def decode(record_type, value):
    try:
        return restore(record_type, json.loads(value))
    except KernelError:
        raise
    except (TypeError, ValueError, KeyError) as error:
        raise KernelError(ReasonCode.INVALID_CONFIG, "invalid contract record") from error


def owned(record):
    """Detach mutable aliases and validate declared structural types at ingress."""
    return decode(type(record), encode(record))
