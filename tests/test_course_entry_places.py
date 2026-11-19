"""Course entry page place options: confirmed campus graph only, no junctions or ordinary buildings."""

from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import test_shared_travel as travel
from good_life.academics.places import place_options
from good_life.graph_package_storage import GraphPackageStore
from good_life.graph_packages import seal_package
from good_life.runtime import LifeRuntime
from good_life.storage import LifeStorage


def package():
    base = travel.graph()
    far = base["nodes"][2]
    extra = [{**far, "place_id": "junction-1", "name": "某路口", "type": "junction"},
             {**far, "place_id": "ordinary-1", "name": "普通建筑", "type": "building"},
             {**far, "place_id": "library", "name": " 图书馆 ", "type": "library"}]
    base["nodes"].extend(extra)
    for node in extra:
        base["edges"].append({**base["edges"][1], "edge_id": "to-" + node["place_id"], "to": node["place_id"]})
    base.pop("digest", None)
    return seal_package(base)


class PlaceOptionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="course-places-")
        self.addCleanup(temporary.cleanup)
        self.storage = LifeStorage.open(temporary.name)
        self.addCleanup(self.storage.close)
        self.package = package()
        GraphPackageStore(self.storage, enabled=True).confirm(self.package, expected_current_digest=None,
                                                expected_proposed_digest=self.package["digest"], confirmed=True)

    def test_lists_named_enterable_places_from_the_confirmed_graph(self):
        graph, places = place_options(self.storage, self.package["graph_id"])
        self.assertEqual(graph, {"graph_id": self.package["graph_id"], "version": self.package["version"]})
        ids = [item["place_id"] for item in places]
        self.assertIn("library", ids)
        for hidden in ("junction-1", "ordinary-1", "unnamed"):
            self.assertNotIn(hidden, ids)
        self.assertEqual(next(item for item in places if item["place_id"] == "library"),
                         {"place_id": "library", "name": "图书馆", "type": "library"})
        self.assertEqual(places, sorted(places, key=lambda item: (item["name"], item["place_id"])))
        self.assertTrue(all(set(item) == {"place_id", "name", "type"} for item in places))
        self.assertEqual(len(place_options(self.storage, self.package["graph_id"], limit=1)[1]), 1)

    def test_no_graph_id_or_unconfirmed_graph_gives_no_places(self):
        for graph_id in ("", "  ", None, "never-confirmed"):
            with self.subTest(graph_id=graph_id):
                self.assertEqual(place_options(self.storage, graph_id), (None, []))

    def test_runtime_reader_reports_the_live_class_group_and_configured_graph(self):
        def runtime(modes, groups, graph_id, closed=False):
            return SimpleNamespace(storage=self.storage, closed=closed,
                plugin=SimpleNamespace(_campus_settings={"academics_graph_id": graph_id}),
                _optional=SimpleNamespace(modes=modes, groups=groups))
        live = LifeRuntime.read_academic_places(runtime({"classes": "active"}, {"classes": object()}, self.package["graph_id"]))
        self.assertTrue(live["classes_enabled"])
        self.assertEqual(live["graph"]["graph_id"], self.package["graph_id"])
        self.assertTrue(live["places"])
        for modes, groups, closed in (({"classes": "settle_only"}, {"classes": object()}, False), ({}, {}, False),
                                      ({"classes": "active"}, {}, False), ({"classes": "active"}, {"classes": object()}, True)):
            with self.subTest(modes=modes, groups=groups, closed=closed):
                self.assertFalse(LifeRuntime.read_academic_places(runtime(modes, groups, self.package["graph_id"], closed))["classes_enabled"])
        self.assertEqual(LifeRuntime.read_academic_places(runtime({}, {}, ""))["places"], [])


if __name__ == "__main__":
    unittest.main()
