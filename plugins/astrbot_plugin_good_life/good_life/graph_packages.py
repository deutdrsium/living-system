"""Opt-in home-area graph package validation and comparison; no I/O or runtime hooks."""
from __future__ import annotations

import copy
from datetime import datetime
from functools import wraps
import hashlib
from itertools import repeat
import json
import math
import re
import struct

from .graph_access import validate_access_fields

SCHEMA = "home-area.graph/v1"
ATTRIBUTION = "© OpenStreetMap contributors"
COLLECTIONS = {"nodes": "place_id", "edges": "edge_id", "gateways": "gateway_id"}
# road/building/facility back graph items; the rest are display-only basemap layers.
VECTOR_KINDS = {"road", "building", "facility", "landcover", "water", "waterway", "place"}
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_RECORDS = 100_000  # Every JSON object, including history snapshots.
MAX_PACKAGE_DEPTH = 64


def check_package_limits(value):
    """Bound compact UTF-8 JSON size, object count and depth without recursion."""
    size, records = 0, 0
    stack = [iter(((value, 1),))]
    while stack:
        try:
            item, depth = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if depth > MAX_PACKAGE_DEPTH:
            raise ValueError("graph package nesting depth exceeds limit")
        kind = type(item)
        if kind is dict or kind is list:
            size += 2 + max(0, len(item) - 1)
            if kind is dict:
                records += 1
                if records > MAX_PACKAGE_RECORDS:
                    raise ValueError("graph package record count exceeds limit")
                if any(type(key) is not str for key in item):
                    raise ValueError("package object keys must be strings")
                size += len(item)  # Colons; keys and values are visited below.
                children = (part for pair in item.items() for part in pair)
            else:
                children = iter(item)
            stack.append(zip(children, repeat(depth + 1)))
        elif kind is str:
            if len(item) > MAX_PACKAGE_BYTES - size:
                raise ValueError("graph package byte size exceeds limit")
            size += 2
            # Chunking avoids allocating an unbounded escaped/encoded string.
            for start in range(0, len(item), 4096):
                size += len(json.dumps(item[start:start + 4096], ensure_ascii=False).encode("utf-8")) - 2
                if size > MAX_PACKAGE_BYTES:
                    raise ValueError("graph package byte size exceeds limit")
        elif item is None or kind is bool:
            size += len(json.dumps(item))
        elif kind in (int, float):
            if abs(item) > 2**53 - 1 or not math.isfinite(item):
                raise ValueError("JSON numbers must be finite and within the safe integer range")
            size += len(json.dumps(item, allow_nan=False))
        else:
            raise ValueError("package must contain only JSON values")
        if size > MAX_PACKAGE_BYTES:
            raise ValueError("graph package byte size exceeds limit")


def _bounded_package(function):
    @wraps(function)
    def bounded(package):
        try:
            check_package_limits(package)
            return function(package)
        except RecursionError as error:
            raise ValueError("graph package nesting exceeds processing limit") from error
    return bounded


def decode_bounded_json(content):
    """Check stored/raw JSON bytes before parsing, then bound its JSON tree."""
    try:
        if type(content) not in (str, bytes) or len(content) > MAX_PACKAGE_BYTES:
            raise ValueError("graph package byte size exceeds limit")
        if type(content) is str:
            size = 0
            for start in range(0, len(content), 4096):
                size += len(content[start:start + 4096].encode("utf-8"))
                if size > MAX_PACKAGE_BYTES:
                    raise ValueError("graph package byte size exceeds limit")
        result = json.loads(content)
        check_package_limits(result)
        return result
    except RecursionError as error:
        raise ValueError("graph package nesting exceeds processing limit") from error


def _canonical(value):
    # IEEE754 tokens give Python and a dependency-free browser identical digests.
    if value is None or type(value) in (str, bool):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if type(value) in (int, float):
        if abs(value) > 2**53 - 1 or not math.isfinite(value):
            raise ValueError("JSON numbers must be finite and within the safe integer range")
        return "#" + struct.pack(">d", float(value) if value else 0.0).hex()
    if type(value) is list:
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if type(value) is dict and all(type(key) is str for key in value):
        return "{" + ",".join(_canonical(key) + ":" + _canonical(value[key]) for key in sorted(value)) + "}"
    raise ValueError("package must contain only JSON values with string object keys")


@_bounded_package
def package_digest(package):
    """SHA256 of typed canonical v1 content, excluding the top-level digest."""
    if type(package) is not dict:
        raise ValueError("graph package must be an object")
    return hashlib.sha256(_canonical({k: v for k, v in package.items() if k != "digest"}).encode("utf-8")).hexdigest()


@_bounded_package
def seal_package(package):
    result = copy.deepcopy(package)
    result["digest"] = package_digest(result)
    check_package_limits(result)
    return result


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return type(value) is str and bool(value.strip())


def _positive(value):
    return type(value) is int and value > 0


def _number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _timestamp(value):
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z", value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _coordinate(record):
    _require(record.get("coordinate_system") == "WGS84", "unknown coordinate system")
    _require(_number(record.get("lat"), -90, 90) and _number(record.get("lon"), -180, 180), "invalid WGS84 coordinates")


def _origin(record):
    _require(type(record.get("source")) is str and record["source"] in {"osm", "manual"}, "invalid item source")
    ids = record.get("osm_ids")
    _require(type(ids) is list and all(type(i) is str for i in ids), "osm_ids must be strings")
    _require(len(ids) == len(set(ids)), "osm_ids must be unique strings")
    _require(all(re.fullmatch(r"(node|way|relation)/[1-9][0-9]*", item) for item in ids), "invalid OSM element id")
    _require(record["source"] != "osm" or bool(ids), "OSM item requires an OSM id")
    _require(_positive(record.get("version")), "invalid item version")


@_bounded_package
def validate_package(package):
    """Return an owned validated copy; reject malformed, lossy or tampered input."""
    _require(type(package) is dict, "graph package must be an object")
    _canonical(package)  # Reject NaN, non-JSON types, non-string keys, huge numbers.
    required = {"schema", "graph_id", "version", "source", "coordinate_system", "attribution", "license",
                "bounds", "nodes", "edges", "gateways", "vector_data", "build_report", "edit_history", "digest"}
    _require(required <= package.keys(), "missing graph package fields")
    _require(package["schema"] == SCHEMA, "unsupported graph schema")
    _require(_text(package["graph_id"]) and _positive(package["version"]), "invalid graph identity/version")
    _require(package["source"] == "osm" and package["coordinate_system"] == "WGS84", "invalid graph source/coordinate system")
    _require(package["attribution"] == ATTRIBUTION and package["license"] == "ODbL-1.0", "OSM attribution/license required")
    bounds = package["bounds"]
    _require(type(bounds) is list and len(bounds) == 4, "bounds must be south,west,north,east")
    s, w, n, e = bounds
    _require(_number(s, -90, 90) and _number(n, -90, 90) and s < n
             and _number(w, -180, 180) and _number(e, -180, 180) and w < e, "invalid bounds")
    indexed = {}
    for collection, key in COLLECTIONS.items():
        records = package[collection]
        _require(type(records) is list, f"{collection} must be an array")
        index = {}
        for record in records:
            _require(type(record) is dict and _text(record.get(key)), f"invalid {key}")
            _require(record[key] not in index, f"duplicate {key}")
            _origin(record)
            _require(record["version"] <= package["version"], "item version exceeds package version")
            index[record[key]] = record
        indexed[collection] = index
    _require(bool(indexed["nodes"]), "graph requires at least one node")
    for node in indexed["nodes"].values():
        validate_access_fields(node, node=True, package_record=True)
        _coordinate(node)
        _require(type(node.get("name")) is str and _text(node.get("type")), "invalid place name/type")
        _require(type(node.get("capabilities")) is list and all(_text(v) for v in node["capabilities"]), "invalid capabilities")
    for edge in indexed["edges"].values():
        validate_access_fields(edge, package_record=True)
        _require(_text(edge.get("from")) and _text(edge.get("to")) and edge["from"] in indexed["nodes"] and edge["to"] in indexed["nodes"] and edge["from"] != edge["to"], "dangling/self edge")
        _require(type(edge.get("direction")) is str and edge["direction"] in {"both", "forward"}, "invalid edge direction")
        _require(_number(edge.get("length_m"), 0, 1e8), "invalid edge distance")
        _require(type(edge.get("estimated_duration_s")) is int and edge["estimated_duration_s"] > 0, "walking time must be positive integer seconds")
        _require(edge.get("estimated") is True and type(edge.get("estimate")) is dict and _text(edge["estimate"].get("model")), "estimate assumptions required")
        _require(type(edge.get("name")) is str, "invalid edge name")
        _require("load" in edge and (edge["load"] is None or type(edge["load"]) is str), "invalid edge load")
    for gateway in indexed["gateways"].values():
        _coordinate(gateway)
        _require(_text(gateway.get("place_id")) and gateway["place_id"] in indexed["nodes"], "dangling gateway")
        _require(type(gateway.get("modes")) is list and bool(gateway["modes"]) and all(_text(v) for v in gateway["modes"]), "invalid gateway modes")
    _require(type(package["vector_data"]) is list, "invalid vector data")
    for feature in package["vector_data"]:
        _require(type(feature) is dict and type(feature.get("kind")) is str and feature["kind"] in VECTOR_KINDS, "invalid vector feature")
        _require(type(feature.get("coordinates")) is list and len(feature["coordinates"]) >= 1, "invalid vector geometry")
        for point in feature["coordinates"]:
            _require(type(point) is list and len(point) == 2 and _number(point[0], -180, 180) and _number(point[1], -90, 90), "invalid vector coordinates")
    report = package["build_report"]
    _require(type(report) is dict and type(report.get("counts")) is dict
             and type(report.get("dropped")) is dict, "invalid build report")
    for mapping in (report["counts"], report["dropped"]):
        _require(all(type(value) is int and value >= 0 for value in mapping.values()), "invalid build report counts")
    _require({"nodes", "edges", "components"} <= report["counts"].keys(), "missing build report counts")
    for field in ("isolated_nodes", "unnamed_places", "missing_access", "missing_opening_information"):
        _require(type(report.get(field)) is list and all(_text(v) for v in report[field]), "invalid build report item list")
    for field in ("components", "isolated_components"):
        _require(type(report.get(field)) is list and all(type(group) is list and all(_text(v) for v in group)
                 for group in report[field]), "invalid build report components")
    _require(type(package["edit_history"]) is list, "invalid edit history")
    for edit in package["edit_history"]:
        _require(type(edit) is dict and _positive(edit.get("version")) and edit["version"] <= package["version"]
                 and _text(edit.get("action")) and _timestamp(edit.get("at")), "invalid edit history entry")
        changes = edit.get("changes", [])
        _require(type(changes) is list, "invalid edit changes")
        for change in changes:
            _require(type(change) is dict and type(change.get("collection")) is str
                     and change["collection"] in COLLECTIONS and _text(change.get("id")), "invalid edit change identity")
            key = COLLECTIONS[change["collection"]]
            for field in ("before", "after"):
                _require(field in change and (change[field] is None or
                         (type(change[field]) is dict and change[field].get(key) == change["id"])), "invalid edit change record")
    assert_stable_places(package, package)
    _require(package["digest"] == package_digest(package), "graph package digest mismatch")
    return copy.deepcopy(package)


def diff_packages(current, proposed):
    """Validate both inputs, compare versions, and enforce stable OSM place IDs."""
    new = validate_package(proposed)
    old = validate_package(current) if current is not None else None
    if old:
        _require(old["graph_id"] == new["graph_id"], "graph identity changed")
        # Retain aliases across *all* accepted history at the storage boundary too.
        assert_stable_places(old, new)
    relation = "initial" if old is None else ("newer" if new["version"] > old["version"] else
               "same" if new["version"] == old["version"] else "older")
    summary = {"graph_id": new["graph_id"], "from_version": old["version"] if old else None,
               "to_version": new["version"], "version_comparison": relation,
               "current_digest": old["digest"] if old else None, "proposed_digest": new["digest"]}
    for collection, key in COLLECTIONS.items():
        before = {r[key]: r for r in old[collection]} if old else {}
        after = {r[key]: r for r in new[collection]}
        summary[collection] = {"added": sorted(after.keys() - before.keys()), "removed": sorted(before.keys() - after.keys()),
                               "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}
    summary["metadata_changed"] = sorted(k for k in new if k not in {*COLLECTIONS, "digest", "version"}
                                         and (old is None or old.get(k) != new[k]))
    summary["can_import"] = relation in {"initial", "newer"}
    return summary


def assert_stable_places(old, new):
    def identities(package):
        by_osm, by_place = {}, {}
        for node in package["nodes"]:
            # A feature identity is exact; a way referenced by multiple road nodes
            # is never used as their identity (builder assigns node/* identities).
            identity = tuple(sorted(node["osm_ids"]))
            by_place[node["place_id"]] = identity
            if identity:
                _require(identity not in by_osm, "duplicate OSM place identity")
                by_osm[identity] = node["place_id"]
        return by_osm, by_place
    old_osm, old_places = identities(old)
    new_osm, new_places = identities(new)
    _require(all(new_osm[k] == old_osm[k] for k in old_osm.keys() & new_osm.keys()), "stable place_id changed for OSM identity")
    _require(all(old_places[k] == new_places[k] for k in old_places.keys() & new_places.keys()), "place_id reused for another OSM identity")



def ordinary_building_places(package):
    """Identify non-enterable unnamed buildings without changing the confirmed package."""
    building_ids = {osm_id for feature in package["vector_data"] if feature["kind"] == "building"
                    for osm_id in feature.get("osm_ids", ())}
    return frozenset(node["place_id"] for node in package["nodes"]
                     if node["type"] == "普通建筑" or node["name"].strip() == "普通建筑"
                     or (not node["name"].strip() and (node["type"] == "building"
                         or node.get("tags", {}).get("building") or building_ids.intersection(node["osm_ids"]))))
