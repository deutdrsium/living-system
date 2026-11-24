"""W6 class wiring through a provider lease, a synthetic map and a virtual clock."""
from dataclasses import replace
from datetime import timedelta
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from test_academics_domain import entry, term
from test_class_attendance import sleep_record
from test_kernel_support import Access, NOW, ROOT, SimulationClock, m
from test_life_runtime import Host
from test_shared_travel import SETTINGS, graph
from test_study import assignment, syllabus

from good_life.academics import Academics, AcademicsAccess, AcademicsConfig, Campus, register_academics, register_class_group
from good_life.academics.attendance import STUDY_CONSUMER
from good_life.academics.service import timestamp
from good_life.activities.class_session import propose_class
from good_life.activities.local_order import TEMPLATE, register_local_order
from good_life.activities.study import propose_study
from good_life.activity_import import DDL
from good_life.contracts.ports import AuthorizedMaterial
from good_life.graph_package_storage import GraphPackageStore
from good_life.graph_packages import seal_package
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig
from good_life.kernel.codec import digest, wire
from good_life.runtime import LifeRuntime, OptionalGroupBinding
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage, StorageReader
from good_life.travel import register_shared_travel_group


class HostedClasses(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory(prefix=".hosted-classes-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(lambda: self.storage.close())
        self.access = Access()
        self.access.free_s = 86400
        self.scope = self.access.scope
        self.owner, self.context = self.scope.owner_key, self.scope.context_key
        self.now = NOW - timedelta(minutes=45)
        self.revision = 0
        self.settings = {**SETTINGS, "campus_enabled": True, "home_place_id": "home"}

    def confirm_graph(self, *, far_travel_s=240):
        package = graph()
        package["nodes"][0]["access_requirement"] = "resident_of:home"
        next(edge for edge in package["edges"] if edge["to"] == "far")["estimated_duration_s"] = far_travel_s
        package = seal_package(package)
        GraphPackageStore(self.storage, enabled=True).confirm(package, expected_current_digest=None,
            expected_proposed_digest=package["digest"], confirmed=True)

    async def bind(self, *, enabled=True, campus=True, home="home", attendance=False, kernel=True, sleep=False, study=False):
        self.runtime = LifeRuntime(self.storage, object(), kernel_enabled=kernel, wall_clock=lambda: self.now,
                                   monotonic_clock=lambda: (self.now - NOW).total_seconds())
        self.addAsyncCleanup(self.runtime.close)
        settings = {**self.settings, "campus_enabled": campus, "home_place_id": home}
        register_shared_travel_group(self.runtime, settings)
        register_class_group(self.runtime, enabled=enabled, attendance_enabled=attendance,
                             academics_config=AcademicsConfig(enabled=enabled, study_enabled=study))
        if kernel:
            def bind_local(context):
                register_local_order(context.registry)
                return OptionalGroupBinding()
            self.runtime.register_optional_group("local", bind_local, phase="pre_kernel")
        host = Host()
        host.life_kernel_access = lambda **_: self.access
        if study:
            ports = SimpleNamespace(resolve_authority=AsyncMock(), read_material=AsyncMock())
            host.life_async_capabilities = lambda **_: {"api_version": "companion.life-host.async.v1",
                "contract_version": 1, "capabilities": ("resolve_authority", "read_material"), "ports": ports}
        if sleep:
            self.install_sleep_host(host)
        with patch.object(DueSleepScheduler, "start"):
            await self.runtime.bind(host)
        await self.runtime._optional.stop_worker()
        if enabled:
            self.assertEqual(self.runtime.optional_diagnostics, {})
            self.academics = self.runtime.academics
            self.campus = self.runtime._optional.groups["classes"].value.campus

    async def restart(self, **kwargs):
        await self.runtime.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temp.name)
        await self.bind(**kwargs)

    async def tick(self, now):
        self.now = now
        for phase in ("settle", "commitments"):
            await self.runtime._optional._run_phase(self.runtime._lease, phase, now)
        self.assertEqual(self.runtime.advance_kernel(now=now)["failures"], ())
        self.assertEqual(self.runtime.optional_diagnostics, {})

    def write(self, kind, record, *, action="create"):
        record_id = record[{"term": "term_id", "timetable": "slot_id", "syllabus": "unit_id",
                            "assignment": "assignment_id"}[kind]]
        result = self.academics.command(self.owner, self.context, {"command_id": f"edit-{self.revision}",
            "expected_revision": self.revision, "action": action, "kind": kind, "id": record_id,
            "record": record}, now=self.now)
        self.revision = result["revision"]

    def session(self):
        return self.academics.next_sessions(self.owner, self.context, now=self.now)[0]

    def participation(self, session):
        return self.academics.participation(self.owner, self.context, session["session_id"])

    def running(self):
        instance, = self.runtime.kernel.repo.active(self.owner)
        self.assertEqual(instance.status, m.ActivityStatus.RUNNING)
        return instance

    def record_sleep(self, started, ended=None):
        record = sleep_record(started, ended)
        with self.storage.transaction() as db:
            db.execute("INSERT OR REPLACE INTO staged_role_activities VALUES (?,?,?,?,?,?,?)",
                (self.owner, "night-sleep", "{}", 2 if ended else 1, record["state"], record["updated_at"], json.dumps(record)))

    def install_sleep_ledger(self):
        with self.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)

    def wake_decision(self):
        row = StorageReader(self.storage).one("SELECT record_json FROM life_class_attendance_wakes WHERE session_id=?",
                                              (self.session()["session_id"],))
        return json.loads(row[0]) if row else None

    async def start_scenario(self, *, attendance=False):
        self.confirm_graph()
        await self.bind(attendance=attendance)
        self.write("term", term())
        self.write("timetable", entry(place_id="far"))
        session = self.session()
        departure = timestamp(session["start_at"]) - timedelta(minutes=9)
        await self.tick(departure - timedelta(minutes=15))
        return session, departure

    async def test_reserved_commute_and_on_time_class_from_home(self):
        session, departure = await self.start_scenario()
        self.assertEqual(self.campus.location(self.owner), ("home", 1))
        self.assertEqual(self.campus.position.evidence(self.owner, 1)["kind"], "configured_home")
        self.assertIn("resident_of:home", self.campus.access_policy.credentials)
        kernel = self.runtime.kernel
        near = departure - timedelta(seconds=90)
        candidate = kernel.propose(self.scope, m.TemplateRef(TEMPLATE.id, TEMPLATE.version),
            opportunity_key="tidy", reason="tidy_requested", now=near, expires_at=departure)
        prepare = kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="tidy-prepare", now=near)
        self.assertIsNotNone(prepare.instance)
        tidy = prepare.instance
        denied = kernel.start(self.scope, tidy.instance_id, expected_revision=tidy.revision,
                             command_id="tidy-start", now=near)
        self.assertEqual(denied.reason_code, m.ReasonCode.NO_TIME_WINDOW)
        await self.tick(departure)
        instance = self.running()
        location = kernel._snapshot(self.scope, departure).state.values["geo.place_id"]
        self.assertNotEqual(location.revision, 1)
        self.assertEqual(instance.checkpoint["location_revision"], location.value["revision"])
        await self.tick(departure + timedelta(seconds=239))
        self.assertEqual(self.campus.location(self.owner), ("home", 1))
        await self.tick(departure + timedelta(seconds=240))
        self.assertEqual(self.campus.location(self.owner), ("far", 2))
        await self.tick(timestamp(session["end_at"]))
        self.assertEqual(self.participation(session)["attendance"]["outcome"], "on_time")
        self.assertEqual(self.participation(session)["attended_s"], 600)

    async def test_material_backed_a5_proposal_binds_ports_on_demand(self):
        ref = m.SourceRef("text", "v1", "hash", 0, 20, NOW, None, "permission-private")
        bound = self.access.audience(self.scope)
        ports = SimpleNamespace(resolve_authority=AsyncMock(return_value=self.scope),
                                read_material=AsyncMock(return_value=AuthorizedMaterial(ref, "Fixture text", bound)))
        with patch.object(Host, "life_async_capabilities", create=True, return_value={
                "api_version": "companion.life-host.async.v1", "contract_version": 1,
                "capabilities": ("resolve_authority", "read_material"), "ports": ports}) as negotiate:
            await self.bind(campus=False)
            negotiate.assert_not_called()
            self.assertFalse(self.runtime.has_study_history)
            self.write("term", term())
            self.write("timetable", entry())
            self.write("syllabus", syllabus(materials=[{"source_ref": wire(ref), "audience_bound": wire(bound)}]))
            self.write("assignment", assignment())
            candidate = await self.runtime.propose_study(self.scope, now=self.now, opportunity_key="a5-material")
        self.assertEqual(candidate.target_ref.id, "essay:0")
        negotiate.assert_called_once()
        ports.read_material.assert_awaited_once()

    async def test_moved_class_preempts_study_and_keeps_partial_work(self):
        self.confirm_graph()
        await self.bind(study=True)
        self.write("term", term())
        self.write("timetable", entry(place_id="far", start_time="17:35", end_time="17:50"))
        self.write("syllabus", syllabus())
        self.write("assignment", assignment())
        study_start = NOW + timedelta(hours=1)
        await self.tick(study_start)
        kernel = self.runtime.kernel
        await propose_study(kernel, self.academics, self.scope, now=study_start, opportunity_key="before-edit")
        await self.tick(study_start)
        study = self.running()
        self.assertEqual(study.planned_duration_s, 1500)

        self.now = study_start + timedelta(minutes=1)
        self.write("timetable", entry(place_id="far", start_time="17:15", end_time="17:30"), action="update")
        session = self.session()
        departure = timestamp(session["start_at"]) - timedelta(minutes=9)
        await self.tick(self.now)
        stopped = kernel.repo.instance(self.owner, study.instance_id)
        self.assertEqual((stopped.status, stopped.elapsed_s), (m.ActivityStatus.CANCELLED, 60))
        item = self.academics.read(self.owner, self.context, "study_progress", {"term_id": "fall", "course_id": "ethics"})["items"][0]
        self.assertEqual(item["completed_work_s"], 60)
        await self.tick(departure)
        self.assertEqual(self.running().target_ref.id, session["session_id"])
        await self.tick(timestamp(session["end_at"]))
        self.assertEqual(self.participation(session)["attendance"]["outcome"], "on_time")

    async def test_already_in_classroom_is_on_time_despite_tick_jitter(self):
        self.confirm_graph()
        await self.bind()
        self.write("term", term())
        self.write("timetable", entry(place_id="home"))
        session = self.session()
        start = timestamp(session["start_at"])
        await self.tick(start - timedelta(minutes=20))
        await self.tick(start + timedelta(milliseconds=500))
        self.assertEqual(self.running().checkpoint["params"]["route"]["travel_s"], 0)
        await self.tick(timestamp(session["end_at"]))
        row = self.participation(session)
        self.assertEqual(row["arrival_at"], session["start_at"])
        self.assertEqual((row["attendance"]["outcome"], row["attendance"]["minutes_missed"]), ("on_time", 0))

    async def test_origin_change_recomputes_departure_without_replacing_wake(self):
        self.confirm_graph()
        await self.bind(attendance=True)
        self.write("term", term())
        self.write("timetable", entry(place_id="far"))
        session = self.session()
        start = timestamp(session["start_at"])
        original_departure = start - timedelta(minutes=9)
        self.install_sleep_ledger()
        self.record_sleep(start - timedelta(hours=16), original_departure - timedelta(hours=1))
        await self.tick(original_departure - timedelta(minutes=15))
        wake = self.wake_decision()
        self.assertIsNotNone(wake)
        self.campus.locate(self.owner, "near", expected_revision=1, evidence_ref="moved-before-class")
        new_departure = original_departure - timedelta(minutes=3)
        await self.tick(new_departure - timedelta(seconds=1))
        self.assertFalse(self.runtime.kernel.repo.foreground_busy(self.owner))
        await self.tick(new_departure)
        self.assertEqual(self.wake_decision(), wake)
        instance = self.running()
        self.assertEqual(instance.started_at, new_departure)
        self.assertEqual(instance.checkpoint["params"]["route"]["from_place_id"], "near")
        self.assertEqual(instance.checkpoint["location_revision"], 2)

    async def test_late_wake_is_reused_after_restart(self):
        self.install_sleep_ledger()
        session, departure = await self.start_scenario(attendance=True)
        start = timestamp(session["start_at"])
        bedtime, wake = start - timedelta(hours=16), departure - timedelta(minutes=13)
        self.record_sleep(bedtime, wake)
        await self.tick(wake)
        decision = self.wake_decision()
        ready = wake + timedelta(minutes=20)
        self.assertEqual(timestamp(decision["ready_at"]), ready)
        await self.restart(attendance=True)
        await self.tick(ready - timedelta(seconds=1))
        self.assertIsNone(self.participation(session))
        await self.tick(ready)
        self.assertEqual(self.running().started_at, ready)
        self.assertEqual(self.wake_decision(), decision)
        await self.tick(timestamp(session["end_at"]))
        grade = self.participation(session)["attendance"]
        self.assertEqual((grade["outcome"], grade["cause_code"], grade["minutes_missed"]), ("late", "overslept", 2))

    async def test_waits_for_sleep_receipt_at_departure(self):
        self.install_sleep_ledger()
        self.confirm_graph()
        await self.bind(attendance=True)
        self.write("term", term())
        self.write("timetable", entry(place_id="far", end_time="17:00"))
        session = self.session()
        start = timestamp(session["start_at"])
        departure = start - timedelta(minutes=4)
        bedtime = start - timedelta(hours=16)
        self.record_sleep(bedtime)
        await self.tick(departure)
        self.assertIsNone(self.wake_decision())
        self.assertIsNone(self.participation(session))
        wake = departure + timedelta(minutes=15)
        self.record_sleep(bedtime, wake)
        await self.tick(wake)
        self.assertIsNone(self.participation(session))
        await self.tick(wake + timedelta(minutes=20))
        self.assertEqual(self.running().started_at, wake + timedelta(minutes=20))
        await self.tick(timestamp(session["end_at"]))
        grade = self.participation(session)["attendance"]
        self.assertEqual((grade["outcome"], grade["cause_code"], grade["minutes_missed"]), ("late", "overslept", 35))

    async def test_commute_resume_accounts_for_the_delay_without_offline_travel(self):
        session, departure = await self.start_scenario()
        await self.tick(departure)
        await self.tick(departure + timedelta(minutes=2))
        self.now = departure + timedelta(minutes=9)
        await self.restart()
        await self.tick(self.now)
        self.assertEqual(self.running().checkpoint["travel_elapsed_s"], 120)
        await self.tick(timestamp(session["end_at"]))
        grade = self.participation(session)["attendance"]
        self.assertEqual((grade["outcome"], grade["cause_code"], grade["minutes_missed"]),
                         ("late", "delayed_en_route", 2))

    async def test_absence_waits_for_end_and_is_recorded_once(self):
        self.install_sleep_ledger()
        session, _ = await self.start_scenario(attendance=True)
        end = timestamp(session["end_at"])
        self.record_sleep(end - timedelta(hours=16))
        await self.tick(end - timedelta(seconds=1))
        self.assertIsNone(self.participation(session))
        await self.tick(end)
        first = self.participation(session)
        row, = [item for item in self.runtime.read_overview_activities(owner_key=self.owner, context_key=self.context,
                 now=end)["items"] if item["activity_id"] == "class:" + session["session_id"]]
        self.assertEqual((row["state"], row["reason_code"], row["simulated"]), ("skipped", "overslept", True))
        self.assertEqual(first["attendance"]["cause_code"], "overslept")
        self.assertEqual(first["attendance"]["outcome"], "absent")
        await self.restart()
        await self.tick(end + timedelta(days=2))
        self.assertEqual(self.participation(session), first)
        event, = self.storage.pending_events(STUDY_CONSUMER, context_key=self.context)
        self.assertEqual(event["payload"]["action"], "borrow_notes")
        count = StorageReader(self.storage).one("SELECT count(*) FROM life_academic_participation", ())[0]
        self.assertEqual(count, 1)

    async def test_no_origin_after_downtime_settles_only_the_live_class(self):
        await self.bind(campus=False, home="")
        self.write("term", term())
        self.write("timetable", entry(place_id="far"))
        session = self.session()
        self.now = timestamp(session["end_at"]) + timedelta(days=2)
        await self.restart(campus=False, home="")
        await self.tick(self.now)
        self.assertEqual(self.participation(session)["attendance"]["cause_code"], "missed")
        self.assertEqual(len(self.storage.pending_events(STUDY_CONSUMER)), 1)

    async def test_rescheduling_expires_old_class_cue_across_restart(self):
        session, departure = await self.start_scenario()
        old = propose_class(self.runtime.kernel, self.academics, self.scope, session["session_id"],
                            now=departure, campus=self.campus)
        self.write("timetable", entry(place_id="near", start_time="16:25", end_time="16:35"), action="update")
        revised = self.session()
        self.assertEqual(revised["session_id"], session["session_id"])
        self.assertEqual(revised["revision"], session["revision"] + 1)
        await self.restart()
        self.assertEqual(self.runtime.kernel.repo.candidate(self.owner, old.candidate_id)[1].status,
                         m.CandidateStatus.EXPIRED)
        await self.tick(departure)
        self.assertEqual(self.runtime.kernel.repo.active(self.owner), ())
        await self.tick(timestamp(revised["start_at"]) - timedelta(minutes=8))
        instance = self.running()
        self.assertEqual(instance.target_ref.revision, revised["revision"])
        self.assertEqual(instance.checkpoint["params"]["route"]["place_id"], "near")

    async def test_legacy_class_recovers_with_its_original_location_revision(self):
        self.confirm_graph()
        self.academics = Academics(config=AcademicsConfig(enabled=True))
        campus = Campus(("near", "far"), (("near", "far", 420),))
        registry = ComponentRegistry()
        register_academics(registry, self.academics, campus=campus)
        legacy = ActivityKernel(self.storage, registry, config=KernelConfig(enabled=True), clock=SimulationClock(),
                                access=AcademicsAccess(self.access, self.academics, registry, campus))
        self.write("term", term())
        self.write("timetable", entry(place_id="far"))
        campus.locate(self.owner, "near")
        campus.locate(self.owner, "near", expected_revision=1)
        session = self.session()
        candidate = propose_class(legacy, self.academics, self.scope, session["session_id"], now=NOW, campus=campus)
        prepared = legacy.prepare_candidate(self.scope, candidate.candidate_id, command_id="v1-prepare", now=NOW).instance
        running = legacy.start(self.scope, prepared.instance_id, expected_revision=prepared.revision,
                               command_id="v1-start", now=NOW)
        self.assertEqual(running.checkpoint["location_revision"], 2)
        legacy.close()
        self.now = NOW + timedelta(minutes=1)
        await self.bind()
        await self.tick(self.now)
        self.assertEqual(self.running().checkpoint["location_revision"], 2)
        self.assertEqual(self.campus.position.read(self.owner).revision, 0)
        await self.tick(self.now + timedelta(minutes=7))
        self.assertEqual(campus.location(self.owner), ("far", 3))

    async def test_flags_off_leave_core_and_empty_storage_unchanged(self):
        await self.bind(enabled=False, campus=False, kernel=False)
        self.assertTrue(self.runtime.ready)
        self.assertIsNone(self.runtime.kernel)
        self.assertIsNone(self.runtime.academics)
        self.assertEqual(self.runtime._optional.groups, {})
        self.assertIsNone(self.runtime.scheduler.hold_night_sleep)
        self.assertIsNone(StorageReader(self.storage).one("""SELECT 1 FROM life_metadata WHERE key IN
            ('academics_schema_version', 'class_attendance_schema_version', 'shared_position_schema_version',
             'hosted_classes_started_at')""", ()))

    async def test_disabling_classes_settles_inflight_work_and_starts_nothing_new(self):
        session, departure = await self.start_scenario()
        await self.tick(departure)
        instance_id = self.running().instance_id
        await self.tick(departure + timedelta(minutes=1))
        await self.restart(enabled=False, campus=False, kernel=False)
        self.assertEqual(self.runtime._optional.modes["classes"], "settle_only")
        now = timestamp(session["end_at"]) + timedelta(days=10)
        await self.runtime._optional._run_phase(self.runtime._lease, "settle", now)
        self.runtime.advance_kernel(now=now)
        instance = self.runtime.kernel.repo.instance(self.owner, instance_id)
        self.assertEqual((instance.status, instance.elapsed_s), (m.ActivityStatus.CANCELLED, 60))
        self.assertEqual(len(self.storage.pending_events(STUDY_CONSUMER)), 1)

    async def test_flags_off_restart_delivers_queued_catch_up_once(self):
        await self.bind(campus=False, home="", study=True)
        self.write("term", term())
        self.write("timetable", entry(place_id="far"))
        session = self.session()
        end = timestamp(session["end_at"])
        await self.tick(end)
        event, = self.storage.pending_events(STUDY_CONSUMER, context_key=self.context)
        self.assertEqual(event["payload"]["action"], "borrow_notes")

        await self.restart(enabled=False, campus=False, kernel=False)
        # Restart again after the first rollback bind clears the class cutover marker.
        await self.restart(enabled=False, campus=False, kernel=False)
        self.assertEqual(self.runtime._optional.modes["classes"], "settle_only")
        for now in (end, end + timedelta(seconds=1)):
            await self.runtime._optional._run_phase(self.runtime._lease, "settle", now)
        self.assertFalse(self.runtime._optional.work_failures)
        self.assertEqual(self.storage.pending_events(STUDY_CONSUMER, context_key=self.context), ())
        followup, = StorageReader(self.storage).all("SELECT session_id,kind FROM life_study_work_followups", ())
        self.assertEqual(tuple(followup), (session["session_id"], "borrow_notes"))
        self.assertEqual(self.runtime.kernel.repo.active(self.owner), ())

    def install_sleep_host(self, host):
        base = host.life_host_capabilities
        self.bedtimes = []
        self.sleep_plans = []
        def snapshot(_context):
            return {"observed_at": wire(self.now), "context_key": self.context,
                "owner": {"installation_id": "test", "bot_id": "bot", "persona_id": "role"},
                "settings": {"enabled": True, "sleep_enabled": True, "sleep_timezone": "Asia/Shanghai",
                    "sleep_time": "23:30", "sleep_variation_minutes": 0, "sleep_minutes": 480,
                    "sleep_window_minutes": 0}, "config_revision": 1, "profile": {}, "busy": False,
                "selected_executor": True, "active_reading": False, "napped_today": False}

        async def start(_context, plan):
            self.bedtimes.append(self.now)
            self.sleep_plans.append(plan)
            self.record_sleep(self.now, self.now + timedelta(hours=7))
            return {"state": "committed"}

        host.life_host_capabilities = lambda **args: {**base(**args),
            "contexts": lambda: [{"owner_key": self.owner, "context_key": self.context}],
            "sleep_snapshot": snapshot, "start_sleep": start, "recover_sleep_creation": start}

    async def test_return_walk_delays_bedtime_and_feeds_the_next_wake(self):
        self.confirm_graph()
        with self.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        self.now = NOW + timedelta(hours=7)
        await self.bind(attendance=True, sleep=True)
        self.write("term", term())
        self.write("timetable", entry(place_id="far", start_time="23:20", end_time="23:30"))
        self.write("timetable", entry(slot_id="tuesday", weekday=2, place_id="near", start_time="08:00", end_time="09:00"))
        session = self.session()
        bedtime = timestamp(session["end_at"])
        await self.tick(timestamp(session["start_at"]) - timedelta(minutes=9))
        await self.runtime.scheduler.tick(now=self.now)
        await self.tick(bedtime)
        self.assertTrue(self.running().template_id.startswith("campus_walk_"))
        await self.tick(bedtime + timedelta(seconds=30))
        await self.runtime.scheduler.tick(now=self.now)
        self.assertEqual(self.bedtimes, [])
        await self.tick(bedtime + timedelta(minutes=4))
        self.assertEqual(self.campus.location(self.owner)[0], "home")
        await self.runtime.scheduler.tick(now=self.now)
        self.assertEqual(self.bedtimes, [bedtime + timedelta(minutes=4)])
        start = timestamp(self.session()["start_at"])
        await self.tick(start - timedelta(minutes=20))
        wake = self.bedtimes[0] + timedelta(hours=7)
        self.assertEqual(timestamp(self.wake_decision()["ready_at"]), wake + timedelta(minutes=20))

    async def test_late_bedtime_leaves_time_for_morning_class_prep_and_commute(self):
        owner = {"installation_id": "test", "bot_id": "bot", "persona_id": "role"}
        self.owner = digest(owner)
        self.scope = self.access.scope = replace(self.scope, owner_key=m.OwnerKey(self.owner))
        self.access.inventory = (self.scope,)
        self.confirm_graph(far_travel_s=1116)
        self.install_sleep_ledger()
        self.now = bedtime = timestamp("2026-11-23T01:30:00+08:00")
        await self.bind(attendance=True)
        self.write("term", term())
        await self.tick(bedtime)
        self.assertEqual(self.campus.location(self.owner)[0], "home")
        scope = {"installation_id": owner["installation_id"], "bot_id": owner["bot_id"], "platform": "test",
                 "kind": "private", "native_scope_id": "native", "account_id": "account",
                 "conversation_id": "conversation", "session_id": "session"}
        sleep_inputs = dict(owner=owner, scope=scope, context_key=self.context,
            request_id="daily-sleep-2026-11-22", observed_at=wire(bedtime), kind="night",
            base_need_minutes=480, workload_score=0, sleep_debt_minutes=67)
        creations = self.runtime.activity_control.creations
        normal_wake = bedtime + timedelta(minutes=510)
        self.assertEqual(timestamp(creations.propose_scheduled_sleep(**sleep_inputs)["work"]["due_at"]), normal_wake)
        self.write("timetable", entry(slot_id="afternoon", place_id="far"))
        self.assertEqual(timestamp(creations.propose_scheduled_sleep(**sleep_inputs)["work"]["due_at"]), normal_wake)
        self.write("timetable", entry(place_id="far", start_time="09:55", end_time="11:35"))
        session = self.session()
        start = timestamp(session["start_at"])

        activity = creations.propose_scheduled_sleep(**sleep_inputs)
        wake_at = timestamp(activity["work"]["due_at"])
        self.assertEqual(wake_at, timestamp("2026-11-23T09:11:24+08:00"))
        self.assertEqual((activity["sleep"]["base_need_minutes"], activity["sleep"]["target_minutes"]), (480, 461))
        self.record_sleep(bedtime, wake_at)
        # Wake settlement and commute scheduling can each land a few seconds after their due time.
        await self.tick(wake_at + timedelta(seconds=3))
        decision = self.wake_decision()
        self.assertEqual(timestamp(decision["ready_at"]), wake_at + timedelta(minutes=20))
        self.assertEqual(decision["planned_travel_s"], 1116)
        await self.tick(wake_at + timedelta(minutes=20, seconds=3))
        self.assertEqual(self.running().target_ref.id, session["session_id"])
        await self.tick(start)
        await self.tick(timestamp(session["end_at"]))
        participation = self.participation(session)
        self.assertLessEqual(timestamp(participation["arrival_at"]), start)
        self.assertEqual(participation["attendance"]["outcome"], "on_time")

    async def test_alarm_applies_only_before_a_morning_class_even_without_attendance(self):
        self.confirm_graph()
        self.now = NOW + timedelta(hours=7, minutes=30)
        await self.bind()
        self.write("term", term())
        self.write("timetable", entry(slot_id="tuesday", weekday=2, place_id="far", start_time="08:00", end_time="09:00"))
        self.write("timetable", entry(slot_id="wednesday", weekday=3, place_id="far"))
        await self.tick(self.now)  # Initialize the configured home through the normal hosted phase.
        alarm = self.runtime.activity_control.creations.night_sleep_alarm
        start = timestamp(self.session()["start_at"])
        self.assertEqual(alarm(self.owner, self.context, self.now), start - timedelta(minutes=29))
        self.assertIsNone(alarm(self.owner, self.context, self.now + timedelta(days=1)))
        await self.restart(enabled=False)
        self.assertIsNone(self.runtime.activity_control.creations.night_sleep_alarm)

    async def test_alarm_falls_back_to_30_minutes_of_travel(self):
        self.now = NOW + timedelta(hours=7, minutes=30)
        await self.bind(attendance=True, campus=False, home="")
        self.write("term", term())
        self.write("timetable", entry(slot_id="tuesday", weekday=2, place_id="far", start_time="08:00", end_time="09:00"))
        alarm = self.runtime.activity_control.creations.night_sleep_alarm
        start = timestamp(self.session()["start_at"])
        self.assertEqual(alarm(self.owner, self.context, self.now), start - timedelta(minutes=55))

    async def test_held_bedtime_survives_restart_during_return_walk(self):
        self.confirm_graph()
        with self.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        self.now = NOW + timedelta(hours=7)
        await self.bind(sleep=True)
        self.write("term", term())
        self.write("timetable", entry(place_id="far", start_time="23:20", end_time="23:30"))
        session = self.session()
        bedtime = timestamp(session["end_at"])
        await self.tick(timestamp(session["start_at"]) - timedelta(minutes=9))
        await self.runtime.scheduler.tick(now=self.now)
        await self.tick(bedtime)
        await self.tick(bedtime + timedelta(seconds=30))
        await self.runtime.scheduler.tick(now=self.now)
        self.assertEqual(self.bedtimes, [])
        walk = self.running()
        self.assertTrue(walk.template_id.startswith("campus_walk_"))

        self.now = bedtime + timedelta(minutes=2)
        await self.restart(sleep=True)
        await self.tick(self.now)
        await self.runtime.scheduler.tick(now=self.now)
        self.assertEqual(self.runtime.kernel.repo.instance(self.owner, walk.instance_id).elapsed_s, 30)
        self.assertEqual(self.bedtimes, [])
        arrival = self.now + timedelta(seconds=210)
        await self.tick(arrival)
        self.assertEqual(self.campus.location(self.owner)[0], "home")
        await self.runtime.scheduler.tick(now=self.now)
        self.assertEqual(self.bedtimes, [arrival])
        plan, = self.sleep_plans
        self.assertEqual(timestamp(plan["scheduled_at"]), bedtime)
        self.assertEqual(plan["request_id"], "daily-sleep-2026-11-16")
