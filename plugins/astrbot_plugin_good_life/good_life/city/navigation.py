"""Offline navigation port and confirmed-home-graph / manual-transit adapter.

No network, geocoding, implicit locations or runtime OSM access. Construction
reads the confirmed archive once; quotes own immutable, versioned route facts.
"""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import heapq
from math import ceil
from typing import Protocol

from ..contracts.models import ReasonCode
from ..graph_access import AccessDecision, OpeningAvailability, UNBOUNDED_OPEN
from ..kernel.codec import digest, integer, moment, name, require


@dataclass(frozen=True)
class Segment:
    origin: str
    destination: str
    mode: str
    duration_s: int
    cost_minor: int
    uncertainty_s: int
    source: str
    delay_s: int = 0
    cancel_after_s: int | None = None
    safe_stop: str | None = None
    refund_minor: int = 0


@dataclass(frozen=True)
class RouteQuote:
    route_id: str
    origin: str
    destination: str
    departure_at: datetime
    valid_until: datetime
    segments: tuple[Segment, ...]
    currency: str
    graph_id: str
    graph_version: int
    graph_digest: str
    attribution: str
    provider: str = "offline-home-transit@1"

    @property
    def duration_s(self):
        return sum(segment.duration_s for segment in self.segments)

    @property
    def cost_minor(self):
        return sum(segment.cost_minor for segment in self.segments)


@dataclass(frozen=True)
class GraphRouteQuote(RouteQuote):
    """Opt-in access evidence; legacy and provider quote wire shapes stay stable."""
    assumptions: tuple[str, ...] = ()
    needs_review: bool = False


class AsyncNavigationAdapter(Protocol):
    """Network-capable callers await this outside kernel/database transactions."""
    async def aquote(self, origin: str, destination: str, departure_at: datetime) -> RouteQuote: ...


class NavigationAdapter(Protocol):
    def quote(self, origin: str, destination: str, departure_at: datetime) -> RouteQuote: ...


class PanoramaPort(Protocol):
    """Optional material lookup only; never evidence of arrival/current facts."""
    def material(self, place_id: str, heading: int): ...


@dataclass(frozen=True)
class TransitService:
    service_id: str
    origin: str
    destination: str
    mode: str
    duration_s: int
    cost_minor: int
    uncertainty_s: int
    # Empty means an explicitly configured duration estimate, not live service.
    departures: tuple[datetime, ...] = ()
    delay_s: int = 0
    cancel_after_s: int | None = None
    safe_stop: str | None = None
    refund_minor: int = 0


def validate_segment(segment):
    require(isinstance(segment, Segment), "invalid navigation segment")
    name(segment.origin)
    name(segment.destination)
    name(segment.source)
    require(segment.mode in ("walk", "bus", "metro", "wait", "stay"), "unsupported travel mode")
    integer(segment.duration_s, 1)
    for value in (segment.cost_minor, segment.uncertainty_s, segment.delay_s, segment.refund_minor):
        integer(value)
    require(segment.delay_s < segment.duration_s and segment.refund_minor <= segment.cost_minor,
            "invalid delay or fare refund")
    if segment.cancel_after_s is not None:
        require(1 <= integer(segment.cancel_after_s, 1) <= segment.duration_s and segment.safe_stop,
                "cancellation needs a timed safe stop")
    else:
        require(segment.safe_stop is None, "safe stop requires a cancellation event")


class OfflineNavigation:
    def __init__(self, store, graph_id, *, currency, external_places=(), services=(),
                 access_policy=None, settings=None, graph_version=None, graph_digest=None,
                 confirmed_campus_only=False):
        from ..economy.wallet import currency as validate_currency
        from ..kernel.codec import freeze
        from ..graph_access import GraphAccessPolicy
        require(access_policy is None or settings is None, "provide policy or settings, not both")
        self.access_policy = GraphAccessPolicy.from_settings(settings) if settings is not None else access_policy
        require(self.access_policy is None or isinstance(self.access_policy, GraphAccessPolicy), "invalid graph access policy")
        package = store.latest(graph_id) if graph_version is None else store.version(graph_id, graph_version, digest=graph_digest)
        require(package is not None, "no confirmed home-area graph", ReasonCode.MISSING_DEPENDENCY)
        from ..graph_packages import ordinary_building_places
        self.blocked_places = ordinary_building_places(package) if confirmed_campus_only else frozenset()
        self.graph = freeze(package)
        self.currency = validate_currency(currency)
        self.nodes = {node["place_id"]: node for node in self.graph["nodes"]}
        self.external_places = tuple(name(place) for place in external_places)
        require(len(set(self.external_places)) == len(self.external_places)
                and not set(self.external_places).intersection(self.nodes), "external place identities overlap")
        self.gateways = {}
        for gateway in self.graph["gateways"]:
            self.gateways.setdefault(gateway["place_id"], set()).update(gateway["modes"])
        self.services = tuple(services)
        require(len({s.service_id for s in self.services}) == len(self.services), "duplicate transit service")
        known = set(self.nodes) | set(self.external_places)
        for service in self.services:
            require(isinstance(service, TransitService), "invalid transit service")
            name(service.service_id)
            integer(service.duration_s, 1)
            integer(service.delay_s)
            validate_segment(self._segment(service))
            require(service.mode in ("walk", "bus", "metro") and service.origin != service.destination,
                    "invalid transit stub")
            require(service.origin in known and service.destination in known, "unknown transit endpoint")
            require(not (service.origin in self.nodes and service.destination in self.nodes),
                    "home-area travel must use confirmed graph edges")
            for endpoint in (service.origin, service.destination, service.safe_stop):
                if endpoint is not None:
                    require(endpoint in known, "unknown safe stop")
                    require(endpoint not in self.nodes or service.mode in self.gateways.get(endpoint, ()),
                            "external transit must connect through a compatible gateway")
            require(type(service.departures) is tuple, "timetable must be immutable")
            times = tuple(moment(value) for value in service.departures)
            require(times == tuple(sorted(set(times))), "departures must be sorted and unique")

    @staticmethod
    def _segment(service):
        return Segment(service.origin, service.destination, service.mode,
            service.duration_s + service.delay_s, service.cost_minor, service.uncertainty_s,
            "manual-transit:" + service.service_id, service.delay_s, service.cancel_after_s,
            service.safe_stop, service.refund_minor)

    @staticmethod
    def _open(record, node=False):
        # Free-text rules cannot be evaluated safely. Unspecified rules mean the
        # configured topology is usable; explicit unknown restrictions fail shut.
        return (record.get("enabled", True) and "access_conditionals" not in record
                and record.get("access") in (None, "yes", "public", "permissive", "designated")
                and record.get("opening_rules" if node else "availability") in (None, "24/7", "always"))

    def _decision(self, record, begins, ends=None, *, node=False):
        if node and record.get("place_id") in self.blocked_places:
            return AccessDecision(False)
        if self.access_policy is None:
            return AccessDecision(self._open(record, node))
        return self.access_policy.evaluate(record, self.graph['graph_id'], begins, ends, node=node)

    @staticmethod
    def _interval_at(intervals, at):
        index = bisect_right(intervals, at, key=lambda interval: interval[0]) - 1
        return index if index >= 0 and at < intervals[index][1] else None

    @staticmethod
    def _parts(segment, wait, uncertainty=0):
        if not wait:
            return (segment,)
        return (Segment(segment.origin, segment.origin, 'wait', wait, 0, uncertainty, segment.source), segment)

    @staticmethod
    def _node_notes(source, target, arrival, start, finish):
        # Waiting can end at the boundary, but boarding/leaving must be strictly
        # before closing. The caller enforces that with the source interval.
        return (*source.decision(arrival, start).assumptions,
                *source.decision(start, start).assumptions,
                *target.decision(finish, finish).assumptions)

    def _walk_candidate(self, segment, edge, source, source_end, target, arrival, until, *, safe_stop=None, wait_uncertainty=0):
        """Yield the earliest flexible departure into each reachable destination interval."""
        if safe_stop is not None and not safe_stop.intervals:
            return
        duration = timedelta(seconds=segment.duration_s)
        stop_offset = timedelta(seconds=segment.cancel_after_s) if safe_stop is not None else None
        earliest = arrival + duration
        edge_index = bisect_left(edge.intervals, earliest, key=lambda interval: interval[1])
        target_index = bisect_right(target.intervals, earliest, key=lambda interval: interval[1])
        for index in range(target_index, len(target.intervals)):
            opens, closes = target.intervals[index]
            earliest_start = max(earliest, opens) - duration
            while edge_index < len(edge.intervals):
                edge_open, edge_close = edge.intervals[edge_index]
                candidate = max(earliest_start, edge_open)
                wait = ceil((candidate - arrival).total_seconds())
                start = arrival + timedelta(seconds=wait)
                finish = start + duration
                if start >= source_end or (until is not None and finish > until):
                    return
                if finish >= closes:
                    break  # The following destination interval may still be reachable.
                if finish > edge_close:
                    edge_index += 1
                    continue
                stop_notes = ()
                if safe_stop is not None:
                    stop_at = start + stop_offset
                    stop_index = bisect_right(safe_stop.intervals, stop_at, key=lambda interval: interval[1])
                    if stop_index == len(safe_stop.intervals):
                        return
                    stop_opens = safe_stop.intervals[stop_index][0]
                    if stop_at < stop_opens:
                        earliest_start = stop_opens - stop_offset
                        continue
                    stop_notes = safe_stop.decision(stop_at, stop_at).assumptions
                notes = (*self._node_notes(source, target, arrival, start, finish),
                         *edge.decision(start, finish).assumptions, *stop_notes)
                yield index, self._parts(segment, wait, wait_uncertainty), notes
                break

    def _service_candidates(self, service, source, source_end, target, arrival, until, safe_stop):
        if not target.intervals or (safe_stop is not None and not safe_stop.intervals):
            return  # Filter closed legacy gateways before validating timetable alignment.
        segment = self._segment(service)
        if not service.departures:
            yield from self._walk_candidate(segment, OpeningAvailability(UNBOUNDED_OPEN), source,
                                            source_end, target, arrival, until, safe_stop=safe_stop,
                                            wait_uncertainty=service.uncertainty_s)
            return
        seen = set()
        for departure in service.departures:
            start = moment(departure)
            if start < arrival:
                continue
            finish = start + timedelta(seconds=segment.duration_s)
            if start >= source_end or (until is not None and finish > until):
                return
            index = self._interval_at(target.intervals, finish)
            if index is None or index in seen:
                continue
            stop_notes = ()
            if safe_stop is not None:
                stop_at = start + timedelta(seconds=segment.cancel_after_s)
                decision = safe_stop.decision(stop_at, stop_at)
                if not decision.allowed:
                    continue
                stop_notes = decision.assumptions
            wait = int((start - arrival).total_seconds())
            if arrival + timedelta(seconds=wait) != start:
                if self.access_policy is not None:
                    continue
                require(False, 'timetable needs whole-second alignment')
            yield index, self._parts(segment, wait, service.uncertainty_s), (
                *self._node_notes(source, target, arrival, start, finish), *stop_notes)
            seen.add(index)
            if index == len(target.intervals) - 1:
                return

    def _walk_edges(self, *, reverse=False):
        adjacency = {}
        for edge in self.graph['edges']:
            if not edge['enabled']:
                continue
            source = 'home-area:' + self.graph['digest'] + ':' + edge['edge_id']
            segment = Segment(edge['from'], edge['to'], 'walk', edge['estimated_duration_s'], 0,
                              edge['estimated_duration_s'], source)
            key = segment.destination if reverse else segment.origin
            adjacency.setdefault(key, []).append((segment, edge))
            if edge['direction'] == 'both':
                key = segment.origin if reverse else segment.destination
                adjacency.setdefault(key, []).append((replace(segment, origin=segment.destination,
                                                               destination=segment.origin), edge))
        return adjacency

    def _availability_for(self, departure_at, until):
        policy = self.access_policy
        cache = {}
        open_access = OpeningAvailability(UNBOUNDED_OPEN)
        closed_access = OpeningAvailability(())

        def availability(record, *, node=False):
            if record is None:
                return open_access
            if node and record.get("place_id") in self.blocked_places:
                return closed_access
            key = id(record)
            if key not in cache:
                if policy is None:
                    cache[key] = open_access if self._open(record, node) else OpeningAvailability(())
                else:
                    cache[key] = policy.availability(record, self.graph['graph_id'], departure_at, until, node=node)
            return cache[key]

        return availability

    def latest_walk_departure_window(self, origin, destination, earliest, arrive_by):
        """Return inclusive first-movement bounds for the latest feasible walking path."""
        windows = self.walk_departure_windows(origin, destination, earliest, arrive_by)
        return windows[-1] if windows else None

    def walk_departure_windows(self, origin, destination, earliest, arrive_by, *, stay_until_deadline=False):
        """Return sorted continuous first-movement windows on the quote's second grid."""
        earliest, arrive_by = moment(earliest), moment(arrive_by)
        require(self.access_policy is not None and origin in self.nodes and destination in self.nodes
                and origin != destination, 'latest departure requires distinct graph walking endpoints and a policy')
        if arrive_by < earliest:
            return ()
        search_end = min(arrive_by, earliest + timedelta(days=7))
        if stay_until_deadline and arrive_by > search_end:
            return ()
        availability = self._availability_for(earliest, search_end)
        origin_index = self._interval_at(availability(self.nodes[origin], node=True).intervals, earliest)
        if origin_index is None:
            return ()

        second = timedelta(seconds=1)
        deadline = (search_end - earliest) // second

        def bounds(intervals, *, strict_end=True):
            # The search uses the same whole-second grid as forward quotes.
            # A point at closing is out of bounds, but an edge may finish there.
            for start, end in intervals:
                low = max(0, -((earliest - start) // second))
                high = (-((earliest - end) // second) - 1 if strict_end else (end - earliest) // second)
                yield low, min(deadline, high)

        adjacency = self._walk_edges(reverse=True)
        queue, best, settled = [], {}, set()
        destination_intervals = availability(self.nodes[destination], node=True).intervals
        deadline_index = self._interval_at(destination_intervals, arrive_by) if stay_until_deadline else None
        for index, (opens, closes) in enumerate(bounds(destination_intervals)):
            if opens <= closes and (not stay_until_deadline or index == deadline_index):
                label = (destination, index)
                best[label] = closes
                heapq.heappush(queue, (-closes, label))
        improvements = 0
        origin_label = (origin, origin_index)
        windows = []
        while queue:
            negative, label = heapq.heappop(queue)
            latest = -negative
            if label in settled or latest != best[label]:
                continue
            require(len(settled) < 50_000, 'route search exceeded 50000 open intervals', ReasonCode.PROVIDER_UNAVAILABLE)
            settled.add(label)
            place, index = label
            target = availability(self.nodes[place], node=True)
            target_open = max(0, -((earliest - target.intervals[index][0]) // second))
            for segment, edge in adjacency.get(place, ()):
                duration = segment.duration_s
                latest_start = latest - duration
                if latest_start < 0:
                    continue
                source = availability(self.nodes[segment.origin], node=True)
                edge_bounds = tuple(bounds(availability(edge).intervals, strict_end=False))
                for source_index, (opens, closes) in enumerate(bounds(source.intervals)):
                    candidate_label = (segment.origin, source_index)
                    if candidate_label in settled:
                        continue
                    lower = max(opens, target_open - duration)
                    upper = min(closes, latest_start)
                    if lower > upper:
                        continue
                    for edge_open, edge_close in reversed(edge_bounds):
                        start = min(upper, edge_close - duration)
                        first = max(lower, edge_open)
                        if start < first:
                            continue
                        if candidate_label == origin_label or start > best.get(candidate_label, -1):
                            improvements += 1
                            require(improvements <= 100_000, 'route search exceeded 100000 improvements',
                                    ReasonCode.PROVIDER_UNAVAILABLE)
                            if candidate_label == origin_label:
                                windows.append((first, start))
                                continue  # Keep earlier openings of this edge as well.
                            best[candidate_label] = start
                            heapq.heappush(queue, (-start, candidate_label))
                        break
        merged = []
        for first, last in sorted(windows):
            if merged and first <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], last))
            else:
                merged.append((first, last))
        return tuple((earliest + first * second, earliest + last * second) for first, last in merged)

    def quote(self, origin, destination, departure_at, *, destination_open_at=None, depart_immediately=False):
        departure_at = moment(departure_at)
        if destination_open_at is not None:
            destination_open_at = moment(destination_open_at)
        known = set(self.nodes) | set(self.external_places)
        require(origin in known and destination in known and origin != destination, "unknown/equal route endpoints")
        adjacency = self._walk_edges()
        # Staying inside the home area never shortcuts through transit.
        services = {}
        if not (origin in self.nodes and destination in self.nodes):
            for service in self.services:
                services.setdefault(service.origin, []).append(service)
        policy = self.access_policy
        until = departure_at + timedelta(days=7) if policy is not None else None
        availability = self._availability_for(departure_at, until)

        # An earlier arrival dominates other arrivals only while waiting can
        # stay within one continuous open interval. Closed gaps get no labels.
        start_index = self._interval_at(availability(self.nodes.get(origin), node=True).intervals, departure_at)
        start_label = (origin, start_index)
        queue = [(0, 0, start_label)] if start_index is not None else []
        best, entered, settled, serial = {start_label: 0}, {}, set(), 0
        while queue:
            elapsed, _, label = heapq.heappop(queue)
            if label in settled or elapsed != best[label]:
                continue
            require(policy is None or len(settled) < 50_000,
                    'route search exceeded 50000 open intervals', ReasonCode.PROVIDER_UNAVAILABLE)
            settled.add(label)
            place, index = label
            arrival = departure_at + timedelta(seconds=elapsed)
            destination_notes = ()
            can_finish = place == destination
            if can_finish and destination_open_at is not None and arrival < destination_open_at:
                classroom = availability(self.nodes.get(destination), node=True)
                stays = classroom.decision(arrival, destination_open_at)
                at_start = classroom.decision(destination_open_at, destination_open_at)
                can_finish = stays.allowed and at_start.allowed
                destination_notes = (*stays.assumptions, *at_start.assumptions)
            if can_finish:
                steps, evidence = [], []
                cursor = label
                while cursor != start_label:
                    cursor, parts, notes = entered[cursor]
                    steps.append(parts)
                    evidence.append(notes)
                route = tuple(segment for parts in reversed(steps) for segment in parts)
                identity = digest((origin, destination, departure_at, self.graph['digest'], route))
                facts = (identity, origin, destination, departure_at, departure_at + timedelta(minutes=5),
                         route, self.currency, self.graph['graph_id'], self.graph['version'],
                         self.graph['digest'], self.graph['attribution'])
                if policy is None:
                    return RouteQuote(*facts)
                notes = tuple(dict.fromkeys(note for group in (*reversed(evidence), destination_notes) for note in group))
                return GraphRouteQuote(*facts, assumptions=notes, needs_review=bool(notes))
            source = availability(self.nodes.get(place), node=True)
            source_end = source.intervals[index][1]
            if depart_immediately and label == start_label:
                source_end = min(source_end, departure_at + timedelta(seconds=1))
            candidates = []
            for segment, edge in adjacency.get(place, ()):
                target = availability(self.nodes[segment.destination], node=True)
                if len(target.intervals) == 1 and (segment.destination, 0) in settled:
                    continue  # Returning to an already settled interval cannot improve it.
                candidates.extend(self._walk_candidate(segment, availability(edge), source, source_end, target, arrival, until))
            for service in services.get(place, ()):
                target = availability(self.nodes.get(service.destination), node=True)
                safe_stop = None
                if policy is not None and service.safe_stop in self.nodes:
                    safe_stop = availability(self.nodes[service.safe_stop], node=True)
                candidates.extend(self._service_candidates(service, source, source_end, target, arrival, until, safe_stop))
            for target_index, parts, notes in candidates:
                end = (parts[-1].destination, target_index)
                total = elapsed + sum(p.duration_s for p in parts)
                if end in settled or total >= best.get(end, float('inf')):
                    continue
                serial += 1
                require(policy is None or serial <= 100_000,
                        'route search exceeded 100000 improvements', ReasonCode.PROVIDER_UNAVAILABLE)
                best[end] = total
                entered[end] = (label, parts, notes)
                heapq.heappush(queue, (total, serial, end))
        require(False, '没有可用离线路线；请检查通路、出口和手工时刻表', ReasonCode.PROVIDER_UNAVAILABLE)
