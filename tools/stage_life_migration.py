"""Preflight or stage the currently implemented life-data migration steps.

The two inputs must be backups taken after related writes are quiesced. This
tool never changes execution ownership or marks the plugin ready to run.
"""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from good_life.activity_import import stage_activities
from good_life.configuration_import import stage_configuration
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.state_import import stage_state, stage_state_configuration
from rehearse_calendar_migration import rehearse as stage_calendar
from verify_exposure_authority import verify_contexts


def _fingerprint(path):
    path = Path(path).resolve(strict=True)
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("source has a live write-ahead log; use a closed backup")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_installation(runtime_path, configuration_path):
    def open_readonly(path):
        return sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    with closing(open_readonly(runtime_path)) as runtime, closing(open_readonly(configuration_path)) as config:
        installation = runtime.execute("SELECT value FROM runtime_metadata WHERE key='installation_id'").fetchone()
        if not installation or not installation[0]:
            raise ValueError("runtime backup has no installation identity")
        rows = config.execute("""SELECT DISTINCT json_extract(t.target_json,'$.installation_id')
            FROM configuration_targets t JOIN configuration_values v USING(target_key)
            WHERE v.field_id LIKE 'role.life.%' OR v.field_id LIKE 'role.state.%'""").fetchall()
        if any(row[0] != installation[0] for row in rows):
            raise ValueError("configuration backup belongs to another installation")


def stage_all(runtime_backup, configuration_backup, *, runtime_version, configuration_version, destination=None):
    sources = {"runtime": Path(runtime_backup).resolve(strict=True),
               "configuration": Path(configuration_backup).resolve(strict=True)}
    if sources["runtime"] == sources["configuration"]:
        raise ValueError("runtime and configuration backups must be separate")
    fingerprints = {key: _fingerprint(path) for key, path in sources.items()}
    _check_installation(sources["runtime"], sources["configuration"])
    steps = (
        ("calendar", lambda target: stage_calendar(sources["runtime"], expected_version=runtime_version,
                                                    destination=target, adopt_legacy=True)),
        ("activities", lambda target: stage_activities(sources["runtime"], expected_version=runtime_version, destination=target)),
        ("browsing", lambda target: stage_exposures(sources["runtime"], expected_version=runtime_version,
                                                     destination=target, validate_source=verify_contexts)),
        ("runtime_markers", lambda target: stage_metadata(sources["runtime"], expected_version=runtime_version, destination=target)),
        ("state", lambda target: stage_state(sources["runtime"], expected_version=runtime_version, destination=target)),
        ("life_configuration", lambda target: stage_configuration(sources["configuration"], expected_version=configuration_version,
                                                                    destination=target)),
        ("state_configuration", lambda target: stage_state_configuration(sources["configuration"], expected_version=configuration_version,
                                                                           destination=target)),
    )
    checks = {name: step(None) for name, step in steps}
    if {key: _fingerprint(path) for key, path in sources.items()} != fingerprints:
        raise ValueError("a backup changed during preflight")
    if destination is None:
        return {"state": "dry_run", "source_sha256": fingerprints, "steps": checks, "cutover_ready": False}
    results = {}
    for name, step in steps:
        result = step(destination)
        if result["summary"] != checks[name]["summary"]:
            raise ValueError("a backup changed between preflight and import")
        results[name] = result
    if {key: _fingerprint(path) for key, path in sources.items()} != fingerprints:
        raise ValueError("a backup changed during import; do not use this staging directory")
    return {"state": "staged", "source_sha256": fingerprints, "steps": results, "cutover_ready": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_backup")
    parser.add_argument("configuration_backup")
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--configuration-version", required=True)
    parser.add_argument("--stage-into", help="isolated destination directory; omit for preflight")
    args = parser.parse_args()
    try:
        result = stage_all(args.runtime_backup, args.configuration_backup, runtime_version=args.runtime_version,
                           configuration_version=args.configuration_version, destination=args.stage_into)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        parser.exit(1, f"life migration staging failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
