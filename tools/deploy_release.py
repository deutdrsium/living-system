"""Build and drive a CI-approved paired code upgrade through the restricted SSH alias.

Use --dry-run for a read-only plan. --execute waits for the user's root approval
before queuing a release. If local polling stops, resume checking the same job with
ssh -T -o BatchMode=yes companion-deploy 'operation <operation_id>'.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time

from build_paired_release import build

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "deutdrsium/living-system-refactor"
POLL_TIMEOUT = 20 * 60
POLL_INTERVAL = 5
REQUEST_TIMEOUT = 30
OPERATION_ERRORS = (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt)
NO_CONSOLE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def output(command):
    return subprocess.check_output(command, cwd=ROOT, text=True, encoding="utf-8",
                                   creationflags=NO_CONSOLE).strip()


def check_source(commit):
    if output(["git", "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("release requires a completely clean working tree")
    head = output(["git", "rev-parse", "HEAD"])
    if commit and output(["git", "rev-parse", "--verify", commit + "^{commit}"]) != head:
        raise ValueError("requested commit must be the checked-out HEAD")
    runs = json.loads(output(["gh", "run", "list", "--repo", REPOSITORY, "--commit", head,
        "--workflow", "ci.yml", "--limit", "100", "--json", "headSha,status,conclusion,event,createdAt"]))
    runs = sorted((run for run in runs if run["headSha"] == head), key=lambda run: run["createdAt"], reverse=True)
    if not runs or runs[0]["status"] != "completed" or runs[0]["conclusion"] != "success":
        raise ValueError("latest CI run for the exact commit must have completed successfully")
    # Include additional commit statuses, not merely one successful CI job.
    checks = json.loads(output(["gh", "api", f"repos/{REPOSITORY}/commits/{head}/check-runs?per_page=100"]))
    if checks.get("total_count", 0) > 100:
        raise ValueError("too many check runs to verify; refusing partial CI evidence")
    latest = {}
    for check in checks.get("check_runs", []):
        key = (check["name"], check.get("app", {}).get("id"))
        if key not in latest or check["id"] > latest[key]["id"]:
            latest[key] = check
    if not latest or any(item["status"] != "completed" or item["conclusion"] not in ("success", "skipped", "neutral") for item in latest.values()):
        raise ValueError("commit check runs are missing, pending or failed")
    statuses = json.loads(output(["gh", "api", f"repos/{REPOSITORY}/commits/{head}/status"]))
    if statuses.get("total_count", 0) and statuses.get("state") != "success":
        raise ValueError("commit status contexts are not green")
    return head


def package(destination):
    release = destination / "paired"
    manifest = build(release)
    archive = destination / "paired.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as bundle:
        for path in sorted(release.iterdir()):
            bundle.add(path, arcname=path.name, recursive=False)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest, manifest


def remote(command, *, source=None, timeout=None):
    result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "companion-deploy", command],
        cwd=ROOT, stdin=source if source else subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, text=True, encoding="utf-8", timeout=timeout,
        creationflags=NO_CONSOLE)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    result.check_returncode()
    return json.loads(result.stdout)


def request_approval(digest, manifest):
    versions = {item["id"]: item["version"] for item in manifest["plugins"]}
    command = " ".join(("/usr/local/sbin/companion-approve", "release", digest, manifest["source_commit"],
                        versions["astrbot_plugin_private_companion"], versions["astrbot_plugin_good_life"]))
    print("Run this approval command from your own root SSH session:", command, sep="\n", flush=True)
    try:
        answer = input("After root approval succeeds, type approved to continue: ")
    except EOFError:
        answer = ""
    if answer != "approved":
        raise ValueError("approval not confirmed; upload retained")


def poll_release(operation_id, digest):
    deadline = time.monotonic() + POLL_TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        status = remote("operation " + operation_id, timeout=min(REQUEST_TIMEOUT, remaining))
        if (not isinstance(status, dict) or status.get("operation_id") != operation_id
                or status.get("action") != "release"):
            raise ValueError("server did not confirm the release operation identity")
        state = status.get("state")
        if state == "succeeded":
            installed = status.get("result")
            if not isinstance(installed, dict) or installed.get("installed") != digest:
                raise ValueError("server's release result did not confirm the installed bundle digest")
            return installed
        if state in ("failed", "manual-recovery"):
            raise ValueError(f"{state}: {status.get('error', 'no error details returned')}")
        if state not in ("queued", "running", "recovering"):
            raise ValueError(f"unexpected release state {state!r}")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_INTERVAL, remaining))
    raise ValueError(f"polling timed out after {POLL_TIMEOUT} seconds")


def deploy_uploaded(archive, digest, manifest):
    with archive.open("rb") as stream:
        uploaded = remote("upload " + digest, source=stream)
    if (not isinstance(uploaded, dict) or uploaded.get("verified") != digest
            or uploaded.get("source_commit") != manifest["source_commit"]):
        raise ValueError("server did not confirm bundle digest and source commit")
    request_approval(digest, manifest)

    try:
        queued = remote("release " + digest, timeout=REQUEST_TIMEOUT)
        operation_id = queued.get("operation_id") if isinstance(queued, dict) else None
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("server did not return a valid operation ID")
    except OPERATION_ERRORS as error:
        raise ValueError(f"release receipt unavailable for {digest}: {str(error) or type(error).__name__}. "
                         "Check the runbook and server status before resubmitting.") from error

    print(f"Release operation ID: {operation_id}", flush=True)
    try:
        if queued.get("state") != "queued":
            raise ValueError(f"unexpected submission state {queued.get('state')!r}")
        installed = poll_release(operation_id, digest)
    except OPERATION_ERRORS as error:
        raise ValueError(f"release operation {operation_id}: {str(error) or type(error).__name__}. "
                         f"Check with ssh -T -o BatchMode=yes companion-deploy 'operation {operation_id}'.") from error
    print(json.dumps(installed, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--commit", help="optional commit, must resolve to checked-out HEAD")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read git/CI and print only; no build or SSH")
    mode.add_argument("--execute", action="store_true", help="run a separately user-approved production upgrade")
    args = parser.parse_args()
    try:
        commit = check_source(args.commit)
        if args.dry_run:
            print(f"commit {commit}: clean, CI green")
            print("build_paired_release.py -> temporary paired.tar.gz -> SHA256")
            print("ssh -T companion-deploy 'upload <sha256>' < paired.tar.gz")
            print(f"root approval: /usr/local/sbin/companion-approve release <sha256> {commit} <private_companion_version> <good_life_version>")
            print("type approved after root approval succeeds")
            print("ssh -T companion-deploy 'release <sha256>' -> operation_id")
            print(f"poll for up to {POLL_TIMEOUT // 60} minutes with ssh -T -o BatchMode=yes companion-deploy 'operation <operation_id>'")
            return
        if not sys.stdin.isatty():
            raise ValueError("execute requires an interactive terminal for the root approval step")
        with tempfile.TemporaryDirectory(prefix="lsr-release-") as temporary:
            archive, digest, manifest = package(Path(temporary))
            if manifest["source_commit"] != commit or check_source(commit) != commit:
                raise ValueError("source commit or CI changed during build")
            deploy_uploaded(archive, digest, manifest)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"paired upgrade failed: {error}\n")


if __name__ == "__main__":
    main()
