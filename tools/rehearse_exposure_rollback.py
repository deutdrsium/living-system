"""Reconstruct activities and browsing receipts into a new isolated host copy."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from good_life.exposure_import import TABLES, _source_rows, _staged_rows, _summary
from rehearse_activity_rollback import _apply as apply_activities, _verify as verify_activities
from rehearse_calendar_rollback import _open
from verify_exposure_authority import verify_contexts


def _verify(host, life, expected_version):
    row = life.execute("SELECT source_version,summary_json FROM staged_exposure_manifest WHERE migration_id='exposures.v2'").fetchone()
    if row is None or row[0] != expected_version:
        raise ValueError("browsing migration manifest is unavailable")
    manifest = json.loads(row[1])
    staged = {name: tuple(tuple(row) for row in rows) for name, rows in _staged_rows(life).items()}
    host_rows = {name: tuple(tuple(row) for row in rows) for name, rows in _source_rows(host).items()}
    created = set()
    for command in life.execute("SELECT payload_json,state FROM life_prepared_commands WHERE kind='activity.create'"):
        if command["state"] == "aborted":
            continue
        payload = json.loads(command["payload_json"])
        activity = payload["activity"]
        if activity["work"]["kind"] == "social":
            if command["state"] != "acknowledged":
                raise ValueError("browsing creation is not confirmed")
            created.add(activity["activity_id"])
    old = {
        "role_exposure_sessions": tuple(row for row in host_rows["role_exposure_sessions"] if row[2] not in created),
        "role_exposures": tuple(row for row in host_rows["role_exposures"] if row[1] not in created),
    }
    if _summary(old) != {name: manifest[name] for name in TABLES}:
        raise ValueError("sealed browsing records differ from the imported baseline")
    if verify_contexts(host) != manifest["context_provenance"]:
        raise ValueError("original browsing authority differs from the imported baseline")
    old_life = {
        "role_exposure_sessions": tuple(row for row in staged["role_exposure_sessions"] if row[2] not in created),
        "role_exposures": tuple(row for row in staged["role_exposures"] if row[1] not in created),
    }
    if old_life != old:
        raise ValueError("original life browsing records were changed")
    sequence = life.execute("SELECT seq FROM sqlite_sequence WHERE name='staged_role_exposure_sessions'").fetchone()
    if (sequence is None or sequence[0] < manifest["session_scan_ceiling"]
            or sequence[0] < max((row[0] for row in staged["role_exposure_sessions"]), default=0)
            or any(row[0] <= manifest["session_scan_ceiling"] for row in staged["role_exposure_sessions"] if row[2] in created)):
        raise ValueError("browsing session sequence changed")
    host_index = {row[:4]: row for row in host_rows["role_exposures"] if row[1] in created}
    life_index = {row[:4]: row for row in staged["role_exposures"] if row[1] in created}
    for key, item in life_index.items():
        stub = host_index.get(key)
        if stub != (*key, "", "", "", item[7], None, None, "", 0):
            raise ValueError("browsing receipt has no matching host relationship index")
    orphaned = set(host_index) - set(life_index)
    for key in orphaned:
        for table in ("role_exposure_topics", "role_exposure_feedback"):
            if host.execute(f"SELECT 1 FROM {table} WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=? LIMIT 1", key).fetchone():
                raise ValueError("an uncommitted browsing index has dependent host records")
    sessions = {(row[1], row[2]) for row in staged["role_exposure_sessions"]}
    if any((row[0], row[1]) not in sessions for row in staged["role_exposures"]):
        raise ValueError("a browsing receipt has no session")
    return staged, created, orphaned


def _apply(target, staged, created, orphaned):
    target.execute("BEGIN IMMEDIATE")
    for key in orphaned:
        target.execute("DELETE FROM role_exposures WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?", key)
    for row in staged["role_exposure_sessions"]:
        if row[2] in created:
            target.execute("""INSERT INTO role_exposure_sessions(rowid,scope_key,activity_id,platform,selected_id,selection_made)
                VALUES (?,?,?,?,?,?)""", row)
    for row in staged["role_exposures"]:
        if row[1] in created:
            changed = target.execute("""UPDATE role_exposures SET title=?,summary=?,source_url=?,fetched_at=?,
                exposed_at=?,attempted_at=?,coverage=?,random_attempt=? WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?""",
                (*row[4:], *row[:4]))
            if changed.rowcount != 1:
                raise ValueError("browsing relationship index disappeared")
    copied = {name: tuple(tuple(row) for row in rows) for name, rows in _source_rows(target).items()}
    if copied != staged or target.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("rollback copy does not match the life browsing ledger")
    verify_contexts(target)
    target.commit()


def rehearse(host_backup, life_backup, *, expected_version, destination=None):
    host_path, life_path = Path(host_backup).resolve(strict=True), Path(life_backup).resolve(strict=True)
    target_path = Path(destination).resolve() if destination is not None else None
    if host_path == life_path or target_path in (host_path, life_path):
        raise ValueError("rollback inputs and destination must be separate")
    with closing(_open(host_path)) as host, closing(_open(life_path)) as life:
        activities, ceilings, events = verify_activities(host, life, expected_version)
        staged, created, orphaned = _verify(host, life, expected_version)
        result = {"state": "dry_run", "activities": len(activities["role_activities"]),
                  "sessions": len(staged["role_exposure_sessions"]), "exposures": len(staged["role_exposures"]), "events": events}
        if target_path is None:
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as target:
                apply_activities(host, target, activities, ceilings, expected_version)
                _apply(target, staged, created, orphaned)
            return result
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(target_path, isolation_level=None)) as target:
                apply_activities(host, target, activities, ceilings, expected_version)
                _apply(target, staged, created, orphaned)
        except BaseException:
            target_path.unlink(missing_ok=True)
            raise
        return {**result, "state": "rehearsed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_backup")
    parser.add_argument("life_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--destination", help="new isolated host copy; omit for in-memory rehearsal")
    args = parser.parse_args()
    try:
        result = rehearse(args.host_backup, args.life_backup, expected_version=args.expected_version, destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "browsing rollback rehearsal failed; source snapshots were not changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
