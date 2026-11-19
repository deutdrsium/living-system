"""Shared A4/A8 position migration and committed campus travel on a synthetic map."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

import test_city_trip as legacy
import test_academics_lifecycle as legacy_classes
from test_academics_domain import entry, term
from test_life_runtime import Host
from test_kernel_support import Access, NOW, ROOT, SimulationClock, m
from good_life.academics import Academics, AcademicsAccess, AcademicsConfig, Campus, register_academics
from good_life.activities.class_session import eligibility_key, propose_class
from good_life.city.domain import City, CityAccess, register_city
from good_life.graph_package_storage import GraphPackageStore
from good_life.graph_packages import seal_package
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig, KernelError
from good_life.kernel.codec import freeze
from good_life.runtime import LifeRuntime, OptionalGroupBinding
from good_life.storage import LifeStorage, StorageReader
from good_life.travel import (STATE_KEY, has_position_history, propose_walk, register_shared_travel,
                              register_shared_travel_group)

SETTINGS = {"academics_graph_id": "test-home", "treat_unknown_opening_as": "allowed"}


def graph():
    package = legacy.home_graph()
    for node in package["nodes"]:
        node.update(access="public", opening_rules="24/7")
    for edge in package["edges"]:
        edge.update(access="public", availability="24/7")
        edge["estimated_duration_s"] = 180 if edge["to"] == "near" else 240
    far = package["nodes"][2]
    building = {**far, "place_id": "unnamed", "name": "", "type": "building", "opening_rules": "24/7", "access": "public"}
    package["nodes"].append(building)
    package["edges"].append({**package["edges"][1], "edge_id": "to-unnamed", "to": "unnamed"})
    return seal_package(package)


class TravelAccess(Access):
    def __init__(self, storage, travel):
        super().__init__()
        self.storage, self.travel = storage, travel
        self.capabilities += ("campus",)

    def snapshot(self, scope, now):
        snapshot = super().snapshot(scope, now)
        state = snapshot.state
        position = self.travel.project(SimpleNamespace(scope=scope, now=now, reader=StorageReader(self.storage)))
        values = {**state.values, STATE_KEY: m.StateValue(position, now, 1, (), m.StateQuality.VALID if position else m.StateQuality.UNKNOWN)}
        return replace(snapshot, state=replace(state, values=freeze(values)))


class SharedTravelTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix=".travel-", dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.storage = LifeStorage.open(self.root)
        self.addCleanup(lambda: self.storage.close())
        self.owner = Access().scope.owner_key
        self.legacy = Campus(("home", "near", "far"), ())
        self.legacy.install(self.storage)
        self.legacy.locate(self.owner, "near")
        self.store = GraphPackageStore(self.storage, enabled=True)
        package = graph()
        self.store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
        self.build_kernel()
        self.addCleanup(lambda: self.kernel.close())

    def build_kernel(self, *, settle_only=False):
        self.registry = ComponentRegistry()
        self.travel = register_shared_travel(self.registry, settings=SETTINGS, graph_store=self.store,
                                            enabled=True, settle_only=settle_only)
        self.campus, self.position = self.travel.campus, self.travel.position
        self.access = TravelAccess(self.storage, self.travel)
        self.scope = self.access.scope
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True),
                                     access=self.access, clock=SimulationClock())

    def reopen(self, *, settle_only=False):
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.root)
        self.store = GraphPackageStore(self.storage)
        self.build_kernel(settle_only=settle_only)
        self.legacy.storage = self.storage

    def prepare(self, *, now=NOW, destination="far", key="walk"):
        candidate = propose_walk(self.kernel, self.campus, self.scope, destination, now=now, opportunity_key=key)
        result = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="prepare-" + key, now=now)
        self.assertTrue(result.gate.allowed)
        return result.instance

    def start(self, *, now=NOW, destination="far", key="walk"):
        item = self.prepare(now=now, destination=destination, key=key)
        return self.kernel.start(self.scope, item.instance_id, expected_revision=item.revision, command_id="start-" + key, now=now)

    def step(self, item, elapsed_s, *, cancel=False):
        action = self.kernel.cancel if cancel else self.kernel.advance
        return action(self.scope, item.instance_id, expected_revision=item.revision,
                      command_id=f"step-{item.instance_id}-{elapsed_s}-{cancel}", now=NOW + timedelta(seconds=elapsed_s))

    def test_full_seven_minute_walk_commits_only_proven_nodes(self):
        self.campus.initialize_position(self.owner)
        walk = self.start()
        self.assertEqual(walk.planned_duration_s, 420)
        self.assertEqual(self.campus.location(self.owner), ("near", 1))
        walk = self.step(walk, 179)
        moving = self.position.read(self.owner)
        self.assertEqual((moving.status, moving.last_confirmed_node, moving.progress_s), ("in_transit", "near", 179))
        self.assertEqual(self.campus.location(self.owner), (None, 2))
        walk = self.step(walk, 180)
        self.assertEqual(self.campus.location(self.owner), ("home", 3))
        walk = self.step(walk, 419)
        self.assertEqual(self.position.read(self.owner).last_confirmed_node, "home")
        final = self.step(walk, 420)
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(final.elapsed_s, 420)
        self.assertEqual(self.campus.location(self.owner), ("far", 5))
        self.assertEqual(self.legacy.location(self.owner), ("near", 1))

    def test_stale_plan_rejects_even_return_to_same_origin(self):
        self.campus.initialize_position(self.owner)
        quote, revision = self.campus.plan_walk(self.owner, "far", departure_at=NOW)
        self.campus.locate(self.owner, "near", expected_revision=revision, evidence_ref="operator-rechecked")
        from good_life.travel.walking import walk_template
        template = self.registry.register_template(walk_template(quote, revision))
        candidate = self.kernel.propose(self.scope, m.TemplateRef(template.id, template.version), opportunity_key="stale",
            reason=template.triggers[0]["id"], now=NOW, expires_at=quote.valid_until)
        item = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="stale-prepare", now=NOW).instance
        with self.assertRaisesRegex(KernelError, "walk position changed"):
            self.kernel.start(self.scope, item.instance_id, expected_revision=item.revision, command_id="stale-start", now=NOW)
        self.assertEqual(self.position.origin(self.owner), ("near", 2))

    def test_conflicting_legacy_sources_are_recorded_once(self):
        city = City()
        city.install(self.storage)
        city.locate(self.owner, "far")
        first = self.campus.initialize_position(self.owner)
        self.assertEqual((first.status, first.revision), ("unknown", 1))
        evidence = self.position.evidence(self.owner, 1)
        self.assertEqual(evidence["reason"], "conflicting_evidence")
        self.assertEqual(evidence["sources"]["campus"]["revision"], 1)
        self.assertEqual(evidence["sources"]["city"]["location"]["place_id"], "far")
        city.locate(self.owner, "near", expected_revision=1)
        self.assertEqual(self.campus.initialize_position(self.owner), first)
        with self.assertRaisesRegex(KernelError, "shared position changed"):
            self.campus.locate(self.owner, "home", expected_revision=0, evidence_ref="confirmation")
        self.campus.locate(self.owner, "home", expected_revision=1, evidence_ref="confirmation")
        self.assertEqual(self.position.origin(self.owner), ("home", 2))
        self.assertEqual(self.position.evidence(self.owner, 2)["ref"], "confirmation")

    def test_missing_city_mapping_needs_operator_confirmation(self):
        city = City()
        city.install(self.storage)
        city.locate("external-owner", "museum")
        first = self.campus.initialize_position("external-owner")
        self.assertEqual(first.status, "unknown")
        self.assertEqual(self.position.evidence("external-owner", 1)["reason"], "missing_mapping")
        city.locate("mapped-owner", "museum")
        mapped = self.campus.initialize_position("mapped-owner", external_places=("museum",))
        self.assertEqual((mapped.status, mapped.place_id), ("at_node", "museum"))
        self.assertEqual(self.campus.location("mapped-owner"), (None, 1))

    def test_replan_starts_from_confirmed_current_stop_and_revision(self):
        self.campus.initialize_position(self.owner)
        final = self.step(self.start(), 180, cancel=True)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        now = NOW + timedelta(seconds=180)
        quote, revision = self.campus.plan_walk(self.owner, "far", departure_at=now)
        self.assertEqual((quote.origin, quote.duration_s, revision), ("home", 240, 2))
        next_walk = self.start(now=now, key="replan")
        self.assertEqual(next_walk.checkpoint["position_revision"], revision)
        self.assertEqual(self.step(next_walk, 420).status, m.ActivityStatus.COMPLETED)

    def test_unknown_stop_blocks_work_until_confirmed(self):
        self.campus.initialize_position(self.owner)
        final = self.step(self.start(), 90, cancel=True)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        unknown = self.position.read(self.owner)
        self.assertEqual((unknown.status, unknown.place_id, unknown.last_confirmed_node), ("unknown", None, "near"))
        self.assertEqual(self.campus.location(self.owner), (None, 2))
        self.assertEqual(City(position=self.position).location(self.owner), (None, 2))
        with self.assertRaisesRegex(KernelError, "no confirmed current stop"):
            self.prepare(now=NOW + timedelta(seconds=90), key="next")
        self.campus.locate(self.owner, "home", expected_revision=unknown.revision, evidence_ref="confirmed-safe-stop")
        self.assertEqual(self.position.origin(self.owner), ("home", 3))

    def test_restart_mid_segment_does_not_count_offline_time(self):
        self.campus.initialize_position(self.owner)
        walk = self.step(self.start(), 200)
        saved = self.position.read(self.owner)
        self.assertEqual((saved.segment_index, saved.progress_s), (1, 20))
        self.reopen()
        now = NOW + timedelta(seconds=1000)
        (recovered,) = self.kernel.recover(self.scope, now=now)
        self.assertEqual((recovered.instance_id, recovered.elapsed_s), (walk.instance_id, 200))
        self.assertEqual(self.position.read(self.owner), saved)
        final = self.step(recovered, 1220)
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.position.origin(self.owner), ("far", saved.revision + 1))

    def test_terminal_proposal_does_not_move_position_before_commit(self):
        self.campus.initialize_position(self.owner)
        walk = self.step(self.start(), 419)
        saved = self.position.read(self.owner)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.step(walk, 420)
        self.assertEqual(self.position.read(self.owner), saved)
        self.reopen(settle_only=True)
        (final,) = self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=421))
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.position.origin(self.owner), ("far", saved.revision + 1))
        self.assertEqual(self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=422)), ())

    def test_confirmed_graph_only_and_ordinary_buildings_are_blocked(self):
        self.campus.initialize_position(self.owner)
        self.assertNotIn("unnamed", self.campus.places)
        with self.assertRaisesRegex(KernelError, "离线路线"):
            self.campus.plan_walk(self.owner, "unnamed", departure_at=NOW)
        with self.assertRaisesRegex(KernelError, "campus place is unavailable"):
            self.campus.locate(self.owner, "unnamed", expected_revision=1, evidence_ref="operator")
        with self.assertRaisesRegex(KernelError, "confirmed campus graph"):
            register_shared_travel(ComponentRegistry(), settings={}, graph_store=self.store, enabled=True)

    def test_flag_off_does_not_read_graph_or_register_schema(self):
        with TemporaryDirectory(prefix=".travel-off-", dir=ROOT) as directory:
            storage = LifeStorage.open(directory)
            self.addCleanup(storage.close)
            reader = StorageReader(storage)
            before = reader.all("SELECT name,sql FROM sqlite_master ORDER BY name", ())
            registry = ComponentRegistry()
            self.assertIsNone(register_shared_travel(registry, settings={}, graph_store=None))
            self.assertFalse(has_position_history(reader))
            self.assertEqual(registry.modules, {})
            self.assertEqual(registry.executors, {})
            self.assertEqual(reader.all("SELECT name,sql FROM sqlite_master ORDER BY name", ()), before)
            storage.close()


class SharedCityRecoveryTests(TestCase):
    setUp = legacy.CityTests.setUp
    confirm = legacy.CityTests.confirm
    adapter = legacy.CityTests.adapter
    template = legacy.CityTests.template
    prepared = legacy.CityTests.prepared
    running = legacy.CityTests.running
    advance = legacy.CityTests.advance

    def test_v1_trip_finishes_before_migration_and_shared_trip_uses_one_position(self):
        old = self.advance(self.running(), 140)
        self.kernel.close()
        registry = ComponentRegistry()
        from good_life.activities.buy_drink import register_buy_drink
        self.wallet, _ = register_buy_drink(registry, configured_currency="CNY", reserve_minor=1,
            payments=({"at_s": 10, "amount_minor": 1},))
        travel = register_shared_travel(registry, settings=SETTINGS, graph_store=self.store, enabled=True)
        self.city = register_city(registry, enabled=True, position=travel.position)
        self.access = CityAccess(self.host, self.wallet, self.city)
        self.registry = registry
        self.kernel = ActivityKernel(self.storage, registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())
        self.assertEqual(travel.campus.initialize_position(self.scope.owner_key).revision, 0)
        (resumed,) = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=200))
        self.assertEqual((resumed.instance_id, resumed.elapsed_s), (old.instance_id, 140))
        self.assertEqual(self.advance(resumed, 800).status, m.ActivityStatus.COMPLETED)
        self.assertEqual(travel.campus.initialize_position(self.scope.owner_key).place_id, "home")
        legacy_position = self.city._legacy_location(self.scope.owner_key)
        now = NOW + timedelta(seconds=900)
        new = self.running(self.template(now=now), now=now, opportunity="shared")
        at_gateway = self.advance(new, 1020)
        self.assertEqual(travel.campus.location(self.scope.owner_key)[0], "far")
        self.assertEqual(self.advance(at_gateway, 1030).checkpoint["location"]["status"], "in_transit")
        self.assertEqual(travel.campus.location(self.scope.owner_key)[0], None)
        self.assertEqual(self.city._legacy_location(self.scope.owner_key), legacy_position)


class SharedClassRecoveryTests(TestCase):
    setUp = legacy_classes.ClassLifecycle.setUp
    write = legacy_classes.ClassLifecycle.write
    seed = legacy_classes.ClassLifecycle.seed
    start = legacy_classes.ClassLifecycle.start
    advance = legacy_classes.ClassLifecycle.advance
    participation = legacy_classes.ClassLifecycle.participation

    def test_v1_class_checkpoint_settles_original_arrival_before_migration(self):
        legacy_campus = self.campus
        old = self.advance(self.start(), 400)
        self.kernel.close()
        store = GraphPackageStore(self.storage, enabled=True)
        package = graph()
        aliases = {"home": "dorm", "near": "classroom", "far": "gate", "unnamed": "other-room"}
        for node in package["nodes"]:
            node["place_id"] = aliases[node["place_id"]]
        for edge in package["edges"]:
            edge["from"], edge["to"] = aliases[edge["from"]], aliases[edge["to"]]
        for gateway in package["gateways"]:
            gateway["place_id"] = aliases[gateway["place_id"]]
        package = seal_package(package)
        store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)

        registry = ComponentRegistry()
        travel = register_shared_travel(registry, settings=SETTINGS, graph_store=store, enabled=True)
        self.campus = travel.campus
        register_academics(registry, self.academics)
        access = AcademicsAccess(self.host, self.academics, registry, self.campus)
        self.kernel = ActivityKernel(self.storage, registry, config=KernelConfig(enabled=True), access=access, clock=SimulationClock())
        self.assertEqual(self.campus.initialize_position(self.owner).revision, 0)
        self.assertIsNone(self.campus.project_location(self.owner)[0])
        (recovered,) = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=500))
        self.assertEqual((recovered.instance_id, recovered.elapsed_s), (old.instance_id, old.elapsed_s))
        arrived = self.advance(recovered, 700)
        self.assertEqual(arrived.checkpoint["schema_version"], 1)
        self.assertTrue(arrived.checkpoint["arrived"])
        self.assertEqual(legacy_campus.location(self.owner), ("classroom", 2))
        completed = self.advance(arrived, 3600)
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.campus.initialize_position(self.owner).place_id, "classroom")
        self.assertEqual(travel.position.evidence(self.owner, 1)["sources"]["campus"]["revision"], 2)


class HostedTravelTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(prefix=".hosted-travel-", dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.storage = LifeStorage.open(self.root)
        self.addCleanup(lambda: self.storage.close())
        self.access = Access()
        self.scope = self.access.scope
        self.owner = self.scope.owner_key
        self.now = NOW

    async def bind(self, settings, *, kernel_enabled=True, academics=None):
        runtime = LifeRuntime(self.storage, object(), kernel_enabled=kernel_enabled, wall_clock=lambda: self.now,
                              monotonic_clock=lambda: (self.now - NOW).total_seconds())
        self.addAsyncCleanup(runtime.close)
        register_shared_travel_group(runtime, settings)
        if academics is not None:
            def bind_academics(context):
                register_academics(context.registry, academics)
                return OptionalGroupBinding(capabilities=("academics",))
            runtime.register_optional_group("test-academics", bind_academics, phase="pre_kernel")
        delivered = asyncio.Event()
        runtime.register_optional_group("test-observer", lambda ctx: OptionalGroupBinding(settle=lambda **_: delivered.set()),
                                        has_history=lambda _: True)
        host = Host()
        host.life_kernel_access = lambda **_: self.access
        await runtime.bind(host)
        await asyncio.wait_for(delivered.wait(), timeout=2)
        return runtime

    async def start_class(self):
        store = GraphPackageStore(self.storage, enabled=True)
        package = graph()
        store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
        legacy = Campus(("near",), ())
        legacy.install(self.storage)
        legacy.locate(self.owner, "near")
        academics = Academics(config=AcademicsConfig(enabled=True))
        academics.install(self.storage)
        context = self.scope.context_key
        for revision, (kind, record_id, record) in enumerate((
            ("term", "fall", term()), ("timetable", "ethics-mon", entry(place_id="far")),
        )):
            academics.command(self.owner, context, {"command_id": "class-" + kind, "expected_revision": revision,
                "action": "create", "kind": kind, "id": record_id, "record": record}, now=NOW)
        session = academics.next_sessions(self.owner, context, now=NOW)[0]
        self.access.resources[eligibility_key(session)] = m.StateValue(1, self.now, 1, (), m.StateQuality.VALID)
        self.academics = academics
        self.settings = {**SETTINGS, "campus_enabled": True}
        runtime = await self.bind(self.settings, academics=academics)
        travel = runtime._optional.groups["shared_travel"].value
        kernel = runtime.kernel
        candidate = propose_class(kernel, academics, self.scope, session["session_id"], now=self.now, campus=travel.campus)
        prepared = kernel.prepare_candidate(self.scope, candidate.candidate_id, now=self.now, command_id="prepare-class").instance
        instance = kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision,
                                command_id="start-class", now=self.now)
        return runtime, travel, instance

    async def test_hosted_class_arrival_pins_the_shared_position_revision(self):
        runtime, travel, instance = await self.start_class()
        location = runtime.kernel._snapshot(self.scope, self.now).state.values["geo.place_id"]
        self.assertEqual(location.value, {"place_id": "near", "revision": 1})
        self.assertNotEqual(location.revision, 1)
        self.assertEqual(instance.checkpoint["location_revision"], 1)
        self.now += timedelta(seconds=420)
        arrived = runtime.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                         command_id="arrive-class", now=self.now)
        self.assertTrue(arrived.checkpoint["arrived"])
        self.assertEqual(travel.position.origin(self.owner), ("far", 2))

    async def test_cancelled_class_after_restart_blocks_routing_from_the_departure_node(self):
        runtime, travel, instance = await self.start_class()
        self.now += timedelta(seconds=80)
        instance = runtime.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                          command_id="partial-commute", now=self.now)
        self.assertEqual(instance.checkpoint["travel_elapsed_s"], 80)
        self.assertEqual(travel.position.origin(self.owner), ("near", 1))
        await runtime.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.root)
        self.now += timedelta(minutes=2)
        restarted = await self.bind(self.settings, academics=self.academics)
        travel = restarted._optional.groups["shared_travel"].value
        kernel = restarted.kernel
        current = kernel.repo.instance(self.owner, instance.instance_id)
        cancelled = kernel.cancel(self.scope, current.instance_id, expected_revision=current.revision,
                                  command_id="cancel-commute", now=self.now)
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        position = travel.position.read(self.owner)
        self.assertEqual((position.status, position.place_id, position.last_confirmed_node, position.revision,
                          position.progress_s), ("unknown", None, "near", 2, 80))
        with self.assertRaisesRegex(KernelError, "no confirmed current stop"):
            travel.campus.plan_walk(self.owner, "home", departure_at=self.now)

    async def test_flag_off_after_restart_settles_walk_once_and_blocks_new_walks(self):
        store = GraphPackageStore(self.storage, enabled=True)
        package = graph()
        store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
        legacy = Campus(("near",), ())
        legacy.install(self.storage)
        legacy.locate(self.owner, "near")

        running = await self.bind({**SETTINGS, "campus_enabled": True})
        travel = running._optional.groups["shared_travel"].value
        self.assertEqual(travel.position.origin(self.owner), ("near", 1))
        location = running.kernel._snapshot(self.scope, NOW).state.values
        self.assertEqual(location[STATE_KEY].value["revision"], 1)
        self.assertEqual(location["geo.place_id"].value, {"place_id": "near", "revision": 1})
        candidate = propose_walk(running.kernel, travel.campus, self.scope, "far", now=NOW, opportunity_key="host-walk")
        prepared = running.kernel.prepare_candidate(self.scope, candidate.candidate_id, now=NOW, command_id="prepare").instance
        walk = running.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision, command_id="start", now=NOW)
        self.now = NOW + timedelta(seconds=50)
        walk = running.kernel.advance(self.scope, walk.instance_id, expected_revision=walk.revision, command_id="advance", now=self.now)
        self.assertEqual(travel.position.read(self.owner).progress_s, 50)
        await running.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.root)

        self.now += timedelta(minutes=30)
        stopped = await self.bind({"campus_enabled": False}, kernel_enabled=False)
        travel = stopped._optional.groups["shared_travel"].value
        self.assertEqual(stopped._optional.modes["shared_travel"], "settle_only")
        settled = stopped.kernel.repo.instance(self.owner, walk.instance_id)
        position = travel.position.read(self.owner)
        self.assertEqual((settled.status, settled.elapsed_s), (m.ActivityStatus.CANCELLED, 50))
        self.assertEqual((position.status, position.revision, position.progress_s), ("unknown", 3, 50))
        with self.assertRaisesRegex(KernelError, "capability_disabled") as error:
            propose_walk(stopped.kernel, travel.campus, self.scope, "far", now=self.now, opportunity_key="disabled")
        self.assertEqual(error.exception.code, "missing_dependency")
        await stopped._optional._run_phase(stopped._lease, "settle", self.now)
        self.assertEqual(travel.position.read(self.owner), position)
        self.assertEqual(stopped.kernel.recover_pending(self.scope, now=self.now), ())

    async def test_hosted_kernel_flag_off_skips_group_without_history(self):
        runtime = await self.bind({"campus_enabled": False})
        self.assertNotIn("shared_travel", runtime._optional.groups)
        self.assertFalse(has_position_history(StorageReader(self.storage)))
        self.assertNotIn("campus_walk@1", runtime.kernel.registry.executors)
