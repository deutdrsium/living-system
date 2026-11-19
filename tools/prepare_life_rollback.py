"""Prepare paired legacy databases from one stopped, consistent backup set.

The source backups are never modified. This builds a new directory containing
the merged runtime and configuration databases; installing it and restoring
the paired legacy code are separate steps while AstrBot remains stopped.
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from prepare_life_cutover import CERTIFICATE_KEY, CONFIG_OWNER_KEY, OWNER_KEYS
from rehearse_configuration_rollback import rehearse as rollback_configuration
from rehearse_life_rollback import rehearse as rollback_runtime
from stage_life_migration import _fingerprint


def prepare(runtime_backup, configuration_backup, life_backup, *, runtime_version, configuration_version,
            destination, legacy_commit):
    if type(legacy_commit) is not str or not re.fullmatch(r"[0-9a-f]{40}", legacy_commit):
        raise ValueError("the legacy source commit must be a full Git SHA")
    sources = {"runtime": Path(runtime_backup).resolve(strict=True),
               "configuration": Path(configuration_backup).resolve(strict=True),
               "life": Path(life_backup).resolve(strict=True)}
    target = Path(destination).resolve()
    if len(set(sources.values())) != 3 or target.exists() or target in sources.values():
        raise ValueError("backup sources must differ and destination must be new")
    fingerprints = {key: _fingerprint(value) for key, value in sources.items()}
    with closing(sqlite3.connect(sources["runtime"].as_uri() + "?mode=ro", uri=True)) as runtime:
        for key in OWNER_KEYS:
            row = runtime.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
            if row is None or row[0] != "good_life":
                raise ValueError("runtime ownership does not match a completed cutover")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".life-rollback-paired-", dir=target.parent) as temporary:
        candidate = Path(temporary) / "candidate"
        candidate.mkdir(mode=0o700)
        host_copy = candidate / "companion-v2.sqlite3"
        config_dir = candidate / "configuration"
        config_dir.mkdir(mode=0o700)
        config_copy = config_dir / "companion-v2.sqlite3"
        runtime_result = rollback_runtime(sources["runtime"], sources["life"], expected_version=runtime_version,
                                          destination=host_copy)
        configuration_result = rollback_configuration(sources["configuration"], sources["life"], sources["runtime"],
            expected_version=configuration_version, destination=config_copy)
        if {key: _fingerprint(value) for key, value in sources.items()} != fingerprints:
            raise ValueError("a backup changed during rollback preparation")
        with closing(sqlite3.connect(host_copy)) as runtime:
            with runtime:
                for key in OWNER_KEYS:
                    runtime.execute("UPDATE runtime_metadata SET value='legacy' WHERE key=? AND value='good_life'", (key,))
                runtime.execute("DELETE FROM runtime_metadata WHERE key=?", (CERTIFICATE_KEY,))
        with closing(sqlite3.connect(config_copy)) as config:
            with config:
                changed = config.execute("UPDATE runtime_metadata SET value='legacy' WHERE key=? AND value='good_life'",
                                         (CONFIG_OWNER_KEY,))
                if changed.rowcount != 1:
                    raise ValueError("configuration ownership changed during rollback")
        result = {"state": "candidate", "legacy_commit": legacy_commit, "source_sha256": fingerprints,
                  "runtime_sha256": _fingerprint(host_copy), "configuration_sha256": _fingerprint(config_copy),
                  "runtime": runtime_result, "configuration": configuration_result}
        fd = os.open(candidate / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
        if target.exists():
            raise ValueError("rollback destination already exists")
        os.rename(candidate, target)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_backup")
    parser.add_argument("configuration_backup")
    parser.add_argument("life_backup")
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--configuration-version", required=True)
    parser.add_argument("--legacy-commit", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.runtime_backup, args.configuration_backup, args.life_backup,
            runtime_version=args.runtime_version, configuration_version=args.configuration_version,
            legacy_commit=args.legacy_commit, destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"paired rollback preparation failed: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
