"""Build one offline, paired cutover candidate from quiesced backups.

This never changes the source databases or installs either plugin. The caller
must stop writes before taking the backups and install the paired outputs while
AstrBot is stopped. Candidate creation requires the paired phase-one implementation.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from stage_life_migration import _fingerprint, stage_all
from verify_calendar_adoption import verify
from good_life.prepared_commands import PreparedCommands
from good_life.configuration_commands import StagedConfigurationCommands
from good_life.state_scheduler import StateSchedules
from good_life.storage import LifeStorage


IMPLEMENTATION_COMPLETE = True
READY_KEY = "life.phase1.cutover_ready.v1"
CONFIG_OWNER_KEY = "life.configuration.execution_owner.v1"
OWNER_KEYS = ("life.calendar.execution_owner.v1", "life.activity.execution_owner.v1", "life.state.execution_owner.v1",
              CONFIG_OWNER_KEY)
CERTIFICATE_KEY = "life.calendar.adoption.v1"


def _check_quiesced(host):
    with closing(sqlite3.connect(host.as_uri() + "?mode=ro", uri=True)) as connection:
        # The process is stopped, so a recorded in-flight attempt has no live
        # worker. Startup recovery marks started attempts uncertain before the
        # life scheduler is admitted. Queued life tasks and attempts without
        # that evidence still require review.
        unsettled = connection.execute("""SELECT COUNT(*) FROM execution_tasks
            t LEFT JOIN execution_attempts a ON a.attempt_id=t.attempt_id AND a.task_id=t.task_id
                AND a.generation=t.generation
            WHERE t.state IN ('created','admitted','running','retry_scheduled','cancelling')
            AND (t.kind='calendar.plan' OR t.kind='role.state.maintain'
                 OR t.kind LIKE 'role.reading.%' OR t.kind LIKE 'role.social.%')
            AND (t.state IN ('created','retry_scheduled') OR a.attempt_id IS NULL
                 OR NOT json_valid(a.record_json)
                 OR (t.state IN ('running','cancelling') AND
                     json_extract(a.record_json,'$.started_at') IS NULL))""").fetchone()[0]
        if unsettled:
            raise ValueError(f"{unsettled} life tasks need recovery evidence before cutover")
        interrupted = dict(connection.execute("""SELECT kind,COUNT(*) FROM execution_tasks
            WHERE state IN ('admitted','running','cancelling') GROUP BY kind ORDER BY kind"""))
        for key in OWNER_KEYS:
            row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
            if row is not None and row[0] != "legacy":
                raise ValueError("life execution ownership was already changed")
        if connection.execute("SELECT 1 FROM runtime_metadata WHERE key=?", (CERTIFICATE_KEY,)).fetchone():
            raise ValueError("calendar adoption was already recorded")
        return interrupted


def _check_configuration(configuration):
    with closing(sqlite3.connect(configuration.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (CONFIG_OWNER_KEY,)).fetchone()
        if row is not None and row[0] != "legacy":
            raise ValueError("life configuration ownership was already changed")
        unsettled = connection.execute("""SELECT COUNT(*) FROM operations
            WHERE owner IN ('role.life.settings','role.state.settings') AND state != 'completed'""").fetchone()[0]
        if unsettled:
            raise ValueError(f"{unsettled} configuration operations are still unsettled")


def prepare(runtime_backup, configuration_backup, *, runtime_version, configuration_version, destination, source_commit):
    if not IMPLEMENTATION_COMPLETE:
        raise ValueError("phase-one execution, configuration and management work is not complete")
    if type(source_commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("the paired source commit must be a full Git SHA")
    host = Path(runtime_backup).resolve(strict=True)
    config = Path(configuration_backup).resolve(strict=True)
    target = Path(destination).resolve()
    if host == config or target.exists():
        raise ValueError("backup sources must differ and destination must be new")
    fingerprints = {"runtime": _fingerprint(host), "configuration": _fingerprint(config)}
    interrupted = _check_quiesced(host)
    _check_configuration(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".life-cutover-", dir=target.parent) as temporary:
        candidate = Path(temporary) / "candidate"
        candidate.mkdir(mode=0o700)
        life_dir = candidate / "life"
        staged = stage_all(host, config, runtime_version=runtime_version, configuration_version=configuration_version,
                           destination=life_dir)
        life_path = life_dir / "good-life.sqlite3"
        certificate = verify(host, life_path, expected_version=runtime_version)["certificate"]
        host_copy = candidate / "companion-v2.sqlite3"
        fd = os.open(host_copy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(host.as_uri() + "?mode=ro", uri=True)) as original, \
                closing(sqlite3.connect(host_copy)) as copy:
            original.backup(copy)
            with copy:
                for key in OWNER_KEYS:
                    copy.execute("INSERT OR REPLACE INTO runtime_metadata(key,value) VALUES (?, 'good_life')", (key,))
                copy.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)",
                             (CERTIFICATE_KEY, json.dumps(certificate, sort_keys=True, separators=(",", ":"))))
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok" or copy.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("host candidate failed integrity checks")
        config_dir = candidate / "configuration"
        config_dir.mkdir(mode=0o700)
        config_copy = config_dir / "companion-v2.sqlite3"
        fd = os.open(config_copy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(config.as_uri() + "?mode=ro", uri=True)) as original, \
                closing(sqlite3.connect(config_copy)) as copy:
            original.backup(copy)
            with copy:
                copy.execute("INSERT OR REPLACE INTO runtime_metadata(key,value) VALUES (?, 'good_life')", (CONFIG_OWNER_KEY,))
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok" or copy.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("configuration candidate failed integrity checks")
        storage = LifeStorage.open(life_dir)
        try:
            PreparedCommands(storage).install()
            StagedConfigurationCommands(storage).install()
            StateSchedules(storage).install()
            with storage.transaction() as db:
                db.execute("INSERT INTO life_metadata(key,value) VALUES (?, 'ready')", (READY_KEY,))
        finally:
            storage.close()
        if {"runtime": _fingerprint(host), "configuration": _fingerprint(config)} != fingerprints:
            raise ValueError("a source backup changed during preparation")
        result = {"state": "candidate", "source_commit": source_commit, "source_sha256": fingerprints,
                  "runtime_version": runtime_version, "configuration_version": configuration_version,
                  "host_sha256": _fingerprint(host_copy), "configuration_sha256": _fingerprint(config_copy),
                  "life_sha256": _fingerprint(life_path),
                  "calendar_certificate": certificate, "migration": staged["steps"],
                  "interrupted_tasks_for_host_recovery": interrupted}
        manifest = candidate / "manifest.json"
        fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        if target.exists():
            raise ValueError("cutover destination already exists")
        os.rename(candidate, target)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_backup")
    parser.add_argument("configuration_backup")
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--configuration-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.runtime_backup, args.configuration_backup, runtime_version=args.runtime_version,
                         configuration_version=args.configuration_version, source_commit=args.source_commit,
                         destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"cutover candidate failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
