"""Trusted implementation of the forced-command wrapper; stdlib only.

Install beside a root-owned copy of life_migration_preflight.py. No code from an
uploaded bundle is imported during verification, backup or preflight.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import runpy
import select
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile

HOST = "astrbot_plugin_private_companion"
LIFE = "astrbot_plugin_good_life"
IDS = (HOST, LIFE)
MINIMUM_VERSIONS = {HOST: (7, 1, 17), LIFE: (0, 1, 0)}
OWNER_KEYS = tuple("life." + key + ".execution_owner.v1" for key in ("calendar", "activity", "state", "configuration"))
FLAGS = ("reading_executor_enabled", "reading_abort_own_enabled", "academics_enabled",
         "kernel_enabled", "campus_enabled", "class_enabled", "attendance_enabled", "study_enabled",
         "wallet_enabled", "city_enabled", "amap_navigation_enabled", "amap_poi_search_enabled", "state_model_enabled")
BACKUP_MAX_AGE = 15 * 60
MIB = 1024 * 1024
MAX_UPLOAD = 64 * MIB
MAX_EXPANDED = 256 * MIB
MAX_TAR_BYTES = MAX_UPLOAD + MIB
MAX_TAR_MEMBERS = 4
MAX_NAME_BYTES = 240
MAX_CONTROL_BYTES = MIB
MAX_JSON_BYTES = 32 * MIB
MAX_JSON_DEPTH = 64
MAX_JSON_VALUES = 500_000
MAX_ZIP_DIRECTORY = 8 * MIB
MAX_ZIP_ENTRIES = 20_000
PARSER_MAX_MEMORY = 384 * MIB
PARSER_CPU_TIMEOUT = 30
PARSER_WALL_TIMEOUT = 45
PARSER_SMALL_RESULT = 4096
PARSER_DIAGNOSTICS = 2048
ZIP_END = struct.Struct("<4s4H2IH")
ZIP_CENTRAL = struct.Struct("<4s6H3I5H2I")
ZIP_LOCAL = struct.Struct("<4s5H3I2H")
UPLOAD_QUOTA_COUNT = 8
UPLOAD_QUOTA_BYTES = 256 * MIB
UPLOAD_METADATA_BUDGET = 4096
UPLOAD_EXPIRY = 24 * 60 * 60
UPLOAD_IDLE_TIMEOUT = 15
UPLOAD_TOTAL_TIMEOUT = 300
DISK_RESERVE = 1024 * MIB
# Up to 40,000 ZIP files, plus per-entry directories and block overhead.
UNPACK_DISK_BUDGET = MAX_TAR_BYTES + MAX_UPLOAD + MAX_EXPANDED + 512 * MIB
BACKUPS_TO_KEEP = 5
AUDIT_MAX_BYTES = MIB
AUDIT_GENERATIONS = 3
DIGEST = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
BACKUP_ID = re.compile(r"[0-9]{8}t[0-9]{6}z-[0-9a-f]{12}")
OPTIONAL_PREFIXES = ("life_kernel_", "life_wallet_", "life_academic_", "life_study_", "life_campus_",
                     "life_city_", "life_state_model_", "life_reading_")


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def journal_mutation(action, *paths):
    # Windows rehearsal readers and atomic journal updates can briefly block
    # each other. POSIX can update an open file atomically.
    deadline = time.monotonic() + 1
    while True:
        try:
            return action(*paths)
        except OSError as error:
            retryable = isinstance(error, PermissionError) or getattr(error, "winerror", None) in (5, 32)
            if os.name != "nt" or not retryable or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def write_json(path, value, *, sync_parent=True):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        journal_mutation(os.replace, temporary, path)
        temporary = None
        if sync_parent:
            sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_control_text(path, cap=MAX_CONTROL_BYTES):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("control file is not a regular file")
        if info.st_size > cap:
            raise ValueError("control file exceeds byte limit")
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError("control file exceeds byte limit")
    return data.decode("utf-8")


def read_control_json(path, cap=MAX_CONTROL_BYTES):
    def integer(raw):
        if len(raw) > 129:
            raise ValueError("control JSON integer exceeds digit limit")
        return int(raw)

    return json.loads(read_control_text(path, cap), parse_int=integer)


def relative(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("unsafe archive or manifest path")
    return path


def files(root):
    result = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("symlink or special file in managed tree")
        for name in names:
            path = Path(base) / name
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def version(root):
    text = read_control_text(root / "metadata.yaml")
    match = re.search(rf"^version:[ \t]*({VERSION.pattern})[ \t\r]*$", text, re.M)
    if match is None:
        raise ValueError("invalid plugin metadata version")
    return match[1]


def verify_code(root, plugin_id, expected=None):
    release = root / "RELEASE_FILES.json"
    if not stat.S_ISREG(release.lstat().st_mode):
        raise ValueError("symlink or special file in managed tree")
    manifest = read_control_json(release)
    expected_files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(expected_files, dict) or not expected_files:
        raise ValueError("missing RELEASE_FILES inventory")
    actual = files(root)
    release_digest = actual.pop("RELEASE_FILES.json")
    # Runtime may generate bytecode; it is never deployed or used for rollback.
    actual = {key: value for key, value in actual.items()
              if "__pycache__" not in PurePosixPath(key).parts and not key.endswith(".pyc")}
    if actual != expected_files:
        raise ValueError("plugin files differ from RELEASE_FILES")
    found = version(root)
    plugin = manifest.get("plugin")
    nested_id = plugin.get("plugin_id") if isinstance(plugin, dict) else None
    identity = manifest.get("plugin_id", nested_id)
    recorded_version = manifest.get("version", manifest.get("product_version"))
    if identity != plugin_id or recorded_version != found or (expected and found != expected):
        raise ValueError("plugin identity/version differs from paired manifest")
    if tuple(map(int, found.split("."))) < MINIMUM_VERSIONS[plugin_id]:
        raise ValueError("pre-phase-one code requires prepare_life_rollback.py and reviewed new-record preservation; refused")
    return {"version": found, "release_files_sha256": release_digest}


def tar_members(source):
    seen = set()
    size = 0
    for header in iter(lambda: source.read(tarfile.BLOCKSIZE), b""):
        if header == bytes(tarfile.BLOCKSIZE):
            end = source.read(tarfile.BLOCKSIZE)
            if end != header or any(block.strip(b"\0") for block in iter(lambda: source.read(MIB), b"")):
                raise ValueError("invalid tar end padding")
            return
        entry = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        # The paired release has four short, flat names. Extended headers,
        # including any PAX, GNU longname and sparse payload, have a zero budget.
        # Check the raw type before following the declared size or reading data.
        if entry.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise ValueError("bundle must contain regular files only; extended tar headers are forbidden")
        if header[257:265] != tarfile.POSIX_MAGIC:
            raise ValueError("bundle must use USTAR")
        name = relative(entry.name)
        if (len(name.parts) != 1 or len(entry.name.encode("utf-8")) > MAX_NAME_BYTES
                or entry.name in seen):
            raise ValueError("invalid or duplicate tar member name")
        seen.add(entry.name)
        size += entry.size
        if (not 0 <= entry.size <= MAX_UPLOAD or size > MAX_UPLOAD or len(seen) > MAX_TAR_MEMBERS
                or (name.suffix != ".zip" and entry.size > MAX_CONTROL_BYTES)):
            raise ValueError("bundle exceeds tar member limits")
        yield entry
        padding = (-entry.size) % tarfile.BLOCKSIZE
        if source.read(padding) != bytes(padding):
            raise ValueError("invalid tar member padding")
    raise ValueError("missing tar end marker")


def check_zip_extra(extra):
    position = 0
    while position < len(extra):
        if position + 4 > len(extra):
            raise ValueError("invalid ZIP extra field")
        field, size = struct.unpack_from("<HH", extra, position)
        if field == 0x0001:
            raise ValueError("ZIP64 is forbidden")
        position += 4 + size
        if position > len(extra):
            raise ValueError("invalid ZIP extra field")


def check_zip_directory(raw):
    """Bound the actual central records before ZipFile allocates its inventory."""
    size = raw.seek(0, os.SEEK_END)
    tail_offset = max(size - ZIP_END.size - 65536, 0)
    raw.seek(tail_offset)
    tail = raw.read(size - tail_offset)
    signature = b"PK\x05\x06"
    # Match ZipFile's lookup, including its fast path for an empty comment.
    if len(tail) >= ZIP_END.size and tail[-ZIP_END.size:].startswith(signature) and tail.endswith(b"\0\0"):
        start = len(tail) - ZIP_END.size
    else:
        start = tail.rfind(signature)
    if start < 0 or start + ZIP_END.size > len(tail):
        raise ValueError("missing ZIP end record")
    _, disk, directory_disk, disk_count, count, length, offset, comment = ZIP_END.unpack_from(tail, start)
    end = tail_offset + start
    if start + ZIP_END.size + comment != len(tail):
        raise ValueError("invalid ZIP end record length")
    if end >= 20:
        raw.seek(end - 20)
        if raw.read(4) == b"PK\x06\x07":
            raise ValueError("ZIP64 is forbidden")
    if (disk or directory_disk or disk_count != count or count == 0xffff
            or length == 0xffffffff or offset == 0xffffffff):
        raise ValueError("ZIP64 or multi-disk ZIP is forbidden")
    if (not 0 < count <= MAX_ZIP_ENTRIES or length > MAX_ZIP_DIRECTORY
            or count * ZIP_CENTRAL.size > length):
        raise ValueError("ZIP central directory exceeds limits")
    if offset + length != end:
        raise ValueError("invalid ZIP central directory bounds")
    raw.seek(offset)
    for _ in range(count):
        if raw.tell() + ZIP_CENTRAL.size > end:
            raise ValueError("invalid ZIP central directory record count")
        fields = ZIP_CENTRAL.unpack(raw.read(ZIP_CENTRAL.size))
        compressed, expanded, name, extra, comment, start_disk, _, _, local = fields[8:]
        if fields[0] != b"PK\x01\x02" or not 0 < name <= MAX_NAME_BYTES:
            raise ValueError("invalid ZIP central directory record")
        if start_disk or 0xffffffff in (compressed, expanded, local):
            raise ValueError("ZIP64 or multi-disk ZIP is forbidden")
        next_record = raw.tell() + name + extra + comment
        if next_record > end or local + ZIP_LOCAL.size > offset:
            raise ValueError("invalid ZIP central directory bounds")
        raw.seek(name, os.SEEK_CUR)
        check_zip_extra(raw.read(extra))
        raw.seek(next_record)
    if raw.tell() != end:
        raise ValueError("invalid ZIP central directory record count")
    return offset


def check_zip_local(raw, entry, directory):
    raw.seek(entry.header_offset)
    signature, _, _, _, _, _, _, compressed, expanded, name, extra = ZIP_LOCAL.unpack(raw.read(ZIP_LOCAL.size))
    if signature != b"PK\x03\x04" or raw.tell() + name + extra + entry.compress_size > directory:
        raise ValueError("invalid ZIP local header bounds")
    if 0xffffffff in (compressed, expanded):
        raise ValueError("ZIP64 is forbidden")
    raw.seek(name, os.SEEK_CUR)
    check_zip_extra(raw.read(extra))


def paired_manifest(path):
    manifest = read_control_json(path)
    if not isinstance(manifest, dict) or manifest.get("schema") != "good-life.paired-release.v1":
        raise ValueError("not a verified paired release")
    source_commit = manifest.get("source_commit")
    items = manifest.get("plugins")
    if (not isinstance(source_commit, str) or COMMIT.fullmatch(source_commit) is None
            or not isinstance(items, list) or len(items) != len(IDS)):
        raise ValueError("not a verified paired release")
    plugins = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("not a verified paired release")
        plugin_id, version, archive, digest = (item.get(key) for key in ("id", "version", "archive", "sha256"))
        if (plugin_id not in IDS or not isinstance(version, str) or VERSION.fullmatch(version) is None
                or not isinstance(archive, str) or len(archive.encode("utf-8")) > MAX_NAME_BYTES
                or not isinstance(digest, str) or DIGEST.fullmatch(digest) is None):
            raise ValueError("not a verified paired release")
        plugins.append({"id": plugin_id, "version": version, "archive": archive, "sha256": digest})
    if {item["id"] for item in plugins} != set(IDS):
        raise ValueError("not a verified paired release")
    return {"schema": manifest["schema"], "source_commit": source_commit, "plugins": plugins}


def unpack_bundle(archive, destination):
    if archive.stat().st_size > MAX_UPLOAD:
        raise ValueError("bundle exceeds upload byte limit")
    outer = destination / "archives"
    outer.mkdir()
    with tempfile.TemporaryFile(dir=destination) as raw:
        with gzip.open(archive, "rb") as source:
            remaining = MAX_TAR_BYTES
            while block := source.read(min(MIB, remaining + 1)):
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError("decompressed tar exceeds byte limit")
                raw.write(block)
        raw.seek(0)
        for entry in tar_members(raw):
            with (outer / entry.name).open("xb") as target:
                remaining = entry.size
                while remaining:
                    block = raw.read(min(MIB, remaining))
                    if not block:
                        raise ValueError("truncated tar member")
                    target.write(block)
                    remaining -= len(block)
    manifest = paired_manifest(outer / "release-manifest.json")
    items = manifest["plugins"]
    expected = {"release-manifest.json", "SHA256SUMS.txt"}
    expanded = destination / "plugins"
    expanded.mkdir()
    expanded_size = 0
    directories = set()
    for item in items:
        name = relative(item["archive"])
        if len(name.parts) != 1 or name.suffix != ".zip" or str(name) in expected:
            raise ValueError("invalid paired archive name")
        expected.add(str(name))
        path = outer / str(name)
        if sha(path) != item["sha256"]:
            raise ValueError("paired archive digest mismatch")
        with path.open("rb") as raw:
            directory = check_zip_directory(raw)
            with zipfile.ZipFile(raw) as source:
                seen = set()
                for entry in source.infolist():
                    member = relative(entry.filename)
                    mode = entry.external_attr >> 16
                    if (member.parts[0] != item["id"] or len(member.parts) < 2
                            or len(entry.filename.encode("utf-8")) > MAX_NAME_BYTES
                            or entry.filename != entry.orig_filename or entry.filename in seen or entry.is_dir()
                            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                        raise ValueError("unsafe plugin archive entry")
                    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise ValueError("unsupported plugin ZIP compression method")
                    if (len(member.parts) == 2 and member.name.casefold() in ("release_files.json", "metadata.yaml")
                            and entry.file_size > MAX_CONTROL_BYTES):
                        raise ValueError("plugin control file exceeds byte limit")
                    seen.add(entry.filename)
                    expanded_size += entry.file_size
                    if expanded_size > MAX_EXPANDED:
                        raise ValueError("expanded release exceeds limits")
                    directories.update(member.parents)
                    if len(directories) > 40000:
                        raise ValueError("expanded release exceeds directory limit")
                    check_zip_local(raw, entry, directory)
                    target = expanded.joinpath(*member.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(entry) as payload, target.open("xb") as output:
                        shutil.copyfileobj(payload, output)
        verify_code(expanded / item["id"], item["id"], item["version"])
    if {path.name for path in outer.iterdir()} != expected:
        raise ValueError("unexpected bundle files")
    life_manifest = read_control_json(expanded / LIFE / "RELEASE_FILES.json")
    if life_manifest.get("source_commit") != manifest["source_commit"]:
        raise ValueError("life source commit differs from paired release")
    sums = "".join(f"{sha(path)}  {path.name}\n" for path in sorted(outer.iterdir()) if path.name != "SHA256SUMS.txt")
    if read_control_text(outer / "SHA256SUMS.txt").replace("\r\n", "\n") != sums:
        raise ValueError("bundle checksum manifest mismatch")
    return manifest


# Run only through an internal Python invocation. The SSH command parser never
# accepts these actions, and runpy loads this trusted file without invoking main.
_PARSER_SCRIPT = """
import os, sys
if os.name == 'posix':
    import resource
    for kind, requested in ((resource.RLIMIT_AS, sys.argv[2]), (resource.RLIMIT_CPU, sys.argv[3])):
        limit = min(value for value in (int(requested), *resource.getrlimit(kind)) if value >= 0)
        resource.setrlimit(kind, (limit, limit))
import runpy
raise SystemExit(runpy.run_path(sys.argv[1])['_parser_child'](*sys.argv[4:]))
"""


def check_json_result(document):
    # The parent decodes a fresh serialization of this bounded graph. Limit
    # both recursion depth and object count before sending any of it back.
    remaining = MAX_JSON_VALUES

    def visit(value, depth):
        nonlocal remaining
        remaining -= 1
        if depth > MAX_JSON_DEPTH or remaining < 0:
            raise ValueError("control JSON exceeds structure limits")
        if isinstance(value, (dict, list)):
            for item in (value.values() if isinstance(value, dict) else value):
                visit(item, depth + 1)

    visit(document, 0)


def _parser_child(action, cap, *arguments):
    try:
        if action == "bundle":
            result = unpack_bundle(*(Path(path) for path in arguments))
        elif action == "verify-code":
            root, plugin, *expected = arguments
            result = verify_code(Path(root), plugin, *expected)
        elif action == "json":
            path, size = arguments
            result = read_control_json(Path(path), int(size))
            check_json_result(result)
        else:
            raise ValueError("unsupported parser action")
        remaining = int(cap)
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(result):
            data = chunk.encode("utf-8", errors="backslashreplace")
            remaining -= len(data)
            if remaining < 0:
                raise ValueError("parser result exceeds byte limit")
            sys.stdout.buffer.write(data)
        return 0
    except Exception as error:
        if isinstance(error, UnicodeError):
            message = "invalid text encoding"
        elif isinstance(error, (ValueError, tarfile.TarError, zipfile.BadZipFile)):
            message = str(error)
        else:
            message = type(error).__name__
        message = re.sub(r"[\x00-\x1f\x7f]", " ", message[:PARSER_DIAGNOSTICS])
        sys.stderr.buffer.write(message.encode("utf-8", errors="replace")[:PARSER_DIAGNOSTICS])
        return 1


def parse_untrusted(action, *paths):
    """Parse bundle, JSON (with optional byte cap), or an installed code tree."""
    result_cap = PARSER_SMALL_RESULT
    if action == "bundle" and len(paths) == 2:
        arguments = [os.fspath(path) for path in paths]
    elif action == "verify-code" and len(paths) in (2, 3):
        root, plugin, *expected = paths
        if plugin not in IDS or (expected and expected[0] is not None
                                and (not isinstance(expected[0], str) or VERSION.fullmatch(expected[0]) is None)):
            raise ValueError("invalid plugin verification arguments")
        arguments = [os.fspath(root), plugin, *(value for value in expected if value is not None)]
    elif action == "json" and len(paths) in (1, 2):
        path, *size = paths
        cap = size[0] if size else MAX_CONTROL_BYTES
        if type(cap) is not int or not 0 < cap <= MAX_JSON_BYTES:
            raise ValueError("invalid control JSON byte cap")
        arguments = [os.fspath(path), str(cap)]
        result_cap = min(2 * cap, MAX_JSON_BYTES)
    else:
        raise ValueError("unsupported parser action or invalid arguments")

    script = Path(__file__).resolve()
    # Preserve caller-relative path semantics while launching in trusted cwd.
    path_count = 2 if action == "bundle" else 1
    arguments[:path_count] = [os.path.abspath(path) for path in arguments[:path_count]]
    command = [sys.executable, "-I", "-S", "-X", "utf8", "-c", _PARSER_SCRIPT, str(script),
               str(PARSER_MAX_MEMORY), str(PARSER_CPU_TIMEOUT), action, str(result_cap), *arguments]
    captured = [None, None]
    with subprocess.Popen(command, cwd=script.parent, env=subprocess_env(), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_console()) as child:
        def read_pipe(index, pipe, limit):
            try:
                captured[index] = pipe.read(limit + 1)
                if len(captured[index]) > limit:
                    child.kill()
            except OSError:
                child.kill()

        readers = []
        limits = ((child.stdout, result_cap), (child.stderr, PARSER_DIAGNOSTICS))
        try:
            for index, (stream, limit) in enumerate(limits):
                reader = threading.Thread(target=read_pipe, args=(index, stream, limit))
                reader.start()
                readers.append(reader)
            child.wait(timeout=PARSER_WALL_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"{action} parser wall timeout") from error
        finally:
            if child.returncode is None:
                child.kill()
            for reader in readers:
                reader.join()
        stdout, stderr = captured
        if stdout is None or stderr is None:
            raise ValueError(f"{action} parser output capture failed")
        if len(stdout) > result_cap or len(stderr) > PARSER_DIAGNOSTICS:
            raise ValueError(f"{action} parser output exceeds byte limit")
        if child.returncode or stderr:
            detail = redact(stderr.decode("utf-8", errors="replace")) if stderr else f"exit {child.returncode}"
            raise ValueError(f"{action} parser refused: {detail}")
    try:
        return json.loads(stdout)
    except (ValueError, RecursionError) as error:
        raise ValueError(f"{action} parser returned invalid JSON") from error


def redact(text):
    text = re.sub(r'''(?ix)(?<![\w-])((?:[\w-]*(?:token|secret|password|api[_-]?key|cookie)[\w-]*)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', r"\1[REDACTED]", text)
    text = re.sub(r"(?im)((?:set-)?cookie\s*:\s*)[^\r\n]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r'''(?ix)(?<![\w-])((?:[\w-]*(?:token|secret|password|api[_-]?key|cookie)[\w-]*)["']?\s*[:=]\s*["']?)[^\s,"';}]+''', r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", "[REDACTED]", text)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)


OPERATION_ID = re.compile(r"[0-9a-f]{32}")
BACKUP_QUOTA_BYTES = 32 * 1024 * MIB
LOG_RESPONSE_BYTES = 256 * 1024
LIFE_BINDING_READY = "[GoodLife] host binding ready"
DOCKER_OUTPUT_BYTES = 16 * 1024
RESPONSE_TIMEOUT = 30
REQUEST_SLOTS = 4
OPERATION_RECORD_COUNT = 128
OPERATION_RECORD_BYTES = MIB
OPERATION_TIMEOUT = 15 * 60
RECOVERY_TIMEOUT = 120
SYSTEMD_FINALIZATION_MARGIN = 30
SUPERVISOR_TAKEOVER_TIMEOUT = 5
HOST_V2_READERS = {"7.1.17": frozenset(), "7.1.18": frozenset({"life.async.intent.v2:"})}


def no_console(flags=0):
    return {"creationflags": subprocess.CREATE_NO_WINDOW | flags} if os.name == "nt" else {}


def subprocess_env():
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if os.name == "nt":
        # Required by Windows DLL, locale and temporary-file handling in rehearsals.
        env.update({key: os.environ[key] for key in ("SystemRoot", "TEMP", "TMP") if key in os.environ})
    return env


@contextmanager
def exclusive(path, *, blocking=False):
    if os.name == "nt":
        with path.open("a+") as stream:
            yield stream
        return
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+", encoding="ascii") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise ValueError("unsafe deployment lock file")
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(stream, flags)
        yield stream


@contextmanager
def response_deadline(enabled):
    if not enabled or os.name == "nt":
        yield
        return

    def timeout(*_):
        raise TimeoutError("wrapper response deadline exceeded")

    old_handler = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, RESPONSE_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def step_deadline(deadline, limit):
    local = time.monotonic() + limit
    return local if deadline is None else min(deadline, local)


def step_timeout(deadline, limit):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("operation deadline exceeded")
    return min(limit, remaining)


def bounded_capture(command, stdout_limit, *, timeout, combine=False):
    deadline = time.monotonic() + timeout
    output = [bytearray(), bytearray()]
    overflow = threading.Event()
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT if combine else subprocess.PIPE,
                             bufsize=0, env=subprocess_env(), **no_console())

    def drain(pipe, result, limit):
        try:
            while chunk := pipe.read(min(8192, limit + 1 - len(result))):
                result.extend(chunk)
                if len(result) > limit:
                    overflow.set()
                    child.kill()
                    return
        except OSError:
            return

    readers = []
    try:
        for pipe, result, limit in zip((child.stdout, child.stderr), output,
                                       (stdout_limit, DOCKER_OUTPUT_BYTES)):
            if pipe is not None:
                reader = threading.Thread(target=drain, args=(pipe, result, limit), daemon=True)
                reader.start()
                readers.append(reader)
        child.wait(timeout=max(0, deadline - time.monotonic()))
        for reader in readers:
            reader.join(max(0, deadline - time.monotonic()))
            if reader.is_alive():
                raise TimeoutError("subprocess output deadline exceeded")
        return child.returncode, bytes(output[0]), bytes(output[1]), overflow.is_set()
    finally:
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


@contextmanager
def request_slot(base, action):
    with ExitStack() as locks:
        first = 0 if action == "status" else 1
        for index in range(first, REQUEST_SLOTS):
            try:
                locks.enter_context(exclusive(base / f"request-{index}.lock"))
            except BlockingIOError:
                continue
            yield
            return
        raise ValueError("all deployment request slots are busy")


def write_readonly_response(output):
    # Avoid a buffered stdout flush retry at interpreter shutdown after an
    # interrupted write to a slow SSH client.
    chunk = memoryview(output)
    fd = sys.stdout.fileno()
    while chunk:
        chunk = chunk[os.write(fd, chunk):]


@contextmanager
def recovery_lock(path, deadline):
    with ExitStack() as locks:
        while True:
            step_timeout(deadline, 0.05)
            try:
                locks.enter_context(exclusive(path))
                break
            except BlockingIOError:
                time.sleep(step_timeout(deadline, 0.05))
        yield


def command_args(original):
    if len(original) > 256 or not re.fullmatch(r"[a-z0-9 -]+", original):
        raise ValueError("invalid command")
    action, *arguments = original.split() or ("",)
    validators = {"logs": re.compile(r"[0-9]{1,3}"), "upload": DIGEST, "release": DIGEST,
                  "rollback": BACKUP_ID, "operation": OPERATION_ID}
    if action == "status" and not arguments:
        return action, None
    if action == "logs" and not arguments:
        return action, None
    if action in validators and len(arguments) == 1 and validators[action].fullmatch(arguments[0]):
        return action, arguments[0]
    raise ValueError("unsupported command or invalid arguments")


def upload_blocks(fd):
    def read():
        return os.read(fd, MIB)

    if os.name == "nt":
        # Windows rehearsals cannot select() on a pipe. The reader only owns
        # stdin, and exits with the helper if a deadline expires.
        blocks = queue.Queue(maxsize=1)

        def reader():
            try:
                while True:
                    block = read()
                    blocks.put(block)
                    if not block:
                        return
            except OSError as error:
                blocks.put(error)

        threading.Thread(target=reader, daemon=True).start()

        def wait(seconds):
            try:
                block = blocks.get(timeout=seconds)
            except queue.Empty:
                return None
            if isinstance(block, OSError):
                raise block
            return block
    else:
        def wait(seconds):
            return read() if select.select([fd], [], [], seconds)[0] else None

    total = time.monotonic() + UPLOAD_TOTAL_TIMEOUT
    idle = time.monotonic() + UPLOAD_IDLE_TIMEOUT
    while True:
        remaining = min(total, idle) - time.monotonic()
        block = wait(remaining) if remaining > 0 else None
        now = time.monotonic()
        if now >= total:
            raise ValueError("upload total timeout")
        if block is None or now >= idle:
            raise ValueError("upload idle timeout")
        if not block:
            return
        idle = now + UPLOAD_IDLE_TIMEOUT
        yield block


def tree_bytes(root, *, allocated=False):
    def size(path):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            return 4096 if allocated else 0
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("symlink or special file in managed tree")
        return ((info.st_size + 4095) // 4096 * 4096) if allocated else info.st_size

    def fail(error):
        raise error

    total = size(root)
    for base, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        total += sum(size(Path(base) / name) for name in dirs + names)
    return total


def approval_record(digest, manifest):
    return {"sha256": digest, "source_commit": manifest["source_commit"],
            "versions": {item["id"]: item["version"] for item in manifest["plugins"]}}


class Deployer:
    def __init__(self, root, *, rehearsal=False):
        self.root = root.resolve(strict=True)
        self.base = self.root / "good-life-deploy"
        self.base.mkdir(mode=0o700, exist_ok=True)
        self.rehearsal = rehearsal
        self.docker_command = [sys.executable, "-X", "utf8", str(self.root / "docker.py")] if rehearsal else ["/usr/bin/docker"]
        self.log_path = self.root / "companion-deploy.log" if rehearsal else Path("/var/log/companion-deploy.log")
        self.approval_dir = (self.root / "etc" if rehearsal else Path("/etc")) / "companion-deploy" / "approved-bundles"

    def log(self, message):
        line = (datetime.now(timezone.utc).isoformat() + " " + message + "\n").encode("utf-8")
        with exclusive(self.log_path.with_suffix(".log.lock"), blocking=True):
            if self.log_path.exists() and self.log_path.stat().st_size + len(line) > AUDIT_MAX_BYTES:
                for index in range(AUDIT_GENERATIONS - 1, 0, -1):
                    old = self.log_path.with_suffix(f".log.{index}")
                    if old.exists():
                        old.replace(self.log_path.with_suffix(f".log.{index + 1}"))
                self.log_path.replace(self.log_path.with_suffix(".log.1"))
            with self.log_path.open("ab") as stream:
                stream.write(line)

    def check_approval_path(self, path, *, directory=False):
        info = path.lstat()
        valid_type = stat.S_ISDIR if directory else stat.S_ISREG
        unsafe = not valid_type(info.st_mode)
        if os.name != "nt":
            uid, gid = (os.geteuid(), os.getegid()) if self.rehearsal else (0, 0)
            unsafe = unsafe or (info.st_uid, info.st_gid) != (uid, gid) or bool(info.st_mode & 0o022)
        elif not self.rehearsal:
            raise ValueError("production approval requires POSIX ownership checks")
        if unsafe:
            raise ValueError("approval allowlist has unsafe ownership, permissions or file type")

    def check_approval_dir(self):
        for path in (self.approval_dir.parent.parent, self.approval_dir.parent, self.approval_dir):
            self.check_approval_path(path, directory=True)

    def approval(self, digest):
        self.check_approval_dir()
        path = self.approval_dir / digest
        try:
            self.check_approval_path(path)
        except FileNotFoundError:
            return None
        with path.open("rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError("approval allowlist entry exceeds size limit")
        return json.loads(data)

    def approve(self, digest, commit, host_version, life_version, *, reapprove=False):
        if (not DIGEST.fullmatch(digest) or not COMMIT.fullmatch(commit)
                or any(not VERSION.fullmatch(value) for value in (host_version, life_version))):
            raise ValueError("approval requires a SHA-256, full source commit and both plugin versions")
        with exclusive(self.base / "staging.lock", blocking=True):
            if self.saved_state("pending.json").get("bundle") == digest:
                raise ValueError("this bundle already has an active or interrupted operation")
            previous_approval = self.approval(digest)
            if previous_approval is not None and "approval_id" in previous_approval:
                raise ValueError("bundle already approved")
            if self.approval("used-" + digest) is not None and not reapprove:
                raise ValueError("bundle already claimed; root must explicitly use --reapprove")
            metadata = self.base / "staging" / digest / "verified.json"
            approval = {"sha256": digest, "source_commit": commit,
                        "versions": {HOST: host_version, LIFE: life_version}}
            if metadata.parent.exists():
                if time.time() - metadata.stat().st_mtime >= UPLOAD_EXPIRY:
                    raise ValueError("unapproved upload expired")
                if self.saved_state(metadata) != approval:
                    raise ValueError("approval does not match verified bundle digest, source commit and versions")
            write_json(self.approval_dir / digest, {**approval, "approval_id": uuid.uuid4().hex})
        self.log("approved bundle " + digest + " source " + commit)

    def approve_rollback(self, identifier, host_version, life_version):
        if self.saved_state("pending.json"):
            raise ValueError("a deployment is already running or requires manual inspection")
        versions = {HOST: host_version, LIFE: life_version}
        if not BACKUP_ID.fullmatch(identifier) or any(not VERSION.fullmatch(v) for v in versions.values()):
            raise ValueError("rollback requires a backup ID and both exact current versions")
        active = self.saved_state("active.json")
        if active.get("backup_id") != identifier or self.current_versions() != versions:
            raise ValueError("rollback backup ID or expected current versions do not match")
        key = "rollback-" + identifier
        if self.approval(key) is not None:
            raise ValueError("rollback already approved")
        write_json(self.approval_dir / key, {"backup_id": identifier, "versions": versions,
                                             "approval_id": uuid.uuid4().hex})
        self.log("approved rollback " + identifier)

    def current_versions(self):
        return {plugin: version(self.root / "plugins" / plugin) for plugin in IDS}

    def reserve(self, required):
        if shutil.disk_usage(self.base).free < DISK_RESERVE + required:
            raise ValueError("insufficient free space on data filesystem for deployment reserve")

    def docker(self, *args, timeout=120):
        code, stdout, _, overflow = bounded_capture(self.docker_command + list(args),
                                                     DOCKER_OUTPUT_BYTES, timeout=timeout)
        if overflow:
            raise ValueError("Docker output exceeds byte limit")
        if code:
            raise ValueError(f"Docker {args[0]} failed (exit {code})")
        return stdout.decode("utf-8", errors="replace")

    def docker_logs(self, *args, timeout=RESPONSE_TIMEOUT):
        code, output, _, overflow = bounded_capture(self.docker_command + ["logs", *args],
                                                     LOG_RESPONSE_BYTES, timeout=timeout, combine=True)
        if code and not overflow:
            raise ValueError("Docker log read failed")
        return output, overflow

    def logs(self, count):
        raw, overflow = self.docker_logs("--tail", str(count), "astrbot")
        if overflow:
            # Drop the last partial line, which may contain an unfinished secret.
            raw = raw.rpartition(b"\n")[0]
        text = redact(raw.decode("utf-8", errors="replace")).encode("utf-8")
        note = b"\n[logs truncated]\n"
        if overflow or len(text) > LOG_RESPONSE_BYTES:
            return text[:LOG_RESPONSE_BYTES - len(note)] + note
        return text

    def running(self):
        state = self.docker("inspect", "--format", "{{.State.Running}}", "astrbot").strip()
        if state not in ("true", "false"):
            raise ValueError("unexpected Docker running state")
        return state == "true"

    @staticmethod
    def binding_ready(logs):
        # AstrBot prefixes log lines. Match the full fixed message at the end.
        return any(line.endswith(LIFE_BINDING_READY) for line in logs.splitlines())

    def container_status(self, *, timeout=5):
        state = self.docker("inspect", "--format", "{{.State.Running}} {{.RestartCount}} "
                            "{{.State.StartedAt}} "
                            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                            "astrbot", timeout=timeout).strip()
        match = re.fullmatch(r"(true|false) ([0-9]{1,12}) ([-0-9T:.]{19,29}Z) (healthy|none|starting|unhealthy)", state)
        if match is None:
            raise ValueError("unexpected Docker container status")
        running, restarts, started_at, health = match.groups()
        return running == "true", (restarts, started_at), health

    def settle(self, *, require_binding=False, identity=None, deadline=None, observe=lambda _: None):
        window = 0.1 if self.rehearsal else 5
        delay = 0.05 if self.rehearsal else 1
        deadline = step_deadline(deadline, 1.5 if self.rehearsal else 90)
        healthy_since = None
        binding = not require_binding
        while time.monotonic() < deadline:
            try:
                running, current, health = self.container_status(timeout=step_timeout(deadline, 5))
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    break
                raise
            observe(running)
            if not running or health == "unhealthy" or (identity is not None and identity != current):
                raise ValueError("container did not remain running and healthy during settle window")
            identity = current
            if health != "starting":
                if not binding:
                    try:
                        raw, overflow = self.docker_logs("--since", identity[1], "--tail", "500", "astrbot",
                                                         timeout=step_timeout(deadline, 5))
                    except (TimeoutError, subprocess.TimeoutExpired):
                        if time.monotonic() >= deadline:
                            break
                        raise
                    if overflow:
                        raise ValueError("settle Docker logs exceed byte limit")
                    binding = self.binding_ready(raw.decode("utf-8", errors="replace"))
                if binding:
                    now = time.monotonic()
                    if healthy_since is None:
                        healthy_since = now
                    elif now - healthy_since >= window and now <= deadline:
                        return
            else:
                healthy_since = None
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
        raise ValueError("container health or GoodLife host binding not confirmed during settle window")

    def database_paths(self):
        data = self.root / "plugin_data"
        runtime = data / HOST / "installations/main/runtime"
        return (runtime / "companion-v2.sqlite3", runtime / "configuration/companion-v2.sqlite3",
                data / LIFE / "good-life.sqlite3")

    @contextmanager
    def database(self, path):
        with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            yield connection

    def flags(self):
        path = self.root / "config" / (LIFE + "_config.json")
        config = parse_untrusted("json", path) if path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError("feature configuration must be a JSON object")
        return {key: config.get(key, False) for key in FLAGS}

    def guard_host_v2(self, db, target_version):
        supported = HOST_V2_READERS.get(target_version, frozenset())
        keys = db.execute("SELECT key FROM runtime_metadata WHERE key GLOB 'life.*.v2*' "
                          "OR (key = 'life.async.ports.schema.version' AND value != '1')")
        if any(not any(key.startswith(prefix) for prefix in supported) for key, in keys):
            raise ValueError(f"host v2 state has no reviewed reader in target version {target_version}")

    def preflight(self, target_host_version=None):
        if any(value is not False for value in self.flags().values()):
            raise ValueError("new features must remain off; flag changes require separate user approval")
        host, config, life = self.database_paths()
        preflight_file = Path(__file__).resolve().with_name("life_migration_preflight.py")
        if self.rehearsal:
            preflight_file = Path(__file__).resolve().parents[1] / "life_migration_preflight.py"
        inspect = runpy.run_path(str(preflight_file))["inspect_database"]
        inspect(host, expected_version="companion-v2.storage.v71")
        for path, table, expected in ((config, "runtime_metadata", "configuration-store.v1"),
                                      (life, "life_metadata", "good-life.storage.v1")):
            with self.database(path) as db:
                row = db.execute(f"SELECT value FROM {table} WHERE key='schema_version'").fetchone()
                if row != (expected,) or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("database version/integrity mismatch")
        with self.database(host) as db:
            self.guard_host_v2(db, target_host_version)
            for key in OWNER_KEYS:
                if db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone() != ("good_life",):
                    raise ValueError("production phase-one ownership mismatch; no automatic migration")
            # Same stopped-task recovery evidence used by prepare_life_cutover.
            count = db.execute("""SELECT COUNT(*) FROM execution_tasks t LEFT JOIN execution_attempts a
                ON a.attempt_id=t.attempt_id AND a.task_id=t.task_id AND a.generation=t.generation
                WHERE t.state IN ('created','admitted','running','retry_scheduled','cancelling')
                AND (t.kind IN ('calendar.plan','role.state.maintain') OR t.kind LIKE 'role.reading.%'
                     OR t.kind LIKE 'role.social.%')
                AND (t.state IN ('created','retry_scheduled') OR a.attempt_id IS NULL
                     OR NOT json_valid(a.record_json) OR (t.state IN ('running','cancelling')
                     AND json_extract(a.record_json,'$.started_at') IS NULL))""").fetchone()[0]
            if count:
                raise ValueError("life tasks need recovery evidence before upgrade")
        with self.database(config) as db:
            self.guard_host_v2(db, target_host_version)
            if db.execute("SELECT value FROM runtime_metadata WHERE key=?", (OWNER_KEYS[-1],)).fetchone() != ("good_life",):
                raise ValueError("configuration ownership mismatch")
            if db.execute("SELECT COUNT(*) FROM operations WHERE owner IN ('role.life.settings','role.state.settings') AND state != 'completed'").fetchone()[0]:
                raise ValueError("configuration operations are unsettled")
        with self.database(life) as db:
            if db.execute("SELECT value FROM life_metadata WHERE key='life.phase1.cutover_ready.v1'").fetchone() != ("ready",):
                raise ValueError("life cutover marker missing")
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if any(name.startswith(OPTIONAL_PREFIXES) for name in tables):
                raise ValueError("optional domain history requires reviewed data-preserving rollback plan")
            if "staged_life_runtime_metadata" in tables and db.execute(
                    "SELECT 1 FROM staged_life_runtime_metadata WHERE key LIKE 'a3.reading.%' LIMIT 1").fetchone():
                raise ValueError("A3 history requires reviewed version-preserving recovery; never downgrade in-flight v2 work")

    def status(self):
        pending = self.saved_state("pending.json")
        if pending:
            return {"container_running": self.running(), "operation": self.public_operation(pending)}
        flags = self.flags()
        if any(type(value) is not bool for value in flags.values()):
            raise ValueError("status feature flags must be booleans")
        output = {"container_running": self.running(), "plugins": {}, "flags": flags, "markers": {}}
        for plugin in IDS:
            code = self.root / "plugins" / plugin
            inventory = read_control_text(code / "RELEASE_FILES.json").encode("utf-8")
            output["plugins"][plugin] = {"version": version(code),
                                         "release_files_sha256": hashlib.sha256(inventory).hexdigest()}
        host, config, life = self.database_paths()
        for label, path, table, keys in (("host", host, "runtime_metadata", OWNER_KEYS),
                                         ("configuration", config, "runtime_metadata", (OWNER_KEYS[-1],)),
                                         ("life", life, "life_metadata", ("life.phase1.cutover_ready.v1",))):
            with self.database(path) as db:
                markers = output["markers"][label] = {}
                for key in keys:
                    row = db.execute(f"SELECT CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) <= 64 "
                                     f"THEN value END FROM {table} WHERE key=?", (key,)).fetchone()
                    markers[key] = row[0] if row else None
        return output

    def saved_state(self, name):
        try:
            return journal_mutation(read_control_json, self.base / name)
        except FileNotFoundError:
            return {}

    def staging_usage(self, staging):
        entries = [path for path in staging.iterdir() if not path.name.startswith("incoming-")]
        return len(entries), sum(tree_bytes(path) for path in entries)

    def interrupt_upload(self):
        lock = self.base / "upload.lock"
        try:
            with exclusive(lock):
                return
        except BlockingIOError:
            pass
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise ValueError("cannot safely interrupt upload without Linux pidfd support")
        holder = re.fullmatch(r"([1-9][0-9]{0,9}) ([0-9]{1,9})\n", read_control_text(lock, 64))
        if holder is None:
            raise ValueError("upload lock holder not yet identified; retry purge")
        pid, fd = map(int, holder.groups())
        try:
            process_fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            process = Path(f"/proc/{pid}")
            command = (process / "cmdline").read_bytes().split(b"\0")
            lock_info = (process / "fdinfo" / str(fd)).read_text(encoding="ascii")
            info = lock.stat()
            held = (process / "fd" / str(fd)).stat()
            if (os.fsencode(Path(__file__).resolve()) not in command
                    or (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino)
                    or not re.search(rf"^lock:[ \t]+\d+:[ \t]+FLOCK[ \t]+ADVISORY[ \t]+WRITE[ \t]+{pid}[ \t]", lock_info, re.M)):
                raise ValueError("recorded PID is not the deployment wrapper holding the upload lock")
            signal.pidfd_send_signal(process_fd, signal.SIGTERM)
            if not select.select([process_fd], [], [], 5)[0]:
                raise ValueError("upload holder did not exit after termination")
        except (FileNotFoundError, ProcessLookupError):
            # The wrapper may have finished during inspection. The caller still
            # has to acquire the upload lock before touching incoming files.
            pass
        finally:
            os.close(process_fd)

    def purge_uploads(self, *, expired_only=False):
        if expired_only:
            return self.purge_staged_uploads(expired_only=True)
        self.interrupt_upload()
        with exclusive(self.base / "upload.lock"):
            for orphan in (self.base / "staging").glob("incoming-*"):
                shutil.rmtree(orphan)
            return self.purge_staged_uploads(expired_only=False)

    def purge_staged_uploads(self, *, expired_only):
        with exclusive(self.base / "staging.lock", blocking=True):
            staging = self.base / "staging"
            pending = self.saved_state("pending.json").get("bundle")
            removed = 0
            cutoff = time.time() - UPLOAD_EXPIRY
            for path in staging.glob("*"):
                if not DIGEST.fullmatch(path.name) or path.name == pending or self.approval(path.name) is not None:
                    continue
                if expired_only and (path / "verified.json").stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
            if removed:
                sync_directory(staging)
            return {"purged_uploads": removed}

    def upload(self, digest):
        def terminate(*_):
            raise ValueError("upload canceled by root purge")

        with exclusive(self.base / "upload.lock") as holder:
            old_handler = signal.signal(signal.SIGTERM, terminate) if os.name != "nt" else None
            try:
                holder.seek(0)
                holder.truncate()
                holder.write(f"{os.getpid()} {holder.fileno()}\n")
                holder.flush()
                with exclusive(self.base / "space.lock"):
                    return self.receive_upload(digest)
            finally:
                if old_handler is not None:
                    signal.signal(signal.SIGTERM, old_handler)

    def upload_limits(self, approval):
        if approval and OPERATION_ID.fullmatch(str(approval.get("approval_id"))):
            return UPLOAD_QUOTA_COUNT + 1, UPLOAD_QUOTA_BYTES
        return UPLOAD_QUOTA_COUNT, UPLOAD_QUOTA_BYTES - MAX_UPLOAD - UPLOAD_METADATA_BUDGET

    def receive_upload(self, digest):
        staging = self.base / "staging"
        staging.mkdir(exist_ok=True, mode=0o700)
        for orphan in staging.glob("incoming-*"):
            shutil.rmtree(orphan)
        self.purge_uploads(expired_only=True)
        target = staging / digest
        with exclusive(self.base / "staging.lock", blocking=True):
            count, used = self.staging_usage(staging)
            if target.exists():
                raise ValueError("bundle already uploaded")
            count_limit, byte_limit = self.upload_limits(self.approval(digest))
            if count >= count_limit or used >= byte_limit:
                raise ValueError("upload count or byte quota exhausted")
            limit = min(MAX_UPLOAD, byte_limit - used)
        self.reserve(limit + UNPACK_DISK_BUDGET)
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=staging) as temporary:
            stage = Path(temporary)
            archive = stage / "bundle.tar.gz"
            size = 0
            with archive.open("xb") as output:
                for block in upload_blocks(sys.stdin.fileno()):
                    size += len(block)
                    if size > limit:
                        raise ValueError("upload exceeds size limit or aggregate byte quota")
                    output.write(block)
            if sha(archive) != digest:
                raise ValueError("bundle sha256 mismatch")
            with tempfile.TemporaryDirectory(dir=stage) as work:
                manifest = parse_untrusted("bundle", archive, Path(work))
            verified = approval_record(digest, manifest)
            write_json(stage / "verified.json", verified)
            with exclusive(self.base / "staging.lock", blocking=True):
                count, used = self.staging_usage(staging)
                approval = self.approval(digest)
                count_limit, byte_limit = self.upload_limits(approval)
                staged_size = tree_bytes(stage)
                if count >= count_limit or used + staged_size > byte_limit or staged_size - size > UPLOAD_METADATA_BUDGET:
                    raise ValueError("upload exceeds count or aggregate byte quota")
                if approval and {key: value for key, value in approval.items() if key != "approval_id"} != verified:
                    raise ValueError("approval does not match verified bundle digest, source commit and versions")
                os.rename(stage, target)
                sync_directory(staging)
        return {"verified": digest, "source_commit": manifest["source_commit"]}

    def backup_sources(self):
        for folder in ("plugins", "plugin_data"):
            for plugin in IDS:
                yield Path(folder) / plugin
        yield Path("config")

    def backup_space(self):
        size = sum(tree_bytes(self.root / path, allocated=True) for path in self.backup_sources())
        for path in self.database_paths():
            with self.database(path) as db:
                pages = db.execute("PRAGMA page_count").fetchone()[0]
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                size += pages * page_size
        return size + 16 * MIB  # manifests, block rounding and SQLite bookkeeping

    def backup_choices(self):
        paths = (self.base / "backups").glob("*/manifest.json")
        return sorted((path.parent for path in paths if BACKUP_ID.fullmatch(path.parent.name)),
                      key=lambda path: read_control_json(path / "manifest.json", MAX_JSON_BYTES)["created_at"], reverse=True)

    def protected_backups(self):
        return {self.saved_state(name).get("backup_id")
                for name in ("active.json", "pending.json", "last-operation.json")}

    def cleanup_backups(self):
        protected = self.protected_backups()
        removed = 0
        for path in (self.base / "backups").glob("*"):
            if BACKUP_ID.fullmatch(path.name) and path.name not in protected and not (path / "manifest.json").exists():
                shutil.rmtree(path)
                removed += 1
        return {"removed_partial_backups": removed}

    def backup_quota(self):
        base = self.base / "backups"
        used = {path: tree_bytes(path, allocated=True) for path in base.glob("*")}
        protected = self.protected_backups()
        evictable = [path for path in reversed(self.backup_choices()) if path.name not in protected]
        available = BACKUP_QUOTA_BYTES - sum(used.values()) + sum(used[path] for path in evictable)
        return available, used, evictable

    def retire_backups_for_quota(self, new_size, used, evictable):
        total = new_size + sum(used.values())
        for path in evictable:
            if total <= BACKUP_QUOTA_BYTES:
                break
            try:
                shutil.rmtree(path)
            except OSError as error:
                self.log(f"backup prune failed {path.name}: {type(error).__name__}")
            else:
                total -= used[path]
                self.log("backup quota pruned " + path.name)
        if total > BACKUP_QUOTA_BYTES:
            raise ValueError("could not retire old backups to fit backup byte quota")

    def prune_backups(self):
        keep = self.protected_backups() | {path.name for path in self.backup_choices()[:BACKUPS_TO_KEEP]}
        for folder in ("backups", "previous", "failed"):
            base = self.base / folder
            if not base.exists():
                continue
            for path in base.iterdir():
                identifier = path.name if folder != "failed" else path.name.rsplit("-", 1)[0]
                if BACKUP_ID.fullmatch(identifier) and identifier not in keep:
                    try:
                        shutil.rmtree(path)
                    except OSError as error:
                        self.log(f"backup prune failed {folder}/{path.name}: {type(error).__name__}")

    def backup(self, identifier):
        # The release worker holds space.lock before its preflight and first stop.
        estimated = self.backup_space()
        available, used, evictable = self.backup_quota()
        if min(estimated, available) <= 0:
            raise ValueError("backup byte quota cannot fit this backup while retaining protected backups")
        self.reserve(min(estimated, available))
        limit = min(estimated, available)
        target = self.base / "backups" / identifier
        consumed = 0

        def allocated(size):
            return (size + 4095) // 4096 * 4096

        def charge(amount):
            nonlocal consumed
            if consumed + amount > limit:
                raise ValueError("backup byte quota or filesystem reservation exceeded while writing")
            consumed += amount

        def copy(source, destination):
            if not stat.S_ISREG(Path(source).lstat().st_mode):
                raise ValueError("symlink or special file in managed tree")
            length = 0
            with open(source, "rb") as incoming, open(destination, "xb") as outgoing:
                while block := incoming.read(MIB):
                    charge(allocated(length + len(block)) - allocated(length))
                    outgoing.write(block)
                    length += len(block)
            shutil.copystat(source, destination)

        code = self.code_pair(self.root / "plugins")
        target.mkdir(parents=True, mode=0o700)
        try:
            charge(4 * 4096)  # target, plugins, plugin_data and sqlite
            for path in self.backup_sources():
                source = self.root / path
                for directory, subdirs, _ in os.walk(source, followlinks=False):
                    if any(not stat.S_ISDIR((Path(directory) / name).lstat().st_mode) for name in subdirs):
                        raise ValueError("symlink or special file in managed tree")
                    charge(4096)
                shutil.copytree(source, target / path, copy_function=copy)
            # WAL/SHM is copied above. The separate snapshots are disposable
            # until verified, so no additional rollback journal is needed.
            snapshots = target / "sqlite"
            snapshots.mkdir()
            for index, source in enumerate(self.database_paths()):
                with self.database(source) as original:
                    # Pin the read snapshot used by the page budget and backup API.
                    original.execute("BEGIN")
                    pages = original.execute("PRAGMA page_count").fetchone()[0]
                    page_size = original.execute("PRAGMA page_size").fetchone()[0]
                    charge(allocated(pages * page_size))

                    def check_pages(_status, _remaining, total):
                        if total > pages:
                            raise ValueError("backup SQLite source grew after shutdown")

                    with closing(sqlite3.connect(snapshots / f"{index}.sqlite3")) as snapshot:
                        snapshot.execute("PRAGMA journal_mode=OFF")
                        original.backup(snapshot, pages=32, progress=check_pages)
                        if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise ValueError("backup SQLite integrity failure")
            manifest = {"schema": "lsr.backup.v1", "id": identifier, "created_at": time.time(),
                        "plugins": code, "files": files(target)}
            content = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            if os.name == "nt":
                content = content.replace("\n", "\r\n")
            manifest_bytes = len(content.encode("utf-8"))
            if manifest_bytes > MAX_JSON_BYTES:
                raise ValueError("backup manifest exceeds byte limit")
            charge(allocated(manifest_bytes))
            actual = tree_bytes(target, allocated=True) + allocated(manifest_bytes)
            if actual > limit:
                raise ValueError("completed backup exceeds byte quota or filesystem reservation")
            write_json(target / "manifest.json", manifest)
            self.retire_backups_for_quota(actual, used, evictable)
        except BaseException:
            shutil.rmtree(target)
            raise
        self.log("backup complete " + identifier)
        return {"backup_id": identifier}

    def code_pair(self, root, expected=None):
        return {plugin: parse_untrusted("verify-code", root / plugin, plugin,
                                        *([expected[plugin]["version"]] if expected else [])) for plugin in IDS}

    def verify_backup(self, target):
        document = parse_untrusted("json", target / "manifest.json", MAX_JSON_BYTES)
        inventory = files(target)
        inventory.pop("manifest.json", None)
        # Deployment journals/previous dirs live outside immutable backup.
        if document.get("schema") != "lsr.backup.v1" or inventory != document.get("files"):
            raise ValueError("backup manifest digest mismatch")
        return document

    def postcheck(self, expected, identity):
        deadline = step_deadline(None, 1.5 if self.rehearsal else 90)
        while time.monotonic() < deadline:
            raw, overflow = self.docker_logs("--since", identity[1], "--tail", "500", "astrbot",
                                             timeout=step_timeout(deadline, 30))
            if overflow:
                raise ValueError("post-check Docker logs exceed byte limit")
            logs = raw.decode("utf-8", errors="replace")
            # AstrBot 4.29 emits Loading plugin, metadata, then AstrBot started.
            # It does not emit a per-plugin successful-load line. Startup completion
            # plus absence of its explicit failure/disabled records is required.
            loaded = all(re.search(r"Loading plugin " + re.escape(plugin) + r" \.\.\.", logs) for plugin in IDS)
            startup_complete = "AstrBot started." in logs
            failed = any(re.search(r"(?:Failed to load plugin |Plugin )" + re.escape(plugin) + r"(?: -----| is disabled\.)", logs) for plugin in IDS)
            running, current, _ = self.container_status(timeout=step_timeout(deadline, 5))
            if not running or current != identity:
                raise ValueError("container start identity changed during post-check")
            if loaded and startup_complete and not failed:
                self.code_pair(self.root / "plugins", expected)
                self.preflight(expected[HOST]["version"])
                # The original phase-one life build predates the ready log.
                self.settle(require_binding=expected[LIFE]["version"] != "0.1.0", identity=identity)
                return
            time.sleep(min(0.1 if self.rehearsal else 3, max(0, deadline - time.monotonic())))
        raise ValueError("post-check failed: container/plugin load evidence missing")

    def replace_code(self, source, previous, expected, original):
        previous.mkdir(parents=True, mode=0o700)
        moves = []
        start_attempted = False
        self.update_pending(phase="replacing")
        try:
            for plugin in IDS:
                current = self.root / "plugins" / plugin
                os.rename(current, previous / plugin)
                moves.append((previous / plugin, current))
                os.rename(source / plugin, current)
                moves.append((current, source / plugin))
            self.log("paired code replaced; start and post-check")
            start_attempted = True
            self.docker("start", "astrbot")
            running, identity, _ = self.container_status()
            if not running:
                raise ValueError("container did not remain running after start")
            self.update_pending(container_start=list(identity))
            self.postcheck(expected, identity)
        except Exception as error:
            if start_attempted:
                self.docker("stop", "--time", "45", "astrbot")
                try:
                    self.preflight(original[HOST]["version"])
                except Exception as guard_error:
                    raise RuntimeError(f"automatic restore refused: {guard_error}") from error
            for old, new in reversed(moves):
                os.rename(old, new)
            if self.code_pair(self.root / "plugins") != original:
                raise RuntimeError("restored code differs from the original pair") from error
            self.update_pending(phase="restored", container_start=None)
            self.docker("start", "astrbot")
            raise RuntimeError("previous paired code restored; databases/configuration retained; " + str(error)) from error

    @staticmethod
    def public_operation(record):
        return {key: record[key] for key in ("operation_id", "action", "state", "phase", "result", "error", "last_known_container_state")
                if record.get(key) is not None}

    def operation(self, identifier):
        pending = self.saved_state("pending.json")
        record = pending if pending.get("operation_id") == identifier else self.saved_state(Path("operations") / (identifier + ".json"))
        if not record:
            raise ValueError("operation not found")
        return self.public_operation(record)

    def update_pending(self, **fields):
        pending = {**self.saved_state("pending.json"), **fields}
        write_json(self.base / "pending.json", pending)
        return pending

    def prune_operations(self, operations, identifier, retry_approval, new_size):
        records = []
        for path in operations.glob("*.json"):
            if not OPERATION_ID.fullmatch(path.stem) or path.stem == identifier:
                continue
            info = path.stat()
            record = read_control_json(path)
            if record.get("state") not in ("failed", "succeeded", "manual-recovery"):
                # Incomplete records cannot be discarded by the retention policy.
                records.append((info.st_mtime_ns, path, info.st_size, False))
                continue
            if retry_approval and record.get("preclaim_approval") == retry_approval:
                journal_mutation(path.unlink)
                continue
            records.append((info.st_mtime_ns, path, info.st_size, True))
        used = sum(size for _, _, size, _ in records) + new_size
        count = len(records) + 1
        for _, path, size, removable in sorted(records):
            if count <= OPERATION_RECORD_COUNT and used <= OPERATION_RECORD_BYTES:
                break
            if removable:
                journal_mutation(path.unlink)
                count -= 1
                used -= size
        if count > OPERATION_RECORD_COUNT or used > OPERATION_RECORD_BYTES:
            raise ValueError("operation journal quota cannot fit unresolved records")

    def finish_operation(self, state, **fields):
        pending = {**self.saved_state("pending.json"), **fields, "state": state}
        identifier = pending["operation_id"]
        operations = self.base / "operations"
        operations.mkdir(mode=0o700, exist_ok=True)
        if state == "manual-recovery":
            write_json(self.base / "pending.json", pending)
        public = self.public_operation(pending)
        retry_approval = None
        if pending["phase"] in ("queued", "preparing"):
            approval = pending["approval"]
            if self.approval("used-" + self.approval_key(pending)) != {**approval, "operation_id": identifier}:
                retry_approval = approval["approval_id"]
                public["preclaim_approval"] = retry_approval
        content = json.dumps(public, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if os.name == "nt":
            content = content.replace("\n", "\r\n")
        self.prune_operations(operations, identifier, retry_approval, len(content.encode("utf-8")))
        write_json(operations / (identifier + ".json"), public)
        if state != "manual-recovery":
            journal_mutation((self.base / "pending.json").unlink)
            sync_directory(self.base)
            stage = self.base / ("work-" + identifier)
            try:
                if stage.is_dir():
                    shutil.rmtree(stage)
            except OSError as error:
                self.log("operation staging cleanup failed " + type(error).__name__)
        self.log("operation " + pending["operation_id"] + " " + state)

    def launch(self, action, argument):
        with exclusive(self.base / "action.lock"):
            if self.saved_state("pending.json"):
                raise ValueError("deployment already running or requires manual inspection")
            # A short staging transaction prevents purge between claim and publication.
            with exclusive(self.base / "staging.lock", blocking=True):
                key = argument if action == "release" else "rollback-" + argument
                approval = self.approval(key)
                if not approval or not OPERATION_ID.fullmatch(str(approval.get("approval_id"))):
                    raise ValueError(f"{action} is not approved")
                identifier = uuid.uuid4().hex
                if action == "release":
                    backup = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz-") + uuid.uuid4().hex[:12]
                    binding = {"bundle": argument, "backup_id": backup}
                else:
                    binding = {"backup_id": argument}
                self.update_pending(operation_id=identifier, action=action, state="queued", phase="queued",
                                    recovery_limit=time.monotonic() + OPERATION_TIMEOUT + RECOVERY_TIMEOUT,
                                    approval=approval, **binding)
            submission = self.spawn_supervisor(identifier)
            if not self.wait_for_takeover(identifier, submission):
                try:
                    with exclusive(self.base / "job.lock"):
                        if not self.check_takeover(identifier):
                            current = self.saved_state("pending.json")
                            if current.get("operation_id") == identifier and current["phase"] == "queued":
                                journal_mutation((self.base / "pending.json").unlink)
                                sync_directory(self.base)
                                raise ValueError("supervisor did not start; approval remains available")
                except BlockingIOError:
                    pass
                if not self.check_takeover(identifier):
                    raise self.ambiguous_submission(identifier)
        return {"operation_id": identifier, "state": "queued"}

    def job_command(self, mode, identifier):
        args = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), mode, identifier]
        if self.rehearsal:
            args.extend(("--test-root", str(self.root)))
        return args

    @staticmethod
    def supervisor_unit(identifier):
        return "companion-deploy-" + identifier + ".service"

    def supervisor_running(self, identifier):
        try:
            code, output, _, overflow = bounded_capture(["/usr/bin/systemctl", "show",
                "--property=LoadState,ActiveState", "--", self.supervisor_unit(identifier)], 1024, timeout=5)
            fields = dict(line.split("=", 1) for line in output.decode("ascii").splitlines())
            if not overflow:
                if fields == {"LoadState": "not-found", "ActiveState": "inactive"}:
                    return False
                if code == 0 and fields.get("LoadState") == "loaded":
                    if fields.get("ActiveState") in ("failed", "inactive"):
                        return False
                    if fields.get("ActiveState") in ("activating", "active", "deactivating", "reloading"):
                        return True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return None

    def take_over(self, identifier):
        self.update_pending(state="running", phase="preparing")
        # The journal is durable now. This receipt only serves the live submitter.
        # Keep one operation-tagged receipt so even a fast terminal job can be acknowledged.
        write_json(self.base / "takeover.json", {"operation_id": identifier}, sync_parent=False)

    def check_takeover(self, identifier):
        if self.saved_state("takeover.json").get("operation_id") == identifier:
            return True
        terminal = self.saved_state(Path("operations") / (identifier + ".json"))
        if terminal.get("state") == "failed" and terminal.get("phase") == "queued":
            raise ValueError(f"supervisor could not take over operation {identifier}; approval remains available")
        return False

    def ambiguous_submission(self, identifier):
        return ValueError(f"supervisor submission unknown; inspect operation {identifier} "
                          f"and systemd unit {self.supervisor_unit(identifier)}")

    def wait_for_takeover(self, identifier, submission):
        if submission is False:
            return False
        deadline = time.monotonic() + SUPERVISOR_TAKEOVER_TIMEOUT
        while not self.check_takeover(identifier):
            if submission is not None and submission.poll() is not None:
                return self.check_takeover(identifier)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                unit_running = None if self.rehearsal else self.supervisor_running(identifier)
                if self.check_takeover(identifier):
                    return True
                if unit_running is False:
                    return False
                raise self.ambiguous_submission(identifier)
            time.sleep(min(0.05, remaining))
        return True

    def spawn_supervisor(self, identifier):
        command = self.job_command("--supervise", identifier)
        if not self.rehearsal:
            runtime_limit = OPERATION_TIMEOUT + RECOVERY_TIMEOUT + SYSTEMD_FINALIZATION_MARGIN
            try:
                subprocess.run(["/usr/bin/systemd-run", "--quiet", "--collect",
                    "--unit=" + self.supervisor_unit(identifier), "--property=Type=exec",
                    f"--property=RuntimeMaxSec={runtime_limit}",
                    "--property=KillMode=control-group", "--", *command],
                    env=subprocess_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10, **no_console())
            except OSError:
                return False  # The systemd-run executable could not start.
            except subprocess.TimeoutExpired:
                pass
            # systemd's receipt is not a durable supervisor takeover.
            return None

        # --test-root selects the explicitly marked, non-installed fixture path.
        self.log("launch detached rehearsal supervisor " + identifier)
        options = (no_console(subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt"
                   else {"start_new_session": True})
        try:
            return subprocess.Popen(command, env=subprocess_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, **options)
        except OSError:
            return False

    @staticmethod
    def approval_key(pending):
        return pending.get("bundle", "rollback-" + pending["backup_id"])

    def claim(self):
        pending = self.saved_state("pending.json")
        approval = pending["approval"]
        key = self.approval_key(pending)
        with exclusive(self.base / "staging.lock", blocking=True):
            if self.approval(key) != approval:
                raise ValueError("reserved approval changed before claim")
            write_json(self.approval_dir / ("used-" + key), {**approval, "operation_id": pending["operation_id"]})
            (self.approval_dir / key).unlink()
            sync_directory(self.approval_dir)
            self.update_pending(phase="claimed")

    def supervise(self, identifier):
        with exclusive(self.base / "job.lock", blocking=True):
            pending = self.saved_state("pending.json")
            if pending.get("operation_id") != identifier or pending["phase"] != "queued":
                return
            recovery_limit = pending.get("recovery_limit", time.monotonic() + OPERATION_TIMEOUT + RECOVERY_TIMEOUT)
            job_end = recovery_limit - RECOVERY_TIMEOUT
            try:
                self.take_over(identifier)
            except OSError as error:
                self.finish_operation("failed", phase="queued",
                                      error="supervisor takeover could not be persisted: " + redact(str(error)[:2048]))
                return
            worker = None
            try:
                limit = OPERATION_TIMEOUT
                if self.rehearsal and (self.root / ".job-timeout").is_file():
                    limit = float((self.root / ".job-timeout").read_text(encoding="ascii"))
                worker_end = step_deadline(job_end, limit)
                step_timeout(worker_end, limit)
                worker = subprocess.Popen(self.job_command("--work", identifier), env=subprocess_env(),
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=os.name != "nt", **no_console())
                worker.wait(timeout=step_timeout(worker_end, limit))
            except (TimeoutError, subprocess.TimeoutExpired):
                reason = "operation exceeded its hard time limit"
            except Exception as error:
                reason = str(error)
            else:
                reason = self.saved_state("pending.json").get("worker_error", "worker exited unexpectedly")
            self.recover_operation(identifier, reason, deadline=recovery_limit, worker=worker)

    def recover_operation(self, identifier, reason, *, deadline=None, worker=None):
        deadline = step_deadline(deadline, RECOVERY_TIMEOUT)
        pending = self.saved_state("pending.json")
        if pending.get("operation_id") != identifier:
            return
        reason = redact(reason[:2048])
        last_known = pending.get("last_known_container_state", "unknown")
        def observe(running):
            nonlocal last_known
            state = "running" if running else "stopped"
            if state != last_known:
                last_known = state
                self.update_pending(last_known_container_state=last_known)

        try:
            if worker is not None and worker.returncode is None:
                try:
                    if os.name == "nt":
                        worker.kill()
                    else:
                        os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # The worker is the journal's only writer until it exits.
                worker.wait(timeout=step_timeout(deadline, 5))
            pending = self.update_pending(state="recovering", error=reason,
                                          last_known_container_state=last_known)
            phase = pending["phase"]
            if phase == "preparing":
                # Reconcile a durable claim before continuing.
                with recovery_lock(self.base / "staging.lock", deadline):
                    key = self.approval_key(pending)
                    approval = pending["approval"]
                    if (self.approval("used-" + key) == {**approval, "operation_id": identifier}
                            and self.approval(key) == approval):
                        (self.approval_dir / key).unlink()
                        sync_directory(self.approval_dir)
            if phase in ("replacing", "backup"):
                # A stop may have reached Docker before the worker was killed.
                self.docker("stop", "--time", "45", "astrbot", timeout=step_timeout(deadline, 50))
                observe(False)
            if phase == "replacing":
                self.finish_operation("manual-recovery", error="container stopped; manual recovery required; " + reason)
                return
            needs_start = phase == "backup"
            if phase == "restored":
                running, _, _ = self.container_status(timeout=step_timeout(deadline, 5))
                observe(running)
                needs_start = not running
            if needs_start:
                self.docker("start", "astrbot", timeout=step_timeout(deadline, 30))
            require_binding = (phase in ("backup", "restored", "committed")
                               and version(self.root / "plugins" / LIFE) != "0.1.0")
            identity = tuple(pending["container_start"]) if phase == "committed" and pending.get("container_start") else None
            self.settle(require_binding=require_binding, identity=identity, deadline=deadline, observe=observe)
            step_timeout(deadline, RECOVERY_TIMEOUT)
        except Exception as error:
            detail = "recovery deadline exceeded" if time.monotonic() >= deadline else redact(str(error)[:1024])
            self.finish_operation("manual-recovery", error="container state not confirmed; manual recovery required; "
                                  + reason + "; " + detail, last_known_container_state=last_known)
            return
        if phase == "committed":
            self.finish_operation("succeeded", result=pending["result"], error=None)
        else:
            self.finish_operation("failed", error=reason)

    def work(self, identifier):
        pending = self.saved_state("pending.json")
        if pending.get("operation_id") != identifier or pending["phase"] != "preparing":
            raise ValueError("operation is not ready to run")
        try:
            if self.approval(self.approval_key(pending)) != pending["approval"]:
                raise ValueError("reserved approval changed before validation")
            if pending["action"] == "release":
                with exclusive(self.base / "space.lock"):
                    self.release(pending)
            else:
                self.rollback(pending)
        except Exception as error:
            self.update_pending(worker_error=redact(str(error)[:2048]))
            raise

    def stop_for_operation(self, target_host_version):
        if not self.running():
            raise ValueError("container must be running before an approved operation")
        self.reserve(0)
        self.preflight(target_host_version)
        self.claim()
        self.update_pending(phase="backup")
        self.docker("stop", "--time", "45", "astrbot")
        self.preflight(target_host_version)

    def release(self, pending):
        digest, identifier = pending["bundle"], pending["backup_id"]
        approval = {k: v for k, v in pending["approval"].items() if k != "approval_id"}
        bundle = self.base / "staging" / digest
        if self.saved_state(bundle / "verified.json") != approval:
            raise ValueError("approval does not match verified bundle digest, source commit and versions")
        if sha(bundle / "bundle.tar.gz") != digest:
            raise ValueError("verified bundle changed")
        self.reserve(UNPACK_DISK_BUDGET)
        stage = self.base / ("work-" + pending["operation_id"])
        stage.mkdir(mode=0o700)
        manifest = parse_untrusted("bundle", bundle / "bundle.tar.gz", stage)
        if approval != approval_record(digest, manifest):
            raise ValueError("approval does not match bundle digest, source commit and versions")
        current = self.code_pair(self.root / "plugins")
        expected = {item["id"]: item for item in manifest["plugins"]}
        for plugin in IDS:
            wanted = tuple(map(int, expected[plugin]["version"].split(".")))
            installed = tuple(map(int, current[plugin]["version"].split(".")))
            if wanted <= installed:
                raise ValueError("release must advance both plugin versions; use guarded rollback for previous code")
        self.stop_for_operation(expected[HOST]["version"])
        self.backup(identifier)
        if self.code_pair(self.root / "plugins") != current:
            raise ValueError("plugin pair changed before backup completion")
        self.replace_code(stage / "plugins", self.base / "previous" / identifier, expected, current)
        write_json(self.base / "active.json", {"backup_id": identifier, "bundle": digest,
            "previous": current, "installed": self.code_pair(self.root / "plugins")})
        write_json(self.base / "last-operation.json", {"backup_id": identifier, "action": "release"})
        self.update_pending(phase="committed", result={"installed": digest, "backup_id": identifier,
                                                      "versions": approval["versions"]})
        with exclusive(self.base / "staging.lock", blocking=True):
            shutil.rmtree(bundle)
        try:
            self.prune_backups()
        except (OSError, ValueError, KeyError) as error:
            self.log("backup prune failed: " + type(error).__name__)

    def rollback(self, pending):
        identifier = pending["backup_id"]
        active = self.saved_state("active.json")
        if active.get("backup_id") != identifier or pending["approval"]["backup_id"] != identifier:
            raise ValueError("only the immediately preceding paired code may be restored")
        if self.current_versions() != pending["approval"]["versions"]:
            raise ValueError("rollback expected current versions do not match")
        self.verify_backup(self.base / "backups" / identifier)
        previous = self.base / "previous" / identifier
        expected = self.code_pair(previous)
        current = self.code_pair(self.root / "plugins")
        if expected != active["previous"] or current != active["installed"]:
            raise ValueError("rollback pair differs from deployment record")
        self.stop_for_operation(expected[HOST]["version"])
        if self.code_pair(self.root / "plugins") != current or self.code_pair(previous) != expected:
            raise ValueError("rollback pair changed during shutdown")
        self.replace_code(previous, self.base / "failed" / (identifier + "-" + uuid.uuid4().hex[:8]), expected, current)
        write_json(self.base / "last-operation.json", {"backup_id": identifier, "action": "rollback"})
        (self.base / "active.json").unlink()
        sync_directory(self.base)
        self.update_pending(phase="committed", result={"rolled_back_code": identifier, "data_restored": False})


def approve_main():
    parser = argparse.ArgumentParser(description="Approve and maintain production operations as root", allow_abbrev=False)
    commands = parser.add_subparsers(dest="action", required=True)
    release = commands.add_parser("release", allow_abbrev=False)
    release.add_argument("--reapprove", action="store_true")
    release.add_argument("sha256")
    release.add_argument("source_commit")
    rollback = commands.add_parser("rollback", allow_abbrev=False)
    rollback.add_argument("backup_id")
    for command in (release, rollback):
        command.add_argument("private_companion_version")
        command.add_argument("good_life_version")
    commands.add_parser("purge-uploads", allow_abbrev=False)
    commands.add_parser("cleanup-backups", allow_abbrev=False)
    recovery = commands.add_parser("recover", allow_abbrev=False)
    recovery.add_argument("operation_id")
    args = parser.parse_args()
    if os.name == "nt" or os.geteuid() != 0 or os.getuid() != 0:
        parser.error("run this command from your own root session")
    os.umask(0o077)
    try:
        deploy = Deployer(Path("/var/lib/astrbot"))
        with exclusive(deploy.base / "action.lock"):
            if args.action == "release":
                deploy.approve(args.sha256, args.source_commit, args.private_companion_version,
                               args.good_life_version, reapprove=args.reapprove)
                result = {"approved_release": args.sha256}
            elif args.action == "rollback":
                deploy.approve_rollback(args.backup_id, args.private_companion_version, args.good_life_version)
                result = {"approved_rollback": args.backup_id}
            elif args.action == "purge-uploads":
                result = deploy.purge_uploads()
            else:
                with exclusive(deploy.base / "job.lock"), exclusive(deploy.base / "worker.lock"):
                    if args.action == "cleanup-backups":
                        result = deploy.cleanup_backups()
                    else:
                        identifier = args.operation_id
                        pending = deploy.saved_state("pending.json")
                        if not OPERATION_ID.fullmatch(identifier) or pending.get("operation_id") != identifier:
                            raise ValueError("no matching interrupted operation")
                        if deploy.supervisor_running(identifier) is not False:
                            raise ValueError(f"cannot confirm that {deploy.supervisor_unit(identifier)} is inactive")
                        deploy.recover_operation(identifier, "root recovered the interrupted operation")
                        result = deploy.operation(identifier)
    except (OSError, ValueError) as error:
        parser.exit(1, f"companion-approve failed: {redact(str(error))}\n")
    print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--test-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--supervise")
    mode.add_argument("--work")
    args = parser.parse_args()
    rehearsal = args.test_root is not None
    if rehearsal and (Path(__file__).resolve() == Path("/usr/local/lib/companion-deploy/deploy_host.py") or not (args.test_root / ".lsr-rehearsal").is_file()):
        parser.error("test root is unavailable in the installed helper")
    os.umask(0o077)
    deploy = Deployer(args.test_root if rehearsal else Path("/var/lib/astrbot"), rehearsal=rehearsal)
    identifier = args.supervise or args.work
    if identifier is not None:
        if not OPERATION_ID.fullmatch(identifier) or (not rehearsal and os.geteuid() != 0):
            parser.error("invalid internal job invocation")
        os.umask(0o077)
        if args.supervise:
            deploy.supervise(identifier)
        else:
            with exclusive(deploy.base / "worker.lock"):
                deploy.work(identifier)
        return

    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    action = "invalid-command"
    try:
        try:
            action, argument = command_args(original)
        except ValueError:
            # Invalid invocations still share the request and audit limits.
            with request_slot(deploy.base, action):
                deploy.log("invalid request=" + ascii(original[:256]))
            raise
        readonly = action in ("logs", "status")
        with response_deadline(readonly), request_slot(deploy.base, action):
            deploy.log("request=" + ascii(original))
            try:
                if action == "logs":
                    count = int(argument or 100)
                    if not 1 <= count <= 500:
                        raise ValueError("logs count must be between 1 and 500")
                    response = deploy.logs(count)
                elif action in ("release", "rollback"):
                    result = deploy.launch(action, argument)
                elif action == "upload":
                    result = deploy.upload(argument)
                elif action == "operation":
                    result = deploy.operation(argument)
                else:
                    result = deploy.status()
            except TimeoutError:
                raise
            except Exception as error:
                deploy.log("failed " + action + " " + type(error).__name__)
                raise
            deploy.log("complete " + action)
            if readonly:
                if action == "status":
                    response = (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                write_readonly_response(response)
            else:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        print("companion-deploy failed: " + redact(str(error)[:2048]), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
