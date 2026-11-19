"""Explicit local campus walking graph; no map provider, fare or teleportation."""
from dataclasses import replace
from datetime import timedelta
import heapq
from inspect import signature
from itertools import takewhile

from ..contracts.models import ActivityStatus, ReasonCode
from ..kernel.codec import KernelError, integer, moment, name, require, wire
from ..kernel.registry import ModuleSpec
from ..storage import StorageReader


def campus_route_timing(campus, origin, destination, departure_at, *, class_start=None, class_end=None):
    """Use timed routing when provided, retaining the manual two-argument port."""
    class_timed = getattr(campus, "class_route_timing", None)
    if class_start is not None and class_timed is not None:
        args = (origin, destination, departure_at, class_start)
        try:
            signature(class_timed).bind(*args, class_end=class_end)
        except (TypeError, ValueError):
            return class_timed(*args)
        return class_timed(*args, class_end=class_end)
    timed = getattr(campus, "route_timing", None)
    if timed is not None:
        return timed(origin, destination, departure_at)
    return campus.route(origin, destination), 0


def route_when_due(timing, now, class_start):
    route, origin_wait_s = timing
    if origin_wait_s or now + timedelta(seconds=route['travel_s']) < class_start:
        return None
    return route


CLASS_STOP_EFFECT = "campus.class_stop_unknown"
SPEC = ModuleSpec("campus", "1", ("geo.place_id",), consumes=("campus.arrive",))
DDL = """CREATE TABLE life_campus_locations (
    owner_key TEXT PRIMARY KEY, place_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1))"""


class Campus:
    def __init__(self, places, paths):
        self.places = tuple(name(item) for item in places)
        require(self.places and len(set(self.places)) == len(self.places), "campus places must be unique")
        self.paths = {}
        for origin, destination, seconds in paths:
            require(origin in self.places and destination in self.places and origin != destination, "unknown campus path endpoint")
            require((origin, destination) not in self.paths, "duplicate campus path")
            self.paths[origin, destination] = integer(seconds, 1)
        self.storage = None

    @classmethod
    def from_settings(cls, store, settings, *, places, paths, position=None):
        """Opt-in package adapter; an absent confirmed graph retains manual paths."""
        graph_id = settings.get('academics_graph_id', '')
        if position is not None:
            require(graph_id and store.current_ref(graph_id), "confirmed campus graph is required",
                    ReasonCode.MISSING_DEPENDENCY)
        if not graph_id:
            return cls(places, paths)
        from ..graph_access import GraphAccessPolicy
        policy_settings = {**settings, "graph_access_enabled": True} if position is not None else settings
        home = settings.get("home_place_id", "")
        if home:
            policy_settings = {**policy_settings, "graph_access_credentials": (
                *policy_settings.get("graph_access_credentials", ()), "resident_of:" + name(home))}
        return ConfirmedGraphCampus(store, graph_id, places=places, paths=paths, home_place_id=home,
                                    access_policy=GraphAccessPolicy.from_settings(policy_settings), position=position)

    def route(self, origin, destination, departure_at=None):
        require(origin in self.places and destination in self.places, "unknown campus place", ReasonCode.MISSING_DEPENDENCY)
        queue, seen = [(0, (origin,))], set()
        while queue:
            elapsed, route = heapq.heappop(queue)
            if route[-1] in seen:
                continue
            seen.add(route[-1])
            if route[-1] == destination:
                return {"from_place_id": origin, "place_id": destination, "path": route, "travel_s": elapsed}
            for (start, end), seconds in sorted(self.paths.items()):
                if start == route[-1] and end not in seen:
                    heapq.heappush(queue, (elapsed + seconds, (*route, end)))
        require(False, "no campus route available", ReasonCode.MISSING_DEPENDENCY)

    def route_timing(self, origin, destination, departure_at):
        """Return the pinnable route and seconds spent waiting before movement."""
        return self.route(origin, destination), 0

    def install(self, storage):
        with storage.transaction() as db:
            version = db.execute("SELECT value FROM life_metadata WHERE key='campus_schema_version'").fetchone()
            present = db.execute("SELECT sql FROM sqlite_master WHERE name='life_campus_locations'").fetchone()
            if version is None:
                require(present is None, "unclaimed campus schema")
                db.execute(DDL)
                db.execute("INSERT INTO life_metadata VALUES ('campus_schema_version','1')")
            else:
                require(version[0] == "1" and present is not None and " ".join(present[0].split()) == " ".join(DDL.split()), "unsupported campus schema")
        self.storage = storage

    def locate(self, owner_key, place_id, *, expected_revision=0):
        """Trusted manual location initialization/correction, fenced and explicit."""
        require(self.storage is not None, "campus is disabled", ReasonCode.MISSING_DEPENDENCY)
        name(owner_key)
        require(place_id in self.places, "unknown campus place")
        with self.storage.transaction() as db:
            previous = db.execute("SELECT revision FROM life_campus_locations WHERE owner_key=?", (owner_key,)).fetchone()
            current = previous[0] if previous else 0
            require(integer(expected_revision) == current, "campus location changed", ReasonCode.STALE_VERSION)
            db.execute("INSERT INTO life_campus_locations VALUES (?,?,?) ON CONFLICT(owner_key) DO UPDATE SET place_id=excluded.place_id,revision=excluded.revision", (owner_key, place_id, current + 1))

    def location(self, owner_key):
        require(self.storage is not None, "campus is disabled", ReasonCode.MISSING_DEPENDENCY)
        row = StorageReader(self.storage).one("SELECT place_id,revision FROM life_campus_locations WHERE owner_key=?", (name(owner_key),))
        return (row[0], row[1]) if row else (None, 0)

    def apply_effect(self, db, instance, effect):
        cp = instance.checkpoint
        route = cp["params"]["route"]
        require(effect.target_owner == SPEC.name and effect.kind == "campus.arrive" and effect.instance_id == instance.instance_id
                and effect.effect_id == instance.instance_id + ":arrive", "invalid campus arrival")
        require(cp["arrived"] and cp["travel_elapsed_s"] == route["travel_s"] and cp["arrival_at"] is not None, "arrival has no completed travel evidence")
        require(effect.payload == {"from_place_id": route["from_place_id"], "place_id": route["place_id"], "expected_revision": cp["location_revision"]}, "arrival payload differs from checkpoint")
        position = getattr(self, "position", None)
        if position is not None:
            previous = position._read(db, instance.owner_key)
            if previous.revision:
                require(previous.status == "at_node" and previous.place_id == route["from_place_id"],
                        "campus origin changed", ReasonCode.STALE_VERSION)
                position.commit(db, instance.owner_key, expected_revision=cp["location_revision"],
                    status="at_node", place_id=route["place_id"], last_confirmed_node=route["place_id"],
                    route_id=instance.template_id, segment_index=len(route["path"]) - 1, progress_s=0,
                    effect_id=effect.effect_id)
                return True
        # The route was pinned at preparation. A new graph cannot rewrite a
        # completed online trip; only the actual origin is fenced at commit.
        current = db.execute("SELECT place_id FROM life_campus_locations WHERE owner_key=?", (instance.owner_key,)).fetchone()
        require(current and current[0] == route["from_place_id"],
                "campus origin changed", ReasonCode.STALE_VERSION)
        db.execute("UPDATE life_campus_locations SET place_id=?,revision=revision+1 WHERE owner_key=?", (route["place_id"], instance.owner_key))
        return True


class ConfirmedGraphCampus(Campus):
    """Select the latest confirmed package for new class proposals only.

    Route facts live in the class template/checkpoint. Neither recovery nor an
    arrival commit consults the latest package or re-routes an in-flight class.
    """
    def __init__(self, store, graph_id, *, places=(), paths=(), access_policy=None, position=None, settle_only=False, home_place_id=""):
        self.home_place_id = home_place_id
        self.store, self.access_policy = store, access_policy
        self.graph_id = None if settle_only and not graph_id else name(graph_id)
        self.settle_only = settle_only
        self.position = position
        self.manual = Campus(places, paths) if places and position is None else None
        self.storage = None
        self._navigation = None

    def _adapter(self):
        from ..city.navigation import OfflineNavigation
        ref = self.store.current_ref(self.graph_id) if self.graph_id is not None else None
        if ref is None:
            return None
        cached = self._navigation
        if cached is None or (cached.graph['version'], cached.graph['digest']) != (ref['version'], ref['digest']):
            cached = OfflineNavigation(self.store, self.graph_id, currency='CNY', access_policy=self.access_policy,
                                       graph_version=ref['version'], graph_digest=ref['digest'],
                                       confirmed_campus_only=self.position is not None)
            self._navigation = cached
        return cached

    @property
    def places(self):
        navigation = self._adapter()
        if navigation is not None:
            return tuple(place for place in navigation.nodes if place not in navigation.blocked_places)
        return self.manual.places if self.manual else ()

    def install(self, storage):
        super().install(storage)
        if self.position is not None:
            self.position.install(storage)

    def _routing_enabled(self):
        require(not self.settle_only, "capability_disabled", ReasonCode.MISSING_DEPENDENCY)

    def initialize_position(self, owner_key, *, external_places=()):
        self._routing_enabled()
        navigation = self._adapter()
        require(self.position is not None and navigation is not None, "shared campus graph is unavailable",
                ReasonCode.MISSING_DEPENDENCY)
        external_places = tuple(name(place) for place in external_places)
        require(not (navigation.nodes.keys() & set(external_places)), "external place identities overlap campus nodes")
        graph = navigation.graph
        return self.position.initialize(owner_key, campus_places=navigation.nodes.keys() - navigation.blocked_places,
            external_places=external_places, graph_ref={key: graph[key] for key in ("graph_id", "version", "digest")},
            home_place_id=self.home_place_id)

    def plan_walk(self, owner_key, destination, *, departure_at):
        self._routing_enabled()
        require(self.position is not None, "shared campus travel is disabled", ReasonCode.MISSING_DEPENDENCY)
        origin, revision = self.position.origin(owner_key)
        navigation = self._adapter()
        require(navigation is not None and origin in navigation.nodes and destination in navigation.nodes,
                "walk endpoints are outside the confirmed campus", ReasonCode.MISSING_DEPENDENCY)
        quote = navigation.quote(origin, destination, departure_at)
        notes = getattr(quote, "assumptions", ())
        valid_until = self._departure_valid_until(navigation, origin, quote.departure_at, quote.segments, notes)
        return replace(quote, valid_until=valid_until + timedelta.resolution), revision

    def locate(self, owner_key, place_id, *, expected_revision=0, evidence_ref=None):
        if self.position is None:
            return super().locate(owner_key, place_id, expected_revision=expected_revision)
        self._routing_enabled()
        navigation = self._adapter()
        require(navigation is not None and place_id in navigation.nodes and place_id not in navigation.blocked_places,
                "campus place is unavailable", ReasonCode.MISSING_DEPENDENCY)
        return self.position.confirm(owner_key, place_id, expected_revision=expected_revision, evidence_ref=evidence_ref)

    def location(self, owner_key):
        if self.position is None:
            return super().location(owner_key)
        with StorageReader(self.storage).locked():
            db = self.storage._connection
            current = self.position._read(db, owner_key)
            if current.revision == 0 and self.position.active(db, owner_key, "class_session_"):
                return super().location(owner_key)
            return self._project_location(current)

    def project_location(self, owner_key):
        return self._project_location(self.position.read(owner_key))

    def _project_location(self, current):
        if current is None or current.status != "at_node":
            return None, current.revision if current is not None else 0
        navigation = self._adapter()
        at_campus = (navigation is not None and current.place_id in navigation.nodes
                     and current.place_id not in navigation.blocked_places)
        return (current.place_id if at_campus else None, current.revision)

    def apply_effect(self, db, instance, effect):
        if self.position is not None:
            from ..travel.walking import EFFECT, apply_walk_effect
            if effect.kind == EFFECT:
                return apply_walk_effect(self.position, db, instance, effect)
            if effect.kind == CLASS_STOP_EFFECT:
                from ..activities.class_session import class_stop_effect
                require(instance.status in (ActivityStatus.CANCELLED, ActivityStatus.COMPLETED)
                        and effect == class_stop_effect(instance, effect.effective_at),
                        "position update lacks an ended class checkpoint")
                previous = self.position._read(db, instance.owner_key)
                require(previous.status == "at_node" and previous.place_id == effect.payload["from_place_id"],
                        "campus origin changed", ReasonCode.STALE_VERSION)
                self.position.commit(db, instance.owner_key, expected_revision=effect.payload["expected_revision"],
                    status="unknown", place_id=None, last_confirmed_node=previous.place_id, route_id=instance.template_id,
                    segment_index=0, progress_s=effect.payload["progress_s"], effect_id=effect.effect_id)
                return True
        return super().apply_effect(db, instance, effect)

    def route(self, origin, destination, departure_at=None):
        route, _origin_wait_s = self.route_timing(origin, destination, departure_at)
        return route

    def route_timing(self, origin, destination, departure_at):
        return self._route_timing(self._adapter(), origin, destination, departure_at)

    def class_route_timing(self, origin, destination, departure_at, class_start, *, class_end=None):
        navigation = self._adapter()
        timing = self._route_timing(navigation, origin, destination, departure_at, class_start=class_start)
        if class_end is not None and navigation is not None and navigation.access_policy is not None:
            require(moment(departure_at) + timedelta(seconds=timing[0]['travel_s']) < class_end,
                    'no feasible campus route before class ends', ReasonCode.PROVIDER_UNAVAILABLE)
        return timing

    def calendar_route(self, origin, destination, now, class_start, class_end, early_departure_lead):
        """Return a current route when due, using one confirmed graph per decision."""
        self._routing_enabled()
        navigation = self._adapter()
        if navigation is None or navigation.access_policy is None or origin == destination:
            timing = self._route_timing(navigation, origin, destination, now, class_start=class_start)
            return route_when_due(timing, now, class_start)
        require(origin in navigation.nodes and destination in navigation.nodes,
                'unknown campus place', ReasonCode.MISSING_DEPENDENCY)
        window = self._class_departure_window(navigation, origin, destination, now, class_start, early_departure_lead)
        if window is None:
            timing = self._route_timing(navigation, origin, destination, now, class_start=class_start)
            require(now + timedelta(seconds=timing[0]['travel_s']) < class_end,
                    'no feasible campus route before class ends', ReasonCode.PROVIDER_UNAVAILABLE)
            return route_when_due(timing, now, class_start)
        due, _latest = window
        if now < due:
            return None
        route, origin_wait_s = self._route_timing(navigation, origin, destination, now, class_start=class_start,
                                                  depart_immediately=True)
        arrival = now + timedelta(seconds=route['travel_s'])
        return route if not origin_wait_s and arrival <= class_start else None

    @staticmethod
    def _class_departure_window(navigation, origin, destination, now, start, lead):
        if now > start:
            return None
        windows = navigation.walk_departure_windows(origin, destination, now, start, stay_until_deadline=True)
        if not windows:
            return None
        opens, latest = next(((opens, latest) for opens, latest in reversed(windows)
                             if latest - opens >= lead), windows[0])
        return max(now, opens, latest - lead), latest

    def class_departure(self, origin, destination, now, start, lead):
        navigation = self._adapter()
        if navigation is None or origin == destination or destination not in navigation.nodes:
            return start, 0
        window = self._class_departure_window(navigation, origin, destination, now, start, lead)
        if window is not None:
            departure, latest = window
            return departure, int((start - latest).total_seconds())
        if now < start:
            try:
                route, _ = self._route_timing(navigation, origin, destination, now, class_start=start)
            except KernelError:
                return start, 0
            travel_s = route["travel_s"]
            return max(now, start - timedelta(seconds=travel_s)), travel_s
        return start, 0

    @staticmethod
    def _departure_valid_until(navigation, origin, departure, segments, assumptions, *, class_start=None):
        prefix = 'home-area:' + navigation.graph['digest'] + ':'
        if any(type(segment.duration_s) is not int or segment.mode not in ('walk', 'wait')
               or not segment.source.startswith(prefix) for segment in segments):
            return departure
        second_end = departure.replace(microsecond=999999)
        if navigation.access_policy is None or second_end == departure:
            return second_end
        # Opening boundaries and walk/wait durations are whole-second. Check
        # the same traversal at the last representable instant of this second
        # to determine whether any subsecond delay is safe, without rerouting.
        edges = {edge['edge_id']: edge for edge in navigation.graph['edges']}
        at, destination = second_end, origin
        assumptions = frozenset(assumptions)

        def allowed(record, start, end=None, *, node=False):
            decision = navigation._decision(record, start, end, node=node)
            return decision.allowed and assumptions.issuperset(decision.assumptions)

        if not allowed(navigation.nodes[origin], at, node=True):
            return departure
        for segment in segments:
            finish = at + timedelta(seconds=segment.duration_s)
            if segment.mode == 'walk':
                record, node = edges[segment.source.removeprefix(prefix)], False
            else:
                record, node = navigation.nodes[segment.origin], True
            if not allowed(record, at, finish, node=node) or not allowed(navigation.nodes[segment.destination], finish, node=True):
                return departure
            at, destination = finish, segment.destination
        if class_start is not None and at <= class_start:
            if not allowed(navigation.nodes[destination], at, class_start, node=True):
                return departure
        return second_end

    def _route_timing(self, navigation, origin, destination, departure_at, *, class_start=None,
                      depart_immediately=False):
        self._routing_enabled()
        if navigation is None:
            require(self.manual is not None, 'no confirmed campus graph or manual paths', ReasonCode.MISSING_DEPENDENCY)
            return self.manual.route_timing(origin, destination, departure_at)
        require(departure_at is not None, 'confirmed campus route requires departure_at')
        departure_at = moment(departure_at)
        class_start = moment(class_start) if class_start is not None and navigation.access_policy is not None else None
        package = navigation.graph
        metadata = {'graph_id': package['graph_id'], 'graph_version': package['version'],
                    'graph_digest': package['digest'], 'attribution': package['attribution'],
                    'departure_at': wire(departure_at)}
        if origin == destination:
            require(origin in navigation.nodes, 'unknown campus place', ReasonCode.MISSING_DEPENDENCY)
            decision = navigation._decision(navigation.nodes[origin], departure_at, node=True)
            require(decision.allowed, 'campus place is unavailable', ReasonCode.MISSING_DEPENDENCY)
            assumptions = decision.assumptions
            if class_start is not None and departure_at < class_start:
                record = navigation.nodes[origin]
                class_access = navigation._decision(record, class_start, node=True)
                wait = navigation._decision(record, departure_at, class_start, node=True)
                require(class_access.allowed and wait.allowed, 'classroom is unavailable through class start',
                        ReasonCode.PROVIDER_UNAVAILABLE)
                assumptions = tuple(dict.fromkeys((*assumptions, *class_access.assumptions, *wait.assumptions)))
            valid_until = self._departure_valid_until(navigation, origin, departure_at, (), assumptions,
                                                      class_start=class_start)
            return {'from_place_id': origin, 'place_id': destination, 'path': (origin,), 'travel_s': 0,
                    **metadata, 'departure_valid_until': wire(valid_until),
                    'assumptions': assumptions, 'needs_review': bool(assumptions)}, 0
        if class_start is None:
            quote = navigation.quote(origin, destination, departure_at)
        else:
            quote = navigation.quote(origin, destination, departure_at, destination_open_at=class_start,
                                      depart_immediately=depart_immediately)
        assumptions = getattr(quote, 'assumptions', ())
        valid_until = self._departure_valid_until(navigation, origin, departure_at, quote.segments, assumptions,
                                                  class_start=class_start)
        origin_wait_s = sum(s.duration_s for s in takewhile(lambda s: s.mode == 'wait', quote.segments))
        return {'from_place_id': origin, 'place_id': destination,
                'path': (origin, *(s.destination for s in quote.segments if s.mode != 'wait')), 'travel_s': quote.duration_s,
                **metadata, 'departure_valid_until': wire(valid_until), 'assumptions': assumptions,
                'needs_review': getattr(quote, 'needs_review', False)}, origin_wait_s
