"""Synthetic access, opening and shared A4/A8 routing regressions only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo
import unittest

from test_kernel_support import NOW, ROOT
from test_city_trip import home_graph
from good_life.academics import Campus, ConfirmedGraphCampus
from good_life.city.navigation import OfflineNavigation, RouteQuote, Segment, TransitService
from good_life.graph_access import (GraphAccessPolicy, access_requirement, opening_schedule)
from good_life.graph_package_storage import GraphPackageStore
from good_life.graph_packages import seal_package, validate_package
from good_life.kernel.codec import KernelError, digest, freeze, wire
from good_life.storage import LifeStorage


MONDAY = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)  # Monday 09:00 Shanghai.
ALWAYS = {'status': 'known', 'timezone': 'Asia/Shanghai',
          'windows': [{'weekdays': list(range(7)), 'start': '00:00', 'end': '24:00'}]}


def graph(version=1):
    p = home_graph(version)
    for r in (*p['nodes'], *p['edges']):
        r['access_requirement'] = 'public'
        r['opening_schedule'] = ALWAYS
    return seal_package(p)


class AccessTests(unittest.TestCase):
    def record(self, access='public', **fields):
        return {'place_id': 'synthetic', 'access': access, 'opening_rules': '24/7', **fields}

    def allowed(self, record, policy=None, at=MONDAY, ends=None):
        return (policy or GraphAccessPolicy()).evaluate(record, 'test', at, ends, node=True)

    def test_osm_and_exact_text_mapping(self):
        for raw, expected in [('yes', 'public'), ('permissive', 'public'), ('no', 'closed'),
                              ('private', 'private'), ('destination', 'unparsed'),
                              ('仅校内有资格人员可用', 'campus_member'), ('please ask reception', 'unparsed'),
                              (None, 'unknown')]:
            with self.subTest(raw=raw):
                self.assertEqual(access_requirement({'access': raw}), expected)
        self.assertEqual(opening_schedule({'opening_rules': None}, node=True), {'status': 'unknown'})
        self.assertEqual(opening_schedule({'opening_rules': 'Mo-Fr 08:00-18:00'}, node=True), {'status': 'unparsed'})

    def test_credentials_and_scoped_private_mapping(self):
        policy = GraphAccessPolicy(credentials={'student', 'resident_of:dorm'},
                                  private_credentials={'test': 'staff', 'test/dorm': 'resident_of:dorm'})
        for requirement, expected in [('public', True), ('campus_member', True), ('student', True),
                                      ('staff', False), ('resident_of:dorm', True),
                                      ('resident_of:other', False), ('closed', False)]:
            with self.subTest(requirement=requirement):
                self.assertEqual(self.allowed(self.record(access_requirement=requirement), policy).allowed, expected)
        self.assertTrue(self.allowed(self.record('private', access_area='dorm'), policy).allowed)
        self.assertFalse(self.allowed(self.record('private', access_area='other'), policy).allowed)
        self.assertTrue(self.allowed(self.record('private', region='dorm'), policy).allowed)
        self.assertFalse(self.allowed(self.record('private', region='dorm', access_area='other'), policy).allowed)
        self.assertFalse(self.allowed(self.record('private'), GraphAccessPolicy(credentials={'student'},
            treat_unparsed_access_as='allowed', treat_unknown_opening_as='allowed')).allowed)

    def test_package_validates_optional_edge_region(self):
        p = home_graph()
        edge = p['edges'][0]
        edge['access'] = 'private'
        for fields in ({}, {'region': None}, {'region': ''}, {'region': 'dorm'}):
            with self.subTest(fields=fields):
                edge.update(fields)
                sealed = seal_package(p)
                self.assertEqual(validate_package(sealed), sealed)
        for region in (7, 0, False, [], {}):
            with self.subTest(region=region), self.assertRaisesRegex(ValueError, 'invalid edge region'):
                edge['region'] = region
                validate_package(seal_package(p))

    def test_malformed_region_or_access_area_fails_closed(self):
        policy = GraphAccessPolicy(credentials={'student'},
            private_credentials={'test': 'student', 'test/dorm': 'student'},
            treat_unparsed_access_as='allowed', treat_unknown_opening_as='allowed')
        edge = {'edge_id': 'synthetic', 'access': 'private', 'availability': None, 'enabled': True}
        self.assertTrue(policy.evaluate(edge, 'test', MONDAY).allowed)
        for fields in ({'region': 7}, {'region': 0}, {'region': 7, 'access_area': 'dorm'},
                       {'access_area': 7}, {'region': 'dorm', 'access_area': None},
                       {'access_area': ''}, {'access_area': '  '},
                       {'access_requirement': 'unparsed', 'region': 7}):
            with self.subTest(fields=fields):
                decision = policy.evaluate({**edge, **fields}, 'test', MONDAY)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.assumptions, ())

    def test_malformed_access_fields_are_rejected_by_package_and_policy(self):
        policy = GraphAccessPolicy(credentials={'student'}, private_credentials={'test': 'student'},
            treat_unparsed_access_as='allowed', treat_unknown_opening_as='allowed')
        always_utc = {**ALWAYS, 'timezone': 'UTC'}
        for node in (True, False):
            opening_field = 'opening_rules' if node else 'availability'
            for field, invalid in [
                ('access_requirement', []), ('access_requirement', 'private'), ('access_requirement', 'vip'),
                ('access_conditionals', []), ('access_conditionals', {}),
                ('access_conditionals', {'foot': 'no'}), ('access_conditionals', {'foot:conditional': ['no']}),
                ('opening_schedule', None), ('opening_schedule', {'status': 'unknown', 'windows': []}),
                ('opening_schedule', {'status': 'unparsed', 'timezone': 'UTC'}),
                ('opening_schedule', {**always_utc, 'timezone': []}),
                ('opening_schedule', {**always_utc, 'windows': [{'weekdays': [True], 'start': '00:00', 'end': '24:00'}]}),
                ('access', ['public']), (opening_field, ['24/7']),
                *([] if node else [('enabled', 1), ('enabled', 'true'), ('enabled', None)]),
            ]:
                with self.subTest(node=node, field=field, invalid=invalid):
                    package = home_graph()
                    record = package['nodes' if node else 'edges'][0]
                    record.update(access_requirement='public', opening_schedule=always_utc)
                    record[field] = invalid
                    with self.assertRaises(ValueError):
                        validate_package(seal_package(package))
                    for candidate in (record, freeze(record)):
                        decision = policy.evaluate(candidate, 'test', MONDAY, node=node)
                        self.assertFalse(decision.allowed)
                        self.assertEqual(decision.assumptions, ())

    def test_policy_requires_the_same_access_fields_as_package_validation(self):
        policy = GraphAccessPolicy(treat_unparsed_access_as='allowed', treat_unknown_opening_as='allowed')
        for collection, fields in (('edges', ('enabled', 'access', 'availability')),
                                   ('nodes', ('access', 'opening_rules'))):
            for field in fields:
                with self.subTest(collection=collection, field=field):
                    package = home_graph()
                    record = package[collection][0]
                    node = collection == 'nodes'
                    self.assertTrue(policy.evaluate(record, 'test', MONDAY, node=node).allowed)
                    del record[field]
                    with self.assertRaises(ValueError):
                        validate_package(seal_package(package))
                    for candidate in (record, freeze(record)):
                        decision = policy.evaluate(candidate, 'test', MONDAY, node=node)
                        self.assertFalse(decision.allowed)
                        self.assertEqual(decision.assumptions, ())

    def test_policy_requires_boolean_node_enabled_without_changing_package_schema(self):
        policy = GraphAccessPolicy(treat_unparsed_access_as='allowed', treat_unknown_opening_as='allowed')
        package = home_graph()
        node = package['nodes'][0]
        self.assertTrue(policy.evaluate(node, 'test', MONDAY, node=True).allowed)
        for enabled in (1, 'true', None):
            with self.subTest(enabled=enabled):
                node['enabled'] = enabled
                sealed = seal_package(package)
                self.assertEqual(validate_package(sealed), sealed)
                decision = policy.evaluate(freeze(node), 'test', MONDAY, node=True)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.assumptions, ())

    def test_frozen_conditionals_and_schedule_match_json_shapes(self):
        record = self.record(opening_schedule=ALWAYS, access_conditionals={'foot:conditional': 'no @ (Mo-Fr)'})
        policy = GraphAccessPolicy(treat_unparsed_access_as='allowed')
        expected = self.allowed(record, policy)
        self.assertTrue(expected.allowed)
        self.assertTrue(expected.needs_review)
        self.assertEqual(self.allowed(freeze(record), policy), expected)
        self.assertFalse(self.allowed(freeze(record)).allowed)

    def test_schedule_zone_is_resolved_once_per_evaluation_and_mutations_are_checked(self):
        schedule = {'status': 'known', 'timezone': 'Asia/Shanghai',
                    'windows': [{'weekdays': [0], 'start': '09:00', 'end': '10:00'}]}
        record = self.record(opening_schedule=schedule)
        policy = GraphAccessPolicy(treat_unknown_opening_as='allowed')
        with patch('good_life.graph_access.ZoneInfo', wraps=ZoneInfo) as zone:
            self.assertTrue(self.allowed(record, policy).allowed)
            zone.assert_called_once_with('Asia/Shanghai')
        schedule['windows'][0]['weekdays'].append(True)
        self.assertFalse(self.allowed(record, policy).allowed)
        schedule['windows'][0]['weekdays'].pop()
        with patch('good_life.graph_access.available_timezones') as enumerate_zones:
            schedule['timezone'] = 'Bad/Zone'
            self.assertFalse(self.allowed(record, policy).allowed)
            enumerate_zones.assert_not_called()
        schedule['timezone'] = 'Asia/Shanghai'
        self.assertTrue(self.allowed(record, policy).allowed)

    def test_unparsed_and_unknown_never_silently_open(self):
        record = self.record('rules need review', opening_rules=None)
        self.assertFalse(self.allowed(record).allowed)
        policy = GraphAccessPolicy(treat_unparsed_access_as='allowed', treat_unknown_opening_as='allowed')
        decision = self.allowed(record, policy)
        self.assertTrue(decision.allowed)
        self.assertEqual(len(decision.assumptions), 2)
        self.assertIn('unparsed', decision.assumptions[0])
        self.assertIn('unknown', decision.assumptions[1])
        for r in (self.record('no'), self.record(access_requirement='staff'), self.record(enabled=False)):
            self.assertFalse(self.allowed(r, policy).allowed)

    def test_timezone_weekdays_boundaries_and_whole_edge_interval(self):
        record = self.record(opening_schedule={'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:00', 'end': '10:00'}]})
        self.assertTrue(self.allowed(record).allowed)
        self.assertFalse(self.allowed(record, at=MONDAY-timedelta(seconds=1)).allowed)
        self.assertFalse(self.allowed(record, at=MONDAY+timedelta(hours=1)).allowed)
        self.assertFalse(self.allowed(record, at=MONDAY+timedelta(days=1)).allowed)
        self.assertTrue(self.allowed(record, ends=MONDAY+timedelta(hours=1)).allowed)
        self.assertFalse(self.allowed(record, ends=MONDAY+timedelta(hours=1, seconds=1)).allowed)

    def test_overnight_and_adjacent_windows(self):
        record = self.record(opening_schedule={'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '22:00', 'end': '02:00'},
                        {'weekdays': [1], 'start': '02:00', 'end': '03:00'}]})
        at = MONDAY + timedelta(hours=14)
        self.assertTrue(self.allowed(record, at=at, ends=at+timedelta(hours=4)).allowed)
        self.assertFalse(self.allowed(record, at=at, ends=at+timedelta(hours=4, seconds=1)).allowed)
        self.assertFalse(self.allowed(self.record(opening_schedule={**ALWAYS, 'windows': []})).allowed)

    def test_dst_nonexistent_and_ambiguous_boundaries_are_unknown(self):
        policy = GraphAccessPolicy(treat_unknown_opening_as='allowed')
        for day, start, at in [(datetime(2026, 3, 8, 7, tzinfo=timezone.utc), '02:30', '03:30'),
                               (datetime(2026, 11, 1, 6, tzinfo=timezone.utc), '01:30', '02:30')]:
            with self.subTest(day=day):
                record = self.record(opening_schedule={'status': 'known', 'timezone': 'America/New_York',
                    'windows': [{'weekdays': [6], 'start': start, 'end': at}]})
                self.assertFalse(self.allowed(record, at=day).allowed)
                result = self.allowed(record, policy, at=day)
                self.assertTrue(result.allowed)
                self.assertIn('unknown', result.assumptions[0])

    def test_unparsed_opening_uses_opening_treatment_only(self):
        record = self.record(opening_rules='ask the building manager')
        self.assertFalse(self.allowed(record, GraphAccessPolicy(treat_unparsed_access_as='allowed')).allowed)
        decision = self.allowed(record, GraphAccessPolicy(treat_unknown_opening_as='allowed'))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.assumptions,
            ('synthetic: opening unparsed; assumed allowed by treat_unknown_opening_as',))

    def test_unrelated_dst_window_does_not_override_known_open_or_closed_times(self):
        for sunday, start, end in [(datetime(2026, 3, 8, tzinfo=timezone.utc), '02:30', '03:30'),
                                  (datetime(2026, 11, 1, tzinfo=timezone.utc), '01:30', '02:30')]:
            with self.subTest(day=sunday):
                record = self.record(opening_schedule={'status': 'known', 'timezone': 'America/New_York',
                    'windows': [{'weekdays': [6], 'start': start, 'end': end},
                                {'weekdays': [6, 0], 'start': '09:00', 'end': '10:00'}]})
                morning = sunday.replace(hour=9, tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
                for at in (morning, morning + timedelta(days=1)):
                    decision = self.allowed(record, at=at)
                    self.assertTrue(decision.allowed)
                    self.assertFalse(decision.assumptions)
                # The early, certainly closed part of Sunday is not an unknown
                # opening just because a later boundary needs DST review.
                decision = self.allowed(record, GraphAccessPolicy(treat_unknown_opening_as='allowed'),
                                        at=sunday + timedelta(hours=5))
                self.assertFalse(decision.allowed)

    def test_all_day_opening_works_without_zoneinfo_data(self):
        from zoneinfo import ZoneInfoNotFoundError
        with patch('good_life.graph_access.ZoneInfo', side_effect=ZoneInfoNotFoundError('no tzdata')) as zone:
            for text in ('24/7', 'always'):
                decision = self.allowed(self.record(opening_rules=text), ends=MONDAY+timedelta(days=8))
                self.assertTrue(decision.allowed)
                self.assertFalse(decision.assumptions)
            self.assertTrue(self.allowed(self.record(opening_schedule={**ALWAYS, 'timezone': 'UTC'})).allowed)
            p = graph()
            for record in (*p['nodes'], *p['edges']):
                record['opening_schedule'] = {**ALWAYS, 'timezone': 'UTC'}
            validate_package(seal_package(p))
            zone.assert_not_called()

    def test_regional_timezone_reports_missing_data_separately_from_unknown_key(self):
        from zoneinfo import ZoneInfoNotFoundError
        p = graph()
        with patch('good_life.graph_access.ZoneInfo', side_effect=ZoneInfoNotFoundError('missing')):
            with patch('good_life.graph_access.available_timezones', return_value=set()):
                with self.assertRaisesRegex(ValueError, 'install the optional tzdata'):
                    validate_package(p)
            with patch('good_life.graph_access.available_timezones', return_value={'Asia/Tokyo'}):
                with self.assertRaisesRegex(ValueError, 'unknown IANA timezone key'):
                    validate_package(p)

    def test_settings_are_default_off_and_owned(self):
        self.assertIsNone(GraphAccessPolicy.from_settings({}))
        policy = GraphAccessPolicy.from_settings({'graph_access_enabled': True,
            'graph_access_credentials': ['student'], 'graph_private_credentials': '{"test":"student"}'})
        self.assertEqual(policy.credentials, frozenset({'student'}))
        self.assertTrue(self.allowed(self.record('private'), policy).allowed)
        with self.assertRaises(TypeError):
            policy.private_credentials['test'] = 'staff'
        for params in ({'credentials': ['public']}, {'private_credentials': {'test': 'public'}},
                       {'treat_unparsed_access_as': 'guess'}, {'credentials': ['student', 'bad']}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                GraphAccessPolicy(**params)

    def test_requires_aware_evaluation(self):
        with self.assertRaises(ValueError):
            self.allowed(self.record(), at=MONDAY.replace(tzinfo=None))


class PackageRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='.graph-access-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)
        self.store = GraphPackageStore(self.storage, enabled=True)

    def confirm(self, package):
        old = self.store.latest(package['graph_id'])
        self.store.confirm(package, expected_current_digest=old['digest'] if old else None,
                           expected_proposed_digest=package['digest'], confirmed=True)

    def test_shared_quotes_pin_graph_and_record_assumptions(self):
        p = graph()
        p['nodes'][1].pop('access_requirement')
        p['nodes'][1]['access'] = 'unspecified narrative condition'
        p = seal_package(p)
        self.confirm(p)
        policy = GraphAccessPolicy(treat_unparsed_access_as='allowed')
        city = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=policy)
        campus = ConfirmedGraphCampus(self.store, 'test-home', access_policy=policy)
        q, route = city.quote('home', 'near', MONDAY), campus.route('home', 'near', MONDAY)
        self.assertEqual(q.duration_s, route['travel_s'])
        self.assertTrue(q.needs_review)
        self.assertEqual(q.assumptions, route['assumptions'])
        newer = graph(2)
        newer['edges'][0]['estimated_duration_s'] = 600
        self.confirm(seal_package(newer))
        self.assertEqual(city.quote('home', 'near', MONDAY).duration_s, 60)
        self.assertEqual(route['graph_version'], 1)
        self.assertEqual(campus.route('home', 'near', MONDAY)['travel_s'], 600)
        self.assertEqual(self.store.version('test-home', 1, digest=p['digest']), p)
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.store.version('test-home', 1, digest='bad')
        self.assertIsNone(self.store.version('test-home', 99))

    def test_absent_confirmed_package_and_no_config_keep_manual_routes(self):
        campus = Campus.from_settings(self.store, {'academics_graph_id': 'test-home'},
                                     places=('home', 'near'), paths=(('home', 'near', 17),))
        self.assertEqual(campus.route('home', 'near', MONDAY)['travel_s'], 17)
        manual = Campus.from_settings(self.store, {}, places=('home', 'near'), paths=(('home', 'near', 17),))
        self.confirm(graph())
        self.assertEqual(campus.route('home', 'near', MONDAY)['travel_s'], 60)
        self.assertEqual(manual.route('home', 'near')['travel_s'], 17)
        self.assertEqual(campus.route('home', 'home', MONDAY)['travel_s'], 0)
        with self.assertRaises(KernelError):
            campus.route('home', 'near')

    def test_structured_default_blocking_and_legacy_unchanged(self):
        package = home_graph()
        self.confirm(package)
        package_digest = package['digest']
        segments = (Segment('home', 'near', 'walk', 60, 0, 60, f'home-area:{package_digest}:to-near'),)
        expected = RouteQuote(digest(('home', 'near', MONDAY, package_digest, segments)),
            'home', 'near', MONDAY, MONDAY + timedelta(minutes=5), segments, 'CNY',
            'test-home', 1, package_digest, package['attribution'])
        legacy = OfflineNavigation(self.store, 'test-home', currency='CNY')
        quote = legacy.quote('home', 'near', MONDAY)
        self.assertEqual(quote, expected)
        self.assertNotIn('needs_review', wire(quote))
        configured = OfflineNavigation(self.store, 'test-home', currency='CNY', settings={'graph_access_enabled': True})
        with self.assertRaises(KernelError):
            configured.quote('home', 'near', MONDAY)

    def test_uses_arrival_time_and_edge_window_in_both_directions(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:01', 'end': '10:00'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        self.assertEqual(nav.quote('home', 'near', MONDAY).duration_s, 60)
        with self.assertRaises(KernelError):
            nav.quote('near', 'home', MONDAY)
        self.assertEqual(nav.quote('near', 'home', MONDAY+timedelta(minutes=1)).duration_s, 60)

    def test_wait_at_open_node_replaces_later_detour_to_reach_window(self):
        p = graph()
        p['nodes'].append({**p['nodes'][2], 'place_id': 'destination', 'name': 'destination'})
        for e in p['edges']:
            e['direction'] = 'forward'
        base = p['edges'][0]
        p['edges'].extend([{**base, 'edge_id': 'far-near', 'from': 'far', 'to': 'near'},
                          {**base, 'edge_id': 'near-destination', 'from': 'near', 'to': 'destination',
                           'opening_schedule': {'status': 'known', 'timezone': 'Asia/Shanghai',
                            'windows': [{'weekdays': [0], 'start': '09:03', 'end': '10:00'}]}}])
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        q = nav.quote('home', 'destination', MONDAY)
        self.assertEqual(q.duration_s, 240)
        self.assertEqual(tuple(s.destination for s in q.segments), ('near', 'near', 'destination'))
        self.assertEqual(tuple(s.mode for s in q.segments), ('walk', 'wait', 'walk'))
        self.assertEqual(q.segments[1].duration_s, 120)
        self.assertFalse(q.needs_review)
        route = ConfirmedGraphCampus(self.store, 'test-home', access_policy=GraphAccessPolicy()).route(
            'home', 'destination', MONDAY)
        self.assertEqual(route['path'], ('home', 'near', 'destination'))
        self.assertEqual(route['travel_s'], q.duration_s)

    def test_wait_upstream_to_reach_a_later_open_interval(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:00', 'end': '09:02'},
                        {'weekdays': [0], 'start': '09:05', 'end': '10:00'}]}
        p['edges'][1].update({'from': 'near', 'estimated_duration_s': 60,
            'opening_schedule': {'status': 'known', 'timezone': 'Asia/Shanghai',
                'windows': [{'weekdays': [0], 'start': '09:06', 'end': '10:00'}]}})
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        q = nav.quote('home', 'far', MONDAY)
        self.assertEqual(q.duration_s, 420)
        self.assertEqual([(s.origin, s.destination, s.mode, s.duration_s) for s in q.segments], [
            ('home', 'home', 'wait', 240), ('home', 'near', 'walk', 60),
            ('near', 'near', 'wait', 60), ('near', 'far', 'walk', 60)])

    def test_legacy_closed_gateway_is_filtered_before_timetable_alignment(self):
        p = home_graph()
        p['nodes'][1]['opening_rules'] = 'closed pending review'
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', external_places=('city',), services=(
            TransitService('closed', 'city', 'near', 'bus', 60, 0, 0,
                           (MONDAY + timedelta(microseconds=1),)),
            TransitService('open', 'city', 'far', 'bus', 600, 0, 0)))
        q = nav.quote('city', 'far', MONDAY)
        self.assertEqual(q.duration_s, 600)
        self.assertEqual(q.segments[0].source, 'manual-transit:open')

    def test_scheduled_boarding_at_exact_gateway_closing_is_denied(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:00', 'end': '09:02'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy(),
            external_places=('city',), services=(TransitService('bus', 'near', 'city', 'bus', 60, 0, 0,
                                                               (MONDAY + timedelta(minutes=2),)),))
        with self.assertRaises(KernelError):
            nav.quote('home', 'city', MONDAY)

    def test_transit_tries_later_departure_if_arrival_gateway_is_closed(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:05', 'end': '09:10'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy(),
            external_places=('city',), services=(TransitService('bus', 'city', 'near', 'bus', 60, 0, 0,
                (MONDAY + timedelta(minutes=1), MONDAY + timedelta(minutes=4))),))
        q = nav.quote('city', 'home', MONDAY)
        self.assertEqual(q.duration_s, 360)
        self.assertEqual([(s.mode, s.duration_s) for s in q.segments], [('wait', 240), ('bus', 60), ('walk', 60)])

    def test_policy_skips_fractional_timetable_and_uses_next_aligned_service(self):
        self.confirm(graph())
        service = TransitService('bus', 'city', 'near', 'bus', 60, 0, 0,
            (MONDAY+timedelta(seconds=1.5), MONDAY+timedelta(seconds=30)))
        options = dict(currency='CNY', external_places=('city',), services=(service,))
        legacy = OfflineNavigation(self.store, 'test-home', **options)
        with self.assertRaisesRegex(KernelError, 'whole-second alignment'):
            legacy.quote('city', 'near', MONDAY)
        nav = OfflineNavigation(self.store, 'test-home', access_policy=GraphAccessPolicy(), **options)
        q = nav.quote('city', 'near', MONDAY)
        self.assertEqual([(s.mode, s.duration_s) for s in q.segments], [('wait', 30), ('bus', 60)])

    def test_unpublished_timetable_waits_for_arrival_window(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:05', 'end': '09:10'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', external_places=('city',),
            access_policy=GraphAccessPolicy(), services=(TransitService('flex', 'city', 'near', 'bus', 60, 0, 10),))
        q = nav.quote('city', 'near', MONDAY)
        self.assertEqual([(s.mode, s.duration_s, s.uncertainty_s) for s in q.segments],
                         [('wait', 240, 10), ('bus', 60, 10)])

    def test_transit_refuses_inaccessible_abort_safe_stop(self):
        p = graph()
        p['nodes'][2]['access_requirement'] = 'staff'
        self.confirm(seal_package(p))
        for departures in ((), (MONDAY,)):
            with self.subTest(departures=departures):
                service = TransitService('bus', 'city', 'near', 'bus', 60, 0, 0,
                                         departures, cancel_after_s=30, safe_stop='far')
                options = dict(currency='CNY', external_places=('city',), services=(service,))
                nav = OfflineNavigation(self.store, 'test-home', access_policy=GraphAccessPolicy(), **options)
                with self.assertRaises(KernelError):
                    nav.quote('city', 'near', MONDAY)
                staff = OfflineNavigation(self.store, 'test-home', access_policy=GraphAccessPolicy(credentials={'staff'}), **options)
                self.assertEqual(staff.quote('city', 'near', MONDAY).segments[-1].safe_stop, 'far')

    def test_safe_stop_opening_selects_departure_and_records_assumption(self):
        p = graph()
        p['nodes'][2]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:01', 'end': '09:02'}]}
        p['nodes'][2]['access_requirement'] = 'unparsed'
        self.confirm(seal_package(p))
        for departures in ((), (MONDAY, MONDAY+timedelta(seconds=30))):
            with self.subTest(departures=departures):
                service = TransitService('bus', 'city', 'near', 'bus', 90, 0, 0,
                                         departures, cancel_after_s=30, safe_stop='far')
                nav = OfflineNavigation(self.store, 'test-home', currency='CNY', external_places=('city',),
                    access_policy=GraphAccessPolicy(treat_unparsed_access_as='allowed'), services=(service,))
                q = nav.quote('city', 'near', MONDAY)
                self.assertEqual([(s.mode, s.duration_s) for s in q.segments], [('wait', 30), ('bus', 90)])
                self.assertTrue(q.needs_review)
                self.assertEqual(q.assumptions, ('far: access unparsed; assumed allowed by treat_unparsed_access_as',))

    def test_latest_walk_departure_respects_edge_and_node_window_boundaries(self):
        p = graph()
        p['edges'][0].update(direction='forward', opening_schedule={'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:00', 'end': '09:02'}]})
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        deadline = MONDAY + timedelta(minutes=10)
        self.assertEqual(nav.latest_walk_departure_window('home', 'near', MONDAY, deadline),
                         (MONDAY, MONDAY+timedelta(minutes=1)))
        fractional = MONDAY + timedelta(microseconds=500_000)
        first, last = nav.latest_walk_departure_window('home', 'near', fractional, deadline)
        self.assertEqual((first, last), (fractional, MONDAY+timedelta(seconds=59.5)))
        self.assertEqual(nav.quote('home', 'near', last).duration_s, 60)
        self.assertIsNone(nav.latest_walk_departure_window('near', 'home', MONDAY, deadline))

    def test_legacy_search_does_not_apply_policy_interval_cap(self):
        # The archive enforces a separate package limit. Supply the synthetic
        # chain directly at this unit boundary to exercise the search limit.
        store = type('SyntheticStore', (), {'latest': lambda self, graph_id: {
            'graph_id': graph_id, 'version': 1, 'digest': 'a' * 64, 'attribution': 'synthetic',
            'nodes': [{'place_id': f'p-{i}'} for i in range(50_001)], 'gateways': [],
            'edges': [{'edge_id': f'e-{i}', 'from': f'p-{i}', 'to': f'p-{i+1}', 'direction': 'forward',
                       'enabled': True, 'estimated_duration_s': 1} for i in range(50_000)]}})()
        nav = OfflineNavigation(store, 'test-home', currency='CNY')
        self.assertEqual(nav.quote('p-0', 'p-50000', MONDAY).duration_s, 50_000)

    def test_transit_reaches_later_boarding_interval_via_upstream_wait(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:00', 'end': '09:02'},
                        {'weekdays': [0], 'start': '09:05', 'end': '09:10'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy(),
            external_places=('city',), services=(TransitService('bus', 'near', 'city', 'bus', 60, 0, 0,
                (MONDAY + timedelta(minutes=3), MONDAY + timedelta(minutes=6))),))
        q = nav.quote('home', 'city', MONDAY)
        self.assertEqual(q.duration_s, 420)
        self.assertEqual([(s.origin, s.mode, s.duration_s) for s in q.segments], [
            ('home', 'wait', 240), ('home', 'walk', 60), ('near', 'wait', 60), ('near', 'bus', 60)])

    def test_wait_for_destination_window_with_fractional_departure(self):
        p = graph()
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:05', 'end': '10:00'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        q = nav.quote('home', 'near', MONDAY + timedelta(microseconds=123456))
        self.assertEqual(q.duration_s, 300)
        self.assertEqual(tuple(s.mode for s in q.segments), ('wait', 'walk'))
        self.assertEqual(q.segments[0].duration_s, 240)
        self.assertEqual(q.segments[0].origin, q.segments[0].destination)
        self.assertFalse(q.needs_review)

    def test_wait_for_edge_and_destination_windows_together(self):
        p = graph()
        p['edges'][0]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:02', 'end': '09:03'},
                        {'weekdays': [0], 'start': '09:06', 'end': '10:00'}]}
        p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:05', 'end': '10:00'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        q = nav.quote('home', 'near', MONDAY)
        self.assertEqual(q.duration_s, 420)
        self.assertEqual(tuple(s.duration_s for s in q.segments), (360, 60))

    def test_cannot_cross_gate_that_closes_mid_traversal(self):
        p = graph()
        p['edges'][0]['estimated_duration_s'] = 120
        p['edges'][0]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:00', 'end': '09:01'}]}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        with self.assertRaises(KernelError):
            nav.quote('home', 'near', MONDAY)

    def test_cannot_wait_through_origin_closure_or_leave_at_closing(self):
        for opens_at in ('09:02', '09:05'):
            with self.subTest(opens_at=opens_at):
                p = graph()
                p['nodes'][0]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
                    'windows': [{'weekdays': [0], 'start': '09:00', 'end': '09:01'}]}
                p['nodes'][1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
                    'windows': [{'weekdays': [0], 'start': opens_at, 'end': '10:00'}]}
                p['version'] = 2 if opens_at == '09:05' else 1
                self.confirm(seal_package(p))
                nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
                with self.assertRaises(KernelError):
                    nav.quote('home', 'near', MONDAY)

    def test_campus_sized_windowed_grid_has_bounded_transition_count(self):
        p = graph()
        node, edge = p['nodes'][0], p['edges'][0]
        p['nodes'] = [{**node, 'place_id': f'p-{x}-{y}', 'name': f'grid {x} {y}'}
                      for x in range(20) for y in range(20)]
        p['edges'] = [{**edge, 'edge_id': f'e-{x}-{y}-{dx}-{dy}', 'from': f'p-{x}-{y}',
                       'to': f'p-{x+dx}-{y+dy}', 'estimated_duration_s': 30}
                      for x in range(20) for y in range(20)
                      for dx, dy in ((1, 0), (0, 1)) if x+dx < 20 and y+dy < 20]
        p['gateways'] = []
        for index in (210, 399):
            p['nodes'][index]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
                'windows': [{'weekdays': [0], 'start': '09:15', 'end': '09:30'}]}
        p['build_report']['counts'] = {'nodes': 400, 'edges': len(p['edges']), 'components': 1}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        with patch.object(nav, '_walk_candidate', wraps=nav._walk_candidate) as transitions:
            q = nav.quote('p-0-0', 'p-19-19', MONDAY)
        self.assertEqual(q.duration_s, 19 * 60)
        self.assertLessEqual(transitions.call_count, len(p['edges']))
        self.assertLessEqual(len([s for s in q.segments if s.mode == 'walk']), 38)

    def test_windowed_400_node_chain_does_not_limit_route_to_256_segments(self):
        p = graph()
        node, edge = p['nodes'][0], p['edges'][0]
        p['nodes'] = [{**node, 'place_id': f'p-{i}', 'name': f'chain {i}'} for i in range(400)]
        p['edges'] = [{**edge, 'edge_id': f'e-{i}', 'from': f'p-{i}', 'to': f'p-{i+1}',
                       'direction': 'forward', 'estimated_duration_s': 1} for i in range(399)]
        p['gateways'] = []
        p['nodes'][-1]['opening_schedule'] = {'status': 'known', 'timezone': 'Asia/Shanghai',
            'windows': [{'weekdays': [0], 'start': '09:15', 'end': '09:30'}]}
        p['build_report']['counts'] = {'nodes': 400, 'edges': 399, 'components': 1}
        self.confirm(seal_package(p))
        nav = OfflineNavigation(self.store, 'test-home', currency='CNY', access_policy=GraphAccessPolicy())
        q = nav.quote('p-0', 'p-399', MONDAY)
        self.assertEqual(q.duration_s, 900)
        self.assertEqual(len([s for s in q.segments if s.mode == 'walk']), 399)

    def test_optional_fields_validation_and_old_package_compatibility(self):
        self.assertEqual(validate_package(home_graph()), home_graph())
        for field, invalid in [('access_requirement', 'vip'), ('access_requirement', 'resident_of:'),
                               ('access_area', ''), ('opening_schedule', {'status': 'guess'}),
                               ('opening_schedule', {'status': 'unknown', 'windows': []}),
                               ('opening_schedule', {'status': 'known', 'timezone': 'Bad/Zone', 'windows': []})]:
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                p = graph()
                p['nodes'][0][field] = invalid
                validate_package(seal_package(p))
        for days, start, end in [([True], '08:00', '18:00'), ([7], '08:00', '18:00'),
                                  ([0, 0], '08:00', '18:00'), ([0], '24:00', '18:00'),
                                  ([0], '08:00', '25:00'), ([], '08:00', '18:00')]:
            with self.subTest(days=days, start=start, end=end), self.assertRaises(ValueError):
                p = graph()
                p['edges'][0]['opening_schedule'] = {'status': 'known', 'timezone': 'UTC',
                    'windows': [{'weekdays': days, 'start': start, 'end': end}]}
                validate_package(seal_package(p))
