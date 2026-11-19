from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest, encode, stamp
from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, ScopeRef, SourceRef, VersionRef
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleArtifact, RoleArtifactKind
from companion_v2.role_work.life.body_clock import RoleBodyClock
from companion_v2.role_work.life import rest_gate
from companion_v2.role_work.work_models import RoleWorkSpec
from good_life.activity_import import DDL
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage


class Registry:
    def __init__(self, records):
        self.records = records

    def dispatch(self, handler, operation, **request):
        assert handler == "activity_read"
        return self.records.dispatch(operation, **request)


class LifeLedgerProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = LifeStorage.open(temporary.name)
        self.addCleanup(self.storage.close)
        self.at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.owner = RoleOwnerRef("installation", "bot", "role")
        self.scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        self.activity = RoleActivity("activity", "role", self.scope, self.at, RoleActivityState.ACTIVE)
        source = SourceRef("source", "role.reading", self.scope, VersionRef("source", 1), self.at)
        self.artifact = RoleArtifact("artifact", "activity", RoleArtifactKind.TEXT, source, self.at, owner=self.owner)
        raw = encode(self.activity)
        owner_key = digest(self.owner)
        with self.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)
            db.execute("INSERT INTO staged_role_activities VALUES (?, 'activity', ?, 1, 'active', ?, ?)",
                       (owner_key, encode(self.owner), stamp(self.at), raw))
            db.execute("INSERT INTO staged_role_activity_revisions(owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json) VALUES (?, 'activity', 1, ?, 'recorded', 'operation', ?)",
                       (owner_key, stamp(self.at), raw))
            db.execute("INSERT INTO staged_role_life_operations VALUES (10, 'operation', ?, ?)", (owner_key, raw))
            db.execute("INSERT INTO staged_role_artifacts VALUES (?, 'artifact', 'activity', ?, ?)",
                       (owner_key, self.scope.key, encode(self.artifact)))
        self.records = StagedActivityRecords(self.storage)
        self.registry = Registry(self.records)
        self.proxy = LifeLedgerReadProxy(SimpleNamespace(installation_id="installation"), self.registry)

    def test_proxy_reads_original_role_and_validates_revisions(self):
        self.assertEqual(self.proxy.activity("activity", owner=self.owner), self.activity)
        self.assertEqual(self.proxy.activity_revision("activity", 1, owner=self.owner), self.activity)
        self.assertEqual(self.proxy.activity_revision_evidence("activity", 1, owner=self.owner),
                         {"provenance": "recorded", "operation_id": "operation"})
        self.assertEqual(self.proxy.activity_revision_coverage("activity", 1, owner=self.owner),
                         {"provenance": "recorded", "updated_at": stamp(self.at), "prior_count": 0})
        self.assertEqual(self.proxy.activity_receipt("operation", owner=self.owner), encode(self.activity))
        page = self.proxy.activity_revisions(owner=self.owner, after=self.at - timedelta(seconds=1),
                                             at=self.at + timedelta(seconds=1))
        self.assertEqual((page[0]["activity_id"], page[0]["revision"]), ("activity", 1))
        self.assertEqual(self.proxy.scan_activity_receipts(after=0, limit=10)[0]["scan_id"], 10)
        self.assertEqual(self.proxy.recent_activity_receipts(owner=self.owner, limit=10),
                         ({"operation_id": "operation"},))
        self.assertEqual(self.proxy.activities_for_role(self.owner), (self.activity,))
        self.assertEqual(self.proxy.activities_for_role(self.owner, state=RoleActivityState.PAUSED), ())
        self.assertEqual(self.proxy.context_activities(self.owner, self.scope, observed_at=self.at), (self.activity,))
        self.assertEqual(self.proxy.artifact("artifact", owner=self.owner), self.artifact)
        self.assertEqual(self.proxy.artifacts_for_role(self.owner), (self.artifact,))
        self.assertEqual(self.proxy.artifacts_for_scope(self.scope, owner=self.owner), (self.artifact,))
        other_scope = replace(self.scope, native_scope_id="other-native", conversation_id="other-conversation")
        self.assertEqual(self.proxy.context_activities(self.owner, other_scope, observed_at=self.at), ())
        self.assertEqual(self.proxy.artifacts_for_scope(other_scope, owner=self.owner), ())

    def test_wrong_owner_or_corrupted_provider_data_fails_closed(self):
        with self.assertRaises(KeyError):
            self.proxy.activity("activity", owner=RoleOwnerRef("installation", "bot", "other"))
        with self.storage.transaction() as db:
            db.execute("UPDATE staged_role_activities SET record_json='{}'")
        with self.assertRaises(TaskError) as caught:
            self.proxy.activity("activity", owner=self.owner)
        self.assertEqual(caught.exception.code, "life_provider_data_invalid")

    def test_unknown_read_operation_is_not_dispatched(self):
        with self.assertRaises(ValueError):
            self.records.dispatch("delete_everything")

    def test_provider_cannot_return_another_role_or_unordered_receipts(self):
        foreign = replace(self.activity, role_id="other")
        self.registry.dispatch = lambda *_args, **_kwargs: encode(foreign)
        with self.assertRaises(TaskError):
            self.proxy.activity("activity", owner=self.owner)
        self.registry.dispatch = lambda *_args, **_kwargs: [
            {"scan_id": 2, "operation_id": "a", "owner_key": "owner"},
            {"scan_id": 2, "operation_id": "b", "owner_key": "owner"},
        ]
        with self.assertRaises(TaskError):
            self.proxy.scan_activity_receipts(after=0, limit=10)

    def test_provider_cannot_return_activity_from_another_conversation(self):
        other_scope = replace(self.scope, native_scope_id="other-native", conversation_id="other-conversation")
        self.registry.dispatch = lambda *_args, **_kwargs: [encode(self.activity)]
        with self.assertRaises(TaskError):
            self.proxy.context_activities(self.owner, other_scope, observed_at=self.at)

    def test_timeline_ceiling_excludes_later_backdated_activity(self):
        ceiling = self.proxy.timeline_activity_ceiling(self.owner, self.scope)
        late = replace(self.activity, activity_id="late")
        with self.storage.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (digest(self.owner), late.activity_id, encode(self.owner), late.revision,
                        late.state.value, stamp(late.updated_at), encode(late)))
            db.execute("INSERT INTO staged_role_activity_revisions(owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json) VALUES (?, ?, ?, ?, 'recorded', NULL, ?)",
                       (digest(self.owner), late.activity_id, late.revision, stamp(late.updated_at), encode(late)))
        original = self.proxy.timeline_activity_candidates(self.owner, self.scope, ceiling=ceiling, end=stamp(self.at))
        self.assertEqual([row["identifier"] for row in original], ["activity"])
        fresh_ceiling = self.proxy.timeline_activity_ceiling(self.owner, self.scope)
        fresh = self.proxy.timeline_activity_candidates(self.owner, self.scope, ceiling=fresh_ceiling, end=stamp(self.at))
        self.assertEqual([row["identifier"] for row in fresh], ["late", "activity"])
        position = tuple(fresh[0][name] for name in ("occurred_at", "kind", "revision", "identifier"))
        next_page = self.proxy.timeline_activity_candidates(self.owner, self.scope, ceiling=fresh_ceiling,
                                                           end=stamp(self.at), after=position)
        self.assertEqual(next_page, fresh[1:])

    def test_timeline_rejects_provider_record_from_another_scope(self):
        other_scope = replace(self.scope, native_scope_id="other-native", conversation_id="other-conversation")
        foreign = replace(self.activity, scope=other_scope)
        self.registry.dispatch = lambda *_args, **_kwargs: [{
            "scan_id": 1, "occurred_at": stamp(self.at), "kind": "activity", "identifier": "activity",
            "revision": 1, "record_json": encode(foreign),
        }]
        with self.assertRaises(TaskError):
            self.proxy.timeline_activity_candidates(self.owner, self.scope, ceiling=1, end=stamp(self.at))

    def test_body_clock_reads_role_sleep_across_private_windows_in_order(self):
        other_scope = replace(self.scope, native_scope_id="other-native", conversation_id="other-conversation")
        work = RoleWorkSpec("sleep", "context", "request", due_at=self.at + timedelta(hours=1))
        first = replace(self.activity, activity_id="sleep-a", work=work)
        second = replace(first, activity_id="sleep-b", scope=other_scope, state=RoleActivityState.PAUSED,
                         updated_at=self.at + timedelta(minutes=1))
        with self.storage.transaction() as db:
            for activity in (first, second):
                db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (digest(self.owner), activity.activity_id, encode(self.owner), activity.revision,
                            activity.state.value, stamp(activity.updated_at), encode(activity)))
        context = SimpleNamespace(scope=self.scope,
                                  role_binding=SimpleNamespace(role=SimpleNamespace(subject_id="role")))
        clock = RoleBodyClock(SimpleNamespace(runtime=self.proxy.runtime), self.proxy)
        self.assertEqual(clock.active(context, "sleep"), (first,))
        self.assertEqual(clock.pending_sleep(context), (second, first))
        self.assertEqual(clock.episodes(context), (first, second))
        self.assertEqual(self.proxy.work_activities(self.owner, kind="sleep", oldest_first=True), (first, second))
        self.assertEqual(self.proxy.work_activities(self.owner, kind="social"), ())

    def test_work_activity_provider_cannot_return_another_role(self):
        foreign = replace(self.activity, role_id="other")
        self.registry.dispatch = lambda *_args, **_kwargs: [encode(foreign)]
        with self.assertRaises(TaskError):
            self.proxy.work_activities(self.owner)

    def test_rest_gate_reads_sleep_from_provider_and_never_falls_back(self):
        work = RoleWorkSpec("sleep", "context", "request", due_at=self.at + timedelta(hours=1))
        sleep = replace(self.activity, activity_id="sleep", work=work)
        with self.storage.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (digest(self.owner), sleep.activity_id, encode(self.owner), sleep.revision,
                        sleep.state.value, stamp(sleep.updated_at), encode(sleep)))
        context = SimpleNamespace(scope=self.scope,
                                  role_binding=SimpleNamespace(role=SimpleNamespace(subject_id="role")))
        settings = rest_gate.Settings.from_values({"rest_reply_active_windows": ""})
        current = rest_gate.sleep_context(self.proxy.runtime, context, life=self.proxy, now=self.at, settings=settings)
        self.assertEqual((current["resting"], current["sleep"]), (True, "sleep"))
        def unavailable(*_args, **_kwargs):
            raise TaskError("life_provider_unavailable", "unavailable")
        self.registry.dispatch = unavailable
        current = rest_gate.sleep_context(self.proxy.runtime, context, life=self.proxy, now=self.at, settings=settings)
        self.assertEqual((current["resting"], current["reason"]), (False, "not_sleeping"))

    def test_unavailable_provider_does_not_fall_back_to_host_data(self):
        def unavailable(*_args, **_kwargs):
            raise TaskError("life_provider_unavailable", "unavailable")
        self.registry.dispatch = unavailable
        with self.assertRaises(TaskError) as caught:
            self.proxy.activity("activity", owner=self.owner)
        self.assertEqual(caught.exception.code, "life_provider_unavailable")

    def test_actual_registry_dispatch_stops_when_provider_is_disabled(self):
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, activated=True, star_cls=plugin)
        context = SimpleNamespace(get_all_stars=lambda: [star])
        registry = LifeProviderRegistry(context, owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = self.records.dispatch
        registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        proxy = LifeLedgerReadProxy(SimpleNamespace(installation_id="installation"), registry)
        self.assertEqual(proxy.activity("activity", owner=self.owner), self.activity)
        star.activated = False
        with self.assertRaises(TaskError) as caught:
            proxy.activity("activity", owner=self.owner)
        self.assertEqual(caught.exception.code, "life_provider_unavailable")


if __name__ == "__main__":
    unittest.main()
