from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, ScopeRef
from companion_v2.role_work.ledger import PersistentRoleLifeLedger
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.runtime import StorageRuntime
from good_life.activity_import import stage_activities
from verify_activity_proxy import verify


class DeletedActivityMigrationIntegrationTests(unittest.TestCase):
    def test_real_deleted_activity_keeps_only_its_deduplication_receipt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = StorageRuntime.open(data_root=root / "host", installation_id="installation")
            try:
                now = datetime(2026, 1, 1, tzinfo=timezone.utc)
                scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
                owner = RoleOwnerRef("installation", "bot", "role")
                ledger = PersistentRoleLifeLedger(runtime, now=lambda: now)
                ledger.add_activity(RoleActivity("deleted-activity", "role", scope, now, RoleActivityState.ACTIVE,
                                                work=RoleWorkSpec("social", "context", "request")))
                ledger.transition_activity("deleted-activity", owner=owner, expected_revision=1,
                    state=RoleActivityState.COMPLETED, operation_id="finished-operation", observed_at=now + timedelta(seconds=1))
                ledger.delete_activity("deleted-activity", owner=owner)
                version = runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
                source = runtime.database_path
            finally:
                runtime.close()
            result = stage_activities(source, expected_version=version, destination=root / "life")
            self.assertEqual((result["summary"]["role_activities"]["rows"],
                              result["summary"]["detached_receipts"]["rows"]), (0, 1))
            checked = verify(source, root / "life" / "good-life.sqlite3", expected_version=version)
            self.assertEqual((checked["roles"], checked["activities"], checked["receipts"]), (1, 0, 1))


if __name__ == "__main__":
    unittest.main()
