from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import digest, encode, stamp
from companion_v2.foundation.refs import ScopeKind, ScopeRef
from companion_v2.role_work.life import physiology as original
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleSleepReceipt
from companion_v2.role_work.work_models import RoleWorkSpec
from good_life import sleep_physiology as moved
from good_life.staged_activity_commands import StagedActivityTransitions


NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
SCOPE = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")


def wire(value):
    return json.loads(encode(value))


def episode(identifier, *, state=RoleActivityState.COMPLETED, kind="night", start=None,
            minutes=420, receipt=True, target=480):
    start = start or NOW - timedelta(hours=12)
    work = RoleWorkSpec("sleep", "context", identifier, due_at=start + timedelta(minutes=target))
    sleep = (RoleSleepReceipt(kind, 480, target, slept_seconds=minutes * 60, workload_score=60,
                              interruptions=1, quality_min=65, quality_max=88) if receipt else None)
    return RoleActivity(identifier, "role", SCOPE, start, state,
                        updated_at=start + timedelta(minutes=minutes), work=work, sleep=sleep)


class SleepPhysiologyCompatibilityTests(unittest.TestCase):
    def test_sleep_history_and_elapsed_time_match_original(self):
        night = episode("night", start=NOW - timedelta(days=2))
        nap = episode("nap", kind="nap", start=NOW - timedelta(days=1), minutes=25, target=25)
        legacy = episode("legacy", start=NOW - timedelta(hours=15), receipt=False)
        active = episode("active", state=RoleActivityState.ACTIVE, start=NOW - timedelta(hours=2), minutes=30)
        for activity in (night, nap, legacy, active):
            with self.subTest(activity=activity.activity_id):
                self.assertEqual(moved.actual_sleep_seconds(wire(activity), NOW), original.actual_sleep_seconds(activity, NOW))
        for history in ((), (night,), (night, nap), (legacy, night), (night, nap, active)):
            for workload in (-1, 0, 50, 100, 101):
                with self.subTest(history=[a.activity_id for a in history], workload=workload):
                    self.assertEqual(moved.sleep_profile([wire(a) for a in history], workload_score=workload, now=NOW),
                                     original.sleep_profile(history, workload_score=workload, now=NOW))

    def test_sleep_quality_recovery_and_readiness_match_original(self):
        for workload in (0, 39, 55, 80, 100):
            for debt in (0, 89, 90, 300, 720):
                for interruptions in (0, 1, 20):
                    self.assertEqual(moved.sleep_quality_range(workload, debt, interruptions, 1),
                                     original.sleep_quality_range(workload, debt, interruptions, 1))
                self.assertEqual(moved.recommended_sleep_minutes(480, debt, workload),
                                 original.recommended_sleep_minutes(480, debt, workload))
                profile = {"sleep_pressure": 50, "sleep_debt_minutes": debt, "workload_score": workload,
                           "awake_minutes": 300, "has_sleep_history": True}
                self.assertEqual(moved.night_sleep_ready(profile), original.night_sleep_ready(profile))
                for local_minutes in (659, 660, 959, 960):
                    self.assertEqual(moved.nap_ready(profile, local_minutes=local_minutes),
                                     original.nap_ready(profile, local_minutes=local_minutes))
        self.assertTrue(moved.night_sleep_ready({"sleep_pressure": None}))

    def test_oversleep_is_repeatable_and_matches_original(self):
        activities = (episode("night"), episode("nap", kind="nap", target=25, minutes=25),
                      episode("short-manual", kind="manual", target=30, minutes=30), episode("legacy", receipt=False))
        for activity in activities:
            for debt in (0, 90, 720):
                for roll in (None, 0, 50, 99):
                    with self.subTest(activity=activity.activity_id, debt=debt, roll=roll):
                        self.assertEqual(moved.oversleep_extension(wire(activity), sleep_debt_minutes=debt, roll=roll),
                                         original.oversleep_extension(activity, sleep_debt_minutes=debt, roll=roll))

    def test_due_sleep_proposal_matches_original_wake_and_oversleep(self):
        # Use fixed, already due synthetic episodes; no scheduler or event is started.
        candidates = [episode("nap", state=RoleActivityState.ACTIVE, kind="nap", target=25, minutes=25),
                      episode("legacy", state=RoleActivityState.ACTIVE, receipt=False),
                      episode("night", state=RoleActivityState.ACTIVE)]
        candidates += [episode("night-" + str(i), state=RoleActivityState.ACTIVE) for i in range(20)]
        transitions = StagedActivityTransitions(None)
        extensions = set()
        for activity in candidates:
            debt = 720
            proposal = transitions.propose_due_sleep(activity=wire(activity), observed_at=stamp(NOW), sleep_debt_minutes=debt)
            receipt = activity.sleep
            if receipt is None:
                target = max(1, min(1440, round((activity.work.due_at - activity.started_at).total_seconds() / 60)))
                low, high = original.sleep_quality_range(0, 0)
                receipt = RoleSleepReceipt("night", target, target, quality_min=low, quality_max=high)
            elapsed = max(0, round((NOW - activity.updated_at).total_seconds()))
            total = min(172800, receipt.slept_seconds + elapsed)
            extension = original.oversleep_extension(activity, sleep_debt_minutes=debt)
            extensions.add(bool(extension))
            if extension:
                self.assertEqual(proposal["state"], "active")
                self.assertEqual(proposal["sleep"], wire(replace(receipt, slept_seconds=total, oversleep_minutes=extension)))
                self.assertEqual(proposal["work"], wire(replace(activity.work, due_at=activity.work.due_at + timedelta(minutes=extension))))
                self.assertEqual(proposal["operation_id"], "sleep-oversleep-" + digest([activity.activity_id, activity.revision]))
                self.assertEqual(proposal["event_consumers"], [])
            else:
                low, high = original.sleep_quality_range(receipt.workload_score, debt, receipt.interruptions, receipt.early_interruptions)
                self.assertEqual(proposal["state"], "completed")
                self.assertEqual(proposal["sleep"], wire(replace(receipt, slept_seconds=total, wake_until=None,
                                                                   quality_min=low, quality_max=high)))
                self.assertEqual(proposal["operation_id"], "body-wake-" + digest([activity.activity_id, activity.revision]))
                self.assertEqual("wardrobe" in proposal["event_consumers"], receipt.kind == "night")
        self.assertEqual(extensions, {False, True})

        future = episode("future", state=RoleActivityState.ACTIVE, start=NOW, minutes=0)
        with self.assertRaises(ValueError):
            transitions.propose_due_sleep(activity=wire(future), observed_at=stamp(NOW), sleep_debt_minutes=0)


if __name__ == "__main__":
    unittest.main()
