"""Amap public POI facts, expiring selection lists, and local place persistence."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import secrets
import threading
import time
from typing import Protocol
from uuid import uuid4

from ..kernel.codec import KernelError, moment
from ..storage import StorageReader
from .coordinates import coordinates, gcj02_to_wgs84
from .navigation import AsyncNavigationAdapter

POI_ENDPOINT = "https://restapi.amap.com/v5/place/text"
POI_LIMIT = 10
_LIST_TTL_S = 120
_CACHE_LIMIT = 128


@dataclass(frozen=True)
class AmapPoi:
    poi_id: str
    name: str
    city_code: str
    gcj02: tuple[float, float]  # longitude, latitude
    retrieved_at: datetime


@dataclass(frozen=True)
class PoiSearchResult:
    status: str  # ok / empty / error; failures carry a local reason_code
    items: tuple[AmapPoi, ...] = ()
    list_token: str | None = None
    expires_at: datetime | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class AmapPoiPlace:
    place_id: str
    poi: AmapPoi
    lon: float
    lat: float
    coordinate_system: str = "WGS84"

    @property
    def city_code(self):
        return self.poi.city_code


class PoiNavigationAdapter(AsyncNavigationAdapter, Protocol):
    """Optional A8 port for authorized public-place search and exact list selection."""

    async def asearch_pois(self, public_place_name: str, *, city_code: str | None = None) -> PoiSearchResult: ...

    def select_poi(self, list_token: str, *, index: int | None = None,
                   poi_id: str | None = None) -> AmapPoiPlace: ...


def parse_pois(payload, city_code, retrieved_at, credential):
    records = payload.get("pois")
    if records is None and payload.get("count") in (0, "0"):
        return ()
    if not isinstance(records, list):
        raise ValueError("invalid POI list")
    items = {}
    for record in records[:POI_LIMIT]:
        if not isinstance(record, dict):
            continue
        poi_id, name = record.get("id"), record.get("name")
        city, location = record.get("citycode"), record.get("location")
        if (not isinstance(poi_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", poi_id)
                or not isinstance(name, str) or not name.strip() or len(name) > 256
                or not name.isprintable() or city != city_code
                or credential in poi_id or credential in name
                or not isinstance(location, str) or len(location) > 64):
            continue
        try:
            lon, lat = map(float, location.split(","))
            coordinates(lon, lat, "GCJ-02")  # Validate before offering a choice.
        except (ValueError, KernelError):
            continue
        items.setdefault(poi_id, AmapPoi(poi_id, name.strip(), city, (lon, lat), retrieved_at))
    if records and not items:
        raise ValueError("no valid POI records")
    return tuple(items.values())


class PoiLists:
    def __init__(self, ttl_s):
        self.ttl_s = ttl_s
        self.monotonic = time.monotonic
        self._lock = threading.Lock()
        self._cache = {}
        self._lists = {}

    def _prune(self, entries, now):
        for key, (_, expires) in tuple(entries.items()):
            if now >= expires:
                del entries[key]

    @staticmethod
    def _insert(entries, key, value):
        if key not in entries and len(entries) >= _CACHE_LIMIT:
            del entries[next(iter(entries))]
        entries[key] = value

    def cached(self, key):
        with self._lock:
            self._prune(self._cache, self.monotonic())
            entry = self._cache.get(key)
            return entry[0] if entry is not None else None

    def cache(self, key, items):
        with self._lock:
            now = self.monotonic()
            self._prune(self._cache, now)
            self._insert(self._cache, key, (items, now + self.ttl_s))

    def issue(self, items, now):
        if not items:
            return PoiSearchResult("empty", reason_code="amap_poi_empty")
        token = secrets.token_urlsafe(24)
        with self._lock:
            current = self.monotonic()
            self._prune(self._lists, current)
            self._insert(self._lists, token, (items, current + _LIST_TTL_S))
        return PoiSearchResult("ok", items, token, now + timedelta(seconds=_LIST_TTL_S))

    def resolve(self, token):
        with self._lock:
            self._prune(self._lists, self.monotonic())
            entry = self._lists.get(token)
            return entry[0] if entry is not None else None


class PoiStore:
    _columns = "place_id, poi_id, name, city_code, gcj_lon, gcj_lat, wgs_lon, wgs_lat, retrieved_at"

    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)

    @staticmethod
    def _place(row):
        poi = AmapPoi(row[1], row[2], row[3], (row[4], row[5]), moment(datetime.fromisoformat(row[8])))
        return AmapPoiPlace(row[0], poi, row[6], row[7])

    def save(self, poi):
        lon, lat = gcj02_to_wgs84(*poi.gcj02)
        with self.storage.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS life_city_amap_places (
                place_id TEXT PRIMARY KEY, poi_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, city_code TEXT NOT NULL,
                gcj_lon REAL NOT NULL, gcj_lat REAL NOT NULL,
                wgs_lon REAL NOT NULL, wgs_lat REAL NOT NULL, retrieved_at TEXT NOT NULL
            )""")
            db.execute(f"""INSERT INTO life_city_amap_places ({self._columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(poi_id) DO NOTHING""",
                ("amap-poi:" + uuid4().hex, poi.poi_id, poi.name, poi.city_code, *poi.gcj02,
                 lon, lat, poi.retrieved_at.isoformat()))
            row = db.execute(f"SELECT {self._columns} FROM life_city_amap_places WHERE poi_id=?",
                             (poi.poi_id,)).fetchone()
            saved = self._place(row)
            # Another list may have saved different facts since this one was issued.
            if (saved.poi.name, saved.poi.city_code, saved.poi.gcj02) != (poi.name, poi.city_code, poi.gcj02):
                return None
            return saved

    def _has_table(self):
        # An untouched or older DB has no POI table until the first valid pick.
        return self.reader.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_city_amap_places'", ()) is not None

    def resolve_saved(self, items):
        if not items:
            return items
        poi_ids = tuple(item.poi_id for item in items)
        placeholders = ",".join("?" for _ in poi_ids)
        with self.reader.locked():
            if not self._has_table():
                return items
            rows = self.reader.all(f"SELECT {self._columns} FROM life_city_amap_places WHERE poi_id IN ({placeholders})",
                                   poi_ids)
        saved = {row[1]: self._place(row).poi for row in rows}
        return tuple(saved.get(item.poi_id, item) for item in items)

    def lookup(self, place_id):
        if not self._has_table():
            return None
        row = self.reader.one(f"SELECT {self._columns} FROM life_city_amap_places WHERE place_id=?", (place_id,))
        return self._place(row) if row else None
