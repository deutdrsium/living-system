"""Compare the host read proxy against a staged production snapshot, read-only."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import digest, encode
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.role_work.codec import activity_from_json, artifact_from_json
from companion_v2.role_work.models import RoleActivityState
from good_life.activity_import import TABLES, _snapshot
from good_life.activity_records import StagedActivityRecords
from verify_activity_shadow import verify as verify_staged


class _Registry:
    def __init__(self, records):
        self.records = records

    def dispatch(self, handler, operation, **request):
        if handler != "activity_read":
            raise ValueError("unexpected provider handler")
        return self.records.dispatch(operation, **request)


def verify(source_path, staged_path, *, expected_version):
    verify_staged(source_path, staged_path, expected_version=expected_version)
    source_path = Path(source_path).resolve(strict=True)
    staged_path = Path(staged_path).resolve(strict=True)
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as source:
        with closing(sqlite3.connect(staged_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as staged:
            for connection in (source, staged):
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
            rows, _ = _snapshot(source, expected_version)
            installation = source.execute("SELECT value FROM runtime_metadata WHERE key='installation_id'").fetchone()
            if installation is None:
                raise ValueError("source installation is missing")
            proxy = LifeLedgerReadProxy(SimpleNamespace(installation_id=installation[0]),
                                        _Registry(StagedActivityRecords(staged)))
            owners = {}
            activities = {}
            for owner_key, identifier, owner_json, _, _, _, raw in rows["role_activities"]:
                owner = RoleOwnerRef(**json.loads(owner_json))
                if digest(owner) != owner_key or owner.installation_id != installation[0]:
                    raise ValueError("source owner identity differs")
                owners[owner_key] = owner
                activity = proxy.activity(identifier, owner=owner)
                if encode(activity) != raw:
                    raise ValueError("activity proxy differs from the original")
                activities.setdefault(owner_key, []).append(activity)
            artifacts = {}
            for owner_key, artifact_id, _, scope_key, raw in rows["role_artifacts"]:
                artifact = artifact_from_json(raw)
                if artifact.owner != owners[owner_key] or artifact.source.scope.key != scope_key:
                    raise ValueError("source artifact identity differs")
                if encode(proxy.artifact(artifact_id, owner=owners[owner_key])) != raw:
                    raise ValueError("artifact proxy differs from the original")
                artifacts.setdefault(owner_key, []).append(artifact)
            for _, owner_key, identifier, revision, updated_at, provenance, operation_id, raw in rows["role_activity_revisions"]:
                owner = owners[owner_key]
                if (encode(proxy.activity_revision(identifier, revision, owner=owner)) != raw
                        or proxy.activity_revision_evidence(identifier, revision, owner=owner)
                        != {"provenance": provenance, "operation_id": operation_id}):
                    raise ValueError("revision proxy differs from the original")
                prior_count = source.execute("""SELECT COUNT(*) FROM role_activity_revisions
                    WHERE owner_key=? AND activity_id=? AND revision < ?""", (owner_key, identifier, revision)).fetchone()[0]
                coverage = proxy.activity_revision_coverage(identifier, revision, owner=owner)
                if coverage != {"provenance": provenance, "updated_at": updated_at, "prior_count": prior_count}:
                    raise ValueError("revision coverage differs from the original")
            for _, operation_id, owner_key, raw in rows["role_life_operations"]:
                receipt_owner = activity_from_json(raw).owner
                if (digest(receipt_owner) != owner_key or receipt_owner.installation_id != installation[0]
                        or (owner_key in owners and owners[owner_key] != receipt_owner)):
                    raise ValueError("receipt owner identity differs")
                owners[owner_key] = receipt_owner
                if proxy.activity_receipt(operation_id, owner=receipt_owner) != raw:
                    raise ValueError("receipt proxy differs from the original")
            expected_scan = [{"scan_id": scan_id, "operation_id": op, "owner_key": owner}
                             for scan_id, op, owner, _ in rows["role_life_operations"]]
            actual, cursor = [], 0
            while True:
                page = proxy.scan_activity_receipts(after=cursor, limit=100)
                actual.extend(page)
                if len(page) < 100:
                    break
                cursor = page[-1]["scan_id"]
            if actual != expected_scan:
                raise ValueError("receipt proxy pagination differs")
            names = TABLES["role_activity_revisions"][1:]
            for owner_key, owner in owners.items():
                expected_activities = sorted(activities.get(owner_key, []), key=lambda item: item.activity_id)
                actual, cursor = [], None
                while True:
                    page = proxy.activities_for_role(owner, after_id=cursor, limit=100)
                    actual.extend(page)
                    if len(page) < 100:
                        break
                    cursor = page[-1].activity_id
                if actual != expected_activities:
                    raise ValueError("activity inventory pagination differs")
                for states in ((), (RoleActivityState.ACTIVE,), (RoleActivityState.ACTIVE, RoleActivityState.PAUSED)):
                    for kind in (None, "sleep", "reading", "social"):
                        for oldest_first in (False, True):
                            expected_work = [item for item in expected_activities
                                if item.scope.kind is ScopeKind.PRIVATE and (not states or item.state in states)
                                and (kind is None or item.work is not None and item.work.kind == kind)]
                            expected_work.sort(key=lambda item: (item.updated_at, item.activity_id), reverse=not oldest_first)
                            expected_work = [item for item in expected_work[:200] if item.work]
                            if list(proxy.work_activities(owner, states=states, kind=kind,
                                                          oldest_first=oldest_first)) != expected_work:
                                raise ValueError("work activity selection differs")
                for state in RoleActivityState:
                    expected_state = [item for item in expected_activities if item.state is state]
                    actual, cursor = [], None
                    while True:
                        page = proxy.activities_for_role(owner, state=state, after_id=cursor, limit=100)
                        actual.extend(page)
                        if len(page) < 100:
                            break
                        cursor = page[-1].activity_id
                    if actual != expected_state:
                        raise ValueError("activity state filter differs")
                scopes = {item.scope.key: item.scope for item in expected_activities}
                ranks = {RoleActivityState.ACTIVE: 0, RoleActivityState.PAUSED: 1,
                         RoleActivityState.COMPLETED: 2, RoleActivityState.PLANNED: 3}
                for scope in scopes.values():
                    expected_scope = [item for item in expected_activities if item.scope == scope]
                    observed_at = max(item.updated_at for item in expected_scope) + timedelta(microseconds=1)
                    expected_scope.sort(key=lambda item: (item.updated_at, item.activity_id), reverse=True)
                    expected_scope.sort(key=lambda item: ranks.get(item.state, 4))
                    if list(proxy.context_activities(owner, scope, observed_at=observed_at, limit=32)) != expected_scope[:32]:
                        raise ValueError("context activity selection differs")
                history = [(row, activity_from_json(row[-1])) for row in rows["role_activity_revisions"]
                           if row[1] == owner_key]
                for scope in {item.scope.key: item.scope for _, item in history}.values():
                    expected_timeline = [{"occurred_at": row[4], "kind": "activity", "identifier": row[2], "revision": row[3]}
                                         for row, item in history if item.scope == scope]
                    expected_timeline.sort(key=lambda row: (row["occurred_at"], row["kind"], row["revision"], row["identifier"]),
                                           reverse=True)
                    end = expected_timeline[0]["occurred_at"]
                    ceiling = proxy.timeline_activity_ceiling(owner, scope)
                    actual, cursor = [], None
                    while True:
                        page = proxy.timeline_activity_candidates(owner, scope, ceiling=ceiling, end=end,
                                                                  after=cursor, limit=65)
                        actual.extend(page)
                        if len(page) < 65:
                            break
                        last = page[-1]
                        cursor = (last["occurred_at"], last["kind"], last["revision"], last["identifier"])
                    if actual != expected_timeline:
                        raise ValueError("timeline activity selection differs")
                expected_artifacts = sorted(artifacts.get(owner_key, []), key=lambda item: item.artifact_id)
                actual, cursor = [], None
                while True:
                    page = proxy.artifacts_for_role(owner, after_id=cursor, limit=100)
                    actual.extend(page)
                    if len(page) < 100:
                        break
                    cursor = page[-1].artifact_id
                if actual != expected_artifacts:
                    raise ValueError("artifact inventory pagination differs")
                for scope in {item.source.scope.key: item.source.scope for item in expected_artifacts}.values():
                    expected_scope = [item for item in expected_artifacts if item.source.scope == scope]
                    actual, cursor = [], None
                    while True:
                        page = proxy.artifacts_for_scope(scope, owner=owner, after_id=cursor, limit=100)
                        actual.extend(page)
                        if len(page) < 100:
                            break
                        cursor = page[-1].artifact_id
                    if actual != expected_scope:
                        raise ValueError("scope artifact selection differs")
                expected = [dict(zip(names, row[1:])) for row in rows["role_activity_revisions"] if row[1] == owner_key]
                if expected:
                    times = [datetime.fromisoformat(row["updated_at"]) for row in expected]
                    start, end = min(times) - timedelta(microseconds=1), max(times) + timedelta(microseconds=1)
                    expected.sort(key=lambda row: (row["updated_at"], row["activity_id"], row["revision"]), reverse=True)
                    actual, cursor = [], None
                    while True:
                        page = proxy.activity_revisions(owner=owner, after=start, at=end, before=cursor, limit=100)
                        actual.extend(page)
                        if len(page) < 100:
                            break
                        last = page[-1]
                        cursor = (last["updated_at"], last["activity_id"], last["revision"])
                    if actual != expected:
                        raise ValueError("revision proxy pagination differs")
                recent = [{"operation_id": row[1]} for row in reversed(rows["role_life_operations"]) if row[2] == owner_key][:100]
                if list(proxy.recent_activity_receipts(owner=owner, limit=100)) != recent:
                    raise ValueError("recent receipt proxy differs")
            return {"state": "matched", "roles": len(owners), "activities": len(rows["role_activities"]),
                    "artifacts": len(rows["role_artifacts"]), "revisions": len(rows["role_activity_revisions"]),
                    "receipts": len(rows["role_life_operations"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("staged_database")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.source_backup, args.staged_database, expected_version=args.expected_version)
    except Exception:
        parser.exit(1, "activity proxy verification failed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
