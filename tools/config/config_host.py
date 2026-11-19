"""Stdlib-only client for the root-owned configuration socket."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import sys
import time
import uuid

SOCKET_PATH = Path("/var/lib/astrbot/companion-config/socket")
AUDIT_PATH = Path("/var/log/companion-config.log")
MAX_COMMAND = 512
MAX_BODY = 32768
MAX_RESPONSE = 256 * 1024
BODY_TIMEOUT = 15
BACKEND_TIMEOUT = 15
OUTPUT_TIMEOUT = 10
AUDIT_LOCK_TIMEOUT = 3
AUDIT_MAX_BYTES = 1024 * 1024
AUDIT_GENERATIONS = 3
MAX_REVISION = 2**63 - 1

PREFIX = re.compile(r"[0-9a-f]{1,64}")
FIELD = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
REVISION = re.compile(r"0|[1-9][0-9]{0,18}")
DIGEST = re.compile(r"[0-9a-f]{64}")
REVERT_ID = re.compile(r"[0-9a-f]{32}")
HISTORY_COUNT = re.compile(r"[1-9][0-9]?|100")
BACKEND_ERRORS = frozenset({
    "stale_revision", "digest_mismatch", "invalid_value", "value_too_large",
    "field_not_allowed", "field_read_only", "target_unavailable", "ambiguous_target",
    "change_unavailable", "configuration_unavailable", "response_too_large",
    "field_scope_not_supported", "invalid_configuration_value", "edit_affects_other_fields",
    "invalid_command", "old_value_too_large",
})


class ConfigError(Exception):
    """Only constructed with a constant, safe error code."""


def command_args(original):
    if not 0 < len(original) <= MAX_COMMAND or not re.fullmatch(r"[A-Za-z0-9_. -]+", original):
        raise ConfigError("invalid_command")
    args = original.split()
    action = args[0] if args else ""
    if (action in ("get", "set") and len(args) == (3 if action == "get" else 6)
            and PREFIX.fullmatch(args[1]) and len(args[2]) <= 128 and FIELD.fullmatch(args[2])):
        if action == "get" or (REVISION.fullmatch(args[3]) and int(args[3]) <= MAX_REVISION
                               and DIGEST.fullmatch(args[4]) and DIGEST.fullmatch(args[5])):
            return args
    if action == "targets" and len(args) == 1:
        return args
    if action == "history" and (len(args) == 1 or len(args) == 2 and HISTORY_COUNT.fullmatch(args[1])):
        return args
    if action == "revert" and len(args) == 2 and REVERT_ID.fullmatch(args[1]):
        return args
    raise ConfigError("invalid_command")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _read_body(fd):
    deadline = time.monotonic() + BODY_TIMEOUT
    body = bytearray()
    try:
        while True:
            if not select.select([fd], [], [], _remaining(deadline))[0]:
                raise TimeoutError
            chunk = os.read(fd, min(8192, MAX_BODY + 1 - len(body)))
            if not chunk:
                return base64.b64encode(body).decode("ascii")
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise ConfigError("body_too_large")
    except TimeoutError:
        raise ConfigError("body_timeout") from None
    except OSError:
        raise ConfigError("body_unavailable") from None


@contextmanager
def _open_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _checked_socket_path(path):
    with _open_directory(path.parent) as directory:
        parent = os.fstat(directory)
        if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ConfigError("unsafe_backend")
        endpoint = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        mode = stat.S_IMODE(endpoint.st_mode)
        if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != 0 or mode != 0o600:
            raise ConfigError("unsafe_backend")
        # Linux resolves this alias through the validated directory fd, even
        # if the original directory path is moved between stat and connect.
        yield f"/proc/self/fd/{directory}/{path.name}"


def _require_root_peer(connection):
    credentials = struct.Struct("=iII")
    _, uid, _ = credentials.unpack(
        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, credentials.size)
    )
    if uid != 0:
        raise ConfigError("unsafe_backend")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json_integer(raw):
    if len(raw) > 1024:
        raise ValueError
    return int(raw)


def _invalid_constant(_):
    raise ValueError


def _check_response_depth(value):
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 8:
            raise ValueError
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _decode_response(frame):
    try:
        response = json.loads(frame.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_int=_json_integer, parse_constant=_invalid_constant)
        if isinstance(response, dict) and response.get("ok") is True and response.keys() == {"ok", "result"}:
            _check_response_depth(response["result"])
            output = (json.dumps(response["result"], ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")) + "\n").encode("utf-8")
            if len(output) <= MAX_RESPONSE:
                return output
        elif (isinstance(response, dict) and response.get("ok") is False
              and response.keys() == {"ok", "error"} and isinstance(response["error"], str)):
            code = response["error"]
            raise ConfigError(code if code in BACKEND_ERRORS else "backend_rejected")
    except (ValueError, RecursionError, OverflowError):
        pass
    raise ConfigError("invalid_response")


def _read_response(connection, deadline):
    frame = bytearray()
    while len(frame) < MAX_RESPONSE:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(8192, MAX_RESPONSE - len(frame)))
        if not chunk:
            break
        if b"\n" in chunk:
            line, _, extra = chunk.partition(b"\n")
            if not extra:
                frame.extend(line)
                return _decode_response(frame)
            break
        frame.extend(chunk)
    raise ConfigError("invalid_response")


def _exchange(path, args, body):
    request = (json.dumps({"args": args, "body": body}, separators=(",", ":")) + "\n").encode("utf-8")
    deadline = time.monotonic() + BACKEND_TIMEOUT
    try:
        with _checked_socket_path(path) as address, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_remaining(deadline))
            connection.connect(address)
            _require_root_peer(connection)
            connection.settimeout(_remaining(deadline))
            connection.sendall(request)
            return _read_response(connection, deadline)
    except TimeoutError:
        raise ConfigError("backend_timeout") from None
    except OSError:
        raise ConfigError("backend_unavailable") from None


def _validate_audit_file(info, owner):
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != owner or info.st_nlink != 1):
        raise OSError("unsafe audit file")


@contextmanager
def _open_audit_file(directory, name, owner):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        _validate_audit_file(os.fstat(fd), owner)
        yield fd
    finally:
        os.close(fd)


def _lock_audit(fd):
    import fcntl

    deadline = time.monotonic() + AUDIT_LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(min(0.05, _remaining(deadline)))


def _rotate(directory, name, owner):
    generations = [name, *(f"{name}.{n}" for n in range(1, AUDIT_GENERATIONS + 1))]
    existing = set()
    for member in generations:
        try:
            info = os.stat(member, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _validate_audit_file(info, owner)
        existing.add(member)
    for older, newer in reversed(list(zip(generations, generations[1:]))):
        if older in existing:
            os.replace(older, newer, src_dir_fd=directory, dst_dir_fd=directory)


def _append_audit(directory, fd, line):
    if os.write(fd, line) != len(line):
        raise OSError("short audit write")
    os.fsync(fd)
    os.fsync(directory)


def _audit(path, request_id, action, outcome):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{timestamp} {request_id} {action} {outcome}\n".encode("ascii")
    try:
        owner = os.geteuid()
        with _open_directory(path.parent) as directory:
            parent = os.fstat(directory)
            if parent.st_uid != owner or parent.st_mode & 0o022:
                raise OSError("unsafe audit directory")
            with _open_audit_file(directory, path.name + ".lock", owner) as lock:
                _lock_audit(lock)
                with _open_audit_file(directory, path.name, owner) as log:
                    if os.fstat(log).st_size + len(line) <= AUDIT_MAX_BYTES:
                        _append_audit(directory, log, line)
                        return
                _rotate(directory, path.name, owner)
                with _open_audit_file(directory, path.name, owner) as log:
                    _append_audit(directory, log, line)
    except OSError:
        raise ConfigError("audit_unavailable") from None


def _dispatch(original, stdin_fd, *, socket_path=SOCKET_PATH, audit_path=AUDIT_PATH):
    request_id = uuid.uuid4().hex
    try:
        args = command_args(original)
    except ConfigError:
        _audit(audit_path, request_id, "invalid-command", "rejected")
        raise

    action = args[0]
    _audit(audit_path, request_id, action, "accepted")
    try:
        body = _read_body(stdin_fd) if action == "set" else None
        result = _exchange(socket_path, args, body)
    except Exception:
        _audit(audit_path, request_id, action, "error")
        raise
    _audit(audit_path, request_id, action, "success")
    return result


def _write_output(fd, data):
    deadline = time.monotonic() + OUTPUT_TIMEOUT
    blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    try:
        pending = memoryview(data)
        while pending:
            if not select.select([], [fd], [], _remaining(deadline))[1]:
                raise TimeoutError
            try:
                pending = pending[os.write(fd, pending):]
            except BlockingIOError:
                continue
    finally:
        os.set_blocking(fd, blocking)


def main():
    try:
        if len(sys.argv) != 1:
            raise ConfigError("invalid_invocation")
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ConfigError("root_required")
        os.umask(0o077)
        result = _dispatch(os.environ.get("SSH_ORIGINAL_COMMAND", ""), 0)
        try:
            _write_output(1, result)
        except OSError:
            raise ConfigError("output_unavailable") from None
    except Exception as error:
        code = str(error) if isinstance(error, ConfigError) else "internal_error"
        try:
            _write_output(2, f"companion-config: {code}\n".encode("ascii"))
        except (OSError, ValueError):
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
