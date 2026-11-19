import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/astrbot_plugin_good_life"))
from good_life import graph_packages
from good_life.graph_packages import check_package_limits, decode_bounded_json, diff_packages, package_digest, seal_package, validate_package
from good_life.graph_package_storage import GraphPackageStore
from good_life.storage import LifeStorage
from tools.osm_graph_builder.builder import build_graph
from tools.osm_graph_builder.source import parse_xml

FIXTURE = ROOT / "tests/fixtures/osm/tiny.osm"
BOUNDS = [30.999, 120.999, 31.003, 121.003]


def package(version=1):
    return build_graph(parse_xml(FIXTURE, BOUNDS), BOUNDS, graph_id="import-fixture", version=version)


class GraphPackageValidationTests(unittest.TestCase):
    def test_validation_is_pure_owned_and_digest_is_checked(self):
        original = package()
        before = copy.deepcopy(original)
        validated = validate_package(original)
        validated["nodes"][0]["name"] = "changed copy"
        self.assertEqual(original, before)
        original["nodes"][0]["name"] = "tamper"
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_package(original)

    def test_size_record_and_depth_limits_precede_recursive_validation(self):
        oversized = package()
        oversized["extra"] = "中" * 5000
        too_many = package()
        deep = package()
        nested = []
        deep["extra"] = nested
        for _ in range(2000):
            child = []
            nested.append(child)
            nested = child
        for candidate, limits, message in ((oversized, {"MAX_PACKAGE_BYTES": 8192}, "byte size"),
                                            (too_many, {"MAX_PACKAGE_RECORDS": 2}, "record count"),
                                            (deep, {"MAX_PACKAGE_DEPTH": 64}, "nesting depth")):
            with self.subTest(message=message), patch.multiple(graph_packages, **limits), patch.object(graph_packages, "_canonical", side_effect=AssertionError("recursive validation must not start")):
                for function in (validate_package, package_digest, seal_package):
                    with self.assertRaisesRegex(ValueError, message):
                        function(candidate)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaisesRegex(ValueError, "nesting depth"):
            check_package_limits({"extra": cyclic})
        # Exact compact UTF-8 bytes, rather than codepoint count, define the limit.
        small = {"field": "中\n\""}
        length = len(json.dumps(small, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        with patch.object(graph_packages, "MAX_PACKAGE_BYTES", length):
            check_package_limits(small)
        with patch.object(graph_packages, "MAX_PACKAGE_BYTES", length - 1), self.assertRaisesRegex(ValueError, "byte size"):
            check_package_limits(small)

    def test_raw_json_limits_and_recursion_errors_are_friendly(self):
        with patch.object(graph_packages, "MAX_PACKAGE_BYTES", 32), patch.object(graph_packages.json, "loads", side_effect=AssertionError("must reject bytes before parsing")):
            with self.assertRaisesRegex(ValueError, "byte size"):
                decode_bounded_json('"' + '中' * 20 + '"')
            with self.assertRaisesRegex(ValueError, "byte size"):
                decode_bounded_json(b' ' * 33)
        with self.assertRaisesRegex(ValueError, "nesting"):
            decode_bounded_json('[' * 2000 + '0' + ']' * 2000)
        candidate = package()
        with patch.object(graph_packages.copy, "deepcopy", side_effect=RecursionError("mocked copy overflow")):
            with self.assertRaisesRegex(ValueError, "nesting"):
                validate_package(candidate)
        with patch.object(graph_packages, "_canonical", side_effect=RecursionError("mocked canonical overflow")):
            with self.assertRaisesRegex(ValueError, "nesting"):
                package_digest(candidate)

    def test_schema_coordinates_references_times_and_attribution_rejected(self):
        edits = [lambda p: p.update(schema="unknown/v7"), lambda p: p.update(coordinate_system="GCJ02"),
                 lambda p: p.update(attribution=""), lambda p: p.update(version=True),
                 lambda p: p["nodes"][0].update(lat=91), lambda p: p["nodes"][0].update(lat=True),
                 lambda p: p["nodes"][0].update(osm_ids=[]), lambda p: p["nodes"][0].update(source={}),
                 lambda p: p["nodes"][0].pop("opening_rules"),
                 lambda p: p["nodes"].append(copy.deepcopy(p["nodes"][0])),
                 lambda p: p["edges"][0].update(to="missing"), lambda p: p["edges"][0].update(to=[]),
                 lambda p: p["edges"][0].update(direction=[]),
                 lambda p: p["edges"][0].update(estimated_duration_s=0),
                 lambda p: p["edges"][0].update(estimated_duration_s=1.2),
                 lambda p: p["edges"][0].update(estimated=False),
                 lambda p: p["gateways"][0].update(place_id="missing"),
                 lambda p: p["vector_data"][0].update(coordinates=[[200, 31]]),
                 lambda p: p.update(edit_history=[{}])]
        for index, edit in enumerate(edits):
            with self.subTest(index=index):
                invalid = package()
                edit(invalid)
                with self.assertRaises(ValueError):
                    validate_package(seal_package(invalid))
        for value in (float("nan"), float("inf"), 2**60, 10**1000, object()):
            invalid = package()
            invalid["extra"] = value
            with self.assertRaises(ValueError):
                seal_package(invalid)

    def test_basemap_vector_kinds_accepted_and_unknown_kind_rejected(self):
        candidate = package()
        self.assertTrue(any(f["kind"] in {"landcover", "water", "waterway", "place"} for f in candidate["vector_data"]))
        self.assertEqual(validate_package(candidate), candidate)
        legacy = package()
        legacy["vector_data"] = [f for f in legacy["vector_data"] if f["kind"] in {"road", "building", "facility"}]
        validate_package(seal_package(legacy))  # Packages from before basemap layers stay valid.
        invalid = package()
        invalid["vector_data"][0]["kind"] = "tile"
        with self.assertRaises(ValueError):
            validate_package(seal_package(invalid))

    def test_diff_versions_added_removed_changed_and_no_mutation(self):
        current, proposed = package(1), package(2)
        proposed["nodes"][0]["name"] = "renamed"
        proposed["nodes"][0]["source"] = "manual"
        removed = proposed["edges"].pop()
        added = copy.deepcopy(proposed["nodes"][0])
        added.update(place_id="manual:room", source="manual", osm_ids=[])
        proposed["nodes"].append(added)
        proposed = seal_package(proposed)
        before = copy.deepcopy((current, proposed))
        diff = diff_packages(current, proposed)
        self.assertEqual(diff["version_comparison"], "newer")
        self.assertTrue(diff["can_import"])
        self.assertEqual(diff["nodes"]["added"], ["manual:room"])
        self.assertIn(current["nodes"][0]["place_id"], diff["nodes"]["changed"])
        self.assertEqual(diff["edges"]["removed"], [removed["edge_id"]])
        self.assertEqual((current, proposed), before)
        self.assertEqual(diff_packages(current, current)["version_comparison"], "same")
        self.assertFalse(diff_packages(current, current)["can_import"])
        self.assertEqual(diff_packages(package(3), package(2))["version_comparison"], "older")
        self.assertEqual(diff_packages(None, current)["version_comparison"], "initial")

    def test_place_id_change_identity_reuse_and_duplicate_osm_identity_rejected(self):
        old, new = package(1), package(2)
        new["nodes"][0]["place_id"] = "renamed-id"
        for edge in new["edges"]:
            if edge["from"] == old["nodes"][0]["place_id"]:
                edge["from"] = "renamed-id"
        new["gateways"][0]["place_id"] = "renamed-id"
        with self.assertRaisesRegex(ValueError, "stable place_id"):
            diff_packages(old, seal_package(new))
        new = package(2)
        new["nodes"][0]["osm_ids"] = ["node/56789"]
        with self.assertRaisesRegex(ValueError, "reused"):
            diff_packages(old, seal_package(new))
        new = package(2)
        new["nodes"][0].update(source="manual", osm_ids=[])
        with self.assertRaisesRegex(ValueError, "reused"):
            diff_packages(old, seal_package(new))
        new = package(2)
        new["nodes"][1]["osm_ids"] = list(new["nodes"][0]["osm_ids"])
        with self.assertRaisesRegex(ValueError, "duplicate OSM"):
            validate_package(seal_package(new))


class GraphPackageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)

    def confirm(self, store, candidate, **kwargs):
        preview = store.preview(candidate)
        return store.confirm(candidate, expected_current_digest=preview["current_digest"],
                             expected_proposed_digest=preview["proposed_digest"], confirmed=True, **kwargs)

    def test_default_off_and_preview_do_not_create_tables(self):
        store = GraphPackageStore(self.storage)
        candidate = package()
        self.assertTrue(store.preview(candidate)["can_import"])
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.confirm(store, candidate)
        self.assertIsNone(self.storage._connection.execute("SELECT name FROM sqlite_master WHERE name='geo_graph_versions'").fetchone())
        self.assertEqual(store.history(candidate["graph_id"]), [])
        self.assertIsNone(store.latest(candidate["graph_id"]))

    def test_import_limits_reject_before_storage_and_bound_stored_json(self):
        store = GraphPackageStore(self.storage, enabled=True)
        candidate = package()
        with patch.object(graph_packages, "MAX_PACKAGE_RECORDS", 2):
            with self.assertRaisesRegex(ValueError, "record count"):
                store.preview(candidate)
            with self.assertRaisesRegex(ValueError, "record count"):
                store.confirm(candidate, expected_current_digest=None, expected_proposed_digest=candidate["digest"], confirmed=True)
        self.assertIsNone(self.storage._connection.execute("SELECT name FROM sqlite_master WHERE name='geo_graph_versions'").fetchone())
        self.confirm(store, candidate)
        for raw, limits, message in ((" " * 1025, {"MAX_PACKAGE_BYTES": 1024}, "byte size"),
                                     ('[' * 2000 + '0' + ']' * 2000, {"MAX_PACKAGE_DEPTH": 64}, "nesting"),
                                     ('{"records":[{},{},{}]}', {"MAX_PACKAGE_RECORDS": 2}, "record count")):
            with self.storage.transaction() as connection:
                connection.execute("UPDATE geo_graph_versions SET package_json=?", (raw,))
            with self.subTest(message=message), patch.multiple(graph_packages, **limits), self.assertRaisesRegex(ValueError, message):
                store.latest(candidate["graph_id"])

    def test_confirmation_review_digests_and_monotonic_version_are_required(self):
        store = GraphPackageStore(self.storage, enabled=True)
        candidate = package()
        with self.assertRaisesRegex(ValueError, "confirmation"):
            store.confirm(candidate, expected_current_digest=None, expected_proposed_digest=candidate["digest"])
        with self.assertRaisesRegex(ValueError, "proposed package changed"):
            store.confirm(candidate, expected_current_digest=None, expected_proposed_digest="stale", confirmed=True)
        self.confirm(store, candidate)
        with self.assertRaisesRegex(ValueError, "must increase"):
            self.confirm(store, candidate)
        second = package(2)
        with self.assertRaisesRegex(ValueError, "current graph changed"):
            store.confirm(second, expected_current_digest=None, expected_proposed_digest=second["digest"], confirmed=True)
        self.assertEqual(len(store.history(candidate["graph_id"])), 1)

    def test_new_versions_are_archived_without_overwriting_previous_or_runtime_records(self):
        store = GraphPackageStore(self.storage, enabled=True)
        first, second = package(1), package(2)
        with self.storage.transaction() as connection:
            connection.execute("CREATE TABLE travel_fixture (graph_version INTEGER, elapsed_s INTEGER)")
            connection.execute("INSERT INTO travel_fixture VALUES (1, 400)")
        self.confirm(store, first)
        self.confirm(store, second)
        self.assertEqual([h["version"] for h in store.history(first["graph_id"])], [1, 2])
        self.assertEqual(store.latest(first["graph_id"]), second)
        stored = json.loads(self.storage._connection.execute("SELECT package_json FROM geo_graph_versions WHERE version=1").fetchone()[0])
        self.assertEqual(stored, first)
        self.assertEqual(tuple(self.storage._connection.execute("SELECT * FROM travel_fixture").fetchone()), (1, 400))
        self.storage.close()
        reopened = LifeStorage.open(self.temp.name)
        self.addCleanup(reopened.close)
        self.assertEqual(GraphPackageStore(reopened).latest(first["graph_id"]), second)

    def test_retired_place_identity_cannot_be_reassigned_in_later_version(self):
        store = GraphPackageStore(self.storage, enabled=True)
        first = package(1)
        self.confirm(store, first)
        removed = next(n for n in first["nodes"] if n["osm_ids"] == ["way/22"])
        second = package(2)
        second["nodes"] = [n for n in second["nodes"] if n["place_id"] != removed["place_id"]]
        self.confirm(store, seal_package(second))
        third = package(3)
        next(n for n in third["nodes"] if n["osm_ids"] == ["way/22"])["place_id"] = "reassigned"
        with self.assertRaisesRegex(ValueError, "stable place_id"):
            self.confirm(store, seal_package(third))
        self.assertEqual(len(store.history(first["graph_id"])), 2)


if __name__ == "__main__":
    unittest.main()
