"""Inspect or stage role-state data and configuration from separate backups."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.state_import import stage_state, stage_state_configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--configuration", action="store_true", help="use a configuration database backup")
    parser.add_argument("--stage-into", help="isolated destination directory; omit for read-only preflight")
    args = parser.parse_args()
    try:
        stage = stage_state_configuration if args.configuration else stage_state
        result = stage(args.source_backup, expected_version=args.expected_version, destination=args.stage_into)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"state rehearsal failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
