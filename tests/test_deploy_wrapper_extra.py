"""Additional archive and rollback refusal regressions, using the same isolated root."""
from contextlib import closing
import hashlib
import io
import shutil
import sqlite3
import tarfile

import pytest

from test_deploy_wrapper import (
    NEW_VERSIONS, approve, approve_rollback, bundle, docker_calls, fake_root,
    host, invoke, make_code, refused_job, release_pair, run_job, upload,
)


def test_bundle_own_manifest_tampering_rejected(fake_root, tmp_path):
    payload, _ = bundle(tmp_path)
    altered = io.BytesIO()
    with (tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as source,
          tarfile.open(fileobj=altered, mode="w:gz", format=tarfile.USTAR_FORMAT) as target):
        for entry in source:
            data = source.extractfile(entry).read()
            if entry.name == "SHA256SUMS.txt":
                tampered = (b"1" if data.startswith(b"0") else b"0") + data[1:]
                assert tampered != data
                data = tampered
            entry.size = len(data)
            target.addfile(entry, io.BytesIO(data))
    payload = altered.getvalue()
    result = invoke(fake_root, "upload " + hashlib.sha256(payload).hexdigest(), payload, ok=False)
    assert b"bundle checksum manifest mismatch" in result.stderr


def test_backup_digest_tamper_refuses_rollback(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    approve_rollback(deploy, identifier)
    path = deploy.base / "backups" / identifier / "sqlite/2.sqlite3"
    path.write_bytes(b"tampered")
    before = docker_calls(fake_root).count("stop")
    assert "backup manifest digest mismatch" in refused_job(fake_root, "rollback " + identifier)
    assert docker_calls(fake_root).count("stop") == before
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"


def test_legacy_code_rollback_refused(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    approve_rollback(deploy, identifier)
    previous = deploy.base / "previous" / identifier / host.HOST
    shutil.rmtree(previous)
    make_code(previous, host.HOST, "7.1.16")
    assert "prepare_life_rollback.py" in refused_job(fake_root, "rollback " + identifier)
    assert host.version(fake_root / "plugins" / host.HOST) == "7.1.18"


def test_a3_history_refuses_rollback_even_when_flags_are_off(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    approve_rollback(deploy, identifier)
    with closing(sqlite3.connect(deploy.database_paths()[2])) as db:
        db.execute("CREATE TABLE staged_life_runtime_metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO staged_life_runtime_metadata VALUES('a3.reading.schema.version','1')")
        db.commit()
    assert "A3 history" in refused_job(fake_root, "rollback " + identifier)
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"


def test_pending_journal_blocks_release_and_rollback(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    approve_rollback(deploy, identifier)
    next_release = tmp_path / "next"
    next_release.mkdir()
    versions = {host.HOST: "7.1.19", host.LIFE: "0.1.2"}
    digest = upload(fake_root, next_release, versions=versions.items())
    approve(deploy, digest, versions=versions)
    host.write_json(deploy.base / "pending.json", {"state": "manual-recovery", "backup_id": identifier})
    before = docker_calls(fake_root).count("stop")
    assert "manual inspection" in refused_job(fake_root, "release " + digest)
    assert "manual inspection" in refused_job(fake_root, "rollback " + identifier)
    assert docker_calls(fake_root).count("stop") == before
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"


def test_task_without_recovery_evidence_blocks_release_before_stop(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    approval = approve(deploy, digest)
    with closing(sqlite3.connect(deploy.database_paths()[0])) as db:
        db.execute("INSERT INTO execution_tasks VALUES('reading','role.reading.consume','running',NULL,1)")
        db.commit()
    assert "life tasks need recovery evidence" in refused_job(fake_root, "release " + digest)
    assert "stop" not in docker_calls(fake_root)
    assert approval.is_file()
    assert (fake_root / "running").exists()
    assert not list((deploy.base / "backups").glob("*/manifest.json"))


def test_legacy_upload_is_refused_before_release(fake_root, tmp_path):
    payload, digest = bundle(tmp_path, versions=((host.HOST, "7.1.16"), (host.LIFE, "0.0.9")))
    result = invoke(fake_root, "upload " + digest, payload, ok=False)
    assert b"prepare_life_rollback.py" in result.stderr


@pytest.mark.parametrize("versions", [NEW_VERSIONS, {host.HOST: "7.1.19", host.LIFE: "0.1.0"}])
def test_release_cannot_reinstall_or_downgrade_current_pair(fake_root, tmp_path, versions):
    deploy, _ = release_pair(fake_root, tmp_path)
    next_release = tmp_path / "next"
    next_release.mkdir()
    digest = upload(fake_root, next_release, versions=versions.items())
    approval = approve(deploy, digest, reapprove=True, versions=versions)
    before = docker_calls(fake_root).count("stop")
    assert "advance both plugin versions" in refused_job(fake_root, "release " + digest)
    assert docker_calls(fake_root).count("stop") == before
    assert approval.is_file()
    assert host.version(fake_root / "plugins" / host.HOST) == "7.1.18"


def test_rollback_requires_expected_current_versions_at_use(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    with pytest.raises(ValueError, match="expected current versions"):
        deploy.approve_rollback(identifier, "7.1.19", "0.1.1")
    approval = approve_rollback(deploy, identifier)
    original = approval.read_bytes()
    current = fake_root / "plugins" / host.HOST
    preserved = tmp_path / "original-current"
    current.rename(preserved)
    make_code(current, host.HOST, "7.1.19")
    before = docker_calls(fake_root).count("stop")
    assert "current versions" in refused_job(fake_root, "rollback " + identifier)
    assert docker_calls(fake_root).count("stop") == before
    assert approval.read_bytes() == original
    shutil.rmtree(current)
    preserved.rename(current)
    assert run_job(fake_root, "rollback " + identifier)["rolled_back_code"] == identifier


def test_rollback_refuses_host_v2_intent_marker(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    approval = approve_rollback(deploy, identifier)
    db_path = deploy.database_paths()[0]
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("INSERT INTO runtime_metadata VALUES('life.async.intent.v2:fixture', 'recorded')")
        db.commit()
    before = host.sha(db_path)
    stops = docker_calls(fake_root).count("stop")
    assert "host v2 state has no reviewed reader" in refused_job(fake_root, "rollback " + identifier)
    assert docker_calls(fake_root).count("stop") == stops
    assert approval.is_file()
    assert (fake_root / "running").exists()
    assert host.sha(db_path) == before
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"


@pytest.mark.parametrize("text", ['Cookie: session=one; csrf=two', '{"cookie":"session=one; csrf=two"}',
    'Authorization: Bearer one', '{"api_key": "one two"}'])
def test_obvious_secrets_redacted(text):
    redacted = host.redact(text)
    assert "one" not in redacted and "two" not in redacted
