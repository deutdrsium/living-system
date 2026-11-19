from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from companion_v2.execution.models import TaskContext, digest, encode
from companion_v2.foundation.refs import AudienceRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, ResolvedIdentity, RoleBinding
from companion_v2.role_work.interests import scope_key
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec
from verify_exposure_authority import verify_contexts


class ExposureAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""CREATE TABLE conversation_batches(context_json TEXT);
            CREATE TABLE execution_tasks(record_json TEXT);
            CREATE TABLE role_activities(owner_key TEXT, activity_id TEXT, record_json TEXT);
            CREATE TABLE role_exposure_sessions(scope_key TEXT, activity_id TEXT);""")
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        human = SubjectRef("installation", SubjectKind.HUMAN, "person")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        principal = AuthenticatedHostPrincipal("installation", "host", "platform", "person", "bot", "account")
        actor = ResolvedIdentity(principal, human, "binding", 1, 1)
        self.context = TaskContext(actor, RoleBinding(scope, role, 1), AudienceRef(scope, (human,)))
        work = RoleWorkSpec("social", self.context.key, "request")
        self.activity = RoleActivity("activity", "role", scope, datetime(2026, 1, 1, tzinfo=timezone.utc),
                                     RoleActivityState.COMPLETED, work=work)
        self.db.execute("INSERT INTO role_activities VALUES (?, ?, ?)",
                        (digest(self.activity.owner), self.activity.activity_id, encode(self.activity)))
        self.db.execute("INSERT INTO role_exposure_sessions VALUES (?, ?)", (scope_key(self.context), "activity"))
        self.db.execute("INSERT INTO conversation_batches VALUES (?)", (encode(self.context),))

    def test_original_context_matches_and_task_snapshot_is_a_valid_fallback(self):
        result = verify_contexts(self.db)
        self.assertEqual((result["state"], result["sessions"], result["contexts"]), ("matched", 1, 1))
        self.assertEqual(len(result["sha256"]), 64)
        self.db.execute("DELETE FROM conversation_batches")
        self.db.execute("INSERT INTO execution_tasks VALUES (?)",
                        (json.dumps({"spec": {"context": json.loads(encode(self.context))}}),))
        self.assertEqual(verify_contexts(self.db)["state"], "matched")

    def test_swapped_scope_and_missing_original_context_are_rejected(self):
        foreign_scope = replace(self.context.scope, session_id="other")
        foreign_context = replace(self.context, role_binding=replace(self.context.role_binding, scope=foreign_scope),
                                  audience=replace(self.context.audience, scope=foreign_scope))
        self.db.execute("UPDATE role_exposure_sessions SET scope_key=?", (scope_key(foreign_context),))
        with self.assertRaisesRegex(ValueError, "matching original"):
            verify_contexts(self.db)
        self.db.execute("UPDATE role_exposure_sessions SET scope_key=?", (scope_key(self.context),))
        self.db.execute("DELETE FROM conversation_batches")
        with self.assertRaisesRegex(ValueError, "matching original"):
            verify_contexts(self.db)

    def test_invalid_owner_or_ambiguous_activity_is_rejected(self):
        self.db.execute("UPDATE role_activities SET owner_key='foreign'")
        with self.assertRaisesRegex(ValueError, "owner index"):
            verify_contexts(self.db)
        self.db.execute("UPDATE role_activities SET owner_key=?", (digest(self.activity.owner),))
        self.db.execute("INSERT INTO role_activities VALUES (?, ?, ?)", ("other", "activity", encode(self.activity)))
        with self.assertRaisesRegex(ValueError, "multiple owners"):
            verify_contexts(self.db)


if __name__ == "__main__":
    unittest.main()
