"""A8 position authority for shared travel. Every revision records its evidence."""
from dataclasses import dataclass, replace
import json

from ..contracts.models import ReasonCode
from ..kernel.codec import encode, freeze, integer, name, require
from ..kernel.repository import KernelRepository
from ..storage import StorageReader

VERSION_KEY = "shared_position_schema_version"
DDL = """CREATE TABLE life_shared_positions (
    owner_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
    position_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
    PRIMARY KEY(owner_key, revision))"""
STATE_KEY = "geo.shared_position"
CAMPUS_KEY = "geo.place_id"
LATEST_POSITION = "SELECT position_json FROM life_shared_positions WHERE owner_key=? ORDER BY revision DESC LIMIT 1"


@dataclass(frozen=True)
class Position:
    owner_key: str
    revision: int
    status: str
    place_id: str | None = None
    last_confirmed_node: str | None = None
    route_id: str | None = None
    segment_index: int = 0
    progress_s: int = 0

    def city_view(self):
        if self.status == "unknown":
            return None
        view = {"status": self.status, "place_id": self.place_id or self.last_confirmed_node,
                "segment_index": self.segment_index, "progress_s": self.progress_s}
        if self.status == "in_transit":
            view["route_id"] = self.route_id
        return freeze(view)


class SharedPosition:
    def __init__(self):
        self.storage = None

    def install(self, storage):
        with storage.transaction() as db:
            version = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
            present = self._table(db, "life_shared_positions")
            if version is None:
                require(present is None, "unclaimed shared position schema")
                db.execute(DDL)
                db.execute("INSERT INTO life_metadata VALUES (?, '1')", (VERSION_KEY,))
            else:
                require(version[0] == "1" and present is not None
                        and present[0].split() == DDL.split(), "unsupported shared position schema")
        self.storage = storage

    @staticmethod
    def _table(db, table):
        return db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()

    def _reader(self):
        require(self.storage is not None, "shared position is disabled", ReasonCode.MISSING_DEPENDENCY)
        return StorageReader(self.storage)

    @staticmethod
    def _read(db, owner_key):
        name(owner_key)
        row = db.execute(LATEST_POSITION, (owner_key,)).fetchone()
        return Position(**json.loads(row[0])) if row else Position(owner_key, 0, "unknown")

    def read(self, owner_key):
        with self._reader().locked():
            return self._read(self.storage._connection, owner_key)

    def evidence(self, owner_key, revision):
        row = self._reader().one("SELECT evidence_json FROM life_shared_positions WHERE owner_key=? AND revision=?",
                                 (name(owner_key), integer(revision, 1)))
        return freeze(json.loads(row[0])) if row else None

    def origin(self, owner_key):
        current = self.read(owner_key)
        require(current.status == "at_node", "no confirmed current stop", ReasonCode.MISSING_DEPENDENCY)
        return current.place_id, current.revision

    def active(self, db, owner_key, *prefixes):
        if not self._table(db, "life_kernel_instances"):
            return ()
        return tuple(i for i in KernelRepository(self.storage).active(owner_key)
                     if i.template_id.startswith(prefixes)
                     and (not i.template_id.startswith("class_session_") or i.checkpoint["params"]["decision"] == "attend"))

    def initialize(self, owner_key, *, campus_places, external_places=(), graph_ref=None, home_place_id=""):
        """Import v1 facts once, with the configured home as a fallback for unknown positions."""
        self._reader()
        campus_places, external_places = frozenset(campus_places), frozenset(external_places)
        owner_key = name(owner_key)
        with self.storage.transaction() as db:
            previous = self._read(db, owner_key)
            home = home_place_id if home_place_id in campus_places else None
            if previous.status != "unknown" or (previous.revision and home is None):
                return previous
            if self.active(db, owner_key, "city_trip_", "class_session_", "campus_walk_"):
                return previous
            if previous.revision:
                following = Position(owner_key, previous.revision + 1, "at_node", home, home)
                return self._write(db, previous, following, {"kind": "configured_home", "graph": graph_ref})
            sources, mapped = {}, []
            if self._table(db, "life_campus_locations"):
                row = db.execute("SELECT place_id,revision FROM life_campus_locations WHERE owner_key=?", (owner_key,)).fetchone()
                if row:
                    sources["campus"] = {"place_id": row[0], "revision": row[1]}
                    mapped.append(row[0] if row[0] in campus_places else None)
            if self._table(db, "life_city_locations"):
                row = db.execute("SELECT location_json,revision FROM life_city_locations WHERE owner_key=?", (owner_key,)).fetchone()
                if row:
                    saved = json.loads(row[0])
                    sources["city"] = {"location": saved, "revision": row[1]}
                    place = saved.get("place_id")
                    mapped.append(place if saved.get("status") == "at_node" and
                                  (place in campus_places or place in external_places) else None)
            place = mapped[0] if mapped and None not in mapped and len(set(mapped)) == 1 else None
            reason = ("confirmed" if place else "missing_evidence" if not mapped else
                      "missing_mapping" if None in mapped else "conflicting_evidence")
            evidence = {"kind": "legacy_initialization", "reason": reason, "sources": sources, "graph": graph_ref}
            if place is None and home:
                place = home
                evidence["kind"] = "configured_home"
            next_position = Position(owner_key, 1, "at_node" if place else "unknown", place, place)
            return self._write(db, previous, next_position, evidence)

    def confirm(self, owner_key, place_id, *, expected_revision, evidence_ref):
        """Trusted operator input. place_id is a confirmed canonical node or external place."""
        self._reader()
        owner_key, place_id, evidence_ref = name(owner_key), name(place_id), name(evidence_ref)
        with self.storage.transaction() as db:
            previous = self._read(db, owner_key)
            self._fence(previous, expected_revision)
            require(not self.active(db, owner_key, "city_trip_", "class_session_", "campus_walk_"), "travel is still active", ReasonCode.STALE_VERSION)
            return self._write(db, previous, Position(owner_key, previous.revision + 1, "at_node", place_id, place_id),
                               {"kind": "manual_confirmation", "ref": evidence_ref})

    @staticmethod
    def _fence(previous, expected_revision):
        require(previous.revision == integer(expected_revision), "shared position changed", ReasonCode.STALE_VERSION)

    @staticmethod
    def _write(db, previous, following, evidence):
        require(following.owner_key == previous.owner_key and following.revision == previous.revision + 1,
                "invalid position revision")
        db.execute("INSERT INTO life_shared_positions VALUES (?,?,?,?)",
                   (following.owner_key, following.revision, encode(following), encode(evidence)))
        return following

    def commit(self, db, owner_key, *, expected_revision, status, place_id, last_confirmed_node,
               route_id, segment_index, progress_s, effect_id):
        """Apply a stage already validated against its pinned executor checkpoint."""
        previous = self._read(db, owner_key)
        self._fence(previous, expected_revision)
        require(previous.revision > 0, "shared position has not been initialized", ReasonCode.MISSING_DEPENDENCY)
        require(status in ("at_node", "in_transit", "unknown"), "invalid position status")
        require((place_id is not None) == (status == "at_node"), "only a confirmed stop has a current node")
        if place_id is not None:
            require(name(place_id) == last_confirmed_node, "confirmed stop differs from last confirmed node")
        if last_confirmed_node is not None:
            name(last_confirmed_node)
        following = replace(previous, revision=previous.revision + 1, status=status, place_id=place_id,
                            last_confirmed_node=last_confirmed_node, route_id=name(route_id),
                            segment_index=integer(segment_index), progress_s=integer(progress_s))
        return self._write(db, previous, following, {"kind": "stage_commit", "effect_id": name(effect_id)})
