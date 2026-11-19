"""Build a deterministic walking graph from a bounded cached OSM extraction."""
from __future__ import annotations

from collections import Counter
import copy
import math
import re

from .package_api import ATTRIBUTION, SCHEMA, diff_packages, seal_package, validate_package
from .source import basemap_kind, validate_bounds
from good_life.graph_access import access_requirement, opening_schedule

DEFAULT_ROADS = {"footway", "pedestrian", "path", "steps", "residential", "living_street", "service", "unclassified", "tertiary", "secondary", "primary", "track"}
ROAD_SPEEDS = {"steps": 0.65, "path": 1.1, "track": 1.0, "footway": 1.4, "pedestrian": 1.4}
PEDESTRIAN_ROADS = {"footway", "path", "pedestrian", "steps"}
WALK_DIRECTIONS = ("forward", "backward")
WALK_DIRECTIONAL_TAGS = {f"{mode}:{direction}" for mode in ("foot", "access") for direction in WALK_DIRECTIONS}
WALK_ACCESS_CONDITIONAL_TAGS = {f"{tag}:conditional" for tag in WALK_DIRECTIONAL_TAGS | {"foot", "access"}}
WALK_DIRECTIONAL_KEY = re.compile(r"(?:access|foot)(?::[^:]+)*:(?:forward|backward)(?::conditional)?")
WALK_CONDITIONAL_KEY = re.compile(r"^(access|foot)(:[^:]+)*:conditional$")
WALK_ONEWAY_KEY = re.compile(r"oneway(?::.+)?")
RAW_CONDITIONAL_KEY = re.compile(r"[^:\s]+(?::[^:\s]+)*:conditional")
WALK_ONEWAY = {"yes": "forward", "1": "forward", "true": "forward", "-1": "backward",
               "no": "both", "0": "both", "false": "both"}
# Display-only layers keep just the tags the editor styles or labels.
BASEMAP_TAGS = {"name", "name:zh", "landuse", "natural", "leisure", "water", "waterway", "place", "intermittent",
                "tunnel", "layer", "amenity"}


def _basemap_tags(tags):
    return {k: v for k, v in sorted(tags.items()) if k in BASEMAP_TAGS}


def _clip_polygon(ring, bounds):
    """Sutherland-Hodgman against the bbox; returns a closed ring or []."""
    s, w, n, e = bounds
    points = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    edges = ((0, w, True), (0, e, False), (1, s, True), (1, n, False))
    for axis, limit, lower in edges:
        if not points:
            break
        inside = (lambda p: p[axis] >= limit) if lower else (lambda p: p[axis] <= limit)
        result = []
        for index, current in enumerate(points):
            previous = points[index - 1]
            if inside(current) != inside(previous):
                t = (limit - previous[axis]) / (current[axis] - previous[axis])
                cut = [previous[0] + t * (current[0] - previous[0]), previous[1] + t * (current[1] - previous[1])]
                cut[axis] = limit
                result.append([round(cut[0], 7), round(cut[1], 7)])
            if inside(current):
                result.append(current)
        points = result
    return points + [points[0]] if len(points) >= 3 else []


def _clip_line(coords, bounds):
    """Liang-Barsky per segment; returns runs inside the bbox without bridging gaps."""
    s, w, n, e = bounds
    runs, run = [], []
    for a, b in zip(coords, coords[1:]):
        t0, t1, dx, dy = 0.0, 1.0, b[0] - a[0], b[1] - a[1]
        keep = True
        for p, q in ((-dx, a[0] - w), (dx, e - a[0]), (-dy, a[1] - s), (dy, n - a[1])):
            if p == 0:
                keep = keep and q >= 0
            elif p < 0:
                t0 = max(t0, q / p)
            else:
                t1 = min(t1, q / p)
        if not keep or t0 > t1:
            if len(run) > 1:
                runs.append(run)
            run = []
            continue
        start = a if t0 == 0 else [round(a[0] + t0 * dx, 7), round(a[1] + t0 * dy, 7)]
        end = b if t1 == 1 else [round(a[0] + t1 * dx, 7), round(a[1] + t1 * dy, 7)]
        if not run or run[-1] != start:
            if len(run) > 1:
                runs.append(run)
            run = [start]
        run.append(end)
        if t1 < 1:
            runs.append(run)
            run = []
    if len(run) > 1:
        runs.append(run)
    return runs


def _basemap_features(way, kind, source_nodes, outside, bounds):
    refs = way["refs"]
    coords = []
    for ref in refs:
        if ref in source_nodes:
            coords.append([source_nodes[ref]["lon"], source_nodes[ref]["lat"]])
        elif ref in outside:
            coords.append(list(outside[ref]))
        else:
            return None  # Unknown vertex: never invent a shape.
    complete = all(ref in source_nodes for ref in refs)
    base = {"kind": kind, "osm_ids": [f'way/{way["id"]}'], "tags": _basemap_tags(way["tags"])}
    if kind == "waterway":
        runs = [coords] if complete else _clip_line(coords, bounds)
        return [{**base, "coordinates": run} for run in runs if len(run) > 1]
    if len(refs) < 4 or refs[0] != refs[-1]:
        return None
    ring = coords if complete else _clip_polygon(coords, bounds)
    return [{**base, "coordinates": ring}] if ring else []


def distance_m(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2 - lat1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(h)))


def walking_time(length_m, road_type, speed_m_s=1.4):
    if not math.isfinite(length_m) or length_m < 0 or not math.isfinite(speed_m_s) or speed_m_s <= 0:
        raise ValueError("distance must be nonnegative and walking speed positive")
    speed = speed_m_s * ROAD_SPEEDS.get(road_type, 1.4) / 1.4
    return max(1, math.ceil(length_m / speed)), {"model": "walking/v1", "base_speed_m_s": speed_m_s,
                                               "road_type": road_type, "effective_speed_m_s": speed}


def _feature_kind(tags, roads, buildings, facilities):
    if tags.get("highway") in roads:
        return "road"
    if tags.get("building") and ("*" in buildings or tags["building"] in buildings):
        return "building"
    if any(tags.get(k) and ("*" in facilities or tags[k] in facilities or f"{k}:{tags[k]}" in facilities) for k in ("amenity", "shop", "leisure")):
        return "facility"
    return None


def _walking_access(tags):
    return tags.get("foot", tags.get("access"))


def _walking_conditionals(tags, *, road=False):
    keys = WALK_ACCESS_CONDITIONAL_TAGS
    if road:
        keys = keys | {"oneway:foot:conditional"} | {
            key for key in tags if key.endswith(":conditional")
            and re.fullmatch(r"[^:\s]+(?::[^:\s]+)*:(?:forward|backward)(?::[^:\s]+)*:conditional", key)
        }
        if tags["highway"] in PEDESTRIAN_ROADS and "oneway:foot" not in tags:
            keys.add("oneway:conditional")
    return {key: tags[key] for key in sorted(keys & tags.keys())}


def _way_directions(tags, direction):
    """Return allowed travel in OSM node order and raw evidence for review."""
    conditionals = _walking_conditionals(tags, road=True)
    evidence = {key: tags[key] for key in sorted(WALK_DIRECTIONAL_TAGS & tags.keys())}
    unsupported = False
    # Generic one-way tags follow the established pedestrian fallback. Other
    # suffixes have no such interpretation and must be reviewed before use.
    interpreted = {"oneway", "oneway:foot", "oneway:conditional"}
    for key in sorted(tags.keys() - evidence.keys() - conditionals.keys() - interpreted):
        if (WALK_DIRECTIONAL_KEY.fullmatch(key) or WALK_CONDITIONAL_KEY.fullmatch(key)
                or WALK_ONEWAY_KEY.fullmatch(key)):
            unsupported = True
            if RAW_CONDITIONAL_KEY.fullmatch(key):
                conditionals[key] = tags[key]
            else:
                evidence[key] = tags[key]
    walking_oneway = WALK_ONEWAY.get(direction)
    if walking_oneway is None:
        key = "oneway:foot" if "oneway:foot" in tags else "oneway"
        evidence[key] = tags[key]
    if unsupported or walking_oneway is None or conditionals.keys() - {"foot:conditional", "access:conditional"}:
        # A shared edge cannot safely infer direction from these tags.
        return set(), evidence, conditionals
    allowed = set(WALK_DIRECTIONS if walking_oneway == "both" else (walking_oneway,))
    for key, value in evidence.items():
        # Leave credentialed/unknown exact directional tags closed in that direction.
        if access_requirement({"access": value}) != "public":
            allowed.discard(key.rsplit(":", 1)[1])
    return allowed, evidence, conditionals


def _place(osm_id, coordinate, tags, kind, version, *, structured_access):
    access = _walking_access(tags) if structured_access else tags.get("access")
    place = {"place_id": "osm:" + osm_id, "name": tags.get("name", ""), "type": kind, "region": None,
             "lat": coordinate["lat"], "lon": coordinate["lon"], "coordinate_system": "WGS84",
             "opening_rules": tags.get("opening_hours"), "capabilities": [], "access": access,
             "source": "osm", "osm_ids": [osm_id], "version": version, "tags": dict(tags)}
    if structured_access and (conditionals := _walking_conditionals(tags)):
        place["access_conditionals"] = conditionals
    return place


_MISSING = object()
_EDGE_METADATA = {"edge_id", "source", "version", "osm_ids", "access_directional", "osm_access_conflicts"}
_EDGE_ACCESS_FIELDS = {"access", "access_requirement", "access_area"}
_EDGE_OPENING_FIELDS = {"availability", "opening_schedule"}
_EDGE_TRAVERSAL_FIELDS = ("from", "to", "direction")


def _edge_overrides(previous, changes):
    """Identify surviving field edits from editor snapshots, including explicit removals."""
    edits = {}
    original = None
    for action, before, after in changes:
        if before is None or after is None:
            if before is None and action != "undo":
                edits.clear()
                original = None
            continue
        if original is None:
            original = before
        fields = (before.keys() | after.keys()).difference(_EDGE_METADATA, _EDGE_TRAVERSAL_FIELDS)
        if type(before.get("enabled")) is not bool or type(after.get("enabled")) is not bool:
            # A partial snapshot cannot confirm that an older re-enable still applies.
            edits.pop("enabled", None)
            fields.discard("enabled")
        if "access" not in before or "access" not in after:
            fields -= _EDGE_ACCESS_FIELDS | {"access_conditionals"}
        for field in fields:
            old, new = before.get(field, _MISSING), after.get(field, _MISSING)
            if old == new:
                continue
            history = edits.setdefault(field, [])
            if action == "undo" and history and history[-1] == (new, old):
                history.pop()
            else:
                history.append((old, new))
    overrides = {}
    conflicts = previous.get("osm_access_conflicts")
    conflicts = conflicts if isinstance(conflicts, dict) else {}
    for field, history in edits.items():
        if not history:
            continue
        value = history[-1][1]
        current = previous.get(field, _MISSING)
        # A prior regeneration can merge conditional evidence or add review metadata.
        if field == "access_conditionals" and isinstance(conflicts.get(field), dict):
            current = conflicts[field].get("previous", current)
        elif field == "estimate" and conflicts and isinstance(value, dict) and dict(value, needs_review=True) == current:
            current = value
        if value == current:
            overrides[field] = value
    if original is None or original.get("source") != "osm":
        # Older packages may lack the snapshots that explain a manual marker.
        # Keep display edits and identified manual estimates without inferring an access override.
        for field in ("name", "load"):
            overrides.setdefault(field, previous[field])
        if previous["estimate"]["model"].startswith("manual-"):
            for field in ("estimate", "estimated_duration_s"):
                overrides.setdefault(field, previous[field])
    return overrides, original or {}


def _edge_access_conflicts(fresh, previous, overrides):
    osm = {field: fresh[field] for field in _EDGE_ACCESS_FIELDS if field in fresh}
    prior = {field: previous[field] for field in _EDGE_ACCESS_FIELDS if field in previous}
    old_conflict = previous.get("osm_access_conflicts")
    if isinstance(old_conflict, dict) and old_conflict.get("osm") == osm:
        # Retain the other side of an existing conflict on repeat builds, while
        # updating it for any explicitly edited fields.
        retained = old_conflict.get("previous")
        if isinstance(retained, dict) and retained.keys() <= _EDGE_ACCESS_FIELDS:
            prior = {**{k: v for k, v in retained.items() if k not in overrides},
                     **{k: v for k, v in prior.items() if k in overrides}}
    return {"previous": prior, "osm": osm} if prior != osm else {}


def _preserve_restrictive_access(merged, previous, fresh):
    """Use the restrictive side of an unclear edit, or close when incomparable."""
    prior = {field: previous[field] for field in _EDGE_ACCESS_FIELDS if field in previous}
    osm = {field: fresh[field] for field in _EDGE_ACCESS_FIELDS if field in fresh}
    if prior == osm:
        return False
    old_rule, new_rule = access_requirement(prior), access_requirement(osm)
    if new_rule == "public" or old_rule == "closed":
        for field in _EDGE_ACCESS_FIELDS:
            merged.pop(field, None)
        merged.update(prior)
        return False
    return old_rule != "public" and new_rule != "closed"


def _directed_pairs(edge):
    pair = (edge["from"], edge["to"])
    return {pair, pair[::-1]} if edge["direction"] == "both" else {pair}


def _complete_edge_history(original, changes, field):
    return original.get("source") == "osm" and all(
        before is not None and after is not None and field in before and field in after
        for _, before, after in changes)


def _merge_manual_edge(fresh, previous, changes, *, hard_closed):
    overrides, original = _edge_overrides(previous, changes)
    opening_history_known = _complete_edge_history(original, changes, "availability") and all(
        changes[-1][2].get(field, _MISSING) == previous.get(field, _MISSING)
        for field in _EDGE_OPENING_FIELDS)
    if not opening_history_known:
        for field in _EDGE_OPENING_FIELDS:
            overrides.pop(field, None)
    merged = dict(fresh, source="manual", version=previous["version"])
    for field in overrides.keys() - {"access_conditionals", "enabled"}:
        if overrides[field] is _MISSING:
            merged.pop(field, None)
        else:
            merged[field] = copy.deepcopy(overrides[field])
    if "estimate" in overrides and previous["estimate"]["model"] in {"manual-retained/v1", "manual-merged/v1"}:
        merged["estimated_duration_s"] = previous["estimated_duration_s"]

    conflicts = _edge_access_conflicts(fresh, previous, overrides)
    ambiguous = not _complete_edge_history(original, changes, "access")
    if ambiguous and not overrides.keys() & _EDGE_ACCESS_FIELDS:
        hard_closed |= _preserve_restrictive_access(merged, previous, fresh)
    prior_pairs, fresh_pairs = _directed_pairs(previous), _directed_pairs(fresh)
    shared_pairs = prior_pairs & fresh_pairs
    if prior_pairs != fresh_pairs:
        conflicts["direction"] = {label: {field: edge[field] for field in _EDGE_TRAVERSAL_FIELDS}
                                  for label, edge in (("previous", previous), ("osm", fresh))}
    if len(shared_pairs) == 1:
        merged["from"], merged["to"] = next(iter(shared_pairs))
        merged["direction"] = "forward"
    elif not shared_pairs:
        # Keep the old endpoints visible for review when neither direction is usable.
        merged.update((field, previous[field]) for field in _EDGE_TRAVERSAL_FIELDS)
    osm_conditions = fresh.get("access_conditionals", {})
    if "access_conditionals" in overrides or (ambiguous and previous.get("access_conditionals")):
        edit = overrides.get("access_conditionals", previous.get("access_conditionals", {}))
        manual_conditions = {} if edit is _MISSING else edit
        if manual_conditions != osm_conditions:
            conflicts["access_conditionals"] = {"previous": manual_conditions, "osm": osm_conditions}
            hard_closed |= ambiguous and any(manual_conditions[key] != osm_conditions[key]
                                             for key in manual_conditions.keys() & osm_conditions.keys())
        if combined := {**manual_conditions, **osm_conditions}:
            merged["access_conditionals"] = combined
    if not opening_history_known:
        old_opening, new_opening = opening_schedule(previous), opening_schedule(fresh)
        if old_opening != new_opening:
            always_open = opening_schedule({"availability": "24/7"})
            if new_opening == always_open:
                merged["availability"] = previous["availability"]
                if "opening_schedule" in previous:
                    merged["opening_schedule"] = previous["opening_schedule"]
                conflicts["opening_schedule"] = {"previous": old_opening, "osm": new_opening}
            elif "opening_schedule" in previous or (old_opening != always_open and previous["availability"] is not None):
                hard_closed = True
                conflicts["opening_schedule"] = {"previous": old_opening, "osm": new_opening}
    if "availability" in overrides and previous["availability"] != fresh["availability"]:
        conflicts["availability"] = {"previous": previous["availability"], "osm": fresh["availability"]}
    elif "opening_schedule" in overrides and "availability" in original and fresh["availability"] != original["availability"]:
        conflicts["availability"] = {"previous": original["availability"], "osm": fresh["availability"]}

    # A stored disable survives refresh, including when no edit snapshot exists.
    # Only a recorded re-enable may override a raw way disable, subject to the
    # merged access rule and all endpoint and direction restrictions.
    explicitly_enabled = overrides.get("enabled") is True
    if previous["enabled"] != fresh["enabled"]:
        conflicts["enabled"] = {"previous": previous["enabled"], "osm": fresh["enabled"]}
    merged["enabled"] = (previous["enabled"] and (fresh["enabled"] or explicitly_enabled)
                         and not hard_closed and bool(shared_pairs) and access_requirement(merged) != "closed")
    if conflicts:
        merged["osm_access_conflicts"] = conflicts
        merged["estimate"] = dict(merged["estimate"], needs_review=True)
    return merged


def graph_report(nodes, edges, dropped, dropped_items=None):
    adjacency = {n["place_id"]: set() for n in nodes}
    for edge in edges:
        if edge["enabled"]:
            adjacency[edge["from"]].add(edge["to"])
            adjacency[edge["to"]].add(edge["from"])
    components, unseen = [], set(adjacency)
    for start in sorted(adjacency):
        if start not in unseen:
            continue
        pending, group = [start], []
        unseen.remove(start)
        while pending:
            current = pending.pop()
            group.append(current)
            for neighbor in sorted(adjacency[current] & unseen):
                unseen.remove(neighbor)
                pending.append(neighbor)
        components.append(sorted(group))
    components.sort(key=lambda group: (-len(group), group))
    review_edges = sorted(e["edge_id"] for e in edges if "osm_access_conflicts" in e)
    return {**({"edges_needing_review": review_edges} if review_edges else {}),
            "counts": {"nodes": len(nodes), "edges": len(edges), "components": len(components)},
            "dropped": dict(sorted(dropped.items())), "dropped_items": list(dropped_items or []), "components": components,
            "isolated_components": components[1:], "isolated_nodes": sorted(k for k, v in adjacency.items() if not v),
            "unnamed_places": sorted(n["place_id"] for n in nodes if not n["name"]),
            "missing_access": sorted(n["place_id"] for n in nodes if n["access"] is None)
                              + sorted(e["edge_id"] for e in edges if e["access"] is None),
            "missing_opening_information": sorted(n["place_id"] for n in nodes if n["opening_rules"] is None),
            "unparsed_access": sorted(r.get('place_id', r.get('edge_id')) for r in (*nodes, *edges)
                                      if {'access_conditionals', 'access_directional', 'osm_access_conflicts'} & r.keys()
                                      or access_requirement(r) == 'unparsed'),
            "unparsed_opening": sorted(r.get('place_id', r.get('edge_id')) for r, is_node in
                                       [(n, True) for n in nodes] + [(e, False) for e in edges]
                                       if opening_schedule(r, node=is_node)['status'] == 'unparsed'),
            "connectivity": "weak components of enabled edges; does not prove legal access or reachability"}


def build_graph(data, bounds, *, graph_id, version=1, roads=None, buildings=None, facilities=None, speed_m_s=1.4, previous=None, structured_access=False):
    bounds = validate_bounds(bounds)
    if type(version) is not int or version <= 0 or type(graph_id) is not str or not graph_id.strip():
        raise ValueError("graph id and positive integer version required")
    walking_time(1, "footway", speed_m_s)
    roads = DEFAULT_ROADS if roads is None else set(roads)
    buildings = {"*"} if buildings is None else set(buildings)
    facilities = {"*"} if facilities is None else set(facilities)
    source_nodes, nodes, edges, gateways, vectors = data["nodes"], {}, [], [], []
    hard_closed_edges = set()
    # Caches made before basemap layers existed lack outside vertices; areas crossing
    # the bbox edge are then skipped until the user rebuilds the cache with --refresh.
    outside, basemap = data.get("basemap_nodes", {}), []
    basemap_skipped = 0
    dropped = Counter(data.get("dropped", {}))
    dropped_items = []

    def add_basemap(way):
        nonlocal basemap_skipped
        kind = basemap_kind(way["tags"])
        if kind is None:
            return
        features = _basemap_features(way, kind, source_nodes, outside, bounds)
        if features is None:
            basemap_skipped += 1
        else:
            basemap.extend(features)

    def drop(reason, way_id, **details):
        dropped[reason] += 1
        dropped_items.append({"osm_id": f"way/{way_id}", "reason": reason, **details})

    def road_node(ref):
        identity = f"node/{ref}"
        place_id = "osm:" + identity
        if place_id not in nodes:
            source = source_nodes[ref]
            tags = source["tags"]
            nodes[place_id] = _place(identity, source, tags, tags.get("amenity", "junction"), version,
                                     structured_access=structured_access)
        return place_id

    for way in sorted(data["ways"], key=lambda w: int(w["id"])):
        tags, refs = way["tags"], way["refs"]
        kind = _feature_kind(tags, roads, buildings, facilities)
        if kind is None:
            drop("filtered_ways", way["id"])
            add_basemap(way)
            continue
        coords = [[source_nodes[r]["lon"], source_nodes[r]["lat"]] for r in refs if r in source_nodes]
        if not coords:
            drop("missing_way_geometry", way["id"])
            continue
        # Split road vectors at missing refs rather than bridging holes/bbox borders.
        if kind == "road":
            runs, run = [], []
            for ref in refs:
                if ref in source_nodes:
                    run.append([source_nodes[ref]["lon"], source_nodes[ref]["lat"]])
                else:
                    if run:
                        runs.append(run)
                    run = []
            if run:
                runs.append(run)
            for run in runs:
                vectors.append({"kind": kind, "osm_ids": [f'way/{way["id"]}'], "coordinates": run, "tags": dict(tags)})
            if tags.get("area") == "yes" or (tags.get("foot") == "no" and not structured_access):
                drop("non_walkable_ways", way["id"])
                continue
            pedestrian = tags["highway"] in PEDESTRIAN_ROADS
            direction = tags.get("oneway:foot", tags.get("oneway", "no") if pedestrian else "no")
            if structured_access:
                allowed, directional, conditionals = _way_directions(tags, direction)
                reverse = allowed == {"backward"}
                directed = len(allowed) == 1
            else:
                conditionals = {}
                reverse = direction == "-1"
                directed = direction in {"yes", "1", "-1", "true"}
            for index, (a, b) in enumerate(zip(refs, refs[1:])):
                if a not in source_nodes or b not in source_nodes:
                    drop("missing_or_outside_segments", way["id"], segment_index=index, refs=[a, b])
                    continue
                length = distance_m(source_nodes[a], source_nodes[b])
                if a == b or length < 0.01:
                    drop("degenerate_segments", way["id"], segment_index=index, refs=[a, b])
                    continue
                start, end = road_node(a), road_node(b)
                if reverse:
                    start, end = end, start
                duration, estimate = walking_time(length, tags["highway"], speed_m_s)
                access = _walking_access(tags)
                endpoint_access = [_walking_access(source_nodes[r]["tags"]) for r in (a, b)]
                # Preserve legacy disabled-private topology unless explicitly opted in.
                blocked_values = {"no"} if structured_access else {"no", "private"}
                edge_id = f'osm:way/{way["id"]}:{a}:{b}:{index}'
                endpoint_closed = any(v in blocked_values for v in endpoint_access)
                if structured_access and (not allowed or endpoint_closed):
                    hard_closed_edges.add(edge_id)
                restricted = access in blocked_values or endpoint_closed or edge_id in hard_closed_edges
                edge = {"edge_id": edge_id, "from": start, "to": end,
                        "name": tags.get("name", ""), "direction": "forward" if directed else "both",
                        "length_m": round(length, 3), "estimated_duration_s": duration, "estimated": True, "estimate": estimate,
                        "load": None, "access": access, "availability": tags.get("opening_hours"), "enabled": not restricted,
                        "source": "osm", "osm_ids": [f'way/{way["id"]}'], "version": version}
                if structured_access and directional:
                    edge["access_directional"] = dict(directional)
                if conditionals:
                    edge["access_conditionals"] = dict(conditionals)
                edges.append(edge)
        else:
            # Do not invent a polygon when its regional boundary is incomplete.
            if any(r not in source_nodes for r in refs):
                drop("incomplete_area_geometry", way["id"])
                if kind == "facility":
                    add_basemap(way)  # e.g. a park crossing the bbox edge: colour only, no place.
                continue
            vectors.append({"kind": kind, "osm_ids": [f'way/{way["id"]}'], "coordinates": coords, "tags": dict(tags)})
            unique = list(dict.fromkeys(refs))
            point = {"lat": sum(source_nodes[r]["lat"] for r in unique) / len(unique),
                     "lon": sum(source_nodes[r]["lon"] for r in unique) / len(unique)}
            node = _place(f'way/{way["id"]}', point, tags, tags.get("building", tags.get("amenity", kind)), version,
                          structured_access=structured_access)
            nodes[node["place_id"]] = node
    for ref, source in sorted(source_nodes.items(), key=lambda pair: int(pair[0])):
        tags = source["tags"]
        kind = _feature_kind(tags, set(), buildings, facilities)
        gateway = tags.get("entrance") not in (None, "no") or tags.get("barrier") in {"gate", "lift_gate", "entrance"}
        if kind or gateway:
            place_id = road_node(ref)
            nodes[place_id]["type"] = tags.get("amenity", tags.get("shop", tags.get("leisure", "entrance" if gateway else kind)))
            if kind:
                vectors.append({"kind": kind, "osm_ids": [f"node/{ref}"], "coordinates": [[source["lon"], source["lat"]]], "tags": dict(tags)})
            if gateway:
                gateways.append({"gateway_id": f"osm:gateway/node/{ref}", "place_id": place_id, "lat": source["lat"], "lon": source["lon"],
                                 "coordinate_system": "WGS84", "modes": ["walk"], "source": "osm", "osm_ids": [f"node/{ref}"], "version": version})
        if tags.get("place") and (tags.get("name") or tags.get("name:zh")):
            basemap.append({"kind": "place", "osm_ids": [f"node/{ref}"], "coordinates": [[source["lon"], source["lat"]]],
                            "tags": _basemap_tags(tags)})
    # Appended after graph-derived vectors so their order (and graph IDs) never change.
    vectors.extend(basemap)
    package = {"schema": SCHEMA, "graph_id": graph_id, "version": version, "source": "osm", "coordinate_system": "WGS84",
               "attribution": ATTRIBUTION, "license": "ODbL-1.0", "bounds": bounds,
               "nodes": sorted(nodes.values(), key=lambda n: n["place_id"]), "edges": edges, "gateways": gateways,
               "vector_data": vectors, "build_report": {}, "edit_history": []}
    if previous:
        old = validate_package(previous)
        if old["graph_id"] != graph_id or version <= old["version"]:
            raise ValueError("regeneration needs the same graph id and a newer version")
        aliases = {tuple(n["osm_ids"]): n["place_id"] for n in old["nodes"] if n["osm_ids"]}
        remap = {n["place_id"]: aliases.get(tuple(n["osm_ids"]), n["place_id"]) for n in package["nodes"]}
        for node in package["nodes"]:
            node["place_id"] = remap[node["place_id"]]
        for edge in package["edges"]:
            edge["from"], edge["to"] = remap[edge["from"]], remap[edge["to"]]
        for gateway in package["gateways"]:
            gateway["place_id"] = remap[gateway["place_id"]]
        package["build_report"] = graph_report(package["nodes"], package["edges"], dropped, dropped_items)
        raw_diff = diff_packages(old, seal_package(package))
        tombstones, edge_changes = {}, {}
        for edit in old["edit_history"]:
            for change in edit.get("changes", []):
                tombstones[(change["collection"], change["id"])] = change["after"] is None
                if structured_access and change["collection"] == "edges":
                    edge_changes.setdefault(change["id"], []).append((edit["action"], change["before"], change["after"]))
        for collection, key in (("nodes", "place_id"), ("edges", "edge_id"), ("gateways", "gateway_id")):
            records = {r[key]: r for r in package[collection]}
            for record in old[collection]:
                if record["source"] == "manual":
                    item_id = record[key]
                    fresh = records.get(item_id)
                    if structured_access and collection == "edges" and fresh is not None:
                        records[item_id] = _merge_manual_edge(fresh, record, edge_changes.get(item_id, ()),
                                                              hard_closed=item_id in hard_closed_edges)
                    else:
                        records[item_id] = copy.deepcopy(record)
            for (deleted_collection, item_id), deleted in tombstones.items():
                if deleted and deleted_collection == collection:
                    records.pop(item_id, None)
            package[collection] = sorted(records.values(), key=lambda r: r[key])
        deleted_nodes = {item_id for (collection, item_id), deleted in tombstones.items() if collection == "nodes" and deleted}
        package["edges"] = [e for e in package["edges"] if e["from"] not in deleted_nodes and e["to"] not in deleted_nodes]
        package["gateways"] = [g for g in package["gateways"] if g["place_id"] not in deleted_nodes]
        # Preserve old endpoints needed by manual edges/gateways, even if OSM removed them.
        nodes = {n["place_id"]: n for n in package["nodes"]}
        old_nodes = {n["place_id"]: n for n in old["nodes"]}
        required = {e[k] for e in package["edges"] for k in ("from", "to")} | {g["place_id"] for g in package["gateways"]}
        for place_id in required - nodes.keys():
            nodes[place_id] = copy.deepcopy(old_nodes[place_id])
        package["nodes"] = sorted(nodes.values(), key=lambda n: n["place_id"])
        # A gateway marks its place: a preserved manual gateway (modes ticked in the editor) follows
        # its place when OSM moves that place, instead of keeping the old coordinates.
        for gateway in package["gateways"]:
            place = nodes[gateway["place_id"]]
            gateway["lat"], gateway["lon"] = place["lat"], place["lon"]
        package["edit_history"] = copy.deepcopy(old["edit_history"])
        package["build_report"] = graph_report(package["nodes"], package["edges"], dropped, dropped_items)
        package["build_report"]["regeneration_proposal"] = raw_diff
        package["build_report"]["manual_records_preserved"] = True
    else:
        package["build_report"] = graph_report(package["nodes"], package["edges"], dropped, dropped_items)
    package["build_report"]["counts"]["gateways"] = len(package["gateways"])
    package["build_report"]["basemap"] = {
        "features": len(basemap), "skipped_incomplete": basemap_skipped,
        "note": "display-only landcover/water/waterway/place layers; never graph nodes or edges"}
    return validate_package(seal_package(package))
