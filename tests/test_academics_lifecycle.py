"""A4 exercises the actual A1 transaction, recovery and idempotency machinery."""
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_academics_domain as domain
from test_academics_domain import entry
from test_kernel_support import Access, NOW, SimulationClock
from good_life.academics import AcademicsAccess, Campus, ClassActivities, register_academics
from good_life.activities.class_session import propose_class
from good_life.contracts.models import ActivityStatus, CandidateStatus, TemplateRef, RecordRef
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig
from good_life.kernel.codec import KernelError


class ClassLifecycle(unittest.TestCase):
    write = domain.DomainCase.write
    seed = domain.DomainCase.seed

    def setUp(self):
        domain.DomainCase.setUp(self)
        self.host = Access()
        self.scope = self.host.scope
        self.owner, self.context = self.scope.owner_key, self.scope.context_key
        self.session = self.seed()
        self.campus = Campus(("dorm", "gate", "classroom", "other-room"),
            (("dorm", "gate", 100), ("gate", "classroom", 200), ("classroom", "other-room", 60)))
        self.registry = ComponentRegistry()
        register_academics(self.registry, self.academics, campus=self.campus)
        self.access = AcademicsAccess(self.host, self.academics, self.registry, self.campus)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())
        self.addCleanup(lambda: self.kernel.close())
        self.campus.locate(self.owner, "dorm")
        self.depart = NOW + timedelta(seconds=300)

    def start(self, *, decision="attend", reason="manual_decision", now=None):
        now = now or self.depart
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=now,
            campus=self.campus, decision=decision, reason=reason)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="prepare-" + decision, now=now)
        self.assertTrue(prepared.gate.allowed, prepared.gate.message)
        instance = prepared.instance
        return self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                 command_id="start-" + decision, now=now)

    def advance(self, instance, seconds, command=None):
        return self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id=command or "advance-" + str(seconds), now=NOW + timedelta(seconds=seconds))

    def participation(self):
        return self.academics.participation(self.owner, self.context, self.session["session_id"])

    def reopen(self):
        self.kernel.close()
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())

    def confirmed_campus(self, *, timed=False, classroom_opens=None, classroom_closes='17:00',
                         path_opens='16:00', path_closes='16:02'):
        from good_life.academics import ConfirmedGraphCampus
        from good_life.graph_package_storage import GraphPackageStore
        from good_life.graph_packages import seal_package
        from test_graph_access import graph
        store = GraphPackageStore(self.storage, enabled=True)
        p = graph()
        aliases = {'home': 'dorm', 'near': 'classroom', 'far': 'gate'}
        for node in p['nodes']:
            node['place_id'] = aliases[node['place_id']]
        for edge in p['edges']:
            edge['from'], edge['to'] = aliases[edge['from']], aliases[edge['to']]
        for gateway in p['gateways']:
            gateway['place_id'] = aliases[gateway['place_id']]
        if timed:
            p['edges'][0]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
                'windows': [{'weekdays': [0], 'start': path_opens, 'end': path_closes}]}
        if classroom_opens is not None:
            classroom = next(n for n in p['nodes'] if n['place_id'] == 'classroom')
            classroom['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
                'windows': [{'weekdays': [0], 'start': classroom_opens, 'end': classroom_closes}]}
        p = seal_package(p)
        store.confirm(p, expected_current_digest=None, expected_proposed_digest=p['digest'], confirmed=True)
        if timed or classroom_opens is not None:
            from good_life.graph_access import GraphAccessPolicy
            policy = GraphAccessPolicy()
        else:
            policy = None
        self.campus = ConfirmedGraphCampus(store, 'test-home', access_policy=policy)
        self.fresh_registry()
        return store, p

    def test_calendar_requotes_at_first_movement_and_pins_current_graph(self):
        from good_life.graph_packages import seal_package
        store, first = self.confirmed_campus(classroom_opens='16:10')
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(self.campus.route('dorm', 'classroom', NOW)['travel_s'], 600)
        self.assertEqual(coordinator.tick(now=NOW), {'advanced': 0, 'started': 0, 'failures': ()})
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertEqual(self.kernel.repo.pending_candidates(self.owner), ())
        self.assertIsNone(self.participation())

        other = self.local_candidate(NOW)
        self.assertEqual(coordinator.tick(now=NOW)['started'], 1)
        self.assertEqual(self.kernel.repo.active(self.owner)[0].candidate_id, other.candidate_id)
        coordinator.tick(now=NOW + timedelta(minutes=3))
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))

        # A longer walk in the latest confirmed graph moves departure a minute earlier.
        updated_edges = [{**edge, 'estimated_duration_s': 120} if edge['to'] == 'classroom' else edge
                         for edge in first['edges']]
        latest = seal_package({**first, 'version': 2, 'edges': updated_edges})
        store.confirm(latest, expected_current_digest=first['digest'], expected_proposed_digest=latest['digest'], confirmed=True)
        due = NOW + timedelta(minutes=8)
        self.assertEqual(coordinator.tick(now=due - timedelta(seconds=1))['started'], 0)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        navigation = self.campus._adapter()
        with patch.object(navigation, 'quote', wraps=navigation.quote) as quote:
            result = coordinator.tick(now=due)
        quote.assert_called_once_with('dorm', 'classroom', due, destination_open_at=NOW + timedelta(minutes=10),
                                      depart_immediately=True)
        self.assertEqual(result['started'], 1)
        self.assertEqual(result['failures'], ())
        running, = self.kernel.repo.active(self.owner)
        pinned = running.checkpoint['params']['route']
        self.assertEqual(running.started_at, due)
        self.assertEqual(pinned['departure_at'], '2026-11-16T08:08:00Z')
        self.assertEqual(pinned['graph_digest'], latest['digest'])
        self.assertEqual(pinned['travel_s'], 120)
        self.assertEqual(running.checkpoint['params']['reason'], 'manual_decision')
        self.assertEqual(coordinator.tick(now=due)['started'], 0)
        self.advance(running, 600)
        self.assertEqual(self.participation()['arrival_at'], self.session['start_at'])
        self.assertIsNone(self.participation()['late_reason'])

    def test_calendar_departs_before_path_closes_and_waits_in_foreground(self):
        self.confirmed_campus(timed=True, path_opens='15:00')
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=NOW - timedelta(minutes=5))['started'], 0)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))

        # A minute-tick caller reaches the configured lead window at 15:56.
        for minute in range(-4, 3):
            tick_at = NOW + timedelta(minutes=minute)
            result = coordinator.tick(now=tick_at)
            self.assertEqual(result['failures'], ())
            if result['started']:
                break
        self.assertEqual(tick_at, NOW - timedelta(minutes=4))
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.started_at, tick_at)
        self.assertEqual(running.checkpoint['params']['route']['travel_s'], 60)
        self.assertEqual(running.checkpoint['params']['route']['departure_at'], '2026-11-16T07:56:00Z')
        waiting = self.advance(running, -180)
        self.assertEqual(waiting.phase, 'wait')
        self.assertTrue(self.kernel.repo.foreground_busy(self.owner))
        self.assertEqual(self.campus.location(self.owner)[0], 'classroom')
        self.assertEqual(self.participation()['arrival_at'], '2026-11-16T07:57:00Z')
        self.assertEqual(self.participation()['attended_s'], 0)
        self.advance(waiting, 1200)
        self.assertEqual(self.participation()['attended_s'], 600)
        self.assertIsNone(self.participation()['late_reason'])

    def test_calendar_waits_for_later_feasible_departure_window(self):
        from good_life.graph_packages import seal_package
        store, first = self.confirmed_campus(timed=True)
        edges = list(first['edges'])
        direct = edges[0]
        opening = direct['opening_schedule']
        edges[0] = {**direct, 'opening_schedule': {**opening, 'windows': [*opening['windows'],
            {'weekdays': [0], 'start': '16:03', 'end': '16:10'}]}}
        latest = seal_package({**first, 'version': 2, 'edges': edges})
        store.confirm(latest, expected_current_digest=first['digest'], expected_proposed_digest=latest['digest'], confirmed=True)

        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        for minute in range(4):
            result = coordinator.tick(now=NOW + timedelta(minutes=minute))
            self.assertEqual(result['failures'], ())
            self.assertEqual(result['started'], 0)
            self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertEqual(coordinator.tick(now=NOW + timedelta(minutes=4))['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.checkpoint['params']['route']['departure_at'], '2026-11-16T08:04:00Z')

    def test_calendar_departs_now_instead_of_waiting_for_a_thirty_second_window(self):
        from good_life.graph_packages import seal_package
        store, first = self.confirmed_campus(timed=True, path_opens='15:00')
        edges = list(first['edges'])
        direct = edges[0]
        opening = direct['opening_schedule']
        edges[0] = {**direct, 'estimated_duration_s': 90,
                    'opening_schedule': {**opening, 'windows': [*opening['windows'],
                        {'weekdays': [0], 'start': '16:04', 'end': '16:06'}]}}
        latest = seal_package({**first, 'version': 2, 'edges': edges})
        store.confirm(latest, expected_current_digest=first['digest'], expected_proposed_digest=latest['digest'], confirmed=True)
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=NOW)['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.started_at, NOW)
        self.assertEqual(running.checkpoint['params']['route']['travel_s'], 90)

    def test_fractional_minute_ticks_depart_before_a_just_in_time_cutoff(self):
        self.confirmed_campus(timed=True, path_closes='16:10')
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        for minute in range(4):
            self.assertEqual(coordinator.tick(now=NOW + timedelta(minutes=minute, milliseconds=500))['started'], 0)
        due = NOW + timedelta(minutes=4, milliseconds=500)
        self.assertEqual(coordinator.tick(now=due)['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.started_at, due)
        self.assertEqual(running.checkpoint['params']['route']['travel_s'], 60)

    def test_class_cannot_wait_in_a_classroom_that_closes_at_class_start(self):
        self.confirmed_campus(timed=True, classroom_opens='15:00', classroom_closes='16:10')
        self.assertEqual(self.campus.route('dorm', 'classroom', NOW)['travel_s'], 60)
        with self.assertRaises(KernelError) as denied:
            propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        self.assertEqual(denied.exception.code, 'provider_unavailable')
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=NOW), {'advanced': 0, 'started': 0, 'failures': ('provider_unavailable',)})
        self.assertEqual(self.kernel.repo.pending_candidates(self.owner), ())
        self.assertIsNone(self.participation())

    def test_class_quote_uses_the_reopening_that_continues_through_class_start(self):
        from good_life.graph_packages import seal_package
        store, first = self.confirmed_campus(classroom_opens='15:00', classroom_closes='16:03')
        nodes = list(first['nodes'])
        room = next(node for node in nodes if node['place_id'] == 'classroom')
        opening = room['opening_schedule']
        nodes[nodes.index(room)] = {**room, 'opening_schedule': {**opening, 'windows': [*opening['windows'],
            {'weekdays': [0], 'start': '16:09', 'end': '16:20'}]}}
        latest = seal_package({**first, 'version': 2, 'nodes': nodes})
        store.confirm(latest, expected_current_digest=first['digest'], expected_proposed_digest=latest['digest'], confirmed=True)
        running = self.start(now=NOW)
        self.assertEqual(running.checkpoint['params']['route']['travel_s'], 540)
        self.advance(running, 600)
        self.assertEqual(self.participation()['arrival_at'], '2026-11-16T08:09:00Z')

    def test_calendar_reports_no_feasible_route_after_cutoff(self):
        self.confirmed_campus(timed=True)
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        result = coordinator.tick(now=NOW + timedelta(minutes=3))
        self.assertEqual(result, {'advanced': 0, 'started': 0, 'failures': ('provider_unavailable',)})
        self.assertEqual(self.kernel.repo.pending_candidates(self.owner), ())
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertIsNone(self.participation())

    def test_calendar_counts_waits_after_first_movement_as_travel(self):
        from good_life.graph_packages import seal_package
        store, first = self.confirmed_campus(classroom_opens='16:10')
        direct = next(e for e in first['edges'] if e['to'] == 'classroom')
        to_gate = next(e for e in first['edges'] if e['to'] == 'gate')
        to_gate = {**to_gate, 'opening_schedule': {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '16:00', 'end': '16:02'}]}}
        onward = {**direct, 'edge_id': 'gate-classroom', 'from': 'gate'}
        latest = seal_package({**first, 'version': 2, 'edges': [to_gate, onward]})
        store.confirm(latest, expected_current_digest=first['digest'], expected_proposed_digest=latest['digest'], confirmed=True)

        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        result = coordinator.tick(now=NOW)
        self.assertEqual(result['failures'], ())
        self.assertEqual(result['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        pinned = running.checkpoint['params']['route']
        self.assertEqual(pinned['path'], ('dorm', 'gate', 'classroom'))
        self.assertEqual(pinned['travel_s'], 600)
        self.advance(running, 600)
        self.assertEqual(self.participation()['arrival_at'], self.session['start_at'])

    def test_deferrable_intermediate_wait_does_not_make_departure_due(self):
        from good_life.graph_packages import seal_package
        store, first = self.confirmed_campus(classroom_opens='16:10')
        direct = next(e for e in first['edges'] if e['to'] == 'classroom')
        to_gate = next(e for e in first['edges'] if e['to'] == 'gate')
        to_gate = {**to_gate, 'estimated_duration_s': 60}
        onward = {**direct, 'edge_id': 'gate-classroom', 'from': 'gate'}
        latest = seal_package({**first, 'version': 2, 'edges': [to_gate, onward]})
        store.confirm(latest, expected_current_digest=first['digest'], expected_proposed_digest=latest['digest'], confirmed=True)

        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        for minute in range(3):
            result = coordinator.tick(now=NOW + timedelta(minutes=minute))
            self.assertEqual((result['started'], result['failures']), (0, ()))
        due = NOW + timedelta(minutes=3)
        self.assertEqual(coordinator.tick(now=due)['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.started_at, due)
        self.assertEqual(running.checkpoint['params']['route']['travel_s'], 420)

    def test_classroom_open_only_after_start_allows_late_attendance(self):
        self.confirmed_campus(classroom_opens='16:11')
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=NOW + timedelta(minutes=9))['started'], 0)
        result = coordinator.tick(now=NOW + timedelta(minutes=10))
        self.assertEqual((result['started'], result['failures']), (1, ()))
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.checkpoint['params']['reason'], 'travel_delay')
        self.advance(running, 720)
        participation = self.participation()
        self.assertEqual(participation['arrival_at'], '2026-11-16T08:11:00Z')
        self.assertEqual(participation['late_reason'], 'travel_delay')
        self.assertEqual(participation['attended_s'], 60)

    def test_explicit_class_start_retains_the_origin_wait(self):
        self.confirmed_campus(classroom_opens='16:10')
        running = self.start(now=NOW)
        self.assertEqual(running.started_at, NOW)
        self.assertEqual(running.checkpoint['params']['route']['travel_s'], 600)
        traveling = self.advance(running, 540)
        self.assertEqual(self.campus.location(self.owner)[0], 'dorm')
        self.assertIsNone(self.participation()['arrival_at'])
        self.advance(traveling, 600)
        self.assertEqual(self.campus.location(self.owner)[0], 'classroom')
        self.assertEqual(self.participation()['arrival_at'], self.session['start_at'])

    def test_confirmed_graph_rejects_a_delayed_first_start(self):
        self.confirmed_campus(timed=True)
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='prepare-graph', now=NOW)
        self.assertTrue(prepared.gate.allowed)
        instance = prepared.instance
        final = self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                  command_id='delayed-first-start', now=NOW+timedelta(minutes=3))
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertEqual(final.reason_code.value, 'no_time_window')
        self.assertIsNone(final.started_at)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertEqual(self.campus.location(self.owner)[0], 'dorm')
        self.assertIsNone(self.participation())

    def test_direct_prepare_persists_graph_expiry_and_replays_after_restart(self):
        self.confirmed_campus()
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        self.assertEqual(candidate.expires_at, NOW + timedelta(seconds=1))
        later = NOW + timedelta(seconds=1)
        denied = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='late-prepare-graph', now=later)
        self.assertFalse(denied.gate.allowed)
        self.assertEqual(denied.gate.reason_code.value, 'no_time_window')
        self.assertIsNone(denied.instance)
        self.assertEqual(self.kernel.repo.candidate(self.owner, candidate.candidate_id)[1].status, CandidateStatus.EXPIRED)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertIsNone(self.participation())
        self.assertEqual(self.campus.location(self.owner)[0], 'dorm')
        self.fresh_registry()
        replay = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='late-prepare-graph', now=later)
        self.assertEqual(replay, denied)
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator._rejected(self.scope, self.session), (False, candidate.candidate_id))
        self.assertEqual(coordinator.tick(now=NOW + timedelta(minutes=9))['started'], 1)

    def test_graph_quote_at_exact_closing_cutoff_refuses_a_subsecond_late_start(self):
        self.confirmed_campus(timed=True)
        depart = NOW + timedelta(minutes=1)
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=depart, campus=self.campus)
        self.assertEqual(candidate.expires_at, depart + timedelta(microseconds=1))
        instance = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='prepare-closing-cutoff', now=depart).instance
        final = self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id='start-after-cutoff', now=depart + timedelta(milliseconds=500))
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertEqual(final.reason_code.value, 'no_time_window')
        self.assertIsNone(final.started_at)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertIsNone(self.participation())

    def test_graph_quote_can_prepare_and_start_within_its_departure_second(self):
        self.confirmed_campus(timed=True)
        depart = NOW + timedelta(milliseconds=125)
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=depart, campus=self.campus)
        self.assertEqual(candidate.expires_at, NOW + timedelta(seconds=1))
        later = depart + timedelta(milliseconds=250)
        preparation = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='prepare-same-second', now=later)
        self.assertTrue(preparation.gate.allowed)
        instance = preparation.instance
        running = self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id='start-same-second', now=later)
        self.assertEqual(running.status, ActivityStatus.RUNNING)
        self.assertEqual(running.started_at, later)

    def local_candidate(self, now):
        from good_life.activities.local_order import LocalOrder, LocalTimedUnits, SPEC, TEMPLATE
        local = LocalOrder()
        local.install(self.storage)
        self.registry.register_module(SPEC, local)
        self.registry.register_executor(LocalTimedUnits())
        self.registry.register_template(TEMPLATE)
        return self.kernel.propose(self.scope, TemplateRef(TEMPLATE.id, TEMPLATE.version),
            opportunity_key='other-domain', reason='tidy_requested', now=now, expires_at=now+timedelta(hours=1))

    def test_tick_expires_stale_graph_candidate_and_calendar_reproposes(self):
        self.confirmed_campus()
        stale = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        due = NOW + timedelta(minutes=9)
        result = self.kernel.tick(now=due)
        self.assertEqual(result['failures'], ())
        self.assertEqual(result['started'], 0)
        expired = self.kernel.repo.candidate(self.owner, stale.candidate_id)[1]
        self.assertEqual(expired.status, CandidateStatus.EXPIRED)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertIsNone(self.participation())
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator._rejected(self.scope, self.session), (False, stale.candidate_id))
        result = coordinator.tick(now=due)
        self.assertEqual(result['failures'], ())
        self.assertEqual(result['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        self.assertNotEqual(running.candidate_id, stale.candidate_id)
        self.assertEqual(running.checkpoint['params']['route']['departure_at'], '2026-11-16T08:09:00Z')
        self.assertEqual(running.started_at, due)
        self.assertEqual(coordinator.tick(now=due)['started'], 0)

    def test_late_graph_prepare_allows_other_candidate_for_same_owner(self):
        self.confirmed_campus(timed=True)
        stale = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        other = self.local_candidate(NOW)
        # Pin queue order because durable candidate IDs are random UUIDs.
        with patch.object(self.kernel.repo, 'pending_candidates', return_value=(stale, other)):
            result = self.kernel.tick(now=NOW+timedelta(minutes=3))
        self.assertEqual(result['failures'], ())
        self.assertEqual(result['started'], 1)
        self.assertEqual(self.kernel.repo.candidate(self.owner, stale.candidate_id)[1].status, CandidateStatus.EXPIRED)
        self.assertEqual(self.kernel.repo.candidate(self.owner, other.candidate_id)[1].status, CandidateStatus.CONSUMED)
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.template_id, 'tidy_desk')
        self.assertIsNone(self.participation())

    def test_crash_before_graph_start_fails_recovery_and_releases_owner(self):
        self.confirmed_campus(timed=True)
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='before-crash', now=NOW).instance
        self.assertEqual(prepared.status, ActivityStatus.PREPARING)
        self.assertTrue(self.kernel.repo.foreground_busy(self.owner))
        self.fresh_registry()
        later = NOW + timedelta(minutes=3)
        final, = self.kernel.recover(self.scope, now=later)
        self.assertEqual(final.instance_id, prepared.instance_id)
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertEqual(final.reason_code.value, 'no_time_window')
        self.assertIsNone(final.started_at)
        self.assertEqual(final.checkpoint, prepared.checkpoint)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))
        self.assertIsNone(self.participation())
        self.assertEqual(self.campus.location(self.owner)[0], 'dorm')
        self.assertEqual(self.kernel.recover(self.scope, now=later), ())
        other = self.local_candidate(later)
        result = self.kernel.tick(now=later)
        self.assertEqual(result['failures'], ())
        self.assertEqual(result['started'], 1)
        self.assertEqual(self.kernel.repo.candidate(self.owner, other.candidate_id)[1].status, CandidateStatus.CONSUMED)
        self.assertEqual(self.kernel.repo.active(self.owner)[0].template_id, 'tidy_desk')

    def test_fresh_graph_quote_cannot_mask_prepared_departure_denial(self):
        self.confirmed_campus()
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=NOW, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id='old-quote', now=NOW).instance
        later = NOW + timedelta(minutes=3)
        newer = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'], now=later, campus=self.campus)
        self.assertNotEqual(candidate.template_ref, newer.template_ref)
        final = self.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision,
            command_id='old-start-with-new-quote', now=later)
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertIsNone(self.participation())
        result = self.kernel.tick(now=later)
        self.assertEqual(result['failures'], ())
        self.assertEqual(result['started'], 1)
        running, = self.kernel.repo.active(self.owner)
        self.assertEqual(running.candidate_id, newer.candidate_id)
        self.assertEqual(running.started_at, later)

    def test_confirmed_graph_is_pinned_through_package_update_and_recovery(self):
        from good_life.graph_packages import seal_package
        store, p = self.confirmed_campus()
        instance = self.start(now=NOW)
        pinned = instance.checkpoint['params']['route']
        self.assertEqual((pinned['graph_version'], pinned['travel_s']), (1, 60))
        newer = {**p, 'version': 2, 'edges': [{**e, 'estimated_duration_s': 900} for e in p['edges']]}
        newer = seal_package(newer)
        store.confirm(newer, expected_current_digest=p['digest'], expected_proposed_digest=newer['digest'], confirmed=True)
        self.assertEqual(self.campus.route('dorm', 'classroom', NOW)['travel_s'], 900)
        self.fresh_registry()
        recovered = self.kernel.recover(self.scope, now=NOW+timedelta(seconds=10))[0]
        self.assertEqual(recovered.checkpoint['params']['route'], pinned)
        arrived = self.advance(recovered, 70)
        self.assertEqual(arrived.checkpoint['travel_elapsed_s'], 60)
        self.assertEqual(self.campus.location(self.owner)[0], 'classroom')
        self.assertEqual(arrived.checkpoint['params']['route']['graph_digest'], p['digest'])

    def test_travel_wait_attend_and_replay(self):
        instance = self.start(now=NOW)
        instance = self.advance(instance, 300)
        self.assertEqual(instance.phase, "wait")
        self.assertEqual(self.campus.location(self.owner)[0], "classroom")
        self.assertEqual(self.participation()["attended_s"], 0)
        instance = self.advance(instance, 800, "class-progress")
        self.assertEqual(instance.phase, "attend")
        self.assertEqual(self.participation()["attended_s"], 200)
        final = self.advance(instance, 1200, "class-final")
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["status"], "attended")
        self.assertEqual(self.participation()["attended_s"], 600)
        self.assertIsNone(self.participation()["late_reason"])
        replay = self.advance(instance, 1200, "class-final")
        self.assertEqual(replay, final)
        self.assertEqual(self.campus.location(self.owner)[1], 2)
        self.assertEqual(self.participation()["attended_s"], 600)
        self.assertFalse(final.outcome.partial)

    def test_late_arrival_and_early_departure_reasons(self):
        instance = self.start(now=NOW + timedelta(seconds=500), reason="travel_delay")
        instance = self.advance(instance, 900)
        self.assertEqual(self.participation()["attended_s"], 100)
        self.assertEqual(self.participation()["late_reason"], "travel_delay")
        final = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="leave", now=NOW + timedelta(seconds=1000))
        self.assertEqual(final.status, ActivityStatus.CANCELLED)
        self.assertEqual(self.participation()["attended_s"], 200)
        self.assertEqual(self.participation()["early_departure_reason"], "travel_delay")
        self.assertTrue(final.outcome.partial)

    def test_delayed_tick_stops_travel_at_class_end(self):
        instance = self.start(now=NOW + timedelta(seconds=1100), reason="late_start")
        final = self.advance(instance, 1500)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(final.elapsed_s, 100)
        self.assertEqual(final.checkpoint["travel_elapsed_s"], 100)
        self.assertEqual(self.campus.location(self.owner)[0], "dorm")
        self.assertIsNone(self.participation()["arrival_at"])
        self.assertEqual(self.participation()["status"], "missed")

    def test_skip_and_attend_have_one_decision(self):
        instance = self.start(decision="skip", reason="health")
        final = self.advance(instance, 300)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["status"], "skipped")
        self.assertEqual(self.participation()["attended_s"], 0)
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=self.depart, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="try-attend", now=self.depart)
        self.assertFalse(prepared.gate.allowed)
        self.assertEqual(self.participation()["decision"], "skip")

    def test_missed_class_never_backfills_attendance(self):
        instance = self.start(decision="skip", reason="missed", now=NOW + timedelta(seconds=1300))
        final = self.advance(instance, 1300)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 0)
        self.assertEqual(self.participation()["decision_reason"], "missed")

    def test_crash_recovery_excludes_offline_interval(self):
        instance = self.start()
        instance = self.advance(instance, 700)
        self.assertEqual(self.participation()["attended_s"], 100)
        self.reopen()
        recovered = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=900))[0]
        self.assertEqual(recovered.started_at, instance.started_at)
        self.assertEqual(self.participation()["attended_s"], 100)
        final = self.advance(recovered, 1200)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 400)
        self.assertTrue(final.outcome.partial)
        self.assertEqual(len(self.participation()["intervals"]), 2)

    def test_recovery_before_first_advance_does_not_reclaim(self):
        instance = self.start()
        self.reopen()
        resumed = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=400))[0]
        self.assertEqual(resumed.status, ActivityStatus.RUNNING)
        self.assertEqual(resumed.started_at, instance.started_at)
        self.assertEqual(self.participation()["sequence"], 0)
        self.assertEqual(self.participation()["attended_s"], 0)

    def fresh_registry(self):
        self.kernel.close()
        self.registry = ComponentRegistry()
        register_academics(self.registry, self.academics, campus=self.campus)
        self.access = AcademicsAccess(self.host, self.academics, self.registry, self.campus)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())
        self.assertEqual(self.registry.templates, {})

    def test_fresh_process_registry_recovers_pinned_template(self):
        instance = self.start()
        instance = self.advance(instance, 700)
        self.fresh_registry()
        resumed = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=900))[0]
        self.assertEqual(resumed.status, ActivityStatus.RUNNING)
        self.assertEqual(resumed.started_at, instance.started_at)
        final = self.advance(resumed, 1200)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 400)

    def test_recovery_after_class_end_closes_without_offline_attendance(self):
        instance = self.start()
        instance = self.advance(instance, 700)
        self.fresh_registry()
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        result = coordinator.tick(now=NOW + timedelta(seconds=1300))
        self.assertEqual(result["failures"], ())
        final = self.kernel.repo.instance(self.owner, instance.instance_id)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 100)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))

    def test_expired_preparing_instance_fails_and_releases_foreground(self):
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="expire-prepare", now=NOW).instance
        self.fresh_registry()
        final = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=1300))[0]
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertIsNone(self.participation())
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))

    def test_terminal_proposal_survives_crash_and_replays_once(self):
        instance = self.start()
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("simulated crash after proposal")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.advance(instance, 1200, "final-crash")
        self.assertEqual(self.participation()["attended_s"], 0)
        self.assertEqual(self.campus.location(self.owner)[0], "dorm")
        self.reopen()
        recovered = self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=1300))[0]
        self.assertEqual(recovered.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 600)
        self.assertEqual(self.campus.location(self.owner), ("classroom", 2))
        self.assertEqual(self.advance(instance, 1200, "final-crash"), recovered)
        self.assertEqual(self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=1300)), ())

    def test_abandon_preserves_settled_participation(self):
        instance = self.start()
        instance = self.advance(instance, 700)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(instance, 1200, "abandonable")
        result_id = self.kernel.repo.pending_result(self.owner, instance.instance_id)
        pending = self.kernel.repo.instance(self.owner, instance.instance_id)
        final = self.kernel.abandon_result(self.scope, result_id, expected_revision=pending.revision,
            command_id="abandon", reason="administrative test", now=NOW + timedelta(seconds=1300))
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertEqual(self.participation()["attended_s"], 100)
        self.assertEqual(self.participation()["status"], "abandoned")

    def test_timetable_revision_fences_pending_candidate(self):
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        self.write("timetable", entry(place_id="other-room"), action="update")
        preparation = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="stale-class", now=NOW)
        self.assertFalse(preparation.gate.allowed)
        self.assertIsNone(preparation.instance)
        self.assertIsNone(self.participation())

    def test_prepared_class_fenced_before_start(self):
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="prepared-old", now=NOW).instance
        self.write("timetable", entry(place_id="other-room"), action="update")
        final = self.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision, command_id="start-old", now=NOW)
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertIsNone(self.participation())

    def test_inflight_and_historical_class_pinned(self):
        instance = self.start()
        self.write("timetable", entry(place_id="other-room"), action="update", now=NOW + timedelta(seconds=350))
        current = self.academics.session_context(self.owner, self.context, self.session["session_id"])
        self.assertEqual(current, self.session)
        self.assertEqual(self.advance(instance, 1200).status, ActivityStatus.COMPLETED)

    def test_host_gate_denial_and_authority(self):
        self.host.free_s = 100
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=self.depart, campus=self.campus)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="short-window", now=self.depart).instance
        final = self.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision, command_id="start-short", now=self.depart)
        self.assertEqual(final.status, ActivityStatus.FAILED)
        self.assertEqual(final.reason_code.value, "no_time_window")
        self.assertIsNone(self.participation())
        self.host.allowed = False
        with self.assertRaises(KernelError) as error:
            propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=self.depart, campus=self.campus)
        self.assertEqual(error.exception.code, "unauthorized")

    def test_explicit_and_calendar_honor_legacy_two_argument_campus_routes(self):
        class LegacyCampus(Campus):
            def route(self, origin, destination):
                return super().route(origin, destination)

        self.campus = LegacyCampus(self.campus.places,
            tuple((origin, destination, seconds) for (origin, destination), seconds in self.campus.paths.items()))
        self.fresh_registry()
        with patch.object(self.campus, 'route', wraps=self.campus.route) as route:
            candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'],
                now=self.depart, campus=self.campus)
            self.assertEqual(candidate.status, CandidateStatus.PENDING)
            route.assert_called_once_with('dorm', 'classroom')
            coordinator = ClassActivities(self.kernel, self.academics, self.campus)
            self.assertEqual(coordinator.tick(now=self.depart)['started'], 1)
            self.assertEqual(len(route.call_args_list), 2)
            self.assertTrue(all(call.args == ('dorm', 'classroom') for call in route.call_args_list))
            running, = self.kernel.repo.active(self.owner)
            self.assertEqual(running.checkpoint['params']['route']['travel_s'], 300)

    def test_explicit_class_proposal_accepts_four_argument_timing_adapter(self):
        calls = []

        class FourArgumentCampus(Campus):
            def class_route_timing(self, origin, destination, departure_at, class_start):
                calls.append((origin, destination, departure_at, class_start))
                return self.route_timing(origin, destination, departure_at)

        self.campus = FourArgumentCampus(self.campus.places,
            tuple((origin, destination, seconds) for (origin, destination), seconds in self.campus.paths.items()))
        self.fresh_registry()
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session['session_id'],
            now=self.depart, campus=self.campus)
        self.assertEqual(candidate.status, CandidateStatus.PENDING)
        self.assertEqual(calls, [('dorm', 'classroom', self.depart, NOW + timedelta(minutes=10))])

    def test_calendar_tick_starts_once_and_bad_map_is_clear(self):
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=NOW)["started"], 0)
        result = coordinator.tick(now=self.depart)
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ())
        self.assertEqual(coordinator.tick(now=self.depart)["started"], 0)
        with self.assertRaisesRegex(KernelError, "route"):
            self.campus.route("other-room", "dorm")


if __name__ == "__main__":
    unittest.main()
