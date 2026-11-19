"""The builder never makes a real network call in this suite."""
from collections import Counter
from contextlib import closing
import copy
from datetime import datetime, timezone
import io
import importlib.util
from http.client import BadStatusLine, HTTPException, IncompleteRead, LineTooLong
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.osm_graph_builder.builder import build_graph, distance_m, graph_report, walking_time
from tools.osm_graph_builder.package_api import seal_package, validate_package
from tools.osm_graph_builder.source import (
    OVERPASS_URL, _basemap_outside_refs, area_km2, center_bounds, fetch_overpass, load_source, parse_file, parse_xml,
)
from tools.osm_graph_builder.__main__ import main, serve_editor
from good_life.city.navigation import OfflineNavigation
from good_life.graph_access import GraphAccessPolicy
from good_life.graph_package_storage import GraphPackageStore
from good_life.kernel.codec import KernelError
from good_life.storage import LifeStorage

FIXTURE = ROOT / "tests/fixtures/osm/tiny.osm"
BOUNDS = [30.999, 120.999, 31.003, 121.003]


def fixture_package(version=1):
    return build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id="fixture-home", version=version)


def conditional_fixture():
    return {"nodes": {
        "1": {"lat": 31, "lon": 121, "tags": {"access": "yes", "opening_hours": "24/7"}},
        "2": {"lat": 31, "lon": 121.0001, "tags": {"access": "yes", "opening_hours": "24/7", "barrier": "gate"}},
    }, "ways": [{"id": "20", "refs": ["1", "2"], "tags": {
        "highway": "footway", "access": "yes", "opening_hours": "24/7",
    }}]}


class OsmBuilderTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch("urllib.request.build_opener", side_effect=AssertionError("real networking forbidden"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def test_parse_filter_build_coordinates_ids_and_report(self):
        extracted = parse_xml(FIXTURE, BOUNDS)
        self.assertNotIn("9", extracted["nodes"])
        self.assertEqual(extracted["dropped"]["unsupported_relations"], 1)
        graph = build_graph(extracted, BOUNDS, graph_id="fixture-home")
        self.assertEqual(graph["source"], "osm")
        self.assertEqual(graph["attribution"], "© OpenStreetMap contributors")
        self.assertEqual(graph["coordinate_system"], "WGS84")
        self.assertEqual(graph["nodes"][0]["osm_ids"], ["node/1"])
        self.assertEqual(len(graph["nodes"]), 6)
        self.assertEqual(len(graph["edges"]), 4)
        self.assertEqual(len(graph["gateways"]), 1)
        self.assertEqual(graph["gateways"][0]["place_id"], "osm:node/1")
        report = graph["build_report"]
        self.assertEqual(report["counts"]["components"], 3)
        self.assertIn("osm:way/22", report["isolated_nodes"])
        self.assertIn("osm:node/2", report["unnamed_places"])
        self.assertIn("osm:node/2", report["missing_access"])
        self.assertEqual(report["dropped"]["missing_or_outside_segments"], 3)
        self.assertIn({"osm_id": "way/23", "reason": "missing_or_outside_segments", "segment_index": 0,
                       "refs": ["2", "999"]}, report["dropped_items"])
        self.assertEqual(report["dropped"]["non_walkable_ways"], 1)
        self.assertEqual(report["dropped"]["degenerate_segments"], 1)
        self.assertEqual(validate_package(graph), graph)

    def test_pedestrian_oneway_and_foot_override(self):
        graph = fixture_package()
        self.assertEqual(graph["edges"][0]["direction"], "forward")
        steps = next(e for e in graph["edges"] if e["osm_ids"] == ["way/21"])
        self.assertEqual((steps["direction"], steps["from"], steps["to"]), ("forward", "osm:node/4", "osm:node/3"))
        private = next(e for e in graph["edges"] if e["osm_ids"] == ["way/25"])
        self.assertFalse(private["enabled"])

    def test_structured_access_opt_in_keeps_private_paths_for_credentials(self):
        graph = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id='fixture-home', structured_access=True)
        private = next(e for e in graph['edges'] if e['osm_ids'] == ['way/25'])
        self.assertTrue(private['enabled'])
        self.assertEqual(next(n for n in graph['nodes'] if n['place_id'] == private['to'])['access'], 'private')

    def test_structured_walking_access_precedence_in_confirmed_route_quote(self):
        graph_id = "synthetic-walking-access"
        departure = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
        for tags, expected in (({"access": "private", "foot": "yes"}, "yes"),
                               ({"access": "no", "foot": "yes"}, "yes"),
                               ({"access": "yes", "foot": "private"}, "private"),
                               ({"foot": "private"}, "private"),
                               ({"access": "private"}, "private"),
                               ({"access": "yes", "foot": "no"}, "no")):
            with (self.subTest(tags=tags), TemporaryDirectory() as directory,
                  closing(LifeStorage.open(directory)) as storage):
                data = {"nodes": {
                    "1": {"lat": 31, "lon": 121, "tags": {"access": "yes", "opening_hours": "24/7"}},
                    "2": {"lat": 31, "lon": 121.0001, "tags": {"barrier": "gate", "opening_hours": "24/7", **tags}},
                    "3": {"lat": 31, "lon": 121.0002, "tags": {"opening_hours": "24/7", **tags}},
                }, "ways": [{"id": "20", "refs": ["1", "2", "3"], "tags": {
                    "highway": "footway", "access": "private", "foot": "yes", "opening_hours": "24/7",
                }}]}
                package = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                store = GraphPackageStore(storage, enabled=True)
                store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
                endpoints = ("osm:node/1", "osm:node/3", departure)
                unmapped = GraphAccessPolicy(credentials={"student"}, treat_unparsed_access_as="allowed")
                mapped = GraphAccessPolicy(credentials={"student"}, private_credentials={graph_id: "student"})
                for policy, allowed in ((GraphAccessPolicy(), expected == "yes"),
                                        (unmapped, expected == "yes"), (mapped, expected != "no")):
                    nav = OfflineNavigation(store, graph_id, currency="CNY", access_policy=policy)
                    if allowed:
                        quote = nav.quote(*endpoints)
                        self.assertEqual([s.destination for s in quote.segments], ["osm:node/2", "osm:node/3"])
                        self.assertFalse(quote.needs_review)
                    else:
                        with self.assertRaises(KernelError):
                            nav.quote(*endpoints)

                self.assertEqual(package["gateways"][0]["place_id"], "osm:node/2")
                self.assertEqual([n["access"] for n in package["nodes"]], ["yes", expected, expected])
                self.assertEqual([(e["access"], e["enabled"]) for e in package["edges"]], [("yes", expected != "no")] * 2)
                legacy = build_graph(data, BOUNDS, graph_id=graph_id)
                self.assertEqual([n["access"] for n in legacy["nodes"]], ["yes", tags.get("access"), tags.get("access")])
                self.assertEqual([(e["access"], e["enabled"]) for e in legacy["edges"]], [("yes", expected == "yes")] * 2)

    def test_structured_conditionals_require_opt_in_in_confirmed_quotes(self):
        graph_id = "synthetic-conditional-access"
        departure = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
        endpoints = ("osm:node/1", "osm:node/2")
        cases = (
            ("node", "foot:conditional"), ("node", "access:conditional"),
            ("way", "foot:conditional"), ("way", "access:conditional"),
            ("way", "foot:forward:conditional"), ("way", "foot:backward:conditional"),
            ("way", "access:forward:conditional"), ("way", "access:backward:conditional"),
            ("way", "oneway:foot:conditional"), ("way", "oneway:conditional"),
            ("way", "motor_vehicle:forward:conditional"),
        )
        for feature, key in cases:
            with (self.subTest(feature=feature, key=key), TemporaryDirectory() as directory,
                  closing(LifeStorage.open(directory)) as storage):
                data = conditional_fixture()
                tags = data["nodes"]["2"]["tags"] if feature == "node" else data["ways"][0]["tags"]
                raw = "yes @ (Mo-Fr 08:00-18:00)" if key.startswith("oneway:") else "no @ (Mo-Fr 08:00-18:00)"
                tags[key] = raw
                if key == "foot:conditional":
                    tags.pop("access")  # The conditional is unparsed even with no baseline tag.
                package = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                record = package["nodes"][1] if feature == "node" else package["edges"][0]
                identity = record["place_id"] if feature == "node" else record["edge_id"]
                self.assertEqual(record["access_conditionals"], {key: raw})
                self.assertEqual(package["build_report"]["unparsed_access"], [identity])
                store = GraphPackageStore(storage, enabled=True)
                store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
                for policy in (None, GraphAccessPolicy(), GraphAccessPolicy(treat_unknown_opening_as="allowed")):
                    nav = OfflineNavigation(store, graph_id, currency="CNY", access_policy=policy)
                    with self.assertRaises(KernelError):
                        nav.quote(*endpoints, departure)
                nav = OfflineNavigation(store, graph_id, currency="CNY", access_policy=GraphAccessPolicy(
                    treat_unparsed_access_as="allowed"))
                hard_closed = feature == "way" and key not in {"foot:conditional", "access:conditional"}
                for origin, destination in (endpoints, endpoints[::-1]):
                    if hard_closed:
                        self.assertFalse(record["enabled"])
                        with self.assertRaises(KernelError):
                            nav.quote(origin, destination, departure)
                    else:
                        quote = nav.quote(origin, destination, departure)
                        self.assertTrue(quote.needs_review)
                        self.assertEqual(quote.assumptions, (
                            f"{identity}: access unparsed; assumed allowed by treat_unparsed_access_as",
                        ))

                legacy_id = graph_id + "-legacy"
                legacy = build_graph(data, BOUNDS, graph_id=legacy_id)
                self.assertFalse(any("access_conditionals" in r for r in (*legacy["nodes"], *legacy["edges"])))
                self.assertEqual(legacy["build_report"]["unparsed_access"], [])
                store.confirm(legacy, expected_current_digest=None, expected_proposed_digest=legacy["digest"], confirmed=True)
                nav = OfflineNavigation(store, legacy_id, currency="CNY")
                self.assertEqual(nav.quote(*endpoints, departure).segments[0].destination, endpoints[1])

    def test_structured_conditions_do_not_bypass_private_access_or_known_prohibitions(self):
        graph_id = "synthetic-conditional-baseline"
        departure = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
        endpoints = ("osm:node/1", "osm:node/2", departure)
        unmapped = GraphAccessPolicy(credentials={"student"}, treat_unparsed_access_as="allowed")
        mapped = GraphAccessPolicy(credentials={"student"}, private_credentials={graph_id: "student"})
        wrong_credential = GraphAccessPolicy(credentials={"staff"}, private_credentials={graph_id: "student"},
                                             treat_unparsed_access_as="allowed")
        mapped_and_assumed = GraphAccessPolicy(credentials={"student"}, private_credentials={graph_id: "student"},
                                               treat_unparsed_access_as="allowed")
        for feature, baseline in (("node", "private"), ("way", "private"), ("node", "no"), ("way", "no")):
            with (self.subTest(feature=feature, baseline=baseline), TemporaryDirectory() as directory,
                  closing(LifeStorage.open(directory)) as storage):
                data = conditional_fixture()
                tags = data["nodes"]["2"]["tags"] if feature == "node" else data["ways"][0]["tags"]
                tags.update(foot=baseline, **{"foot:conditional": "yes @ (Mo-Fr 08:00-18:00)"})
                package = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                self.assertEqual(package["edges"][0]["enabled"], baseline == "private")
                record = package["nodes"][1] if feature == "node" else package["edges"][0]
                self.assertEqual(record["access"], baseline)
                self.assertIn("foot:conditional", record["access_conditionals"])
                store = GraphPackageStore(storage, enabled=True)
                store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
                for policy in (unmapped, mapped, wrong_credential, mapped_and_assumed):
                    nav = OfflineNavigation(store, graph_id, currency="CNY", access_policy=policy)
                    if baseline == "private" and policy is mapped_and_assumed:
                        quote = nav.quote(*endpoints)
                        self.assertTrue(quote.needs_review)
                        self.assertEqual(len(quote.assumptions), 1)
                        self.assertIn("access unparsed", quote.assumptions[0])
                    else:
                        with self.assertRaises(KernelError):
                            nav.quote(*endpoints)

    def test_structured_conditional_oneway_follows_walking_modes(self):
        for highway, oneway_foot in (("residential", {}), ("footway", {"oneway:foot": "no"})):
            with self.subTest(highway=highway, oneway_foot=oneway_foot):
                data = conditional_fixture()
                data["ways"][0]["tags"].update(highway=highway, **oneway_foot, **{
                    "oneway:conditional": "yes @ (Mo-Fr)", "bicycle:conditional": "no @ (Mo-Fr)",
                })
                package = build_graph(data, BOUNDS, graph_id="synthetic-other-modes", structured_access=True)
                self.assertEqual(package["edges"][0]["direction"], "both")
                self.assertNotIn("access_conditionals", package["edges"][0])

    def test_structured_directional_way_tags_close_only_safe_baseline_directions(self):
        cases = (
            ({"foot:forward": "no"}, ("2", "1", "forward", True)),
            ({"access:backward": "private"}, ("1", "2", "forward", True)),
            ({"foot:forward": "destination"}, ("2", "1", "forward", True)),
            ({"foot:forward": "yes", "access:forward": "no"}, ("2", "1", "forward", True)),
            ({"foot:forward": "no", "access:backward": "no"}, ("1", "2", "both", False)),
            ({"oneway:foot": "reversible"}, ("1", "2", "both", False)),
            ({"oneway:foot": "-1", "foot:backward": "no"}, ("1", "2", "both", False)),
            ({"oneway:foot": "-1", "foot:forward": "no"}, ("2", "1", "forward", True)),
            ({"oneway:foot": "-1"}, ("2", "1", "forward", True)),
            ({"oneway:foot": "no", "oneway": "yes"}, ("1", "2", "both", True)),
            ({"foot": "no", "foot:forward": "yes"}, ("1", "2", "both", False)),
        )
        for tags, (a, b, direction, enabled) in cases:
            with self.subTest(tags=tags):
                data = conditional_fixture()
                data["ways"][0]["tags"].update(tags)
                graph = build_graph(data, BOUNDS, graph_id="synthetic-directional", structured_access=True)
                edge = graph["edges"][0]
                self.assertEqual((edge["from"], edge["to"], edge["direction"], edge["enabled"]),
                                 (f"osm:node/{a}", f"osm:node/{b}", direction, enabled))
                evidence = {key: value for key, value in tags.items() if key.endswith((":forward", ":backward"))
                            or value == "reversible"}
                self.assertEqual(edge.get("access_directional", {}), evidence)
                self.assertEqual(graph["build_report"]["unparsed_access"], [edge["edge_id"]] if evidence else [])
                legacy = build_graph(data, BOUNDS, graph_id="synthetic-directional")
                self.assertEqual(legacy["build_report"]["unparsed_access"], [])
                if tags.get("foot") != "no":
                    self.assertTrue(legacy["edges"][0]["enabled"])
                    self.assertEqual(legacy["edges"][0]["direction"],
                                     "forward" if tags.get("oneway:foot") == "-1" else "both")

    def test_structured_unsupported_walking_way_tags_close_and_preserve_raw_evidence(self):
        graph_id = "synthetic-unsupported-walking-tags"
        departure = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
        endpoints = ("osm:node/1", "osm:node/2")
        cases = (
            ("access:foot:forward", "no", "access_directional"),
            ("foot:campus:backward", "yes", "access_directional"),
            ("access:foot:forward:conditional", "no @ (Mo-Fr)", "access_conditionals"),
            ("foot:campus:conditional", "  raw rule  ", "access_conditionals"),
            ("oneway:forward", "yes", "access_directional"),
            ("oneway:pedestrian", "yes", "access_directional"),
            ("oneway:foot:campus", "no", "access_directional"),
            ("oneway:pedestrian:conditional", "yes @ (Mo-Fr)", "access_conditionals"),
            ("oneway:bicycle", "yes", "access_directional"),
            ("oneway:psv:forward:conditional", "yes @ (Mo-Fr)", "access_conditionals"),
            ("oneway:reversible", "yes", "access_directional"),
            ("foot:way:conditional", "yes @ (Mo-Fr)", "access_conditionals"),
        )
        for key, value, evidence_field in cases:
            with (self.subTest(key=key), TemporaryDirectory() as directory,
                  closing(LifeStorage.open(directory)) as storage):
                data = conditional_fixture()
                data["ways"][0]["tags"].update(foot="yes", **{key: value})
                package = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                edge = package["edges"][0]
                self.assertFalse(edge["enabled"])
                self.assertEqual(edge[evidence_field], {key: value})
                self.assertEqual(package["build_report"]["unparsed_access"], [edge["edge_id"]])
                store = GraphPackageStore(storage, enabled=True)
                store.confirm(package, expected_current_digest=None, expected_proposed_digest=package["digest"], confirmed=True)
                for policy in (GraphAccessPolicy(), GraphAccessPolicy(treat_unparsed_access_as="allowed")):
                    nav = OfflineNavigation(store, graph_id, currency="CNY", access_policy=policy)
                    for origin, destination in (endpoints, endpoints[::-1]):
                        with self.assertRaises(KernelError):
                            nav.quote(origin, destination, departure)
                legacy = build_graph(data, BOUNDS, graph_id=graph_id)["edges"][0]
                self.assertTrue(legacy["enabled"])
                self.assertEqual(legacy["direction"], "both")
                self.assertNotIn(evidence_field, legacy)

    def test_structured_walking_regex_keeps_other_modes_and_node_rules_unchanged(self):
        data = conditional_fixture()
        data["ways"][0]["tags"].update({"access:bicycle": "no", "motor_vehicle": "no"})
        data["nodes"]["2"]["tags"].update({"access:foot:forward": "no", "oneway:pedestrian": "yes"})
        package = build_graph(data, BOUNDS, graph_id="synthetic-ignored-modes", structured_access=True)
        self.assertEqual(package["build_report"]["unparsed_access"], [])
        self.assertTrue(package["edges"][0]["enabled"])
        self.assertEqual(package["edges"][0]["direction"], "both")

    def test_structured_conditional_prefix_must_end_at_a_colon(self):
        data = conditional_fixture()
        data["ways"][0]["tags"].update({"accessory:conditional": "no @ (Mo-Fr)",
                                         "footway:conditional": "no @ (Mo-Fr)"})
        package = build_graph(data, BOUNDS, graph_id="synthetic-conditional-prefix", structured_access=True)
        self.assertTrue(package["edges"][0]["enabled"])
        self.assertNotIn("access_conditionals", package["edges"][0])
        self.assertEqual(package["build_report"]["unparsed_access"], [])

    def test_access_conditionals_validation_requires_raw_osm_tag_mapping(self):
        data = conditional_fixture()
        tags = {"access:conditional": "  unmapped text  ", "foot:forward:conditional": "no @ (Mo-Fr)"}
        data["ways"][0]["tags"].update(tags)
        package = build_graph(data, BOUNDS, graph_id="conditional-validation", structured_access=True)
        self.assertEqual(validate_package(package)["edges"][0]["access_conditionals"], tags)
        for invalid in (None, [], {}, "no @ (Mo-Fr)", {"foot": "no"}, {":conditional": "no"},
                        {"foot:conditional ": "no"}, {"foot:conditional": ["no"]}):
            with self.subTest(invalid=invalid):
                package["edges"][0]["access_conditionals"] = invalid
                with self.assertRaisesRegex(ValueError, "invalid access_conditionals"):
                    validate_package(seal_package(package))

    def test_structured_manual_fields_survive_previous_regeneration(self):
        package = fixture_package()
        for r in (package['nodes'][0], package['edges'][0]):
            r.update(source='manual', access_requirement='student', access_area='campus',
                     opening_schedule={'status': 'known', 'timezone': 'Asia/Shanghai',
                        'windows': [{'weekdays': [0, 1, 2, 3, 4], 'start': '08:00', 'end': '18:00'}]})
        package = seal_package(package)
        regenerated = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id='fixture-home', version=2, previous=package)
        for collection, key in (('nodes', 'place_id'), ('edges', 'edge_id')):
            before = package[collection][0]
            after = next(r for r in regenerated[collection] if r[key] == before[key])
            self.assertEqual(after, before)

    def test_structured_previous_uses_fresh_unedited_fields_and_stable_conflict_evidence(self):
        graph_id = "synthetic-regeneration"
        for history_mode in ("editor", "none", "older"):
            for old_access, fresh_access in (("yes", "private"), ("private", "yes")):
                with self.subTest(history=history_mode, old_access=old_access):
                    data = conditional_fixture()
                    tags = data["ways"][0]["tags"]
                    tags.update(access=old_access, name="OSM name")
                    old = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                    edge = old["edges"][0]
                    before = copy.deepcopy(edge)
                    edge.update(name="Local name", source="manual", version=2)
                    old["version"] = 2
                    if history_mode != "none":
                        edit = {"version": 2, "at": "2026-11-17T00:00:00Z", "action": "edit_edges"}
                        if history_mode == "editor":
                            edit["changes"] = [{"collection": "edges", "id": edge["edge_id"],
                                                "before": before, "after": copy.deepcopy(edge)}]
                        old["edit_history"].append(edit)
                    old = seal_package(old)
                    tags.update(access=fresh_access, name="Fresh OSM", opening_hours="Mo-Fr 09:00-17:00")
                    data["nodes"]["2"]["lon"] += 0.0003
                    expected = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)["edges"][0]
                    for version in (3, 4):
                        old = build_graph(data, BOUNDS, graph_id=graph_id, version=version, previous=old,
                                          structured_access=True)
                        edge = old["edges"][0]
                        expected_access = fresh_access if history_mode == "editor" else "private"
                        self.assertEqual((edge["name"], edge["access"]), ("Local name", expected_access))
                        for field in ("availability", "length_m", "estimated_duration_s", "enabled"):
                            self.assertEqual(edge[field], expected[field])
                        self.assertEqual(edge["osm_access_conflicts"], {"previous": {"access": old_access},
                                                                        "osm": {"access": fresh_access}})
                        self.assertTrue(edge["estimate"]["needs_review"])
                        self.assertEqual(old["build_report"]["unparsed_access"], [edge["edge_id"]])
                        self.assertEqual(old["build_report"]["edges_needing_review"], [edge["edge_id"]])

    def assert_regeneration_keeps_limited_opening(self, fresh_hours, history):
        graph_id = "synthetic-regeneration-opening"
        schedule = {"status": "known", "timezone": "UTC", "windows": [
            {"weekdays": [0], "start": "09:00", "end": "10:00"}]}
        data = conditional_fixture()
        previous = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
        edge = previous["edges"][0]
        before = copy.deepcopy(edge)
        edge.update(name="Local name", opening_schedule=schedule, source="manual", version=2)
        previous["version"] = 2
        if history != "missing":
            after = copy.deepcopy(edge)
            del after["opening_schedule"]
            if history == "partial":
                del before["availability"]
                del after["availability"]
            previous["edit_history"] = [{"version": 2, "action": "edit_edges", "at": "2026-11-17T00:00:00Z",
                "changes": [{"collection": "edges", "id": edge["edge_id"], "before": before, "after": after}]}]
        data["ways"][0]["tags"]["opening_hours"] = fresh_hours
        regenerated = build_graph(data, BOUNDS, graph_id=graph_id, version=3, previous=seal_package(previous),
                                  structured_access=True)
        result = regenerated["edges"][0]
        self.assertTrue(result["enabled"])
        self.assertEqual(result["opening_schedule"], schedule)
        conflict = result["osm_access_conflicts"]["opening_schedule"]
        self.assertEqual(conflict["previous"], schedule)
        self.assertEqual(conflict["osm"]["status"], "known")
        self.assertIn(result["edge_id"], regenerated["build_report"]["edges_needing_review"])
        policy = GraphAccessPolicy()
        for hour, allowed in ((9, True), (16, False)):
            at = datetime(2026, 11, 16, hour, tzinfo=timezone.utc)
            self.assertEqual(policy.evaluate(result, graph_id, at).allowed, allowed)

    def test_structured_previous_checks_opening_history_independently(self):
        for history in ("partial", "inconsistent"):
            with self.subTest(history=history):
                self.assert_regeneration_keeps_limited_opening("24/7", history)

    def test_structured_previous_normalizes_unrestricted_opening_text(self):
        for fresh_hours in (" 24/7 ", "  always  "):
            with self.subTest(fresh_hours=fresh_hours):
                self.assert_regeneration_keeps_limited_opening(fresh_hours, "missing")

    def test_structured_previous_intersects_ordered_traversals(self):
        graph_id = "synthetic-regeneration-directions"
        a, b = "osm:node/1", "osm:node/2"
        cases = (
            (a, b, "forward", "-1", a, b, False),
            (b, a, "forward", "yes", b, a, False),
            (a, b, "forward", "no", a, b, True),
            (a, b, "both", "-1", b, a, True),
        )
        for start, end, direction, fresh_oneway, expected_start, expected_end, enabled in cases:
            with self.subTest(start=start, direction=direction, fresh_oneway=fresh_oneway):
                data = conditional_fixture()
                old = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                edge = old["edges"][0]
                before = copy.deepcopy(edge)
                edge.update({"from": start, "to": end, "direction": direction, "name": "Manual name",
                             "source": "manual", "version": 2})
                old["version"] = 2
                old["edit_history"] = [{"version": 2, "action": "edit_edges", "at": "2026-11-17T00:00:00Z", "changes": [
                    {"collection": "edges", "id": edge["edge_id"], "before": before, "after": copy.deepcopy(edge)}]}]
                data["ways"][0]["tags"]["oneway:foot"] = fresh_oneway
                old = seal_package(old)
                regenerated = build_graph(data, BOUNDS, graph_id=graph_id, version=3, previous=old, structured_access=True)
                result = regenerated["edges"][0]
                self.assertEqual((result["from"], result["to"], result["direction"], result["enabled"]),
                                 (expected_start, expected_end, "forward", enabled))
                self.assertEqual(result["name"], "Manual name")
                conflict = result["osm_access_conflicts"]["direction"]
                self.assertEqual(conflict["previous"], {"from": start, "to": end, "direction": direction})
                self.assertEqual(conflict["osm"], {"from": b if fresh_oneway == "-1" else a,
                                                    "to": a if fresh_oneway == "-1" else b,
                                                    "direction": "both" if fresh_oneway == "no" else "forward"})
                self.assertEqual(regenerated["build_report"]["edges_needing_review"], [edge["edge_id"]])
                self.assertTrue(result["estimate"]["needs_review"])
                legacy = build_graph(data, BOUNDS, graph_id=graph_id, version=3, previous=old)
                self.assertEqual(legacy["edges"][0], edge)

    def test_structured_previous_disable_survives_missing_or_ambiguous_history_until_reenabled(self):
        graph_id = "synthetic-regeneration-disabled"
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                data = conditional_fixture()
                old = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                edge = old["edges"][0]
                before = copy.deepcopy(edge)
                edge.update(source="manual", version=2, enabled=False)
                old["version"] = 2
                if ambiguous:
                    # Valid historical snapshot shape, but it lacks the relevant field.
                    del before["enabled"]
                    old["edit_history"] = [{"version": 2, "action": "edit_edges", "at": "2026-11-17T00:00:00Z",
                                            "changes": [{"collection": "edges", "id": edge["edge_id"],
                                                         "before": before, "after": copy.deepcopy(edge)}]}]
                for version in (3, 4):
                    old = build_graph(data, BOUNDS, graph_id=graph_id, version=version, previous=seal_package(old),
                                      structured_access=True)
                    edge = old["edges"][0]
                    self.assertFalse(edge["enabled"])
                    self.assertEqual(edge["osm_access_conflicts"]["enabled"], {"previous": False, "osm": True})
                    self.assertEqual(old["build_report"]["edges_needing_review"], [edge["edge_id"]])
                before = copy.deepcopy(edge)
                edge.update(enabled=True, version=5)
                old["version"] = 5
                old["edit_history"].append({"version": 5, "action": "edit_edges", "at": "2026-11-17T01:00:00Z",
                                            "changes": [{"collection": "edges", "id": edge["edge_id"],
                                                         "before": before, "after": copy.deepcopy(edge)}]})
                result = build_graph(data, BOUNDS, graph_id=graph_id, version=6, previous=seal_package(old),
                                     structured_access=True)["edges"][0]
                self.assertTrue(result["enabled"])
                self.assertNotIn("osm_access_conflicts", result)

    def test_structured_previous_keeps_explicit_access_and_new_conditional_evidence(self):
        graph_id = "synthetic-explicit-access"
        departure = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
        endpoints = ("osm:node/1", "osm:node/2", departure)
        data = conditional_fixture()
        tags = data["ways"][0]["tags"]
        tags["foot"] = "no"
        old = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
        edge = old["edges"][0]
        before = copy.deepcopy(edge)
        edge.update(name="Manual name", access="permissive", enabled=True, source="manual", version=2)
        old["version"] = 2
        old["edit_history"] = [{"version": 2, "action": "edit_edges", "at": "2026-11-17T00:00:00Z", "changes": [
            {"collection": "edges", "id": edge["edge_id"], "before": before, "after": copy.deepcopy(edge)}]}]
        old = seal_package(old)
        raw = "no @ (Mo-Fr 08:00-18:00)"
        for version in (3, 4, 5, 6):
            if version == 4:
                tags["foot:conditional"] = raw
            if version == 6:
                tags["foot:forward:conditional"] = raw
            old = build_graph(data, BOUNDS, graph_id=graph_id, version=version, previous=old, structured_access=True)
            edge = old["edges"][0]
            self.assertEqual((edge["name"], edge["access"]), ("Manual name", "permissive"))
            self.assertEqual(edge["osm_access_conflicts"], {"previous": {"access": "permissive"}, "osm": {"access": "no"},
                                                            "enabled": {"previous": True, "osm": False}})
            self.assertEqual(old["build_report"]["unparsed_access"], [edge["edge_id"]])
            self.assertEqual(edge["enabled"], version < 6)
            conditionals = {key: raw for key in ("foot:conditional", "foot:forward:conditional") if key in tags}
            self.assertEqual(edge.get("access_conditionals", {}), conditionals)
            with TemporaryDirectory() as directory, closing(LifeStorage.open(directory)) as storage:
                store = GraphPackageStore(storage, enabled=True)
                store.confirm(old, expected_current_digest=None, expected_proposed_digest=old["digest"], confirmed=True)
                nav = OfflineNavigation(store, graph_id, currency="CNY", access_policy=GraphAccessPolicy(
                    treat_unparsed_access_as="allowed"))
                if version == 6:
                    with self.assertRaises(KernelError):
                        nav.quote(*endpoints)
                else:
                    quote = nav.quote(*endpoints)
                    self.assertTrue(quote.needs_review)
                    self.assertTrue(any('regenerated OSM access differs' in note for note in quote.assumptions))

    def test_structured_previous_access_change_cannot_implicitly_enable_edge(self):
        graph_id = "synthetic-regeneration-no-implicit-enable"
        for partial in (False, True):
            with self.subTest(partial=partial):
                data = conditional_fixture()
                old = build_graph(data, BOUNDS, graph_id=graph_id, structured_access=True)
                edge = old["edges"][0]
                before = copy.deepcopy(edge)
                if partial:
                    del before["enabled"]
                edge.update(access="permissive", source="manual", version=2)
                old["version"] = 2
                old["edit_history"] = [{"version": 2, "action": "edit_edges", "at": "2026-11-17T00:00:00Z",
                                        "changes": [{"collection": "edges", "id": edge["edge_id"],
                                                     "before": before, "after": copy.deepcopy(edge)}]}]
                data["ways"][0]["tags"]["foot"] = "no"
                result = build_graph(data, BOUNDS, graph_id=graph_id, version=3, previous=seal_package(old),
                                     structured_access=True)["edges"][0]
                self.assertEqual(result["access"], "permissive")
                self.assertFalse(result["enabled"])
                self.assertTrue(result["estimate"]["needs_review"])

    def test_oneway_fallback_only_for_pedestrian_types_and_explicit_foot_wins(self):
        for road_type in ("footway", "path", "pedestrian", "steps", "residential"):
            for foot, expected in ((None, "both" if road_type == "residential" else "forward"),
                                   ("no", "both"), ("yes", "forward"), ("-1", "forward")):
                with self.subTest(road_type=road_type, foot=foot):
                    data = parse_xml(FIXTURE, BOUNDS)
                    way = next(w for w in data["ways"] if w["id"] == "20")
                    way["tags"]["highway"] = road_type
                    if foot is not None:
                        way["tags"]["oneway:foot"] = foot
                    graph = build_graph(data, BOUNDS, graph_id="direction-fixture")
                    edge = next(e for e in graph["edges"] if e["osm_ids"] == ["way/20"])
                    self.assertEqual(edge["direction"], expected)
                    self.assertEqual(edge["from"], "osm:node/2" if foot == "-1" else "osm:node/1")

    def test_connectivity_handles_many_isolated_components_without_repeated_min(self):
        nodes = [{"place_id": f"n-{i:05}", "name": "", "access": None, "opening_rules": None} for i in range(3000)]
        # Repeated min(unseen) was the quadratic path, even though output was correct.
        with patch("tools.osm_graph_builder.builder.min", side_effect=AssertionError("repeated component minimum"), create=True):
            report = graph_report(nodes, [], {})
        self.assertEqual(report["counts"]["components"], len(nodes))
        self.assertEqual(report["components"], [[n["place_id"]] for n in nodes])
        self.assertEqual(report["isolated_nodes"], [n["place_id"] for n in nodes])

    def test_estimates_use_distance_speed_and_road_type_never_zero(self):
        a = {"lat": 0, "lon": 0}
        b = {"lat": 0, "lon": .001}
        self.assertAlmostEqual(distance_m(a, b), 111.195, places=2)
        normal, assumptions = walking_time(140, "footway")
        slow, _ = walking_time(140, "steps")
        self.assertEqual(normal, 100)
        self.assertEqual(slow, 216)
        self.assertEqual(assumptions["effective_speed_m_s"], 1.4)
        self.assertEqual(walking_time(0, "footway")[0], 1)
        self.assertEqual(walking_time(140, "footway", 2.8)[0], 50)
        for distance, speed in ((-1, 1.4), (1, 0), (1, float("nan"))):
            with self.assertRaises(ValueError):
                walking_time(distance, "path", speed)

    def test_category_selection_and_repeatability(self):
        data = parse_xml(FIXTURE, BOUNDS)
        first = build_graph(data, BOUNDS, graph_id="fixture-home", roads={"steps"}, buildings=set(), facilities={"cafe"})
        self.assertEqual(len(first["edges"]), 1)
        self.assertFalse(any(f["kind"] == "building" for f in first["vector_data"]))
        self.assertEqual(fixture_package(), fixture_package())

    def test_xml_unordered_nodes_and_unsupported_documents(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unordered.osm"
            path.write_text('<osm><way id="8"><nd ref="1"/><nd ref="2"/><tag k="highway" v="path"/></way><node id="1" lat="31" lon="121"/><node id="2" lat="31.001" lon="121"/></osm>')
            self.assertEqual(len(parse_xml(path, BOUNDS)["ways"]), 1)
            path.write_text('<!DOCTYPE osm [<!ENTITY x "expanded">]><osm/>')
            with self.assertRaisesRegex(ValueError, "DTD"):
                parse_xml(path, BOUNDS)
            for content in ('<html/>', '<osm><node', ''):
                path.write_text(content)
                with self.assertRaisesRegex(ValueError, "OSM XML"):
                    parse_xml(path, BOUNDS)
            path.write_bytes('<!DOCTYPE osm><osm/>'.encode('utf-16'))
            with self.assertRaisesRegex(ValueError, "UTF-16"):
                parse_xml(path, BOUNDS)

    def test_optional_pbf_dependency_does_not_affect_xml(self):
        with patch.dict(sys.modules, {"osmium": None}):
            with self.assertRaisesRegex(ValueError, "optional tool dependency") as raised:
                parse_file("missing.osm.pbf", BOUNDS)
            hint = str(raised.exception)
            self.assertIn("py -3 -m venv .venv", hint)
            self.assertIn(r".venv\Scripts\python.exe -m pip install -r tools\osm_graph_builder\requirements-pbf.txt", hint)
            self.assertNotIn("py -3 -m pip", hint)
            self.assertTrue(parse_file(FIXTURE, BOUNDS)["nodes"])

    @unittest.skipUnless(importlib.util.find_spec("osmium"), "optional PBF parser not installed")
    def test_pbf_roundtrip_matches_xml_when_optional_parser_installed(self):
        import osmium
        with TemporaryDirectory() as directory:
            folder = Path(directory) / "本地提取"
            folder.mkdir()
            pbf = folder / "微型.osm.pbf"
            with osmium.SimpleWriter(pbf) as writer:
                for item in osmium.FileProcessor(FIXTURE):
                    writer.add(item)
            self.assertEqual(parse_file(pbf, BOUNDS), parse_file(FIXTURE, BOUNDS))
            self.assertEqual(build_graph(parse_file(pbf, BOUNDS), BOUNDS, graph_id="fixture-home"), fixture_package())

    def test_offline_cache_works_after_original_file_removed_and_never_fetches(self):
        fetcher = Mock(side_effect=AssertionError("unexpected network"))
        with TemporaryDirectory() as directory:
            source = Path(directory) / "local.osm"
            shutil.copyfile(FIXTURE, source)
            data, info = load_source(directory, BOUNDS, source=source, fetcher=fetcher)
            self.assertFalse(info["cache_hit"])
            source.unlink()
            cached, info = load_source(directory, BOUNDS, source=source, allow_network=True, fetcher=fetcher)
            self.assertTrue(info["cache_hit"])
            self.assertEqual(data, cached)
            with self.assertRaises(FileNotFoundError):
                load_source(directory, BOUNDS, source=source, refresh=True, fetcher=fetcher)
            fetcher.assert_not_called()

    def test_cache_miss_requires_explicit_network_and_refresh_is_explicit(self):
        fetcher = Mock(return_value=FIXTURE.read_bytes())
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no matching cache"):
                load_source(directory, BOUNDS, fetcher=fetcher)
            fetcher.assert_not_called()
            data, info = load_source(directory, BOUNDS, allow_network=True, fetcher=fetcher)
            fetcher.assert_called_once()
            root = Path(directory) / info["cache_key"]
            self.assertEqual((root / "overpass.osm").read_bytes(), FIXTURE.read_bytes())
            self.assertEqual(data, load_source(directory, BOUNDS, allow_network=True, fetcher=fetcher)[0])
            fetcher.assert_called_once()
            load_source(directory, BOUNDS, allow_network=True, refresh=True, fetcher=fetcher)
            self.assertEqual(fetcher.call_count, 2)
            self.assertIn("[out:xml]", fetcher.call_args.args[0])

    def test_large_online_bbox_rejected_before_request_but_local_file_allowed(self):
        fetcher = Mock(side_effect=AssertionError("unexpected network"))
        bounds = [30, 120, 32, 122]
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "offline regional file"):
                load_source(directory, bounds, allow_network=True, fetcher=fetcher)
            self.assertTrue(load_source(directory, bounds, source=FIXTURE, fetcher=fetcher)[0]["nodes"])
            fetcher.assert_not_called()
        small = center_bounds(31, 121, 200)
        self.assertLess(area_km2(small), 1)
        with self.assertRaises(ValueError):
            center_bounds(90, 121, 200)

    def test_failure_has_cooldown_no_retry_and_keeps_working_cache(self):
        fetcher = Mock(side_effect=OSError("temporary failure"))
        with TemporaryDirectory() as directory:
            original, _ = load_source(directory, BOUNDS, source=FIXTURE)
            with self.assertRaisesRegex(ValueError, "cooldown 60"):
                load_source(directory, BOUNDS, refresh=True, allow_network=True, fetcher=fetcher, clock=lambda: 100)
            fetcher.assert_called_once()
            with self.assertRaisesRegex(ValueError, "cooldown active"):
                load_source(directory, BOUNDS, refresh=True, allow_network=True, fetcher=fetcher, clock=lambda: 101)
            fetcher.assert_called_once()
            self.assertEqual(load_source(directory, BOUNDS)[0], original)

    def test_retry_after_and_server_error_response_are_backed_off_without_retry(self):
        for failure in (urllib.error.HTTPError("https://example.invalid/", 429, "busy", {"Retry-After": "300"}, None),
                        b'<osm><remark>runtime error: Query timed out</remark></osm>'):
            with self.subTest(failure=type(failure).__name__), TemporaryDirectory() as directory:
                fetcher = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
                with self.assertRaisesRegex(ValueError, "no automatic retry"):
                    load_source(directory, BOUNDS, allow_network=True, fetcher=fetcher, clock=lambda: 100)
                with self.assertRaisesRegex(ValueError, "cooldown active"):
                    load_source(directory, BOUNDS, allow_network=True, refresh=True, fetcher=fetcher, clock=lambda: 101)
                fetcher.assert_called_once()
                cooldown = Path(directory) / "overpass-backoff.json"
                expected = 400 if isinstance(failure, Exception) else 160
                self.assertEqual(json.loads(cooldown.read_text())["retry_after"], expected)

    def test_cooldown_is_global_across_shifted_bbox_and_center_inputs(self):
        failure = urllib.error.HTTPError("https://example.invalid/", 429, "busy", {"Retry-After": "300"}, None)
        fetcher = Mock(side_effect=failure)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "cooldown 300"):
                load_source(directory, BOUNDS, allow_network=True, fetcher=fetcher, clock=lambda: 100)
            shifted = [BOUNDS[0] + .0001, *BOUNDS[1:]]
            for bounds in (shifted, center_bounds(31.001, 121.001, 100)):
                with self.subTest(bounds=bounds), self.assertRaisesRegex(ValueError, "wait 250 seconds"):
                    load_source(directory, bounds, allow_network=True, fetcher=fetcher, clock=lambda: 150)
            fetcher.assert_called_once()
            # The CLI's center/radius path must hit the same global guard.
            guarded = lambda *a, **kw: load_source(*a, **kw, clock=lambda: 150)
            with patch("tools.osm_graph_builder.__main__.load_source", side_effect=guarded), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as errors:
                result = main(["--overpass", "--center", "31.001", "121.001", "--radius-m", "100",
                               "--cache", directory, "--graph-id", "cooldown", "--output", str(Path(directory) / "output.json")])
            self.assertEqual(result, 2)
            self.assertIn("wait 250 seconds", errors.getvalue())
            fetcher.assert_called_once()
            # Cached/local builds remain usable while online requests are blocked.
            load_source(directory, shifted, source=FIXTURE)
            self.assertTrue(load_source(directory, shifted)[1]["cache_hit"])

    def test_all_fetch_and_parse_failures_persist_cooldown_and_are_friendly_in_cli(self):
        failures = (IncompleteRead(b"partial"), BadStatusLine("bad status"), LineTooLong("header"),
                    HTTPException("broken response"), RuntimeError("unexpected fetch failure"))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), TemporaryDirectory() as directory:
                with patch("tools.osm_graph_builder.source.fetch_overpass", side_effect=failure) as fetcher, patch("sys.stderr", new_callable=io.StringIO) as errors:
                    code = main(["--overpass", "--bbox", *map(str, BOUNDS), "--cache", directory,
                                 "--graph-id", "failure", "--output", str(Path(directory) / "output.json")])
                self.assertEqual(code, 2)
                self.assertIn("Overpass fetch failed; no automatic retry", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertTrue((Path(directory) / "overpass-backoff.json").exists())
                fetcher.assert_called_once()
        with TemporaryDirectory() as directory, patch("tools.osm_graph_builder.source.parse_xml", side_effect=KeyError("lat")):
            fetcher = Mock(return_value=b"<osm/>")
            with self.assertRaisesRegex(ValueError, "no automatic retry"):
                load_source(directory, BOUNDS, allow_network=True, fetcher=fetcher, clock=lambda: 100)
            self.assertEqual(json.loads((Path(directory) / "overpass-backoff.json").read_text())["retry_after"], 160)

    def test_local_editor_server_only_serves_editor_on_loopback(self):
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        server.server_port = 8765
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch("tools.osm_graph_builder.__main__.HTTPServer", return_value=server) as factory, patch("sys.stdout", new_callable=io.StringIO):
            serve_editor(8765)
        self.assertEqual(factory.call_args.args[0], ("127.0.0.1", 8765))
        handler = object.__new__(factory.call_args.args[1])
        handler.path = "/"
        handler.wfile = io.BytesIO()
        for method in ("send_response", "send_header", "end_headers", "send_error"):
            setattr(handler, method, Mock())
        handler.do_GET()
        self.assertIn(b"graph-core", handler.wfile.getvalue())
        headers = dict(call.args for call in handler.send_header.call_args_list)
        csp = headers["Content-Security-Policy"]
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("img-src blob: data: https://tile.openstreetmap.org;", csp)
        self.assertEqual(csp.count("https://"), 1, "the tile host is the only remote origin")
        page = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("img-src blob: data: https://tile.openstreetmap.org;", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("innerHTML", page)
        handler.path = "/../../private.json"
        handler.do_GET()
        handler.send_error.assert_called_once_with(404)
        self.assertFalse(hasattr(handler, "do_POST"))

    def test_fetch_request_is_single_generic_post_with_no_redirects(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"<osm/>"
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener) as build_opener:
            self.assertEqual(fetch_overpass("[out:xml];out;"), b"<osm/>")
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, OVERPASS_URL)
        self.assertEqual(request.get_header("User-agent"), "osm-graph-builder/0.1")
        self.assertEqual(request.get_method(), "POST")
        with self.assertRaises(urllib.error.HTTPError):
            build_opener.call_args.args[0].redirect_request(request, None, 302, "redirect", {}, "https://api.openstreetmap.org/")

    def test_regeneration_keeps_manual_values_deletions_and_stable_ids(self):
        old = fixture_package(2)
        old["nodes"][0]["name"] = "人工门名"
        old["nodes"][0]["source"] = "manual"
        removed = old["edges"].pop(0)
        old["edit_history"] = [{"version": 2, "action": "remove_edges", "at": "2026-11-17T00:00:00Z", "changes": [
            {"collection": "edges", "id": removed["edge_id"], "before": removed, "after": None}]}]
        old = seal_package(old)
        new = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id=old["graph_id"], version=3, previous=old)
        self.assertEqual(new["nodes"][0]["place_id"], old["nodes"][0]["place_id"])
        self.assertEqual(new["nodes"][0]["name"], "人工门名")
        self.assertNotIn(removed["edge_id"], {e["edge_id"] for e in new["edges"]})
        self.assertEqual(new["edit_history"], old["edit_history"])
        self.assertIn("regeneration_proposal", new["build_report"])

    def test_cli_build_and_cache_rebuild_refuses_overwrite(self):
        with TemporaryDirectory() as directory, patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            output = Path(directory) / "v1.json"
            options = ["--bbox", *map(str, BOUNDS), "--cache", directory, "--graph-id", "fixture-home"]
            self.assertEqual(main([*options, "--source", str(FIXTURE), "--output", str(output)]), 0)
            self.assertEqual(main([*options, "--output", str(output)]), 2)
            self.assertEqual(main([*options, "--previous", str(output), "--output", str(Path(directory) / "v2.json")]), 0)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for local editor tests")
    def test_editor_core_edits_and_exports_validate_in_python(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "output.json"
            source.write_text(json.dumps(fixture_package(), ensure_ascii=False), encoding="utf-8")
            batch = Path(directory) / "batch.json"
            gateways = Path(directory) / "gateways.json"
            dropped = Path(directory) / "dropped-gateway.json"
            consolidated = Path(directory) / "consolidated-gateway.json"
            result = subprocess.run(["node", str(ROOT / "tools/osm_graph_builder/test_editor.cjs"),
                                     str(source), str(output), str(batch), str(gateways), str(dropped), str(consolidated)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            exported = validate_package(json.loads(output.read_text(encoding="utf-8")))
            self.assertEqual(exported["version"], 2)
            self.assertTrue(exported["edit_history"])
            self.assertTrue(any(n["source"] == "manual" for n in exported["nodes"]))
            # Python/JS digest parity after batch operations (validate_package checks the digest).
            merged = validate_package(json.loads(batch.read_text(encoding="utf-8")))
            entry = merged["edit_history"][-1]
            self.assertEqual(entry["action"], "batch_merge")
            gone = {"osm:node/2", "osm:node/3", "osm:node/4"}
            self.assertEqual(set(entry["note"]["merged_places"]), gone)
            # Regeneration from OSM must not resurrect merged-away places or dropped edges.
            regenerated = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id=merged["graph_id"], version=3, previous=merged)
            places = {n["place_id"] for n in regenerated["nodes"]}
            self.assertFalse(gone & places)
            self.assertIn("manual:node/east", places)
            edges = {e["edge_id"]: e for e in regenerated["edges"]}
            for edge_id in entry["note"]["removed_internal_edges"] + entry["note"]["collapsed_parallel_edges"][0]["removed"]:
                self.assertNotIn(edge_id, edges)
            for edge_id in entry["note"]["rewired_edges"]:
                self.assertEqual(edges[edge_id], next(e for e in merged["edges"] if e["edge_id"] == edge_id))

            # Editor gateways: bus/metro validate, and ticked modes survive two regenerations.
            edited = validate_package(json.loads(gateways.read_text(encoding="utf-8")))
            self.assertEqual({e["action"] for e in edited["edit_history"]}, {"add_gateway", "edit_gateway_modes"})
            expected = {"osm:gateway/node/1": ("osm:node/1", ["walk", "metro"], ["node/1"]),
                        "manual:gateway/school": ("osm:node/2", ["bus", "metro"], [])}
            package = edited
            for version in (3, 4):
                package = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id=edited["graph_id"], version=version, previous=package)
                found = {g["gateway_id"]: (g["place_id"], g["modes"], g["osm_ids"]) for g in package["gateways"]}
                self.assertEqual(found, expected)
                self.assertTrue(all(g["source"] == "manual" for g in package["gateways"]))
                self.assertEqual(package["build_report"]["counts"]["gateways"], 2)
            # Cancelling the OSM gate is a tombstone: the entrance tag is still in OSM, but it stays gone.
            cancelled = validate_package(json.loads(dropped.read_text(encoding="utf-8")))
            self.assertEqual(cancelled["gateways"], [])
            regenerated = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id=cancelled["graph_id"], version=3, previous=cancelled)
            self.assertEqual(regenerated["gateways"], [])
            self.assertIn("osm:node/1", {n["place_id"] for n in regenerated["nodes"]}, "only the gateway is cancelled, not the place")
            self.assertEqual(len(fixture_package()["gateways"]), 1, "fixture still has the OSM entrance")
            # Merged gates consolidated by a mode edit: one record, OSM gate ID kept, the other stays gone.
            merged_gate = validate_package(json.loads(consolidated.read_text(encoding="utf-8")))
            regenerated = build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id=merged_gate["graph_id"], version=3, previous=merged_gate)
            self.assertEqual([(g["gateway_id"], g["place_id"], g["modes"], g["osm_ids"]) for g in regenerated["gateways"]],
                             [("osm:gateway/node/1", "osm:node/2", ["metro"], ["node/1"])])

    def test_manual_gateway_follows_its_osm_place_on_regeneration(self):
        old = fixture_package(2)
        place = next(n for n in old["nodes"] if n["place_id"] == "osm:node/2")
        gateway = {"gateway_id": "manual:gateway/metro", "place_id": "osm:node/2", "lat": place["lat"], "lon": place["lon"],
                   "coordinate_system": "WGS84", "modes": ["walk", "bus", "metro"], "source": "manual", "osm_ids": [], "version": 2}
        old["gateways"].append(gateway)
        old["edit_history"] = [{"version": 2, "action": "add_gateway", "at": "2026-11-17T00:00:00Z", "changes": [
            {"collection": "gateways", "id": gateway["gateway_id"], "before": None, "after": gateway}]}]
        old = seal_package(old)
        self.assertEqual(validate_package(old)["gateways"][-1]["modes"], ["walk", "bus", "metro"], "bus/metro are valid modes")
        extracted = parse_xml(FIXTURE, BOUNDS)
        extracted["nodes"]["2"]["lat"] += 0.0002  # OSM moved the place after the editor marked it.
        new = build_graph(extracted, BOUNDS, graph_id=old["graph_id"], version=3, previous=old)
        moved = next(g for g in new["gateways"] if g["gateway_id"] == "manual:gateway/metro")
        node = next(n for n in new["nodes"] if n["place_id"] == "osm:node/2")
        self.assertEqual((moved["lat"], moved["lon"]), (node["lat"], node["lon"]))
        self.assertEqual(moved["lat"], place["lat"] + 0.0002)
        self.assertEqual((moved["modes"], moved["source"]), (["walk", "bus", "metro"], "manual"))

    def test_basemap_layers_never_change_graph_content(self):
        with_layers = fixture_package()
        kinds = Counter(f["kind"] for f in with_layers["vector_data"])
        self.assertEqual({k: kinds[k] for k in ("landcover", "water", "waterway", "place")},
                         {"landcover": 2, "water": 1, "waterway": 1, "place": 1})
        # Strip every display-only way/node tag: graph items and graph-derived vectors are identical.
        data = parse_xml(FIXTURE, BOUNDS)
        data["ways"] = [w for w in data["ways"] if w["id"] not in {"40", "41", "42", "43"}]
        data["nodes"]["50"]["tags"] = {}
        data.pop("basemap_nodes")
        without = build_graph(data, BOUNDS, graph_id="fixture-home")
        for collection in ("nodes", "edges", "gateways"):
            self.assertEqual(with_layers[collection], without[collection])
        graph_vectors = [f for f in with_layers["vector_data"] if f["kind"] in {"road", "building", "facility"}]
        self.assertEqual(graph_vectors, without["vector_data"])
        self.assertEqual(with_layers["vector_data"][:len(graph_vectors)], graph_vectors, "basemap layers are appended")
        place_ids = {n["place_id"] for n in with_layers["nodes"]}
        for feature in with_layers["vector_data"]:
            if feature["kind"] in {"landcover", "water", "waterway", "place"}:
                self.assertFalse({"osm:" + i for i in feature["osm_ids"]} & place_ids, "basemap never becomes a place")
                for lon, lat in feature["coordinates"]:
                    self.assertTrue(BOUNDS[0] <= lat <= BOUNDS[2] and BOUNDS[1] <= lon <= BOUNDS[3], "clipped to bbox")
        water = next(f for f in with_layers["vector_data"] if f["kind"] == "water")
        self.assertEqual(water["tags"], {"name": "镜湖", "natural": "water"})
        self.assertEqual(water["coordinates"][0], water["coordinates"][-1])
        self.assertEqual(max(lat for _, lat in water["coordinates"]), BOUNDS[2])
        park = next(f for f in with_layers["vector_data"] if f["osm_ids"] == ["way/43"])
        self.assertEqual(park["kind"], "landcover")
        self.assertEqual(min(lon for lon, _ in park["coordinates"]), BOUNDS[1])
        self.assertEqual(with_layers["build_report"]["basemap"]["features"], 5)

    def test_outside_vertex_budget_skips_huge_way_but_keeps_later_small_ones(self):
        nodes = {"1": {"lat": 31.0, "lon": 121.0, "tags": {}}}
        huge = {"id": "10", "refs": ["1", "a", "b", "c", "1"], "tags": {"natural": "water"}}
        small = {"id": "11", "refs": ["1", "d"], "tags": {"waterway": "stream"}}
        with patch("tools.osm_graph_builder.source.MAX_BASEMAP_OUTSIDE_NODES", 2):
            self.assertEqual(_basemap_outside_refs(nodes, [huge, small]), {"d"})

    def test_old_cache_without_basemap_vertices_still_builds(self):
        with TemporaryDirectory() as directory:
            data, info = load_source(directory, BOUNDS, source=FIXTURE)
            cached = Path(directory) / info["cache_key"] / "extract.json"
            entry = json.loads(cached.read_text(encoding="utf-8"))
            entry["data"].pop("basemap_nodes")  # A cache written before this feature.
            cached.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            old, info = load_source(directory, BOUNDS)
            self.assertTrue(info["cache_hit"])
            graph = build_graph(old, BOUNDS, graph_id="fixture-home")
            fresh = fixture_package()
            for collection in ("nodes", "edges", "gateways"):
                self.assertEqual(graph[collection], fresh[collection])
            # Fully inside features still draw; edge-crossing areas wait for --refresh.
            kinds = Counter(f["kind"] for f in graph["vector_data"])
            self.assertEqual((kinds["landcover"], kinds["water"], kinds["waterway"]), (1, 0, 0))
            self.assertEqual(graph["build_report"]["basemap"]["skipped_incomplete"], 3)
            refreshed, _ = load_source(directory, BOUNDS, source=FIXTURE, refresh=True)
            self.assertIn("46", refreshed["basemap_nodes"])

    def test_overpass_query_adds_basemap_layers_in_one_request(self):
        fetcher = Mock(return_value=FIXTURE.read_bytes())
        with TemporaryDirectory() as directory:
            data, _ = load_source(directory, BOUNDS, allow_network=True, fetcher=fetcher)
        fetcher.assert_called_once()
        query = fetcher.call_args.args[0]
        self.assertEqual(query.count("out body;"), 1)
        for fragment in ('way["landuse"]', 'way["waterway"]', 'way["water"]', 'way["natural"~"^(', 'node["place"]["name"]',
                         'nwr["highway"]', 'nwr["leisure"]', "(._;>;);"):
            self.assertIn(fragment, query)
        self.assertEqual(data["basemap_nodes"]["49"], [121.0026, 31.004])
        self.assertNotIn("49", data["nodes"])


if __name__ == "__main__":
    unittest.main()
