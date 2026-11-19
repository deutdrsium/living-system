"""Offline checks for the config forced-command client and sudoers policy."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools/config"
spec = importlib.util.spec_from_file_location("config_host", TOOLS / "config_host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
DIGEST = "a" * 64
REVERT_ID = "b" * 32
EDIT_TOKEN = "c" * 64
LINUX = sys.platform == "linux"


@pytest.mark.parametrize("command, expected", [
    ("targets", ["targets"]),
    ("get b83 role.life.enabled", ["get", "b83", "role.life.enabled"]),
    (f"get {DIGEST} role.profile.text_v2", ["get", DIGEST, "role.profile.text_v2"]),
    (f"set a role.life.enabled 0 {EDIT_TOKEN} {DIGEST}", ["set", "a", "role.life.enabled", "0", EDIT_TOKEN, DIGEST]),
    (f"set a role.life.enabled 9223372036854775807 {EDIT_TOKEN} {DIGEST}",
     ["set", "a", "role.life.enabled", "9223372036854775807", EDIT_TOKEN, DIGEST]),
    ("history", ["history"]),
    ("history 1", ["history", "1"]),
    ("history 100", ["history", "100"]),
    (f"revert {REVERT_ID}", ["revert", REVERT_ID]),
])
def test_command_grammar(command, expected):
    assert host.command_args(command) == expected


@pytest.mark.parametrize("command", [
    "", " ", "bash", "targets extra", "targets; history", "targets\nhistory", "targets\t", "targets\x00",
    "get a field extra", "get A field", f"get {DIGEST}a field", "get a .field", "get a field..id",
    "get a field/setting", "get a 字段", "get a field." + "x" * 128, "get a 'field'", " " * 512 + "history",
    f"set a field.id -1 {EDIT_TOKEN} {DIGEST}", f"set a field.id +1 {EDIT_TOKEN} {DIGEST}",
    f"set a field.id 00 {EDIT_TOKEN} {DIGEST}", f"set a field.id 9223372036854775808 {EDIT_TOKEN} {DIGEST}",
    f"set a field.id 1 {EDIT_TOKEN} {DIGEST.upper()}", f"set a field.id 1 {EDIT_TOKEN} {DIGEST[:-1]}",
    f"set a field.id 1 {DIGEST}", f"set a field.id 1 {EDIT_TOKEN.upper()} {DIGEST}",
    f"set a field.id 1 {EDIT_TOKEN[:-1]} {DIGEST}", f"set a field.id 1 {EDIT_TOKEN} {DIGEST} extra",
    "history 0", "history 101", "history 1 2", f"revert {REVERT_ID[:-1]}", f"revert {REVERT_ID.upper()}",
])
def test_rejects_invalid_command_without_echoing_input(command):
    with pytest.raises(host.ConfigError, match="^invalid_command$"):
        host.command_args(command)


def test_dispatch_passes_only_validated_tokens_and_audits_metadata(monkeypatch, tmp_path):
    audits = []
    requests = []
    path = tmp_path / "log"
    endpoint = tmp_path / "socket"

    def audit(log, request_id, action, status):
        assert log == path
        audits.append((request_id, action, status))

    def exchange(socket_path, args, body):
        assert socket_path == endpoint
        requests.append((args, body))
        if args[0] == "revert":
            raise host.ConfigError("backend_rejected")
        return b"null\n"

    def body(fd):
        assert fd == 5
        return base64.b64encode(b"private-value").decode("ascii")

    monkeypatch.setattr(host, "_audit", audit)
    monkeypatch.setattr(host, "_exchange", exchange)
    monkeypatch.setattr(host, "_read_body", body)

    def dispatch(command, fd=-1):
        return host._dispatch(command, fd, socket_path=endpoint, audit_path=path)

    with pytest.raises(host.ConfigError, match="^invalid_command$"):
        dispatch("invalid private-input")
    assert dispatch("get b8 role.life.enabled") == b"null\n"
    assert dispatch(f"set b8 role.life.enabled 4 {EDIT_TOKEN} {DIGEST}", 5) == b"null\n"
    with pytest.raises(host.ConfigError, match="^backend_rejected$"):
        dispatch(f"revert {REVERT_ID}")
    assert requests == [
        (["get", "b8", "role.life.enabled"], None),
        (["set", "b8", "role.life.enabled", "4", EDIT_TOKEN, DIGEST],
         base64.b64encode(b"private-value").decode("ascii")),
        (["revert", REVERT_ID], None),
    ]
    assert [(action, status) for _, action, status in audits] == [
        ("invalid-command", "rejected"), ("get", "accepted"), ("get", "success"),
        ("set", "accepted"), ("set", "success"), ("revert", "accepted"), ("revert", "error"),
    ]
    assert len({audits[n][0] for n in (0, 1, 3, 5)}) == 4
    assert all(audits[n][0] == audits[n + 1][0] for n in (1, 3, 5))


def test_body_is_bounded_and_preserves_bytes(monkeypatch, tmp_path):
    # Windows select() only handles sockets. The body reader still consumes real file data.
    monkeypatch.setattr(host, "select", SimpleNamespace(select=lambda readers, *_: (readers, [], [])))
    data = bytes(range(256)) * (host.MAX_BODY // 256)
    path = tmp_path / "stdin"
    path.write_bytes(data)
    with path.open("rb") as stream:
        assert base64.b64decode(host._read_body(stream.fileno()), validate=True) == data
    path.write_bytes(data + b"x")
    with path.open("rb") as stream, pytest.raises(host.ConfigError, match="^body_too_large$"):
        host._read_body(stream.fileno())


def test_body_deadline_is_total_even_with_incremental_input(monkeypatch):
    clock = iter([0, host.BODY_TIMEOUT / 2, host.BODY_TIMEOUT])
    monkeypatch.setattr(host, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(host, "select", SimpleNamespace(select=lambda readers, *_: (readers, [], [])))
    monkeypatch.setattr(host, "os", SimpleNamespace(read=lambda *_: b"x"))
    with pytest.raises(host.ConfigError, match="^body_timeout$"):
        host._read_body(0)


class ResponseStream:
    def __init__(self, data):
        self.data = io.BytesIO(data)

    def settimeout(self, timeout):
        assert timeout > 0

    def recv(self, size):
        return self.data.read(size)


def read_response(data):
    return host._read_response(ResponseStream(data), time.monotonic() + 2)


def test_response_output_is_only_result_json():
    assert read_response('{"ok":true,"result":["值",null,true]}\n'.encode()) == '["值",null,true]\n'.encode()


@pytest.mark.parametrize("error, code", [("stale_revision", "stale_revision"),
                                          ("secret=private-diagnostic", "backend_rejected")])
def test_only_allowlisted_backend_codes_are_forwarded(error, code):
    response = (json.dumps({"ok": False, "error": error}) + "\n").encode("utf-8")
    with pytest.raises(host.ConfigError, match=f"^{code}$"):
        read_response(response)


@pytest.mark.parametrize("data", [
    b'{"ok":true,"result":null}', b'{"ok":true,"result":null}\nmore',
    b'{"ok":true,"result":null,"error":"x"}\n', b'{"ok":true}\n', b'{"ok":1,"result":null}\n',
    b'{"ok":false,"error":{"secret":true}}\n', b'{"ok":false,"result":null}\n',
    b'{"ok":false,"ok":true,"result":null}\n', b'{"ok":true,"result":{"x":1,"x":2}}\n',
    b'{"ok":true,"result":NaN}\n', b'{"ok":true,"result":1e99999}\n', b'{"ok":true,"result":"\xff"}\n',
    b'{"ok":true,"result":"\\ud800"}\n', b'{"ok":true,"result":' + b"[" * 2000 + b"]" * 2000 + b"}\n",
])
def test_invalid_response_is_rejected(data):
    with pytest.raises(host.ConfigError, match="^invalid_response$"):
        read_response(data)


def test_response_cap_includes_newline():
    start, end = b'{"ok":true,"result":"', b'"}\n'
    result_size = host.MAX_RESPONSE - len(start) - len(end)
    assert read_response(start + b"x" * result_size + end) == b'"' + b"x" * result_size + b'"\n'
    with pytest.raises(host.ConfigError, match="^invalid_response$"):
        read_response(start + b"x" * (result_size + 1) + end)


@pytest.mark.parametrize("uid", [0, 1000])
def test_peer_credentials_require_uid_zero(monkeypatch, uid):
    monkeypatch.setattr(host.socket, "SO_PEERCRED", 17, raising=False)

    class Peer:
        def getsockopt(self, level, option, size):
            assert (level, option, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return struct.pack("=iII", 123, uid, 0)

    if uid == 0:
        host._require_root_peer(Peer())
    else:
        with pytest.raises(host.ConfigError, match="^unsafe_backend$"):
            host._require_root_peer(Peer())


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="requires a root-owned Linux UNIX socket")
def test_root_socket_exchange_uses_single_json_request(tmp_path):
    directory = tmp_path / "ipc"
    directory.mkdir(mode=0o700)
    endpoint = directory / "socket"
    received = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(endpoint))
        server.listen()
        server.settimeout(2)
        endpoint.chmod(0o666)
        with pytest.raises(host.ConfigError, match="^unsafe_backend$"):
            host._exchange(endpoint, ["targets"], None)
        endpoint.chmod(0o600)
        symlink = directory / "link"
        symlink.symlink_to(endpoint)
        with pytest.raises(host.ConfigError, match="^unsafe_backend$"):
            host._exchange(symlink, ["targets"], None)

        def respond():
            connection, _ = server.accept()
            with connection, connection.makefile("rb") as stream:
                connection.settimeout(2)
                received.append(stream.readline(64 * 1024))
                connection.sendall(b'{"ok":true,"result":{"updated":true}}\n')

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        try:
            body = base64.b64encode('"文本"'.encode()).decode("ascii")
            args = ["set", "aa", "role.life.enabled", "0", EDIT_TOKEN, DIGEST]
            assert host._exchange(endpoint, args, body) == b'{"updated":true}\n'
        finally:
            worker.join(timeout=3)
        assert not worker.is_alive()
    assert len(received) == 1 and received[0].endswith(b"\n")
    assert json.loads(received[0]) == {"args": args, "body": body}


@pytest.mark.skipif(not LINUX, reason="requires Linux audit locking and file modes")
def test_audit_rotation_is_private_and_retains_three_generations(tmp_path, monkeypatch):
    path = tmp_path / "config.log"
    monkeypatch.setattr(host, "AUDIT_MAX_BYTES", 100)
    for n in range(5):
        host._audit(path, f"{'a' * 31}{n}", "targets", "accepted")
    for generation in range(4):
        member = path if generation == 0 else tmp_path / f"config.log.{generation}"
        assert stat.S_IMODE(member.stat().st_mode) == 0o600
        assert f"{'a' * 31}{4 - generation} targets accepted" in member.read_text()
    assert not (tmp_path / "config.log.4").exists()
    assert stat.S_IMODE((tmp_path / "config.log.lock").stat().st_mode) == 0o600


@pytest.mark.skipif(not LINUX, reason="requires Linux no-follow file opens")
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions", "lock", "rotation"])
def test_audit_rejects_unsafe_files(tmp_path, monkeypatch, unsafe):
    path = tmp_path / "config.log"
    sentinel = tmp_path / "unchanged"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o600)
    if unsafe == "symlink":
        path.symlink_to(sentinel)
    elif unsafe == "hardlink":
        os.link(sentinel, path)
    elif unsafe == "permissions":
        path.touch(mode=0o644)
    elif unsafe == "lock":
        (tmp_path / "config.log.lock").symlink_to(sentinel)
    else:
        monkeypatch.setattr(host, "AUDIT_MAX_BYTES", 1)
        (tmp_path / "config.log.1").symlink_to(sentinel)
    with pytest.raises(host.ConfigError, match="^audit_unavailable$"):
        host._audit(path, "a" * 32, "history", "accepted")
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.skipif(not LINUX, reason="requires Linux audit locking and file modes")
def test_missing_audit_stops_backend_request(tmp_path, monkeypatch):
    def unexpected_exchange(*_):
        pytest.fail("request reached backend without an audit record")

    monkeypatch.setattr(host, "_exchange", unexpected_exchange)
    with pytest.raises(host.ConfigError, match="^audit_unavailable$"):
        host._dispatch("targets", -1, audit_path=tmp_path / "missing" / "config.log")


def wsl_inline(script, path):
    wsl = shutil.which("wsl.exe") if os.name == "nt" else None
    if wsl is None:
        pytest.skip("requires WSL Ubuntu")
    return subprocess.run(
        [wsl, "-d", "Ubuntu", "-e", "/bin/bash", "-p", "-c", script, "companion-config-test", str(path)],
        capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )


def test_shell_wrapper_rejects_argv_before_accessing_host_paths():
    result = wsl_inline('wrapper=$(/usr/bin/wslpath -u "$1") || exit; exec /bin/bash -p "$wrapper" unexpected',
                        TOOLS / "companion-config")
    assert result.returncode == 64, result.stderr
    assert result.stdout == b""
    assert result.stderr == b"companion-config: invalid_invocation\n"


def test_sudoers_syntax():
    result = wsl_inline('policy=$(/usr/bin/wslpath -u "$1") || exit; '
                        'if [[ ! -x /usr/sbin/visudo ]]; then exit 77; fi; '
                        'exec /usr/sbin/visudo -cf "$policy"', TOOLS / "companion-config.sudoers")
    if result.returncode == 77:
        pytest.skip("visudo is unavailable in WSL Ubuntu")
    assert result.returncode == 0, result.stderr
