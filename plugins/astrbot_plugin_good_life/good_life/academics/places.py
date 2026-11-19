"""Read-only class place options from the confirmed campus graph.

The course entry page lets the operator pick a timetable place by name. The
list comes from the latest confirmed package for ``academics_graph_id``; it
never reads a draft, a source export or a hard-coded campus table. Unnamed
road junctions and ordinary (non-enterable) buildings are left out.
"""
from ..graph_package_storage import GraphPackageStore
from ..graph_packages import ordinary_building_places

MAX_PLACES = 2000


def place_options(storage, graph_id, *, limit=MAX_PLACES):
    """Return ``(graph_ref, places)``; ``(None, [])`` when no graph is confirmed."""
    if type(graph_id) is not str or not graph_id.strip():
        return None, []
    package = GraphPackageStore(storage).latest(graph_id)
    if package is None:
        return None, []
    blocked = ordinary_building_places(package)
    places = []
    for node in package["nodes"]:
        label = node["name"].strip()
        if not label or node["place_id"] in blocked or node["type"] == "junction":
            continue
        places.append({"place_id": node["place_id"], "name": label, "type": node["type"]})
    places.sort(key=lambda item: (item["name"], item["place_id"]))
    return {"graph_id": package["graph_id"], "version": package["version"]}, places[:limit]
