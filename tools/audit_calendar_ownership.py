"""Inspect calendar ownership in a read-only companion database backup."""

import argparse
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.identity_policy.repository import PersistentIdentityDirectory
from companion_v2.integration.calendar_ownership_audit import audit_calendar_ownership
from good_life.calendar_import import _identity_fingerprint, _source_summary


class Snapshot:
    """Read adapter over one explicitly opened SQLite snapshot."""
    def __init__(self, connection, installation_id):
        self.connection, self.installation_id = connection, installation_id

    def read_one(self, statement, parameters):
        return self.connection.execute(statement, parameters).fetchone()

    def read_all(self, statement, parameters):
        return self.connection.execute(statement, parameters).fetchall()

    @contextmanager
    def read_snapshot(self):
        yield


def adoption_plan_in_snapshot(connection, *, expected_version):
    """Audit role ownership using the caller's already-open read snapshot."""
    summary = _source_summary(connection, expected_version)
    installation = connection.execute("SELECT value FROM runtime_metadata WHERE key='installation_id'").fetchone()
    if installation is None or not installation[0]:
        raise ValueError("source installation is unavailable")
    runtime = Snapshot(connection, installation[0])
    report = audit_calendar_ownership(runtime, PersistentIdentityDirectory(runtime))
    identity_hash = _identity_fingerprint(connection)
    return {"source_version": expected_version, "source_summary": summary,
            "identity_sha256": identity_hash, "summary": report["summary"], "moves": list(report["moves"])}


def adoption_plan(source_path, *, expected_version):
    """Return a private adoption plan bound to one read-only source snapshot."""
    source = Path(source_path).resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return adoption_plan_in_snapshot(connection, expected_version=expected_version)


def inspect(source_path, *, expected_version):
    plan = adoption_plan(source_path, expected_version=expected_version)
    return {"state": "inspected", "source_version": expected_version, **plan["summary"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("--expected-version", required=True)
    arguments = parser.parse_args()
    try:
        result = inspect(arguments.source_backup, expected_version=arguments.expected_version)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "calendar ownership inspection failed; no data was changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
