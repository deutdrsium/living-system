"""Manual one-request Amap smoke check; never imported/run by CI.

Run from the repo with .venv/Scripts/python.exe tools/amap_smoke.py --help.
Use your own Web服务 key via AMAP_WEB_SERVICE_KEY or the hidden prompt, never a
CLI argument. --send-to-amap is explicit consent to send the two supplied
WGS84 coordinates (converted to GCJ-02), citycode, key and departure time.
This checks the provider boundary only: no graph import, trip or wallet write.
It sends exactly one HTTP request, without retries; repeated invocations consume
provider quota. Production embedding uses the persistent UsageGuard instead.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from getpass import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/astrbot_plugin_good_life"))
from good_life.city.amap import (AmapConfig, AmapError, StablePlace, _http, _parse_plan,
    _status, transit_parameters)
from good_life.kernel.codec import KernelError


async def smoke(args, key):
    config = AmapConfig(enabled=True, api_key=key, city_code=args.city, retries=0)
    config.validate()
    origin = StablePlace("smoke-origin", *args.origin_wgs84, "WGS84")
    destination = StablePlace("smoke-destination", *args.destination_wgs84, "WGS84", args.destination_city or "")
    departure = datetime.fromisoformat(args.departure) if args.departure else datetime.now(timezone.utc)
    params = transit_parameters(origin, destination, departure, config)
    payload = await _http(params, config.timeout_s)
    _status(payload)
    route = payload.get("route") or {}
    results = []
    for plan in (route.get("transits") or [])[:10]:
        try:
            results.append(_parse_plan(plan, {"bus", "metro"}))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    if not results:
        print("No supported, complete transit option returned. No trip created.")
        return 1
    for index, (duration, fare, legs) in enumerate(results, 1):
        fare_text = "unknown (cannot commit a trip)" if fare is None else f"CNY {fare / 100:.2f} estimated"
        print(f"Option {index}: {duration}s estimated, fare {fare_text}; legs {legs}")
    print("Adapter adds an uncertainty margin before execution; smoke output is the raw estimate.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin-wgs84", type=float, nargs=2, metavar=("LON", "LAT"), required=True)
    parser.add_argument("--destination-wgs84", type=float, nargs=2, metavar=("LON", "LAT"), required=True)
    parser.add_argument("--city", required=True, help="citycode, e.g. 021 (not adcode)")
    parser.add_argument("--destination-city", help="optional destination citycode for cross-city planning")
    parser.add_argument("--departure", help="ISO 8601 with timezone; defaults to now")
    parser.add_argument("--send-to-amap", action="store_true", help="explicitly authorize the disclosed one-request network send")
    args = parser.parse_args()
    if not args.send_to_amap:
        parser.error("Add --send-to-amap only after reading the location/key disclosure above.")
    key = os.environ.get("AMAP_WEB_SERVICE_KEY") or getpass("Amap Web服务 Key (hidden): ")
    if not key.strip():
        parser.error("A Web服务 key is required.")
    try:
        return asyncio.run(smoke(args, key))
    except AmapError as error:
        print(f"{error.code}/{error.reason_code}: {error}", file=sys.stderr)
    except KernelError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
    except Exception:
        # Never print raw transport/payload exceptions which can echo the key.
        print("Smoke check failed; private request details suppressed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
