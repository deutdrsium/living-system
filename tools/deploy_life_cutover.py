"""Perform one stopped, paired production cutover from a verified release.

Run on the AstrBot host. The release directory contains the two archives,
release-manifest.json, a Git-archive tools.zip, and baseline-manifest.json.
Backups and candidates stay in a private directory under the AstrBot data root.
Once the new container has been started, rollback requires the paired rollback
tool because the life database may already contain new work.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile


HOST_ID = "astrbot_plugin_private_companion"
LIFE_ID = "astrbot_plugin_good_life"
RUNTIME_VERSION = "companion-v2.storage.v71"
CONFIGURATION_VERSION = "configuration-store.v1"


def log(message):
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive, destination, expected_root):
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = PurePosixPath(entry.filename)
            if (name.is_absolute() or not name.parts or name.parts[0] != expected_root
                    or any(part in (".", "..") for part in name.parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("release archive contains an unexpected path")
        source.extractall(destination)


def check_baseline(root, manifest_path, baseline_commit):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not re.fullmatch(r"[0-9a-f]{12,40}", baseline_commit)
            or not str(manifest.get("source_commit", "")).startswith(baseline_commit)
            or type(manifest.get("files")) is not dict):
        raise ValueError("production baseline manifest is invalid")
    for relative, expected in manifest["files"].items():
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("production baseline contains an invalid path")
        path = root.joinpath(*name.parts)
        if not path.is_file() or hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != expected:
            raise ValueError("production plugin source changed since the recorded baseline")
    return len(manifest["files"])


def snapshot(source, destination, expected_version):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original, \
            closing(sqlite3.connect(destination)) as backup:
        original.backup(backup)
        version = backup.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()
        if version is None or version[0] != expected_version or backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("stopped database version or integrity does not match")
    os.chmod(destination, 0o600)


def deploy(data_root, release_dir, *, tools_sha256, container, baseline_commit):
    data = Path(data_root).resolve(strict=True)
    release = Path(release_dir).resolve(strict=True)
    plugins = data / "plugins"
    plugin_data = data / "plugin_data"
    host_code, life_code = plugins / HOST_ID, plugins / LIFE_ID
    host_data, life_data = plugin_data / HOST_ID, plugin_data / LIFE_ID
    runtime = host_data / "installations" / "main" / "runtime"
    host_db, config_db = runtime / "companion-v2.sqlite3", runtime / "configuration" / "companion-v2.sqlite3"
    if not host_code.is_dir() or not host_data.is_dir() or not host_db.is_file() or not config_db.is_file():
        raise ValueError("expected production plugin or database is missing")
    if life_code.exists() or life_data.exists():
        raise ValueError("life plugin or data already exists; refusing a second migration")
    status = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", container], text=True).strip()
    if status != "true":
        raise ValueError("AstrBot container must be running before cutover")
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    commit = manifest.get("source_commit")
    if (type(commit) is not str or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit)
            or {item.get("id") for item in manifest.get("plugins", [])} != {HOST_ID, LIFE_ID}):
        raise ValueError("paired release manifest is invalid")
    for item in manifest["plugins"]:
        name = item["archive"]
        if Path(name).name != name or sha256(release / name) != item["sha256"]:
            raise ValueError("paired release archive does not match its manifest")
    tools_archive = release / "tools.zip"
    if sha256(tools_archive) != tools_sha256:
        raise ValueError("cutover tools archive checksum differs")
    log(f"Verified {check_baseline(host_code, release / 'baseline-manifest.json', baseline_commit)} production source files")
    base = data / "good-life-cutovers"
    base.mkdir(mode=0o700, exist_ok=True)
    run = base / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:8])
    run.mkdir(mode=0o700)
    log(f"Private backup and migration directory: {run}")
    prepared = run / "prepared"
    (prepared / "plugins").mkdir(parents=True, mode=0o700)
    for item in manifest["plugins"]:
        extract(release / item["archive"], prepared / "plugins", item["id"])
    extract(tools_archive, prepared, "tools")
    shutil.copy2(release / "release-manifest.json", run / "release-manifest.json")
    shutil.copy2(release / "baseline-manifest.json", run / "baseline-manifest.json")
    stopped = started = False
    moves = []
    created_life_data = False

    def move(source, destination):
        if destination.exists():
            raise ValueError("cutover target unexpectedly exists")
        os.rename(source, destination)
        moves.append((source, destination))

    try:
        log("Stopping AstrBot to quiesce both databases")
        # Treat an interrupted stop as potentially successful and always try to
        # start the original service when preparation fails.
        stopped = True
        subprocess.run(["docker", "stop", "--time", "45", container], check=True)
        backup = run / "backup"
        backup.mkdir(mode=0o700)
        shutil.copytree(host_code, backup / "source", symlinks=True)
        shutil.copytree(host_data, backup / "plugin-data", symlinks=True)
        snapshots = backup / "sqlite"
        snapshots.mkdir(mode=0o700)
        host_snapshot, config_snapshot = snapshots / "runtime.sqlite3", snapshots / "configuration.sqlite3"
        snapshot(host_db, host_snapshot, RUNTIME_VERSION)
        snapshot(config_db, config_snapshot, CONFIGURATION_VERSION)
        log("Saved source, assets, configuration and consistent stopped database backups")
        candidate = run / "candidate"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(str(prepared / "plugins" / name) for name in (HOST_ID, LIFE_ID))
        command = [sys.executable, str(prepared / "tools" / "prepare_life_cutover.py"),
                   str(host_snapshot), str(config_snapshot), "--runtime-version", RUNTIME_VERSION,
                   "--configuration-version", CONFIGURATION_VERSION, "--source-commit", commit,
                   "--destination", str(candidate)]
        with (run / "candidate-output.json").open("w", encoding="utf-8") as output:
            subprocess.run(command, env=env, stdout=output, check=True)
        log("Prepared and checked the paired data candidate")
        previous = run / "original-database-files"
        (previous / "runtime").mkdir(parents=True, mode=0o700)
        (previous / "configuration").mkdir(mode=0o700)
        move(host_code, run / "original-code")
        move(prepared / "plugins" / HOST_ID, host_code)
        move(prepared / "plugins" / LIFE_ID, life_code)
        for original, folder, replacement in (
            (host_db, previous / "runtime", candidate / "companion-v2.sqlite3"),
            (config_db, previous / "configuration", candidate / "configuration" / "companion-v2.sqlite3"),
        ):
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(original) + suffix)
                if path.exists():
                    move(path, folder / path.name)
            move(replacement, original)
        life_data.mkdir(mode=0o700)
        created_life_data = True
        move(candidate / "life" / "good-life.sqlite3", life_data / "good-life.sqlite3")
        log("Installed both plugins and the paired databases; starting AstrBot")
        # A start can succeed even if the client loses its response. From this
        # point on, restoring snapshots directly could discard new life data.
        started = True
        subprocess.run(["docker", "start", container], check=True)
        log("AstrBot started with the new owner; online acceptance is still required")
        return {"state": "started", "source_commit": commit, "backup": str(run)}
    except BaseException:
        if stopped and not started:
            log("Cutover stopped before new execution; restoring the original files")
            for source, destination in reversed(moves):
                if destination.exists() and not source.exists():
                    os.rename(destination, source)
                else:
                    raise RuntimeError("automatic restoration needs manual inspection")
            if created_life_data and life_data.is_dir():
                life_data.rmdir()
            subprocess.run(["docker", "start", container], check=True)
            log("Original AstrBot service restarted")
        elif started:
            try:
                running = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Running}}", container],
                                                  text=True).strip()
                if running != "true":
                    subprocess.run(["docker", "start", container], check=True)
                    log("Restarted AstrBot with the installed paired data")
            except Exception as error:
                log(f"Could not restart AstrBot automatically: {type(error).__name__}")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir")
    parser.add_argument("--tools-sha256", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--data-root", default="/var/lib/astrbot")
    parser.add_argument("--container", default="astrbot")
    args = parser.parse_args()
    try:
        result = deploy(args.data_root, args.release_dir, tools_sha256=args.tools_sha256,
                        container=args.container, baseline_commit=args.baseline_commit)
    except Exception as error:
        log(f"Cutover failed: {type(error).__name__}: {error}")
        raise SystemExit(1) from error
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
