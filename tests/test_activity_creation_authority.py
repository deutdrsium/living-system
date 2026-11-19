from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest, encode
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.integration import activity_creation_authority, life_commands
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleSleepReceipt
from companion_v2.role_work.work_models import RoleWorkSpec
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_creations import StagedActivityCreations
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


def wire(value):
    return json.loads(encode(value))


class ActivityCreationAuthorityTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def prepare_creation(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _manager = self.setup_state()
        grant = self.grant(context.actor, context.scope, "role.life.run")
        await self.start(host)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        request_id = "autonomous-sleep"
        work = RoleWorkSpec("sleep", context.key, request_id, due_at=NOW + timedelta(hours=8))
        activity = RoleActivity("sleep-" + digest([context.key, request_id]), owner.persona_id, context.scope,
            NOW, RoleActivityState.ACTIVE, work=work, sleep=RoleSleepReceipt("night", 480, 480))
        request = {"command_id": "create-sleep", "owner_key": digest(owner), "context_key": context.key,
                   "kind": "activity.create", "expected_revision": 0,
                   "payload": {"owner": wire(owner), "activity": wire(activity)}}
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        commands = PreparedCommands(life)
        commands.install()
        creations = StagedActivityCreations(life)
        creations.prepare(request, prepared_at=NOW)
        return app, context, grant, owner, request, life, commands, creations

    async def test_real_identity_authority_and_decision_preserve_context(self):
        app, context, _grant, owner, request, life, commands, creations = await self.prepare_creation()
        self.assertTrue(activity_creation_authority.authorize_identity(app.tasks, context, request, observed_at=NOW).is_success)
        altered = deepcopy(request)
        altered["payload"]["activity"]["scope"]["session_id"] = "another-session"
        with self.assertRaises(TaskError) as mismatch:
            activity_creation_authority.authorize_identity(app.tasks, context, altered, observed_at=NOW)
        self.assertEqual(mismatch.exception.code, "life_activity_creation_invalid")
        altered = deepcopy(request)
        altered["payload"]["owner"]["persona_id"] = "another-role"
        with self.assertRaises(TaskError):
            activity_creation_authority.authorize_identity(app.tasks, context, altered, observed_at=NOW)
        with self.assertRaises(TaskError):
            activity_creation_authority.authorize_identity(app.tasks, context, request, observed_at=NOW + timedelta(hours=9))
        decision = life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=NOW,
            verify_prepared=commands.verify_prepared,
            authorize=lambda value, at: activity_creation_authority.authorize_identity(app.tasks, value, request, observed_at=at),
            apply=lambda *_: None)
        creations.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=creations.verify_commit)
        creations.acknowledge(request["command_id"], verify_host_completion=lambda value: life_commands.verify_completion(self.runtime, value))
        actual = StagedActivityRecords(life).dispatch("activity", owner_key=digest(owner),
                                                       activity_id=request["payload"]["activity"]["activity_id"])
        self.assertEqual(json.loads(actual), request["payload"]["activity"])

    async def test_revoked_life_grant_blocks_a_prepared_creation(self):
        app, context, grant, _owner, request, _life, commands, _creations = await self.prepare_creation()
        self.identity.revoke_grant(grant.grant_id, expected_revision=self.identity.revision, revoked_at=NOW)
        with self.assertRaises(TaskError):
            life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=NOW,
                verify_prepared=commands.verify_prepared,
                authorize=lambda value, at: activity_creation_authority.authorize_identity(app.tasks, value, request, observed_at=at),
                apply=lambda *_: None)
        self.assertEqual(commands.pending()[0]["state"], "prepared")


if __name__ == "__main__":
    unittest.main()
