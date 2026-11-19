"""Reconstruct all implemented life runtime data into one isolated host copy.

Both inputs must be closed, consistent backups from a quiesced system. This
rehearsal never replaces a production database or changes execution ownership.
Configuration is still owned by the host and is not moved by this tool.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from rehearse_calendar_rollback import rehearse as calendar
from rehearse_exposure_rollback import rehearse as activities_and_browsing
from rehearse_reading_markers_rollback import rehearse as reading_markers
from rehearse_state_rollback import rehearse as state
from stage_life_migration import _fingerprint


def rehearse(host_backup, life_backup, *, expected_version, destination=None):
    host = Path(host_backup).resolve(strict=True)
    life = Path(life_backup).resolve(strict=True)
    target = Path(destination).resolve() if destination is not None else None
    if host == life or target in (host, life):
        raise ValueError("rollback sources and destination must be separate")
    if target is not None and target.exists():
        raise ValueError("rollback destination already exists")
    fingerprints = {"host": _fingerprint(host), "life": _fingerprint(life)}
    steps = (("calendar", calendar), ("state", state),
             ("reading_markers", reading_markers), ("activities_and_browsing", activities_and_browsing))
    if target is None:
        results = {name: step(host, life, expected_version=expected_version) for name, step in steps}
        if {"host": _fingerprint(host), "life": _fingerprint(life)} != fingerprints:
            raise ValueError("a backup changed during rollback rehearsal")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".life-rollback-", dir=target.parent) as temporary:
            source = host
            results = {}
            for index, (name, step) in enumerate(steps):
                copy = Path(temporary) / f"{index}.sqlite3"
                results[name] = step(source, life, expected_version=expected_version, destination=copy)
                if source != host:
                    source.unlink()
                source = copy
            if {"host": _fingerprint(host), "life": _fingerprint(life)} != fingerprints:
                raise ValueError("a backup changed during rollback rehearsal")
            with closing(sqlite3.connect(source)) as check:
                if (check.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                        or check.execute("PRAGMA foreign_key_check").fetchone()):
                    raise ValueError("combined rollback copy failed integrity checks")
            # Linking within the same parent publishes a complete file without
            # overwriting an existing destination or exposing a partial copy.
            os.link(source, target)
    return {"state": "dry_run" if target is None else "rehearsed", "source_sha256": fingerprints,
            "steps": results, "ownership_changed": False, "configuration_owner": "host"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_backup")
    parser.add_argument("life_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--destination", help="new isolated host copy; omit for read-only preflight")
    args = parser.parse_args()
    try:
        result = rehearse(args.host_backup, args.life_backup, expected_version=args.expected_version,
                          destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"life rollback rehearsal failed: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
