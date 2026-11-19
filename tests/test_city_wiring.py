"""W6-5 integration through the hosted kernel, a synthetic graph and fake Amap HTTP."""
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import uuid4

from test_academics_domain import entry, term
from test_amap_navigation import response
from test_kernel_support import Access, NOW, ROOT, m
from test_life_runtime import Host
from test_shared_travel import SETTINGS, graph

from good_life.academics import AcademicsConfig, register_class_group
from good_life.city import amap
from good_life.city.domain import register_city_group
from good_life.economy import Evidence, register_wallet_group
from good_life.graph_package_storage import GraphPackageStore
from good_life.graph_packages import seal_package
from good_life.kernel import KernelError
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage, StorageReader
from good_life.travel import register_shared_travel_group


class HostedCityTrips(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(prefix=".hosted-city-", dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.storage = LifeStorage.open(self.root)
        self.addCleanup(lambda: self.storage.close())
        self.access = Access()
        self.scope = self.access.scope
        self.owner = self.scope.owner_key
        self.now = NOW
        self.requests = []
        self.addCleanup(amap.UsageGuard._next.clear)

        async def http(params, timeout, *, endpoint=amap.ENDPOINT):
            self.requests.append(endpoint)
            if endpoint == amap.POI_ENDPOINT:
                label = params["keywords"]
                longitude = "121.4200" if label == "Origin" else "121.4500"
                return {"status": "1", "infocode": "10000", "pois": [
                    {"id": "test-" + label, "name": label, "citycode": "021", "location": longitude + ",31.2200"}]}
            return response()

        self.enterContext(patch.object(amap, "_http", http))

    def confirm_graph(self, *, restricted=False):
        package = graph()
        if restricted:
            for node in package["nodes"]:
                if node["place_id"] in ("near", "far"):
                    node["access_requirement"] = "student"
            package = seal_package(package)
        GraphPackageStore(self.storage, enabled=True).confirm(package, expected_current_digest=None,
            expected_proposed_digest=package["digest"], confirmed=True)

    async def bind(self, *, wallet=True, city=True, campus=True, navigation=True, poi=True, classes=False):
        self.runtime = LifeRuntime(self.storage, object(), kernel_enabled=True, wall_clock=lambda: self.now,
                                   monotonic_clock=lambda: (self.now - NOW).total_seconds())
        self.addAsyncCleanup(self.runtime.close)
        register_shared_travel_group(self.runtime, {**SETTINGS, "campus_enabled": campus, "home_place_id": "home"})
        register_class_group(self.runtime, enabled=classes, academics_config=AcademicsConfig(enabled=classes))
        register_wallet_group(self.runtime, enabled=wallet)
        register_city_group(self.runtime, enabled=wallet and city, config={
            "amap_navigation_enabled": navigation, "amap_poi_search_enabled": poi,
            "amap_navigation_key": uuid4().hex, "amap_navigation_city_code": "021"})
        host = Host()
        host.life_kernel_access = lambda **_: self.access
        with patch.object(DueSleepScheduler, "start"):
            await self.runtime.bind(host)
        await self.runtime._optional.stop_worker()
        self.check_diagnostics()
        self.kernel = self.runtime.kernel
        groups = self.runtime._optional.groups
        self.city = groups["city"].value if "city" in groups else None
        self.wallet = groups["wallet"].value if "wallet" in groups else None
        self.position = groups["shared_travel"].value.position if "shared_travel" in groups else None

    async def restart(self, **flags):
        await self.runtime.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.root)
        await self.bind(**flags)

    async def tick(self, seconds):
        self.now = NOW + timedelta(seconds=seconds)
        self.kernel.recover_pending(self.scope, now=self.now)
        for phase in ("settle", "commitments"):
            await self.runtime._optional._run_phase(self.runtime._lease, phase, self.now)
        self.assertEqual(self.runtime.advance_kernel(now=self.now)["failures"], ())
        self.check_diagnostics()

    def check_diagnostics(self):
        expected = {name: {"reason": "capability_disabled"} for name, mode in self.runtime._optional.modes.items()
                    if mode == "settle_only"}
        self.assertEqual(self.runtime.optional_diagnostics, expected)

    def seed_wallet(self):
        self.wallet.record_income(self.owner, "initial", m.Money(100, "CNY"), operation_id="initial",
            expected_revision=0, evidence=Evidence("initial", NOW, m.Provenance.SIMULATED))

    def budget(self):
        quote = self.wallet.quote_available(self.owner)
        return quote.balance_minor, quote.reserved_minor, quote.available_minor

    async def search(self):
        return await self.runtime.search_city_pois(self.scope, "Museum", opportunity_key="trip")

    async def begin_external_trip(self):
        self.confirm_graph()
        await self.bind()
        self.seed_wallet()
        navigation = self.city._navigation()
        origin = await navigation.asearch_pois("Origin")
        place = navigation.select_poi(origin.list_token, index=0)
        self.position.confirm(self.owner, place.place_id, expected_revision=0, evidence_ref="confirmed-origin")
        result = await self.search()
        self.assertEqual(result, {"status": "ok", "items": ("Museum",), "reason_code": None})
        prepared = await self.runtime.start_city_trip(self.scope, opportunity_key="trip", index=0)
        self.assertTrue(prepared.gate.allowed)
        self.assertEqual(prepared.instance.status, m.ActivityStatus.RUNNING)
        self.assertEqual(self.requests, [amap.POI_ENDPOINT, amap.POI_ENDPOINT, amap.ENDPOINT, amap.ENDPOINT])
        self.assertEqual(self.budget(), (100, 12, 88))
        return prepared.instance

    async def test_external_trip_cancels_at_next_stop_and_reroutes_from_there(self):
        trip = await self.begin_external_trip()
        outbound = trip.checkpoint["params"]["outbound"]
        self.assertNotIn("gateway_id", outbound["evidence"][0])
        self.now = NOW + timedelta(seconds=10)
        requested = self.kernel.cancel(self.scope, trip.instance_id, expected_revision=trip.revision,
                                       command_id="mid-leg", now=self.now)
        self.assertTrue(requested.checkpoint["cancel_requested"])
        self.assertEqual(self.budget(), (94, 6, 88))
        await self.tick(420)
        self.assertEqual(self.kernel.repo.instance(self.owner, trip.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.position.origin(self.owner)[0], outbound["destination"])
        self.assertEqual(self.budget(), (94, 0, 94))
        reroute = await self.runtime.start_city_trip(self.scope, opportunity_key="reroute", destination=outbound["origin"])
        self.assertEqual(reroute.instance.checkpoint["params"]["outbound"]["origin"], outbound["destination"])

    async def test_restart_recovers_pinned_route_and_settles_each_leg_once(self):
        trip = await self.begin_external_trip()
        await self.tick(10)
        self.assertEqual(self.budget(), (94, 6, 88))
        self.now = NOW + timedelta(seconds=1000)
        await self.restart()
        await self.tick(1000)
        recovered = self.kernel.repo.instance(self.owner, trip.instance_id)
        self.assertEqual(recovered.elapsed_s, 10)
        self.assertEqual(recovered.checkpoint["params"], trip.checkpoint["params"])
        self.now = NOW + timedelta(seconds=1830)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("interrupted after staging")):
            with self.assertRaisesRegex(RuntimeError, "interrupted after staging"):
                self.kernel.advance(self.scope, trip.instance_id, expected_revision=recovered.revision,
                                    command_id="end-trip", now=self.now)
        self.assertIsNotNone(self.kernel.repo.pending_result(self.owner, trip.instance_id))
        await self.restart()
        await self.tick(1830)
        await self.tick(1831)
        self.assertEqual(self.budget(), (88, 0, 88))
        self.assertEqual([tx.amount_minor for tx in self.wallet.transactions(self.owner)], [100, 6, 6])
        self.assertEqual(self.kernel.repo.instance(self.owner, trip.instance_id).status, m.ActivityStatus.COMPLETED)
        self.assertEqual(len(self.requests), 4)

    async def test_disabled_groups_release_unused_budget_at_persisted_stop(self):
        trip = await self.begin_external_trip()
        await self.tick(420)
        await self.restart(wallet=False, city=False, campus=False)
        self.assertEqual(self.runtime._optional.modes["wallet"], "settle_only")
        self.assertEqual(self.runtime._optional.modes["city"], "settle_only")
        await self.tick(700)
        self.assertEqual(self.kernel.repo.instance(self.owner, trip.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.budget(), (94, 0, 94))
        self.assertEqual(self.position.origin(self.owner)[0], trip.checkpoint["params"]["outbound"]["destination"])
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.runtime.read_wallet_page(owner_key=self.owner, context_key=self.scope.context_key), {"enabled": False})

    async def test_disabling_city_mid_leg_settles_fare_and_marks_stop_unknown(self):
        trip = await self.begin_external_trip()
        await self.tick(10)
        self.assertEqual(self.budget(), (94, 6, 88))
        self.assertEqual(self.position.read(self.owner).status, "in_transit")

        await self.restart(city=False)
        await self.tick(100)
        await self.tick(101)
        settled = self.kernel.repo.instance(self.owner, trip.instance_id)
        self.assertEqual((settled.status, settled.elapsed_s), (m.ActivityStatus.CANCELLED, 10))
        stop = self.position.read(self.owner)
        self.assertEqual((stop.status, stop.place_id, stop.last_confirmed_node),
                         ("unknown", None, trip.checkpoint["params"]["outbound"]["origin"]))
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertEqual(self.budget(), (94, 0, 94))
        self.assertEqual(len(self.requests), 4)

    async def test_disabling_only_campus_settles_city_at_confirmed_stop(self):
        trip = await self.begin_external_trip()
        await self.tick(420)

        await self.restart(campus=False)
        self.assertEqual(self.runtime._optional.modes["city"], "settle_only")
        self.assertEqual(self.runtime._optional.modes["wallet"], "active")
        await self.tick(430)
        self.assertEqual(self.kernel.repo.instance(self.owner, trip.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.position.origin(self.owner)[0], trip.checkpoint["params"]["outbound"]["destination"])
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertEqual(self.budget(), (94, 0, 94))
        self.assertEqual(len(self.requests), 4)

    async def test_invalid_and_expired_poi_do_not_quote_or_spend(self):
        self.confirm_graph()
        await self.bind()
        await self.tick(0)
        self.seed_wallet()
        await self.search()
        with self.assertRaises(amap.AmapError) as error:
            await self.runtime.start_city_trip(self.scope, opportunity_key="trip", index=4)
        self.assertEqual(error.exception.reason_code, "amap_poi_not_in_list")
        self.now += timedelta(minutes=20)
        with self.assertRaises(amap.AmapError) as error:
            await self.runtime.start_city_trip(self.scope, opportunity_key="trip", index=0)
        self.assertEqual(error.exception.reason_code, "amap_poi_list_unavailable")
        self.assertEqual(self.requests, [amap.POI_ENDPOINT])
        self.assertEqual(self.budget(), (100, 0, 100))
        self.assertEqual(self.position.origin(self.owner), ("home", 1))

    async def test_access_gate_rejects_restricted_gateway_even_when_it_is_the_origin(self):
        self.confirm_graph(restricted=True)
        await self.bind()
        self.seed_wallet()
        self.position.confirm(self.owner, "near", expected_revision=0, evidence_ref="reported-stop")
        await self.search()
        with self.assertRaises(amap.AmapError) as error:
            await self.runtime.start_city_trip(self.scope, opportunity_key="trip", index=0)
        self.assertEqual(error.exception.reason_code, "amap_no_gateway")
        self.assertEqual(self.requests, [amap.POI_ENDPOINT])
        self.assertEqual(self.budget(), (100, 0, 100))
        self.assertEqual(self.position.origin(self.owner), ("near", 1))

    def schedule_class(self, scope, start_time):
        for revision, (kind, record_id, record) in enumerate((
                ("term", "fall", term()),
                ("timetable", "ethics-mon", entry(place_id="far", start_time=start_time)))):
            self.runtime.academics.command(self.owner, scope.context_key, {"command_id": f"class-{revision}",
                "expected_revision": revision, "action": "create", "kind": kind, "id": record_id,
                "record": record}, now=self.now)

    async def test_class_preempts_trip_and_reserves_the_next_free_window(self):
        self.confirm_graph()
        await self.bind(navigation=False, classes=True)
        await self.tick(0)
        trip = (await self.runtime.start_city_trip(self.scope, opportunity_key="trip", destination="far", stay_s=600)).instance
        await self.tick(1)
        self.schedule_class(self.scope, "16:08")
        await self.tick(2)
        self.assertTrue(self.kernel.repo.instance(self.owner, trip.instance_id).checkpoint["cancel_requested"])
        await self.tick(240)
        self.assertEqual(self.kernel.repo.instance(self.owner, trip.instance_id).status, m.ActivityStatus.CANCELLED)
        denied = await self.runtime.start_city_trip(self.scope, opportunity_key="too-late", destination="home")
        self.assertEqual(denied.gate.reason_code, m.ReasonCode.NO_TIME_WINDOW)
        self.assertIsNone(denied.instance)
        self.assertEqual(self.position.origin(self.owner)[0], "far")
        await self.tick(480)
        lesson, = self.kernel.repo.active(self.owner)
        self.assertEqual(lesson.status, m.ActivityStatus.RUNNING)
        self.assertTrue(lesson.template_id.startswith("class_session_"))
        self.assertEqual(self.requests, [])

    async def test_class_reservation_allows_cancelled_trip_to_reach_stop_after_restart(self):
        self.confirm_graph()
        await self.bind(navigation=False, classes=True)
        await self.tick(0)
        trip = (await self.runtime.start_city_trip(self.scope, opportunity_key="trip", destination="far", stay_s=600)).instance
        await self.tick(10)
        class_scope = replace(self.scope, context_key=m.ContextKey("class-context"))
        self.access.inventory = (self.scope, class_scope)
        self.schedule_class(class_scope, "16:04")

        self.now = NOW + timedelta(seconds=200)
        await self.restart(navigation=False, classes=True)
        await self.tick(200)
        cancelling = self.kernel.repo.instance(self.owner, trip.instance_id)
        self.assertTrue(cancelling.checkpoint["cancel_requested"])
        self.assertEqual(cancelling.elapsed_s, 10)
        await self.tick(430)
        self.assertEqual(self.kernel.repo.instance(self.owner, trip.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.position.origin(self.owner)[0], "far")
        await self.tick(431)
        lesson, = self.kernel.repo.active(self.owner)
        self.assertTrue(lesson.template_id.startswith("class_session_"))
        self.assertEqual((lesson.status, lesson.context_key), (m.ActivityStatus.RUNNING, class_scope.context_key))
        self.assertGreater(lesson.started_at, NOW + timedelta(minutes=4))

    async def test_wallet_off_keeps_city_and_provider_off(self):
        await self.bind(wallet=False, campus=False)
        self.assertIsNone(self.city)
        self.assertIsNone(self.wallet)
        self.assertTrue(self.runtime.ready)
        with self.assertRaisesRegex(KernelError, "city trips are disabled"):
            await self.search()
        reader = StorageReader(self.storage)
        self.assertIsNone(reader.one("SELECT 1 FROM sqlite_master WHERE name GLOB 'life_wallet_*' OR name GLOB 'life_city_*'", ()))
        self.assertEqual(self.requests, [])
