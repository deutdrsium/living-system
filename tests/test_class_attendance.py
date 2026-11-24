"""Default-off A4 attendance, tested with the real A1 kernel and SQLite."""
from datetime import timedelta
from unittest.mock import patch

import pytest

import test_academics_domain as domain
from test_academics_domain import entry, term
from test_kernel_support import Access, SimulationClock
from test_study import assignment, syllabus
from good_life.academics import Academics, AcademicsAccess, AcademicsConfig, AttendanceConfig, Campus, ClassAttendance, register_academics
from good_life.academics.attendance import FOLLOW_UP, STUDY_CONSUMER
from good_life.academics.service import timestamp
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig
from good_life.kernel.codec import KernelError, wire
from good_life.storage import LifeStorage


def sleep_record(start_at, end_at=None):
    return {"started_at": wire(start_at), "updated_at": wire(end_at or start_at),
            "state": "completed" if end_at else "active", "scope": {"kind": "private"},
            "work": None if end_at else {"kind": "sleep"}, "sleep": {"kind": "night"}}


class TestAttendance:
    def setup_method(self):
        self.domain = domain.DomainCase()
        self.domain.setUp()
        self.host = Access()
        self.host.free_s = 7200
        self.scope = self.host.scope
        self.domain.owner, self.domain.context = self.scope.owner_key, self.scope.context_key
        self.start = timestamp("2026-11-16T00:00:00Z")
        self.end = self.start + timedelta(hours=1)
        self.departure = self.start - timedelta(minutes=5)
        self.check_at = self.departure - timedelta(minutes=15)
        self.domain.write("term", term(start_date="2026-11-15", week_starts=["2026-11-15", "2026-11-22"]), now=self.check_at)
        self.domain.write("timetable", entry(start_time="08:00", end_time="09:00"), now=self.check_at)
        self.session = self.domain.academics.next_sessions(self.scope.owner_key, self.scope.context_key, now=self.check_at)[0]
        self.session_id = self.session["session_id"]
        self.open_kernel()
        self.campus.locate(self.scope.owner_key, "dorm")

    def teardown_method(self):
        self.kernel.close()
        self.domain.storage.close()
        self.domain.doCleanups()

    def open_kernel(self):
        self.campus = Campus(("dorm", "classroom"), (("dorm", "classroom", 300),))
        registry = ComponentRegistry()
        register_academics(registry, self.domain.academics, campus=self.campus)
        access = AcademicsAccess(self.host, self.domain.academics, registry, self.campus)
        self.kernel = ActivityKernel(self.domain.storage, registry, config=KernelConfig(enabled=True), access=access, clock=SimulationClock())

    def restart(self):
        self.kernel.close()
        self.domain.storage.close()
        self.domain.storage = LifeStorage.open(self.domain.temp.name)
        self.domain.academics = Academics(config=AcademicsConfig(enabled=True))
        self.domain.academics.install(self.domain.storage)
        self.open_kernel()
        self.attendance = ClassAttendance(self.domain.academics, config=AttendanceConfig(enabled=True))

    def wake(self, config=None, **inputs):
        self.attendance = ClassAttendance(self.domain.academics, config=config or AttendanceConfig(enabled=True))
        return self.check(**inputs)

    def check(self, *, now=None, **inputs):
        return self.attendance.check_wake(self.kernel, self.scope, self.session_id,
            now=now or self.departure, planned_departure_at=self.departure, planned_travel_s=300, **inputs)

    def propose(self, now, *, with_route=True, delay_s=0, route=None, no_viable_route=False):
        if with_route and route is None:
            route = self.campus.route("dorm", "classroom")
        return self.attendance.propose(self.kernel, self.scope, self.session_id, now=now, route=route,
                                       delay_s=delay_s, no_viable_route=no_viable_route)

    def run_candidate(self, now, **kwargs):
        candidate = self.propose(now, **kwargs)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="prepare", now=now)
        assert prepared.gate.allowed, prepared.gate.message
        instance = prepared.instance
        return self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision, command_id="start", now=now)

    def finish(self, instance, *, now=None):
        return self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="finish", now=now or self.end)

    def participation(self):
        return self.domain.academics.participation(self.scope.owner_key, self.scope.context_key, self.session_id)

    def follow_ups(self):
        return self.domain.storage.pending_events(STUDY_CONSUMER, context_key=self.scope.context_key)

    def test_disabled_component_does_not_install_or_propose(self):
        self.attendance = ClassAttendance(self.domain.academics)
        assert self.check() is None
        assert self.propose(self.departure) is None
        assert not self.domain.storage._connection.execute("SELECT 1 FROM life_metadata WHERE key='class_attendance_schema_version'").fetchone()
        assert self.participation() is None

    def test_overslept_absent_and_only_one_record_after_both_crash_boundaries(self):
        waiting = sleep_record(self.start - timedelta(hours=8))
        cutoff = self.end - timedelta(minutes=25)
        assert self.wake(now=cutoff - timedelta(seconds=1), sleep_records=(waiting,)) is None
        saved = self.check(now=cutoff, sleep_records=(waiting,))
        assert timestamp(saved["ready_at"]) == self.end - timedelta(minutes=5)
        assert self.propose(self.check_at, with_route=False) is None
        assert self.participation() is None

        self.restart()
        assert self.check(now=self.end, a6_enabled=True, unwell=True) == saved
        assert self.domain.storage._connection.execute("SELECT COUNT(*) FROM life_class_attendance_wakes").fetchone()[0] == 1
        instance = self.run_candidate(cutoff, with_route=False)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash after result proposal")):
            with pytest.raises(RuntimeError, match="crash after result proposal"):
                self.finish(instance, now=cutoff)
        assert self.participation()["status"] == "active"
        assert self.follow_ups() == ()
        self.restart()
        assert len(self.kernel.recover_pending(self.scope, now=self.end)) == 1
        assert self.kernel.recover_pending(self.scope, now=self.end) == ()
        self.finish(instance, now=cutoff)  # Replay the original finish command.
        assert self.propose(self.start, with_route=False) is None
        assert self.participation()["attendance"] == {"outcome": "absent", "minutes_missed": 60,
            "cause_code": "overslept", "cause_label": "睡过头"}
        assert self.participation()["attended_s"] == 0
        assert self.campus.location(self.scope.owner_key)[0] == "dorm"
        assert self.domain.storage._connection.execute("SELECT COUNT(*) FROM life_academic_participation").fetchone()[0] == 1
        event, = self.follow_ups()
        assert event["kind"] == FOLLOW_UP and event["payload"]["action"] == "borrow_notes"
        assert event["payload"]["extra_work_s"] == 0

    @pytest.mark.parametrize("known_infeasible", [False, True])
    def test_no_viable_route_settles_absence_once(self, known_infeasible):
        ready = self.end - timedelta(minutes=2)
        wake = ready - timedelta(minutes=20)
        self.wake(now=wake, sleep_records=(sleep_record(self.start - timedelta(hours=8), wake),))
        assert self.participation() is None
        now = ready if known_infeasible else self.end
        instance = self.run_candidate(now, with_route=False, no_viable_route=known_infeasible)
        self.finish(instance, now=now)

        assert self.participation()["attendance"] == {"outcome": "absent", "minutes_missed": 60,
            "cause_code": "overslept", "cause_label": "睡过头"}
        assert self.participation()["attended_s"] == 0
        self.restart()
        assert self.propose(self.end + timedelta(minutes=1), with_route=False) is None
        event, = self.follow_ups()
        assert event["payload"]["action"] == "borrow_notes"

    def test_mild_oversleep_defers_departure_and_settles_partial_attendance(self):
        for kind, record_id, record in (("syllabus", "week1", syllabus()), ("assignment", "essay", assignment())):
            self.domain.write(kind, record, record_id=record_id, now=self.check_at)
        wake = self.departure - timedelta(minutes=10)
        saved = self.wake(now=wake, sleep_records=(sleep_record(self.start - timedelta(hours=8), wake),))
        ready = timestamp(saved["ready_at"])
        assert ready == wake + timedelta(minutes=20)
        assert self.propose(ready - timedelta(seconds=1)) is None
        assert self.kernel.repo.active(self.scope.owner_key) == ()
        self.finish(self.run_candidate(ready))
        assert self.participation()["attendance"] == {"outcome": "late", "minutes_missed": 10,
            "cause_code": "overslept", "cause_label": "睡过头"}
        assert (self.participation()["status"], self.participation()["attended_s"]) == ("partial", 3000)
        event, = self.follow_ups()
        assert (event["payload"]["action"], event["payload"]["assignment_id"], event["payload"]["extra_work_s"]) == ("catch_up_notes", "essay", 600)

    @pytest.mark.parametrize("use_existing_route", [False, True])
    def test_en_route_delay_uses_online_arrival(self, use_existing_route):
        self.wake()
        route = self.campus.route("dorm", "classroom")
        if use_existing_route:
            route = {**route, "travel_s": 720}
        self.finish(self.run_candidate(self.departure, route=route, delay_s=0 if use_existing_route else 420))
        assert self.participation()["attendance"] == {"outcome": "late", "minutes_missed": 7,
            "cause_code": "delayed_en_route", "cause_label": "路上耽搁"}
        assert self.participation()["attended_s"] == 3180

    @pytest.mark.parametrize("delay_in_quote", [False, True])
    def test_route_delay_consumes_remaining_buffer_after_late_wake(self, delay_in_quote):
        self.departure = self.start - timedelta(minutes=10)
        ready = self.start - timedelta(minutes=8)
        self.wake(sleep_records=(sleep_record(self.start - timedelta(hours=8), ready - timedelta(minutes=20)),))
        self.restart()
        route = self.campus.route("dorm", "classroom")
        if delay_in_quote:
            route = {**route, "travel_s": 900}
        instance = self.run_candidate(ready, route=route, delay_s=0 if delay_in_quote else 600)
        self.finish(instance)
        assert self.participation()["attendance"] == {"outcome": "late", "minutes_missed": 7,
            "cause_code": "delayed_en_route", "cause_label": "路上耽搁"}

    @pytest.mark.parametrize("a6_enabled", [False, True])
    def test_normal_on_time_and_unwell_only_with_a6(self, a6_enabled):
        self.wake(a6_enabled=a6_enabled, unwell=True)
        self.finish(self.run_candidate(self.departure, with_route=not a6_enabled), now=self.departure if a6_enabled else None)
        grade = self.participation()["attendance"]
        if a6_enabled:
            assert (grade["outcome"], grade["cause_code"], grade["cause_label"]) == ("absent", "unwell", "身体不适")
        else:
            assert grade == {"outcome": "on_time", "minutes_missed": 0, "cause_code": None, "cause_label": None}
            assert self.participation()["attended_s"] == 3600
            assert self.follow_ups() == ()

    def test_expired_route_cannot_create_an_absence(self):
        self.wake()
        route = {**self.campus.route("dorm", "classroom"), "travel_s": 7200,
                 "departure_at": self.check_at.isoformat()}
        assert self.propose(self.departure, route=route) is None
        assert self.participation() is None

    def test_cancellation_while_waiting_for_class_has_no_attendance_grade(self):
        self.wake()
        route = {**self.campus.route("dorm", "classroom"), "travel_s": 60}
        running = self.run_candidate(self.departure, route=route)
        arrived = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
            command_id="arrive", now=self.departure + timedelta(minutes=1))
        assert arrived.phase == "wait"
        self.kernel.cancel(self.scope, arrived.instance_id, expected_revision=arrived.revision,
            command_id="cancel-before-class", now=self.start)
        assert "attendance" not in self.participation()
        assert self.follow_ups() == ()

    def test_early_wake_uses_the_latest_night_and_ignores_naps(self):
        bedtime = self.start - timedelta(hours=8)
        wake = self.departure - timedelta(minutes=25)
        saved = self.wake(sleep_records=(sleep_record(bedtime, bedtime + timedelta(minutes=5)),
            sleep_record(bedtime + timedelta(minutes=10), wake),
            {**sleep_record(wake, self.departure), "sleep": {"kind": "nap"}}))
        assert timestamp(saved["ready_at"]) == wake + timedelta(minutes=20)
        assert saved["wake_delay_s"] == 0
        self.finish(self.run_candidate(self.departure))
        assert self.participation()["attendance"]["outcome"] == "on_time"

    def test_still_asleep_at_departure_then_wakes(self):
        bedtime = self.start - timedelta(hours=8)
        sleeping = sleep_record(bedtime)
        sleeping["work"]["due_at"] = wire(self.departure - timedelta(minutes=20))
        assert self.wake(sleep_records=(sleeping,)) is None
        assert not self.domain.storage._connection.execute("SELECT 1 FROM life_class_attendance_wakes").fetchone()
        wake = self.departure + timedelta(minutes=15)
        saved = self.check(now=wake, sleep_records=(sleep_record(bedtime, wake),))
        ready = wake + timedelta(minutes=20)
        assert timestamp(saved["ready_at"]) == ready
        assert self.propose(ready - timedelta(seconds=1)) is None
        self.finish(self.run_candidate(ready))
        assert self.participation()["attendance"]["minutes_missed"] == 35

    def test_no_night_record_at_departure_is_on_time(self):
        assert self.wake(now=self.check_at) is None
        saved = self.check()
        assert timestamp(saved["ready_at"]) == self.departure
        self.finish(self.run_candidate(self.departure))
        assert self.participation()["attendance"]["outcome"] == "on_time"

    def test_hosted_attendance_off_ignores_active_night_sleep(self):
        night = sleep_record(self.start - timedelta(hours=8))
        saved = self.wake(AttendanceConfig(enabled=True, use_sleep_records=False), sleep_records=(night,))
        assert timestamp(saved["ready_at"]) == self.departure
        self.finish(self.run_candidate(self.departure))
        assert self.participation()["attendance"]["outcome"] == "on_time"

    def test_pre_departure_window_is_bounded(self):
        self.attendance = ClassAttendance(self.domain.academics, config=AttendanceConfig(enabled=True))
        with pytest.raises(KernelError, match="execution window"):
            self.attendance.check_wake(self.kernel, self.scope, self.session_id,
                now=self.check_at - timedelta(hours=1), planned_departure_at=self.departure)
