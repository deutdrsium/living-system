"""Inspect or stage calendar data from a consistent companion database backup."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from audit_calendar_ownership import adoption_plan
from good_life.calendar_adoption import adopt_staged_calendar
from good_life.calendar_import import stage_calendar
from good_life.storage import LifeStorage


def rehearse(source_backup, *, expected_version, destination=None, adopt_legacy=False):
    result = stage_calendar(source_backup, expected_version=expected_version, destination=destination)
    if adopt_legacy:
        plan = adoption_plan(source_backup, expected_version=expected_version)
        result["ownership"] = plan["summary"]
        if destination is not None:
            life = LifeStorage.open(destination)
            try:
                result["adoption"] = adopt_staged_calendar(life, plan)
            finally:
                life.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--stage-into", help="isolated destination directory; omit for read-only dry run")
    parser.add_argument("--adopt-legacy", action="store_true",
                        help="inspect and, in staging only, adopt verified old role plans")
    args = parser.parse_args()
    try:
        result = rehearse(args.source_backup, expected_version=args.expected_version,
                          destination=args.stage_into, adopt_legacy=args.adopt_legacy)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"calendar rehearsal failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
