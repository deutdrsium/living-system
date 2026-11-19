"""Local release authorization gates; no network or SSH calls."""
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import deploy_release as release

HEAD = "a" * 40
DIGEST = "b" * 64
OPERATION_ID = "c" * 32
OPERATION_COMMAND = "operation " + OPERATION_ID
QUEUED = {"operation_id": OPERATION_ID, "state": "queued"}


def responses(*, dirty="", conclusion="success", status="completed", checks=None, statuses=None):
    return iter([dirty, HEAD, json.dumps([{"headSha": HEAD, "status": status, "conclusion": conclusion,
        "createdAt": "2026-11-18T00:00:00Z", "event": "pull_request"}]),
        json.dumps(checks or {"total_count": 1, "check_runs": [{"id": 1, "name": "good-life", "app": {"id": 1},
            "status": "completed", "conclusion": "success"}]}),
        json.dumps(statuses or {"total_count": 0, "state": "pending"})])


def test_clean_exact_commit_green(monkeypatch):
    values = responses()
    monkeypatch.setattr(release, "output", lambda command: next(values))
    assert release.check_source(None) == HEAD


def test_dirty_tree_refused_before_ci(monkeypatch):
    calls = []
    def output(command):
        calls.append(command)
        return "?? unexpected-file"
    monkeypatch.setattr(release, "output", output)
    with pytest.raises(ValueError, match="clean working tree"):
        release.check_source(None)
    assert len(calls) == 1 and calls[0][0] == "git"


@pytest.mark.parametrize("status,conclusion", [("in_progress", ""), ("completed", "failure"), ("completed", "cancelled")])
def test_ci_not_green_refused(monkeypatch, status, conclusion):
    values = responses(status=status, conclusion=conclusion)
    monkeypatch.setattr(release, "output", lambda command: next(values))
    with pytest.raises(ValueError, match="CI run"):
        release.check_source(None)


def test_absent_ci_refused(monkeypatch):
    values = iter(["", HEAD, "[]"])
    monkeypatch.setattr(release, "output", lambda command: next(values))
    with pytest.raises(ValueError, match="CI run"):
        release.check_source(None)


def test_additional_failed_check_refused(monkeypatch):
    values = responses(checks={"total_count": 1, "check_runs": [{"id": 1, "name": "other", "status": "completed", "conclusion": "failure"}]})
    monkeypatch.setattr(release, "output", lambda command: next(values))
    with pytest.raises(ValueError, match="check runs"):
        release.check_source(None)


def test_newer_rerun_supersedes_old_failure(monkeypatch):
    values = responses(checks={"total_count": 2, "check_runs": [
        {"id": 1, "name": "good-life", "status": "completed", "conclusion": "failure"},
        {"id": 2, "name": "good-life", "status": "completed", "conclusion": "success"}]})
    monkeypatch.setattr(release, "output", lambda command: next(values))
    assert release.check_source(None) == HEAD


def test_failed_commit_status_refused(monkeypatch):
    values = responses(statuses={"total_count": 1, "state": "failure"})
    monkeypatch.setattr(release, "output", lambda command: next(values))
    with pytest.raises(ValueError, match="status contexts"):
        release.check_source(None)


def test_dry_run_prints_without_build_or_ssh(monkeypatch, capsys):
    monkeypatch.setattr(release, "check_source", lambda commit: HEAD)
    def forbidden(*args, **kwargs):
        pytest.fail("dry run attempted build, temporary files or SSH")
    monkeypatch.setattr(release, "package", forbidden)
    monkeypatch.setattr(release, "remote", forbidden)
    monkeypatch.setattr(release.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(sys, "argv", ["deploy_release.py", "--dry-run"])
    release.main()
    printed = capsys.readouterr().out
    assert "upload <sha256>" in printed
    assert f"companion-approve release <sha256> {HEAD}" in printed
    assert "type approved" in printed
    assert "'release <sha256>' -> operation_id" in printed
    assert "20 minutes" in printed and "'operation <operation_id>'" in printed
    assert "backup" not in printed and "install <sha256>" not in printed


def operation_reply(state, **fields):
    return {"operation_id": OPERATION_ID, "action": "release", "state": state, **fields}


@pytest.fixture
def mocked_release(monkeypatch, tmp_path):
    archive = tmp_path / "paired.tar.gz"
    archive.write_bytes(b"synthetic bundle")
    manifest = {"source_commit": HEAD, "plugins": [
        {"id": "astrbot_plugin_private_companion", "version": "7.1.18"},
        {"id": "astrbot_plugin_good_life", "version": "0.1.1"},
    ]}
    monkeypatch.setattr(release, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda seconds: None))

    def setup(*operation_replies, queue_reply=QUEUED):
        calls = []
        replies = iter(operation_replies)

        def confirm(prompt):
            calls.append("confirm")
            return "approved"

        def remote(command, *, source=None, timeout=None):
            calls.append(command)
            if command == "upload " + DIGEST:
                assert source.read() == b"synthetic bundle"
                return {"verified": DIGEST, "source_commit": HEAD, "plugins": []}
            assert source is None and 0 < timeout <= release.REQUEST_TIMEOUT
            if command == "release " + DIGEST:
                reply = queue_reply
            else:
                assert command == OPERATION_COMMAND
                reply = next(replies)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(release, "remote", remote)
        monkeypatch.setattr("builtins.input", confirm)
        return calls, lambda: release.deploy_uploaded(archive, DIGEST, manifest)

    return setup


def test_approved_release_uploads_queues_polls_and_prints_result(mocked_release, capsys):
    result = {"installed": DIGEST, "backup_id": "synthetic-backup", "versions": {
        "astrbot_plugin_private_companion": "7.1.18", "astrbot_plugin_good_life": "0.1.1",
    }}
    calls, deploy = mocked_release(operation_reply("queued"), operation_reply("running"),
                                   operation_reply("recovering"), operation_reply("succeeded", result=result))
    deploy()
    assert calls == ["upload " + DIGEST, "confirm", "release " + DIGEST] + [OPERATION_COMMAND] * 4
    printed = capsys.readouterr().out.splitlines()
    assert f"/usr/local/sbin/companion-approve release {DIGEST} {HEAD} 7.1.18 0.1.1" in printed
    assert f"Release operation ID: {OPERATION_ID}" in printed
    assert json.loads(printed[-1]) == result


def test_declined_approval_leaves_upload_unqueued(mocked_release, monkeypatch):
    calls, deploy = mocked_release()
    monkeypatch.setattr("builtins.input", lambda prompt: "cancel")
    with pytest.raises(ValueError, match="approval not confirmed"):
        deploy()
    assert calls == ["upload " + DIGEST]


@pytest.mark.parametrize("state", ["failed", "manual-recovery"])
def test_terminal_failure_reports_details_and_operation(mocked_release, state):
    calls, deploy = mocked_release(operation_reply(state, error="synthetic server error"))
    with pytest.raises(ValueError, match=f"{OPERATION_ID}: {state}: synthetic server error") as failure:
        deploy()
    assert OPERATION_COMMAND in str(failure.value)
    assert calls[-2:] == ["release " + DIGEST, OPERATION_COMMAND]


@pytest.mark.parametrize("error", [
    OSError("lost connection"),
    subprocess.CalledProcessError(255, "ssh"),
    subprocess.TimeoutExpired("ssh", 30),
    json.JSONDecodeError("invalid reply", "", 0),
    KeyboardInterrupt(),
])
def test_poll_interruption_preserves_operation_and_instructions(mocked_release, error, capsys):
    calls, deploy = mocked_release(error)
    with pytest.raises(ValueError, match=rf"{OPERATION_ID}.*ssh -T -o BatchMode=yes companion-deploy '{OPERATION_COMMAND}'"):
        deploy()
    assert OPERATION_ID in capsys.readouterr().out
    assert calls[-2:] == ["release " + DIGEST, OPERATION_COMMAND]


@pytest.mark.parametrize("queue_reply", [OSError("lost connection"), {"operation_id": "bad ID", "state": "queued"}])
def test_missing_valid_queue_receipt_is_not_retried(mocked_release, queue_reply):
    calls, deploy = mocked_release(queue_reply=queue_reply)
    with pytest.raises(ValueError, match=rf"receipt unavailable for {DIGEST}.*server status"):
        deploy()
    assert calls == ["upload " + DIGEST, "confirm", "release " + DIGEST]


@pytest.mark.parametrize("reply,reason", [
    (operation_reply("succeeded", operation_id="d" * 32), "operation identity"),
    (operation_reply("running", action="rollback"), "operation identity"),
    (operation_reply("unknown"), "unexpected release state"),
    (operation_reply("succeeded", result={"installed": "a" * 64}), "installed bundle digest"),
])
def test_unexpected_operation_reply_fails_with_manual_polling(mocked_release, reply, reason):
    calls, deploy = mocked_release(reply)
    with pytest.raises(ValueError, match=rf"{reason}.*{OPERATION_COMMAND}"):
        deploy()
    assert calls[-2:] == ["release " + DIGEST, OPERATION_COMMAND]


def test_polling_is_bounded_including_ssh_requests(monkeypatch):
    now = 0
    sleeps = []
    timeouts = []

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def remote(command, *, timeout):
        assert command == OPERATION_COMMAND
        timeouts.append(timeout)
        return operation_reply("running")

    monkeypatch.setattr(release, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
    monkeypatch.setattr(release, "POLL_TIMEOUT", 7)
    monkeypatch.setattr(release, "remote", remote)
    with pytest.raises(ValueError, match="polling timed out after 7 seconds"):
        release.poll_release(OPERATION_ID, DIGEST)
    assert timeouts == [7, 2]
    assert sleeps == [5, 2]


def test_git_export_excludes_ignored_local_content_and_uses_committed_bytes(monkeypatch, tmp_path):
    import build_paired_release as builder
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repository, text=True, encoding="utf-8",
                                       creationflags=release.NO_CONSOLE).strip()
    git("init", "-q")
    (repository / ".gitignore").write_text("*secrets*.json\n", encoding="utf-8")
    for plugin in ("astrbot_plugin_good_life", "astrbot_plugin_private_companion"):
        code = repository / "plugins" / plugin
        code.mkdir(parents=True)
        (code / "main.py").write_bytes(b"# committed\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm",
        "Synthetic source fixture\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    commit = git("rev-parse", "HEAD")
    ignored = repository / "plugins/astrbot_plugin_good_life/local-secrets.json"
    ignored.write_text('{"synthetic": "must-never-ship"}', encoding="utf-8")
    assert git("status", "--porcelain") == ""
    # A concurrent local edit after the clean check must not affect commit bytes.
    (ignored.parent / "main.py").write_bytes(b"# uncommitted\n")
    destination = tmp_path / "export"
    destination.mkdir()
    monkeypatch.setattr(builder, "ROOT", repository)
    snapshot = builder.export_sources(commit, destination)
    assert not list(snapshot.rglob("*secrets*"))
    assert (snapshot / "plugins/astrbot_plugin_good_life/main.py").read_bytes().replace(b"\r\n", b"\n") == b"# committed\n"


def test_remote_failure_keeps_wrapper_diagnostics_visible(monkeypatch, capsys):
    def failed(command, **kwargs):
        assert command[-1] == OPERATION_COMMAND
        assert kwargs["timeout"] == 3
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="operation state unavailable\n")
    monkeypatch.setattr(release.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        release.remote(OPERATION_COMMAND, timeout=3)
    assert "operation state unavailable" in capsys.readouterr().err
