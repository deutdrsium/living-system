"""Run with py -3 -m tools.osm_graph_builder (from the repository root)."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys

from .builder import build_graph
from .package_api import diff_packages, validate_package
from .source import center_bounds, load_source, validate_bounds


def _categories(value):
    return {item.strip() for item in value.split(",") if item.strip()}


def serve_editor(port):
    content = Path(__file__).with_name("editor.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/editor.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob: data: https://tile.openstreetmap.org; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Local graph editor: http://127.0.0.1:{server.server_port}/ (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a local OSM walking graph; cached/offline by default.")
    parser.add_argument("--serve-editor", action="store_true", help="serve the self-contained editor on loopback only")
    parser.add_argument("--port", type=int, default=8765)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    group.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"))
    parser.add_argument("--radius-m", type=float)
    parser.add_argument("--source", type=Path, help="local .osm/.osm.pbf regional extract")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "osm-graph-builder")
    parser.add_argument("--overpass", action="store_true", help="explicitly permit one Overpass request on a cache miss")
    parser.add_argument("--refresh", action="store_true", help="explicitly replace a cache entry from source/Overpass")
    parser.add_argument("--roads", type=_categories, help="comma-separated highway categories; empty excludes all")
    parser.add_argument("--buildings", type=_categories, default={"*"}, help="building categories or * (default)")
    parser.add_argument("--facilities", type=_categories, default={"*"}, help="amenity/shop/leisure values, key:value, or *")
    parser.add_argument("--walking-speed", type=float, default=1.4, help="base speed in metres/second (default 1.4)")
    parser.add_argument("--structured-access", action="store_true", help="keep private paths enabled for explicit credential evaluation; no remains closed")
    parser.add_argument("--graph-id")
    parser.add_argument("--version", type=int)
    parser.add_argument("--previous", type=Path, help="preserve manual edits/stable IDs and include regeneration proposal")
    parser.add_argument("--output", type=Path, help="new package filename (existing files are never overwritten)")
    args = parser.parse_args(argv)
    if args.serve_editor:
        if not 0 <= args.port <= 65535:
            parser.error("port must be between 0 and 65535")
        serve_editor(args.port)
        return 0
    if not args.graph_id or not args.output or (args.bbox is None and args.center is None):
        parser.error("--graph-id, --output and --bbox or --center are required")
    if (args.center is None) != (args.radius_m is None):
        parser.error("--radius-m is required only with --center")
    if args.source and args.overpass:
        parser.error("choose --source or --overpass")
    try:
        if args.output.exists():
            raise ValueError("output exists; choose a new filename to retain history")
        bounds = validate_bounds(args.bbox) if args.bbox else center_bounds(*args.center, args.radius_m)
        previous = validate_package(json.loads(args.previous.read_text(encoding="utf-8"))) if args.previous else None
        version = args.version if args.version is not None else (previous["version"] + 1 if previous else 1)
        data, cache_info = load_source(args.cache, bounds, source=args.source, refresh=args.refresh, allow_network=args.overpass)
        package = build_graph(data, bounds, graph_id=args.graph_id, version=version, roads=args.roads,
                              buildings=args.buildings, facilities=args.facilities, speed_m_s=args.walking_speed, previous=previous, structured_access=args.structured_access)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create protects a concurrently created version too.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(package, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"output": str(args.output), "digest": package["digest"], "cache": cache_info,
                          "report": package["build_report"], "diff": diff_packages(previous, package)}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"osm-graph-builder: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
