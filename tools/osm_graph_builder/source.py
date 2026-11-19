"""Bounded local extraction and single-request, explicitly refreshed Overpass cache."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_ONLINE_AREA_KM2 = 25
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Outside-bbox vertices kept only so basemap areas/waterways can be clipped to the bbox.
MAX_BASEMAP_OUTSIDE_NODES = 50_000

BASEMAP_NATURAL = {"water", "wood", "scrub", "grassland", "heath", "wetland", "beach", "sand", "bare_rock", "scree"}
BASEMAP_LEISURE = {"park", "garden", "pitch", "playground", "sports_centre", "stadium", "track", "golf_course",
                   "nature_reserve", "common", "recreation_ground", "dog_park"}
WATERWAY_LINES = {"river", "stream", "canal", "drain", "ditch", "brook", "tidal_channel"}


def basemap_kind(tags):
    """Classify a display-only layer. Never used for graph nodes or edges."""
    closed_area = tags.get("area") != "no"
    if (tags.get("natural") == "water" or tags.get("water") or tags.get("waterway") in {"riverbank", "dock"}
            or tags.get("landuse") in {"reservoir", "basin"}) and closed_area:
        return "water"
    if tags.get("waterway") in WATERWAY_LINES:
        return "waterway"
    if tags.get("landuse") or tags.get("natural") in BASEMAP_NATURAL or tags.get("leisure") in BASEMAP_LEISURE:
        return "landcover"
    return None


def validate_bounds(bounds):
    if len(bounds) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) for v in bounds):
        raise ValueError("bounds must contain south west north east")
    s, w, n, e = bounds
    if not (-85 <= s < n <= 85 and -180 <= w < e <= 180):
        raise ValueError("invalid bbox (latitudes must be between -85 and 85; no antimeridian crossing)")
    return [float(v) for v in bounds]


def center_bounds(lat, lon, radius_m):
    if not all(math.isfinite(v) for v in (lat, lon, radius_m)) or radius_m <= 0 or abs(lat) >= 85:
        raise ValueError("center/radius must be finite, radius positive, latitude within -85..85")
    dy = math.degrees(radius_m / 6371008.8)
    dx = dy / math.cos(math.radians(lat))
    return validate_bounds([lat - dy, lon - dx, lat + dy, lon + dx])


def area_km2(bounds):
    s, w, n, e = validate_bounds(bounds)
    return 6371.0088**2 * math.radians(e - w) * (math.sin(math.radians(n)) - math.sin(math.radians(s)))


def inside(lat, lon, bounds):
    s, w, n, e = bounds
    return s <= lat <= n and w <= lon <= e


def _tags(element):
    return {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}


def _safe_xml(path):
    # ElementTree doesn't fetch external entities. Reject DTDs/expansions as well.
    tail = b""
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            probe = (tail + chunk).upper()
            if b"\x00" in probe:
                raise ValueError("OSM XML must not contain NUL/UTF-16/UTF-32 data")
            if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                raise ValueError("OSM XML must not contain a DTD or entity declarations")
            tail = probe[-16:]


def _elements(path):
    try:
        context = ET.iterparse(path, events=("start", "end"))
        _, root = next(context)
        if root.tag != "osm":
            raise ValueError("expected an OSM XML document")
        for event, element in context:
            if event == "end" and element.tag in {"remark", "error"}:
                raise ValueError("OSM response contains a server error; use a smaller bbox or an offline file")
            if event == "end" and element.tag in {"node", "way", "relation"}:
                yield element
                element.clear()
                root.clear()
    except (ET.ParseError, StopIteration) as error:
        raise ValueError("invalid OSM XML document") from error


def parse_xml(path, bounds):
    bounds = validate_bounds(bounds)
    _safe_xml(path)
    nodes, ways = {}, []
    counts = {"outside_nodes": 0, "outside_ways": 0, "unsupported_relations": 0}
    # Two streaming passes support unordered XML without loading a regional file.
    for element in _elements(path):
        if element.tag == "node":
            lat, lon = float(element.attrib["lat"]), float(element.attrib["lon"])
            if inside(lat, lon, bounds):
                nodes[element.attrib["id"]] = {"lat": lat, "lon": lon, "tags": _tags(element)}
            else:
                counts["outside_nodes"] += 1
    for element in _elements(path):
        if element.tag == "way":
            refs = [nd.attrib["ref"] for nd in element.findall("nd")]
            if any(ref in nodes for ref in refs):
                ways.append({"id": element.attrib["id"], "refs": refs, "tags": _tags(element)})
            else:
                counts["outside_ways"] += 1
        elif element.tag == "relation":
            counts["unsupported_relations"] += 1
    needed = _basemap_outside_refs(nodes, ways)
    outside = {}
    if needed:
        # Third pass only when a display-only area/waterway crosses the bbox edge.
        for element in _elements(path):
            if element.tag == "node" and element.attrib["id"] in needed:
                outside[element.attrib["id"]] = [float(element.attrib["lon"]), float(element.attrib["lat"])]
    return {"nodes": nodes, "ways": ways, "dropped": counts, "basemap_nodes": outside}


def _basemap_outside_refs(nodes, ways):
    """Outside vertices of display-only ways, bounded; graph extraction never reads them."""
    needed = set()
    for way in sorted(ways, key=lambda w: int(w["id"])):
        if basemap_kind(way["tags"]) is None:
            continue
        missing = {ref for ref in way["refs"] if ref not in nodes} - needed
        if len(needed) + len(missing) > MAX_BASEMAP_OUTSIDE_NODES:
            continue  # One huge way must not starve the smaller ones after it.
        needed |= missing
    return needed


def parse_pbf(path, bounds):
    try:
        import osmium
    except ImportError as error:
        raise ValueError("PBF requires optional tool dependency in a project virtual environment (not the global Python): "
                         "py -3 -m venv .venv, then .venv\\Scripts\\python.exe -m pip install -r tools\\osm_graph_builder\\requirements-pbf.txt, "
                         "and run the tool with .venv\\Scripts\\python.exe; XML needs no dependency") from error
    bounds = validate_bounds(bounds)
    nodes, ways = {}, []
    counts = {"outside_nodes": 0, "outside_ways": 0, "unsupported_relations": 0}

    class Nodes(osmium.SimpleHandler):
        def node(self, node):
            if node.location.valid() and inside(node.location.lat, node.location.lon, bounds):
                nodes[str(node.id)] = {"lat": node.location.lat, "lon": node.location.lon, "tags": dict(node.tags)}
            else:
                counts["outside_nodes"] += 1

    class Ways(osmium.SimpleHandler):
        def way(self, way):
            refs = [str(node.ref) for node in way.nodes]
            if any(ref in nodes for ref in refs):
                ways.append({"id": str(way.id), "refs": refs, "tags": dict(way.tags)})
            else:
                counts["outside_ways"] += 1

        def relation(self, relation):
            counts["unsupported_relations"] += 1

    # PathLike selects pyosmium's filesystem overload (Unicode-safe on Windows).
    Nodes().apply_file(Path(path))
    Ways().apply_file(Path(path))
    needed = _basemap_outside_refs(nodes, ways)
    outside = {}

    class Outside(osmium.SimpleHandler):
        def node(self, node):
            if str(node.id) in needed and node.location.valid():
                outside[str(node.id)] = [node.location.lon, node.location.lat]

    if needed:
        Outside().apply_file(Path(path))
    return {"nodes": nodes, "ways": ways, "dropped": counts, "basemap_nodes": outside}


def parse_file(path, bounds):
    return parse_pbf(path, bounds) if str(path).lower().endswith(".pbf") else parse_xml(path, bounds)


def _atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(content)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, "redirect refused", headers, fp)


def fetch_overpass(query):
    """Exactly one HTTP request. No redirects, retries, concurrency or tiling."""
    request = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode("ascii"),
                                     headers={"User-Agent": "osm-graph-builder/0.1", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=90) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("Overpass response exceeds 64 MiB; use an offline regional extract")
    return content


def overpass_query(bounds):
    """One union query: graph categories plus display-only basemap layers and place names."""
    bbox = ",".join(format(v, ".9f") for v in validate_bounds(bounds))
    natural = "|".join(sorted(BASEMAP_NATURAL))
    return (f'[out:xml][timeout:60];(nwr["highway"]({bbox});nwr["building"]({bbox});nwr["amenity"]({bbox});'
            f'nwr["shop"]({bbox});nwr["leisure"]({bbox});node["entrance"]({bbox});node["barrier"]({bbox});'
            f'way["landuse"]({bbox});way["natural"~"^({natural})$"]({bbox});way["waterway"]({bbox});'
            f'way["water"]({bbox});node["place"]["name"]({bbox}););(._;>;);out body;')


def load_source(cache_dir, bounds, *, source=None, refresh=False, allow_network=False, fetcher=None, clock=time.time):
    """Cache-first. Existing cache never uses the network without refresh.

    The cache key is the extraction bbox; selection categories are applied later.
    A --source path only replaces an existing entry when refresh=True.
    """
    bounds = validate_bounds(bounds)
    key = hashlib.sha256(json.dumps(bounds, separators=(",", ":")).encode()).hexdigest()[:24]
    root = Path(cache_dir) / key
    cached = root / "extract.json"
    if cached.exists() and not refresh:
        entry = json.loads(cached.read_text(encoding="utf-8"))
        if entry.get("bounds") != bounds or entry.get("cache_schema") != 1:
            raise ValueError("invalid extraction cache; use --refresh")
        return entry["data"], {"cache_key": key, "cache_hit": True, "origin": entry["origin"]}
    if source is not None:
        data = parse_file(source, bounds)
        origin = "local"
    else:
        if not allow_network:
            raise ValueError("no matching cache; supply --source or explicitly opt in with --overpass")
        if area_km2(bounds) > MAX_ONLINE_AREA_KM2:
            raise ValueError("online bbox exceeds 25 km²; use an offline regional file")
        cooldown = Path(cache_dir) / "overpass-backoff.json"
        now = clock()
        remaining = json.loads(cooldown.read_text())["retry_after"] - now if cooldown.exists() else 0
        if remaining > 0:
            raise ValueError(f"Overpass cooldown active; wait {math.ceil(remaining)} seconds or use an offline file")
        query = overpass_query(bounds)
        try:
            raw = (fetcher or fetch_overpass)(query)
            if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("invalid/oversized Overpass response")
            # Parse before replacing the last working raw cache.
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root, suffix=".osm", delete=False) as stream:
                pending = Path(stream.name)
                stream.write(raw)
            try:
                data = parse_xml(pending, bounds)
                pending.replace(root / "overpass.osm")
            finally:
                pending.unlink(missing_ok=True)
        except Exception as error:
            failed_at = clock()
            delay = 60
            if isinstance(error, urllib.error.HTTPError) and error.headers:
                retry_after = error.headers.get("Retry-After", "")
                try:
                    requested = float(retry_after)
                except (TypeError, ValueError):
                    try:
                        requested = parsedate_to_datetime(retry_after).timestamp() - failed_at
                    except (TypeError, ValueError, OverflowError):
                        requested = 0
                if math.isfinite(requested):
                    delay = max(delay, requested)
            _atomic(cooldown, json.dumps({"retry_after": failed_at + delay}).encode())
            raise ValueError(f"Overpass fetch failed; no automatic retry; cooldown {delay:g} seconds") from error
        cooldown.unlink(missing_ok=True)
        origin = "overpass"
    entry = {"cache_schema": 1, "bounds": bounds, "origin": origin,
             "cached_at": datetime.now(timezone.utc).isoformat(), "data": data}
    _atomic(cached, json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return data, {"cache_key": key, "cache_hit": False, "origin": origin}
