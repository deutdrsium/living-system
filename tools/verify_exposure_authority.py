"""Verify browsing-session provenance against original serialized host contexts."""

import argparse
from contextlib import closing
import hashlib
import json
from itertools import chain
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskContext, digest
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.interests import scope_key
from good_life.exposure_import import _snapshot


def verify_contexts(connection):
    contexts = {}
    documents = ((row[0], False) for row in connection.execute("SELECT context_json FROM conversation_batches"))
    tasks = ((row[0], True) for row in connection.execute("SELECT record_json FROM execution_tasks"))
    for raw, is_task in chain(documents, tasks):
        try:
            document = json.loads(raw)
            context = TaskContext.restore(document["spec"]["context"] if is_task else document)
            contexts[context.key] = context
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    activities = {}
    for owner_key, identifier, raw in connection.execute("""SELECT owner_key, activity_id, record_json
            FROM role_activities WHERE json_extract(record_json, '$.work.kind')='social'"""):
        if identifier in activities:
            raise ValueError("a browsing activity identifier has multiple owners")
        activity = activity_from_json(raw)
        if digest(activity.owner) != owner_key or activity.activity_id != identifier:
            raise ValueError("a browsing activity has an invalid owner index")
        activities[identifier] = activity
    sessions = tuple(connection.execute("SELECT scope_key, activity_id FROM role_exposure_sessions"))
    evidence = []
    for saved_scope, identifier in sessions:
        activity = activities.get(identifier)
        if activity is None or activity.work is None:
            raise ValueError("a browsing session has no original activity")
        context = contexts.get(activity.work.context_key)
        if (context is None or context.scope != activity.scope or scope_key(context) != saved_scope
                or context.role_binding.role.subject_id != activity.role_id):
            raise ValueError("a browsing session has no matching original role and context")
        evidence.append((saved_scope, identifier, context.key, digest(activity.owner)))
    fingerprint = hashlib.sha256(json.dumps(sorted(evidence), ensure_ascii=True,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"state": "matched", "sessions": len(sessions),
            "contexts": len({activities[identifier].work.context_key for _, identifier in sessions}), "sha256": fingerprint}


def verify(source_path, *, expected_version):
    source = Path(source_path).resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        _snapshot(connection, expected_version)
        return verify_contexts(connection)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.source_backup, expected_version=args.expected_version)
    except (OSError, sqlite3.Error, ValueError, KeyError):
        parser.exit(1, "browsing provenance verification failed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
