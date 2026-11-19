"""Explicit A8 installation, location ownership and read-only gate projection."""
from dataclasses import replace
from datetime import timedelta
import json

from ..contracts.models import StateValue, StateQuality, ReasonCode, TemplateRef
from ..kernel.codec import encode, freeze, integer, moment, name, require
from ..kernel.registry import ModuleSpec
from ..kernel.repository import KernelRepository
from ..storage import StorageReader
from ..economy.access import WalletAccess

SPEC = ModuleSpec("city", "1", ("geo.city_location", "city.secured_budget_minor"),
    emits=("city.trip.progressed", "city.trip.completed", "city.trip.cancelled"), consumes=("city.location",))
DDL = """CREATE TABLE life_city_locations (
    owner_key TEXT PRIMARY KEY, location_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1))"""


class City:
    def __init__(self, *, position=None):
        self.position = position
        self.storage = None

    def host_navigation(self, campus, config, clock):
        self.campus, self.amap_config, self.clock = campus, config, clock
        self._amap = None
        self._poi_lists = {}

    def _navigation(self):
        from .amap import AmapNavigation
        offline = self.campus._adapter()
        require(offline is not None, "confirmed city graph unavailable", ReasonCode.MISSING_DEPENDENCY)
        if not self.amap_config.enabled or not self.amap_config.api_key.strip():
            return offline
        if self._amap is None or self._amap.offline is not offline:
            self._amap = AmapNavigation(offline, (), self.amap_config, storage=self.storage, clock=self.clock)
        return self._amap

    async def search_pois(self, kernel, scope, public_place_name, *, opportunity_key, city_code=None):
        from .amap import AmapNavigation
        scope = kernel._scope(scope, "kernel.propose")
        name(opportunity_key)
        navigation = self._navigation()
        require(isinstance(navigation, AmapNavigation) and self.amap_config.public_poi_search_enabled,
                "POI search is disabled", ReasonCode.MISSING_DEPENDENCY)
        key = scope.owner_key, scope.context_key
        search = object()
        self._poi_lists[key] = search, None
        result = await navigation.asearch_pois(public_place_name, city_code=city_code)
        kernel._scope(scope, "kernel.propose")
        if self._poi_lists.get(key) == (search, None):
            selected = (scope, opportunity_key, navigation, result) if result.status == "ok" else None
            self._poi_lists[key] = search, selected
        return {"status": result.status, "items": tuple(poi.name for poi in result.items),
                "reason_code": result.reason_code}

    async def start_trip(self, kernel, scope, *, opportunity_key, destination=None, index=None, stay_s=0):
        from ..activities.city_trip import CityTrip, city_trip_template
        from .amap import AmapError, AmapNavigation
        scope = kernel._scope(scope, "kernel.propose")
        name(opportunity_key)
        integer(stay_s)
        require((destination is None) != (index is None), "provide a place ID or a POI index")
        origin = self.position.origin(scope.owner_key)
        if index is not None:
            key = scope.owner_key, scope.context_key
            _, selected = self._poi_lists.get(key, (None, None))
            if (selected is None or (scope, opportunity_key) != selected[:2]
                    or moment(self.clock()) >= selected[3].expires_at):
                raise AmapError("amap_poi_list_unavailable", "地点列表不存在或已过期，请重新搜索")
            _, _, search_adapter, result = selected
            destination = search_adapter.select_poi(result.list_token, index=index).place_id
            del self._poi_lists[key]
        else:
            name(destination)

        navigation = self._navigation()
        offline = navigation.offline if isinstance(navigation, AmapNavigation) else navigation
        wallet = kernel.registry.modules["economy"][1]
        require(wallet.currency == offline.currency, "route currency differs from wallet", ReasonCode.MISSING_DEPENDENCY)
        departure = now = moment(self.clock())
        if isinstance(navigation, AmapNavigation):
            outbound = await navigation.aquote(origin[0], destination, departure)
            kernel._scope(scope, "kernel.propose")
            inbound = await navigation.aquote(destination, origin[0],
                departure + timedelta(seconds=outbound.duration_s + stay_s))
            if outbound.provider == "amap-transit-v5@1" or inbound.provider == "amap-transit-v5@1":
                now = moment(self.clock())
        else:
            outbound = offline.quote(origin[0], destination, departure)
            inbound = offline.quote(destination, origin[0], departure + timedelta(seconds=outbound.duration_s + stay_s))

        template = city_trip_template(outbound, inbound, stay_s=stay_s)
        CityTrip.check_departure(template.params, now)
        with self.storage._lock:
            kernel._scope(scope, "kernel.propose")
            require(self.position.origin(scope.owner_key) == origin and self.campus._adapter() is offline,
                    "trip origin or graph changed during quotation", ReasonCode.STALE_VERSION)
            reference = TemplateRef(template.id, template.version)
            key = (template.id, template.version)
            new_template = key not in kernel.registry.templates
            if new_template:
                kernel.registry.register_template(template)
            try:
                candidate = kernel.propose(scope, reference, opportunity_key=opportunity_key,
                    reason="trip_requested", now=now, expires_at=now + timedelta(seconds=60))
                prepared = kernel.prepare_candidate(scope, candidate.candidate_id,
                    command_id="prepare-" + candidate.candidate_id, now=now)
            finally:
                # Successful preparation already pinned the template in SQLite.
                if new_template:
                    del kernel.registry.templates[key]
            if prepared.instance is None:
                return prepared
            instance = prepared.instance
            started = kernel.start(scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="start-" + instance.instance_id, now=now)
            return replace(prepared, instance=started)

    def install(self, storage):
        with storage.transaction() as db:
            version = db.execute("SELECT value FROM life_metadata WHERE key='city_schema_version'").fetchone()
            present = db.execute("SELECT sql FROM sqlite_master WHERE name='life_city_locations'").fetchone()
            if version is None:
                require(present is None, "unclaimed city schema")
                db.execute(DDL)
                db.execute("INSERT INTO life_metadata VALUES ('city_schema_version','1')")
            else:
                require(version[0] == "1" and present is not None and " ".join(present[0].split()) == " ".join(DDL.split()),
                        "unsupported city schema")
        self.storage = storage

    def locate(self, owner_key, place_id, *, expected_revision=0, evidence_ref=None):
        """Trusted explicit initialization; never called by quoting or planning.

        Shared travel requires a stable audit reference for manual confirmation.
        """
        if self.position is not None:
            return self.position.confirm(owner_key, place_id, expected_revision=expected_revision, evidence_ref=evidence_ref)
        require(self.storage is not None, "city is disabled", ReasonCode.MISSING_DEPENDENCY)
        location = {"status": "at_node", "place_id": name(place_id), "segment_index": 0, "progress_s": 0}
        with self.storage.transaction() as db:
            require(not any(i.template_id.startswith("city_trip_") for i in KernelRepository(self.storage).active(owner_key)),
                    "cannot relocate during an active city trip", ReasonCode.STALE_VERSION)
            old = db.execute("SELECT revision FROM life_city_locations WHERE owner_key=?", (name(owner_key),)).fetchone()
            require(integer(expected_revision) == (old[0] if old else 0), "city location changed", ReasonCode.STALE_VERSION)
            db.execute("INSERT INTO life_city_locations VALUES (?,?,?) ON CONFLICT(owner_key) DO UPDATE SET location_json=excluded.location_json,revision=excluded.revision",
                       (owner_key, encode(location), expected_revision + 1))

    def location(self, owner_key):
        if self.position is not None:
            current = self.position.read(owner_key)
            return current.city_view(), current.revision
        return self._legacy_location(owner_key)

    def _legacy_location(self, owner_key):
        require(self.storage is not None, "city is disabled", ReasonCode.MISSING_DEPENDENCY)
        row = StorageReader(self.storage).one("SELECT location_json,revision FROM life_city_locations WHERE owner_key=?", (name(owner_key),))
        return (freeze(json.loads(row[0])), row[1]) if row else (None, 0)

    def apply_effect(self, db, instance, effect):
        from ..activities.city_trip import trip_effects
        require(effect.target_owner == "city" and effect.kind == "city.location"
                and effect in trip_effects(instance, effect.effective_at), "location lacks persisted trip evidence")
        if self.position is not None:
            previous = self.position._read(db, instance.owner_key)
            if previous.revision:
                location = effect.payload["location"]
                at_node = location["status"] == "at_node"
                params = instance.checkpoint["params"]
                # A v1 at-node payload omits route identity. Its pinned stage
                # identifies the leg even after stopping or reaching an end.
                leg = "outbound" if location["segment_index"] < len(params["outbound"]["segments"]) else "return"
                self.position.commit(db, instance.owner_key, expected_revision=effect.payload["expected_revision"],
                    status=location["status"], place_id=location["place_id"] if at_node else None,
                    last_confirmed_node=location["place_id"], route_id=location.get("route_id", params[leg]["route_id"]),
                    segment_index=location["segment_index"], progress_s=location["progress_s"], effect_id=effect.effect_id)
                return True
        current = db.execute("SELECT revision FROM life_city_locations WHERE owner_key=?", (instance.owner_key,)).fetchone()
        require(current is not None and current[0] == effect.payload["expected_revision"], "city origin changed", ReasonCode.STALE_VERSION)
        db.execute("UPDATE life_city_locations SET location_json=?,revision=revision+1 WHERE owner_key=?",
                   (encode(effect.payload["location"]), instance.owner_key))
        return True


class CityAccess(WalletAccess):
    def __init__(self, host_access, wallet, city):
        super().__init__(host_access, wallet)
        self.city = city

    def snapshot(self, scope, now):
        from ..activities.city_trip import BUDGET, LOCATION, segments
        # Use the common storage lock for location, money and instance proofs.
        require(self.city.storage is self.wallet.storage and self.city.storage is not None, "city and wallet must share storage")
        with StorageReader(self.city.storage).locked():
            snapshot = super().snapshot(scope, now)
            location, revision = self.city.location(scope.owner_key)
            # A completed trip retains its route index in storage. A fresh plan
            # may start there, but only while physically at a node.
            projection = location
            active = tuple(i for i in KernelRepository(self.city.storage).active(scope.owner_key)
                           if i.context_key == scope.context_key and i.template_id.startswith("city_trip_"))
            if self.city.position is not None and revision == 0 and active:
                location, revision = self.city._legacy_location(scope.owner_key)
                projection = location
            if (not active or (len(active) == 1 and active[0].elapsed_s == 0)) and location is not None and location["status"] == "at_node":
                projection = freeze({**location, "segment_index": 0, "progress_s": 0})
            quote = self.wallet.quote_available(scope.owner_key)
            budget = quote.available_minor
            if len(active) > 1:
                budget = None
            elif active and active[0].checkpoint["reserved"]:
                instance = active[0]
                cp, offset, spent = instance.checkpoint, 0, 0
                for segment in segments(instance.checkpoint["params"]):
                    if instance.elapsed_s > offset:
                        spent += segment["cost_minor"]
                    offset += segment["duration_s"]
                reservation = self.wallet.reservation(scope.owner_key, instance.instance_id + ":budget")
                limit = cp["params"]["reserve_minor"]
                budget = limit if (reservation and reservation.status == "active" and reservation.limit_minor == limit
                                   and reservation.amount_remaining_minor == limit - spent) else None
            values = dict(snapshot.state.values)
            values[LOCATION] = StateValue(projection, now, max(1, revision), (), StateQuality.VALID if projection else StateQuality.UNKNOWN)
            values[BUDGET] = StateValue(budget, now, max(1, quote.revision), (), StateQuality.VALID if budget is not None else StateQuality.UNKNOWN)
            capabilities = tuple(dict.fromkeys((*snapshot.capabilities, "city"))) if len(active) <= 1 else snapshot.capabilities
            return replace(snapshot, state=replace(snapshot.state, values=freeze(values)), capabilities=capabilities)


def register_city(registry, *, enabled=False, position=None, settle_only=False):
    """No module, schema, executor or runtime writes unless explicitly opted in.

    Register A2's wallet separately (register_buy_drink is compatible). Pass a
    CityAccess to the existing ActivityKernel; production wiring stays off.
    """
    if enabled is not True:
        return None
    from ..activities.city_trip import CityTrip
    city = City(position=position)
    registry.register_module(SPEC, city)
    registry.register_executor(CityTrip(settle_only=settle_only))
    return city


def register_city_group(runtime, *, enabled, config):
    from ..activities.city_trip import CityActivities
    from ..runtime import OptionalGroupBinding
    from .amap import AmapConfig

    def has_history(reader):
        return reader.one("SELECT 1 FROM life_metadata WHERE key='city_schema_version'") is not None

    def bind(context):
        travel = context.groups["shared_travel"]
        settle_only = context.mode == "settle_only"
        city = register_city(context.registry, enabled=True, position=travel.position, settle_only=settle_only)
        if not settle_only:
            city.host_navigation(travel.campus, AmapConfig.from_plugin(config), runtime.wall_clock)

        def settle(*, now):
            CityActivities(runtime.kernel).settle_hosted(now=now, settle_only=settle_only)

        return OptionalGroupBinding(value=city, settle=settle)

    runtime.register_optional_group("city", bind, phase="pre_kernel", requires=("wallet", "shared_travel"),
                                    enabled=enabled, has_history=has_history)
