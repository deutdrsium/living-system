"""A8 offline quotes, safe-stop travel, shared A2 settlement and crash recovery."""
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from test_kernel_support import Access, NOW, ROOT, SimulationClock, m
from good_life.activities.buy_drink import register_buy_drink
from good_life.activities.city_trip import city_trip_template
from good_life.city import CityAccess, OfflineNavigation, TransitService, register_city
from good_life.activities.city_trip import CityActivities
from good_life.economy import Evidence
from good_life.graph_packages import seal_package, ATTRIBUTION
from good_life.graph_package_storage import GraphPackageStore
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig, KernelError
from good_life.storage import LifeStorage


def home_graph(version=1, *, closed=False):
    common = {"source": "manual", "osm_ids": [], "version": version}
    nodes = [{**common, "place_id": place, "name": place, "type": "path", "region": None,
              "coordinate_system": "WGS84", "lat": 31.0, "lon": 121.0,
              "capabilities": [], "opening_rules": None, "access": None} for place in ("home", "near", "far")]
    edges = [{**common, "edge_id": "to-" + place, "from": "home", "to": place, "direction": "both",
              "length_m": seconds, "estimated_duration_s": seconds, "estimated": True,
              "estimate": {"model": "test-walk"}, "enabled": not (closed and place == "near"),
              "name": "", "load": None, "access": None, "availability": None}
             for place, seconds in (("near", 60), ("far", 120))]
    return seal_package({"schema": "home-area.graph/v1", "graph_id": "test-home", "version": version,
        "source": "osm", "coordinate_system": "WGS84", "attribution": ATTRIBUTION, "license": "ODbL-1.0",
        "bounds": [30.9, 120.9, 31.1, 121.1], "nodes": nodes, "edges": edges,
        "gateways": [{**common, "gateway_id": "gateway-" + place, "place_id": place,
                      "coordinate_system": "WGS84", "lat": 31.0, "lon": 121.0, "modes": ["bus", "metro"]}
                     for place in ("near", "far")], "vector_data": [], "edit_history": [],
        "build_report": {"counts": {"nodes": 3, "edges": 2, "components": 1}, "dropped": {},
                         "isolated_nodes": [], "unnamed_places": [], "missing_access": [],
                         "missing_opening_information": [], "components": [], "isolated_components": []}})


class CityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix=".city-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.storage = LifeStorage.open(self.temporary.name)
        self.addCleanup(lambda: self.storage.close())
        self.store = GraphPackageStore(self.storage, enabled=True)
        self.confirm(home_graph())
        self.host = Access()
        self.scope = self.host.scope
        self.registry = ComponentRegistry()
        self.wallet, _ = register_buy_drink(self.registry, configured_currency="CNY", reserve_minor=1,
                                          payments=({"at_s": 10, "amount_minor": 1},))
        self.city = register_city(self.registry, enabled=True)
        self.access = CityAccess(self.host, self.wallet, self.city)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True),
                                     access=self.access, clock=SimulationClock())
        self.addCleanup(lambda: self.kernel.close())
        self.city.locate(self.scope.owner_key, "home")
        self.wallet.record_income(self.scope.owner_key, "initial", m.Money(100, "CNY"), operation_id="initial",
            expected_revision=0, evidence=Evidence("initial", NOW, m.Provenance.SIMULATED))

    def confirm(self, package):
        old = self.store.latest("test-home")
        self.store.confirm(package, expected_current_digest=old["digest"] if old else None,
                           expected_proposed_digest=package["digest"], confirmed=True)

    def adapter(self, **outbound):
        # Complete journey via far is faster despite near being the closest exit.
        return OfflineNavigation(self.store, "test-home", currency="CNY", external_places=("museum", "safe-stop"), services=(
            TransitService("slow-out", "near", "museum", "bus", 600, 8, 60),
            TransitService("fast-out", "far", "museum", "metro", 180, 6, 20, **outbound),
            TransitService("fast-back", "museum", "far", "metro", 180, 6, 20),
            TransitService("slow-back", "museum", "near", "bus", 600, 8, 60)))

    def template(self, adapter=None, *, now=NOW, reserve_minor=16):
        adapter = adapter or self.adapter()
        out = adapter.quote("home", "museum", now)
        back = adapter.quote("museum", "home", now + timedelta(seconds=out.duration_s + 60))
        template = city_trip_template(out, back, stay_s=60, reserve_minor=reserve_minor)
        self.registry.register_template(template)
        return template

    def prepared(self, template=None, *, now=NOW, opportunity="trip"):
        template = template or self.template()
        candidate = self.kernel.propose(self.scope, m.TemplateRef(template.id, template.version),
            opportunity_key=opportunity, reason="trip_requested", now=now, expires_at=now + timedelta(hours=1))
        return self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="prepare-" + opportunity, now=now)

    def running(self, template=None, *, now=NOW, opportunity="trip"):
        prepared = self.prepared(template, now=now, opportunity=opportunity)
        self.assertTrue(prepared.gate.allowed, prepared.gate.message)
        instance = prepared.instance
        return self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                 command_id="start-" + opportunity, now=now)

    def advance(self, instance, seconds, command=None):
        return self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id=command or "advance-" + str(seconds), now=NOW + timedelta(seconds=seconds))

    def quote(self):
        q = self.wallet.quote_available(self.scope.owner_key)
        return q.balance_minor, q.reserved_minor, q.available_minor

    def reopen(self, *, enabled=True):
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temporary.name)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=enabled),
                                     access=self.access, clock=SimulationClock())
        if not enabled:
            # Disabled settlement uses handlers on its supplied transaction.
            self.wallet.storage = self.storage
            self.city.storage = self.storage

    def test_missed_departure_retires_preparing_instance_on_recovery(self):
        prepared = self.prepared()
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temporary.name)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True),
                                     access=self.access, clock=SimulationClock())
        activities = CityActivities(self.kernel)
        result = activities.recover(self.scope, now=NOW + timedelta(seconds=1))
        self.assertEqual(result[0].status, m.ActivityStatus.CANCELLED)
        self.assertEqual(result[0].outcome.reason_code, m.ReasonCode.CANCELLED)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, prepared.instance.instance_id).status,
                         m.ActivityStatus.CANCELLED)

    def test_missed_departure_cancel_replays_pending_terminal_proposal(self):
        instance = self.prepared().instance
        before_location = self.city.location(self.scope.owner_key)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("cancel proposal persisted")):
            with self.assertRaisesRegex(RuntimeError, "cancel proposal persisted"):
                CityActivities(self.kernel).recover(self.scope, now=NOW + timedelta(seconds=1))
        self.assertIsNotNone(self.kernel.repo.pending_result(self.scope.owner_key, instance.instance_id))
        self.assertTrue(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.reopen()
        activities = CityActivities(self.kernel)
        recovered = activities.recover(self.scope, now=NOW + timedelta(seconds=2))
        self.assertEqual(recovered[0].status, m.ActivityStatus.CANCELLED)
        self.assertIsNone(self.kernel.repo.pending_result(self.scope.owner_key, instance.instance_id))
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertEqual(activities.recover(self.scope, now=NOW + timedelta(seconds=3)), ())
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(self.city.location(self.scope.owner_key), before_location)
        self.assertEqual([t.kind for t in self.wallet.transactions(self.scope.owner_key)], ["income"])

    def test_retirement_authority_failure_does_not_block_another_owner(self):
        blocked = self.prepared().instance
        revoked_scope = self.scope
        self.scope = replace(self.scope, owner_key=m.OwnerKey("other-owner"), context_key=m.ContextKey("other-context"))
        self.host.inventory = (revoked_scope, self.scope)
        self.city.locate(self.scope.owner_key, "home")
        self.wallet.record_income(self.scope.owner_key, "initial", m.Money(100, "CNY"), operation_id="initial",
            expected_revision=0, evidence=Evidence("initial", NOW, m.Provenance.SIMULATED))
        running = self.running(self.registry.template(m.TemplateRef(blocked.template_id, blocked.template_version)),
                               opportunity="other-owner")
        with patch.object(self.host, "authorize", side_effect=lambda scope, action: scope == self.scope):
            result = CityActivities(self.kernel).tick(now=NOW + timedelta(seconds=1))
        self.assertIn("unauthorized", result["failures"])
        self.assertEqual(result["advanced"], 1)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, running.instance_id).elapsed_s, 1)
        self.assertEqual(self.kernel.repo.instance(revoked_scope.owner_key, blocked.instance_id).status,
                         m.ActivityStatus.PREPARING)

    def test_retirement_unexpected_failure_is_isolated_and_redacted(self):
        running = self.running()
        activities = CityActivities(self.kernel)
        with patch.object(activities, "_retire_missed_preparing", side_effect=RuntimeError("private failure detail")):
            result = activities.tick(now=NOW + timedelta(seconds=1))
        self.assertIn("unexpected_error:RuntimeError", result["failures"])
        self.assertNotIn("private failure detail", str(result))
        self.assertEqual(result["advanced"], 1)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, running.instance_id).elapsed_s, 1)

    def test_cancel_requested_recovery_gates_only_to_next_safe_stop(self):
        instance = self.advance(self.running(), 140)
        pending = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="cancel-recovery", now=NOW + timedelta(seconds=150))
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temporary.name)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True),
                                     access=self.access, clock=SimulationClock())
        self.host.free_s = 160
        recovered = CityActivities(self.kernel).recover(self.scope, now=NOW + timedelta(seconds=151))
        self.assertEqual(recovered[0].status, m.ActivityStatus.RUNNING)
        final = self.kernel.advance(self.scope, recovered[0].instance_id,
            expected_revision=recovered[0].revision, command_id="cancel-recovery-finish", now=NOW + timedelta(seconds=301))
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(final.checkpoint["location"]["place_id"], "museum")
        self.assertEqual(self.quote(), (94, 0, 94))

    def test_home_quote_uses_confirmed_graph_and_closed_edges(self):
        adapter = self.adapter()
        quote = adapter.quote("near", "far", NOW)
        self.assertEqual(quote.duration_s, 180)
        self.assertEqual([s.destination for s in quote.segments], ["home", "far"])
        self.assertEqual(quote.cost_minor, 0)
        self.assertEqual(quote.attribution, ATTRIBUTION)
        self.assertTrue(all(s.uncertainty_s > 0 and s.source.startswith("home-area:") for s in quote.segments))
        self.confirm(home_graph(2, closed=True))
        with self.assertRaises(KernelError):
            self.adapter().quote("near", "far", NOW)
        # Existing quotes/adapters retain archived v1; new proposals use v2.
        self.assertEqual(adapter.quote("near", "far", NOW).graph_version, 1)
        self.assertEqual(self.store.history("test-home")[-1]["version"], 2)

    def test_gateway_comparison_timetable_and_no_network(self):
        adapter = self.adapter(departures=(NOW + timedelta(seconds=200),))
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            quote = adapter.quote("home", "museum", NOW)
        self.assertEqual([s.mode for s in quote.segments], ["walk", "wait", "metro"])
        self.assertEqual([s.duration_s for s in quote.segments], [120, 80, 180])
        self.assertEqual(quote.cost_minor, 6)
        with self.assertRaises(KernelError):
            OfflineNavigation(self.store, "test-home", currency="CNY", external_places=("museum",),
                services=(TransitService("bad", "home", "museum", "bus", 60, 1, 0),))
        invalid = home_graph(2)
        invalid["gateways"][0]["coordinate_system"] = "unknown"
        with self.assertRaisesRegex(ValueError, "coordinate"):
            self.confirm(seal_package(invalid))

    def test_full_round_trip_shared_budget_stage_charge_and_refund(self):
        started = self.running(self.template(self.adapter(delay_s=30, refund_minor=2)))
        self.assertEqual(self.quote(), (100, 16, 84))
        instance = self.advance(started, 119)
        self.assertEqual(instance.checkpoint["location"]["status"], "in_transit")
        instance = self.advance(instance, 121)
        self.assertEqual(self.quote(), (94, 10, 84))
        self.assertTrue(any(e["reason_code"] == "provider_unavailable" and "30" in e["message"]
                            for e in instance.checkpoint["explanations"]))
        instance = self.advance(instance, 330)
        self.assertEqual(instance.checkpoint["location"]["place_id"], "museum")
        self.assertEqual(self.quote(), (96, 10, 86))
        instance = self.advance(instance, 391)
        self.assertEqual(self.quote(), (90, 4, 86))
        final = self.advance(instance, 690, "finish")
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(final.checkpoint["location"]["place_id"], "home")
        self.assertEqual(self.quote(), (90, 0, 90))
        self.assertEqual(self.advance(instance, 690, "finish"), final)
        txs = self.wallet.transactions(self.scope.owner_key)
        self.assertEqual([(t.kind, t.amount_minor) for t in txs], [("income", 100), ("spend", 6), ("refund", 2), ("spend", 6)])
        self.assertEqual(txs[2].related_transaction_id, txs[1].transaction_id)
        self.assertEqual(len({t.instance_id for t in txs[1:]}), 1)

    def test_cancel_request_waits_for_safe_endpoint_and_keeps_paid_fare(self):
        instance = self.advance(self.running(), 140)
        cancelled = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="cancel-moving", now=NOW + timedelta(seconds=150))
        self.assertEqual(cancelled.status, m.ActivityStatus.RUNNING)
        self.assertEqual(cancelled.checkpoint["location"]["status"], "in_transit")
        self.assertTrue(cancelled.checkpoint["cancel_requested"])
        final = self.advance(cancelled, 300)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(final.checkpoint["location"]["place_id"], "museum")
        self.assertEqual(self.quote(), (94, 0, 94))
        self.assertEqual(final.outcome.reason_code, m.ReasonCode.CANCELLED)

    def test_cancel_without_prior_tick_preserves_elapsed_travel(self):
        instance = self.running()
        pending = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="cancel-first-tick", now=NOW + timedelta(seconds=150))
        self.assertEqual(pending.status, m.ActivityStatus.RUNNING)
        self.assertEqual(pending.elapsed_s, 150)
        self.assertEqual(pending.checkpoint["location"]["status"], "in_transit")
        self.assertEqual(self.quote(), (94, 10, 84))
        final = self.advance(pending, 300)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(final.checkpoint["location"]["place_id"], "museum")
        self.assertEqual(self.quote(), (94, 0, 94))

    def test_cancel_while_waiting_ends_immediately_at_known_node(self):
        instance = self.running(self.template(self.adapter(departures=(NOW + timedelta(seconds=200),))))
        final = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="cancel-wait", now=NOW + timedelta(seconds=160))
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(final.elapsed_s, 160)
        self.assertEqual(final.checkpoint["location"]["place_id"], "far")
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_stub_cancel_at_designated_stop_refunds_only_paid_segment(self):
        instance = self.running(self.template(self.adapter(cancel_after_s=80, safe_stop="safe-stop", refund_minor=6)))
        final = self.advance(instance, 400)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(final.elapsed_s, 200)
        self.assertEqual(final.checkpoint["location"]["place_id"], "safe-stop")
        self.assertIn("safe-stop", final.checkpoint["explanations"][-1]["message"])
        self.assertEqual(final.checkpoint["explanations"][-1]["reason_code"], "cancelled")
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual([t.kind for t in self.wallet.transactions(self.scope.owner_key)], ["income", "spend", "refund"])

    def test_crash_at_terminal_proposal_and_effect_rollback_then_replay(self):
        instance = self.advance(self.running(), 121)
        self.assertEqual(self.quote(), (94, 10, 84))
        original = self.city.apply_effect
        def crash_after_money(db, inst, effect):
            original(db, inst, effect)
            raise RuntimeError("crash after money before transaction commit")
        with patch.object(self.city, "apply_effect", side_effect=crash_after_money):
            with self.assertRaisesRegex(RuntimeError, "crash after money"):
                self.advance(instance, 660, "finish-crash")
        self.assertEqual(self.quote(), (94, 10, 84))
        self.reopen()
        self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=700))
        final = self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (88, 0, 88))
        self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=701))
        self.assertEqual(self.quote(), (88, 0, 88))
        self.assertEqual([t.amount_minor for t in self.wallet.transactions(self.scope.owner_key)], [100, 6, 6])

    def test_refund_crash_rolls_back_and_recovers_without_duplicate_credit(self):
        instance = self.running(self.template(self.adapter(cancel_after_s=80, safe_stop="safe-stop", refund_minor=6)))
        original = self.wallet._mutate
        def crash_on_refund(db, owner, quote, operation_id, evidence, kind, payload, business_key):
            result = original(db, owner, quote, operation_id, evidence, kind, payload, business_key)
            if kind == "refund":
                raise RuntimeError("refund interrupted")
            return result
        with patch.object(self.wallet, "_mutate", side_effect=crash_on_refund):
            with self.assertRaisesRegex(RuntimeError, "refund interrupted"):
                self.advance(instance, 400)
        self.assertEqual(self.quote(), (100, 16, 84))
        self.reopen()
        self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=500))
        self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=501))
        self.assertEqual(self.quote(), (100, 0, 100))
        transactions = self.wallet.transactions(self.scope.owner_key)
        self.assertEqual([t.kind for t in transactions], ["income", "spend", "refund"])
        for event in self.storage.pending_events("kernel.audit.v1"):
            envelope = self.kernel.repo.envelope(event["event_id"])
            self.assertEqual(envelope.provenance, m.Provenance.SIMULATED)
            self.assertEqual(envelope.audience_bound.context_keys, (self.scope.context_key,))

    def test_running_restart_does_not_count_offline_time_or_double_charge(self):
        instance = self.advance(self.running(), 121)
        self.reopen()
        recovered = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=1000))
        current = self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)
        self.assertEqual(current.elapsed_s, 121)
        self.assertEqual(self.quote(), (94, 10, 84))
        final = self.advance(current, 1539)
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (88, 0, 88))

    def test_insufficient_funds_and_delay_time_are_gate_rejections(self):
        result = self.prepared(self.template(reserve_minor=101))
        self.assertFalse(result.gate.allowed)
        self.assertEqual(result.gate.reason_code, m.ReasonCode.INSUFFICIENT_RESOURCE)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.host.free_s = 700
        result = self.prepared(self.template(self.adapter(delay_s=200)), opportunity="late")
        self.assertFalse(result.gate.allowed)
        self.assertEqual(result.gate.reason_code, m.ReasonCode.NO_TIME_WINDOW)

    def test_disabled_registration_and_kernel_leave_schema_unchanged(self):
        registry = ComponentRegistry()
        with self.storage._lock:
            before = tuple(self.storage._connection.iterdump())
        self.assertIsNone(register_city(registry))
        disabled = ActivityKernel(self.storage, registry)
        disabled.close()
        with self.storage._lock:
            self.assertEqual(tuple(self.storage._connection.iterdump()), before)
        self.assertEqual(registry.modules, {})
        self.assertEqual(registry.executors, {})

    def test_cancellation_before_start_has_no_charge_or_location_write(self):
        instance = self.prepared().instance
        before = self.city.location(self.scope.owner_key)
        final = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="cancel-prepared", now=NOW)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.city.location(self.scope.owner_key), before)
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_second_trip_starts_at_completed_position(self):
        final = self.advance(self.running(), 660)
        template = self.template(now=NOW + timedelta(seconds=700))
        second = self.running(template, now=NOW + timedelta(seconds=700), opportunity="second")
        self.assertEqual(second.status, m.ActivityStatus.RUNNING)
        self.reopen()
        self.kernel.recover(self.scope, now=NOW + timedelta(seconds=710))
        self.assertEqual(self.quote(), (88, 16, 72))

    def test_seven_minute_home_walk_and_free_round_trip(self):
        graph = home_graph(2)
        graph["edges"][0]["estimated_duration_s"] = 420
        self.confirm(seal_package(graph))
        adapter = self.adapter()
        out = adapter.quote("home", "near", NOW)
        back = adapter.quote("near", "home", NOW + timedelta(seconds=420))
        template = city_trip_template(out, back)
        self.registry.register_template(template)
        instance = self.advance(self.running(template), 419)
        self.assertEqual(instance.checkpoint["location"]["status"], "in_transit")
        self.assertEqual(instance.checkpoint["location"]["place_id"], "home")
        self.assertEqual(instance.checkpoint["location"]["route_id"], out.route_id)
        instance = self.advance(instance, 420)
        self.assertEqual(instance.checkpoint["location"]["place_id"], "near")
        final = self.advance(instance, 840)
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(len(self.wallet.transactions(self.scope.owner_key)), 1)

    def test_free_route_does_not_require_funded_wallet(self):
        self.scope = replace(self.scope, owner_key=m.OwnerKey("empty-owner"))
        self.host.scope = self.scope
        self.host.inventory = (self.scope,)
        self.city.locate(self.scope.owner_key, "home")
        adapter = self.adapter()
        outbound = adapter.quote("home", "near", NOW)
        inbound = adapter.quote("near", "home", NOW + timedelta(seconds=60))
        template = city_trip_template(outbound, inbound)
        self.registry.register_template(template)
        final = self.advance(self.running(template), 120)
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (0, 0, 0))
        self.assertEqual(self.wallet.transactions(self.scope.owner_key), ())
        self.assertEqual(self.wallet.quote_available(self.scope.owner_key).revision, 0)

    def test_start_is_atomic_and_rejects_changed_origin_or_departure(self):
        prepared = self.prepared().instance
        original = self.wallet.apply_effect
        def fail_reserve(db, instance, effect):
            original(db, instance, effect)
            raise RuntimeError("reserve transaction interrupted")
        with patch.object(self.wallet, "apply_effect", side_effect=fail_reserve):
            with self.assertRaisesRegex(RuntimeError, "reserve transaction"):
                self.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision,
                                  command_id="interrupted-start", now=NOW)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, prepared.instance_id), prepared)
        with self.assertRaises(KernelError):
            self.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision,
                              command_id="missed-departure", now=NOW + timedelta(seconds=1))
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_disabled_kernel_can_finish_persisted_terminal_proposal(self):
        instance = self.advance(self.running(), 121)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("proposal persisted")):
            with self.assertRaisesRegex(RuntimeError, "proposal persisted"):
                self.advance(instance, 660)
        self.reopen(enabled=False)
        self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=700))
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id).status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (88, 0, 88))

    def test_provider_failure_has_no_financial_or_location_effects(self):
        before = self.city.location(self.scope.owner_key)
        adapter = OfflineNavigation(self.store, "test-home", currency="CNY", external_places=("museum",))
        with self.assertRaises(KernelError) as error:
            adapter.quote("home", "museum", NOW)
        self.assertEqual(error.exception.error.reason_code, m.ReasonCode.PROVIDER_UNAVAILABLE)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(self.city.location(self.scope.owner_key), before)


if __name__ == "__main__":
    unittest.main()
