"""Life overview projection over the persisted execution and schedule stores."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta

import test_academics_domain as academic_tests
from test_academics_domain import entry, term
from test_kernel_support import KernelCase, NOW, m
from good_life.academics import Academics, AcademicsConfig, AttendanceConfig, ClassAttendance
from good_life.kernel.codec import freeze
from good_life.overview_reads import LIMIT, read_activities


class KernelOverviewTests(KernelCase):
    def read(self, **scope):
        args = {"owner_key": self.scope.owner_key, "context_key": self.scope.context_key}
        return read_activities(self.storage, **(args | scope), now=NOW, kernel=True, academics=False)["items"]

    def test_kernel_execution_reads_only_matching_owner_context_and_pinned_audience(self):
        running = self.running()
        item, = self.read()
        self.assertEqual((item["state"], item["simulated"]), ("running", True))
        self.assertEqual(self.read(context_key="other-context"), [])
        self.assertEqual(self.read(owner_key="other-role"), [])
        with self.storage.transaction() as db:
            db.execute("UPDATE life_kernel_instances SET audience_json=? WHERE instance_id=?",
                       ('{"context_keys":["other-context"]}', running.instance_id))
        self.assertEqual(self.read(), [])

    def test_non_reading_domains_preserve_outcomes_from_pinned_records(self):
        source = self.prepared()
        summary = m.Outcome(m.OutcomeStatus.FAILED, False, "private diagnostic", m.ReasonCode.PROVIDER_UNAVAILABLE)
        session = {"session_id": "session", "course_name": "伦理学", "revision": 1}
        scenarios = (
            ("study_local", "study", m.ActivityStatus.FAILED, "study", summary, {}, {}, "failed", "provider_unavailable"),
            ("city_trip", "city", m.ActivityStatus.CANCELLED, "at_stop", None, {}, {"cancel_requested": True,
                "explanations": [{"reason_code": "provider_unavailable"}, {"reason_code": "cancelled"}]},
                "cancelled", "delay_abandoned"),
            ("class_session_local", "campus_trip", m.ActivityStatus.UNKNOWN, "travel", None,
                {"session": session, "decision": "attend"}, {}, "unknown", "result_unknown"),
            ("class_session_local", "class", m.ActivityStatus.COMPLETED, "skip", None,
                {"session": session, "decision": "skip"}, {"attended_s": 0}, "skipped", "class_absent"),
            ("purchase_local", "buy_drink", m.ActivityStatus.FAILED, "pay", summary, {}, {}, "failed", "provider_unavailable"),
            ("local_timed_units", "tidy_desk", m.ActivityStatus.FAILED, "tidy", m.Outcome(m.OutcomeStatus.FAILED,
                False, "Local result abandoned: private diagnostic", m.ReasonCode.INVALID_CONFIG), {}, {}, "failed", "abandoned"),
        )
        template, prepared, audience = self.kernel.repo.pinned(source.owner_key, source.instance_id)
        with self.storage.transaction() as db:
            # The foreground slot can hold one preparing/running/unknown instance.
            db.execute("DELETE FROM life_kernel_instances WHERE instance_id=?", (source.instance_id,))
            for index, (executor, template_id, status, phase, outcome, params, checkpoint, _, _) in enumerate(scenarios):
                pinned = replace(template, executor=executor + "@1", params=freeze(params))
                record = replace(source, instance_id=f"scenario-{index}", template_id=template_id, status=status,
                    phase=phase, outcome=outcome, checkpoint=freeze(checkpoint),
                    reason_code=m.ReasonCode.RESULT_UNKNOWN if status is m.ActivityStatus.UNKNOWN else None)
                self.kernel.repo.create(db, record, pinned, prepared, audience)
        items = {item["activity_id"]: item for item in self.read()}
        for index, (*_, state, reason) in enumerate(scenarios):
            item = items[f"kernel:scenario-{index}"]
            self.assertEqual((item["state"], item["reason_code"], item["simulated"]), (state, reason, True))
            self.assertNotIn("private diagnostic", str(item))
        self.assertEqual(items["kernel:scenario-2"]["kind"], "campus_trip")
        self.assertEqual(items["kernel:scenario-4"]["title"], "买饮料")

    def test_management_prioritizes_live_and_recent_rows_across_sources(self):
        now = NOW + timedelta(hours=1)
        owner, context = self.scope.owner_key, self.scope.context_key
        academics = Academics(config=AcademicsConfig(enabled=True))
        academics.install(self.storage)
        for revision, (kind, record, identifier) in enumerate((
                ("term", term(), "fall"), ("timetable", entry(), "ethics-mon"))):
            academics.command(owner, context, {"command_id": kind, "expected_revision": revision,
                "action": "create", "kind": kind, "id": identifier, "record": record}, now=NOW)
        session = academics.next_sessions(owner, context, now=NOW)[0]
        attendance = ClassAttendance(academics, config=AttendanceConfig(enabled=True))
        attendance.settle_missed(self.scope, session, now=now, write_guard=lambda *_: nullcontext())

        old = NOW - timedelta(days=3)
        source = self.prepared(now=old)
        template, prepared, audience = self.kernel.repo.pinned(owner, source.instance_id)
        with self.storage.transaction() as db:
            db.execute("DELETE FROM life_kernel_instances WHERE instance_id=?", (source.instance_id,))
            pinned = replace(template, executor="city_trip@1")
            for index in range(LIMIT):
                trip = replace(source, instance_id=f"old-trip-{index}", status=m.ActivityStatus.COMPLETED)
                self.kernel.repo.create(db, trip, pinned, prepared, audience)
            for identifier, status, updated in (("live", m.ActivityStatus.RUNNING, old),
                                                ("recent", m.ActivityStatus.COMPLETED, now)):
                trip = replace(source, instance_id=identifier, status=status, updated_at=updated)
                self.kernel.repo.create(db, trip, pinned, prepared, audience)

        data = read_activities(self.storage, owner_key=owner, context_key=context, now=now,
                               kernel=True, academics=True, management=True)
        self.assertEqual(len(data["items"]), LIMIT)
        self.assertTrue(data["partial"])
        live, recent, missed = data["items"][:3]
        self.assertEqual([item["activity_id"] for item in (live, recent, missed)],
                         ["kernel:live", "kernel:recent", "class:" + session["session_id"]])
        self.assertEqual(missed["attendance"]["outcome"], "absent")


class AcademicOverviewTests(academic_tests.DomainCase):
    def test_class_schedule_distinguishes_cancelled_and_unconfirmed(self):
        self.write("term", term())
        self.write("timetable", entry(exceptions=[{"date": "2026-11-16", "cancelled": True,
            "start_time": None, "end_time": None, "place_id": None}]))
        self.write("timetable", entry(slot_id="other", start_time="16:30", end_time="16:40"))
        read = lambda context: read_activities(self.storage, owner_key=self.owner, context_key=context,
            now=NOW + timedelta(hours=1), kernel=False, academics=True)["items"]
        self.assertEqual([(item["state"], item["reason_code"]) for item in read(self.context)],
                         [("cancelled", "cancelled"), ("unknown", "result_unknown")])
        self.assertEqual(read("other-context"), [])
