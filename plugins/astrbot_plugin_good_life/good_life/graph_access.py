"""Small explicit graph access vocabulary; no NLP or inferred campus membership.

Optional v1 fields: access_requirement, access_area, access_conditionals and opening_schedule.
Weekdays are Monday=0..Sunday=6. Windows are local half-open intervals; an
end <= start crosses midnight (equal means 24 hours). 24:00 is end-only.
Unknown text stays unparsed. Only a configured policy may assume it usable.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


REQUIREMENTS = frozenset(('public', 'campus_member', 'student', 'staff', 'closed', 'unknown', 'unparsed'))
# Exact statements only. Narrative qualifications/alternatives are never stripped.
ACCESS_TEXT = {'yes': 'public', 'permissive': 'public', 'public': 'public',
               'designated': 'public', 'no': 'closed',
               '仅校内有资格人员可用': 'campus_member', '仅学生可用': 'student',
               '仅教职工可用': 'staff', '禁止通行': 'closed'}


def valid_requirement(value):
    return isinstance(value, str) and (value in REQUIREMENTS or
        bool(re.fullmatch(r'resident_of:[^\s]+', value)))


def _zone(key):
    return timezone.utc if key == 'UTC' else ZoneInfo(key)


def _valid_access_area(value):
    return type(value) is str and bool(value.strip())


def validate_access_fields(record, *, node=False, package_record=False):
    """Validate access fields and return the timezone of a known explicit schedule."""
    kind = 'place' if node else 'edge'
    region = record.get('region')
    if region is not None and type(region) is not str:
        raise ValueError(f'invalid {kind} region')
    # v1 requires enabled on edges and leaves non-schema node fields untouched.
    # In policy mode, an enabled value on either record must be a boolean.
    if not (package_record and node):
        if type(record.get('enabled', True if node else None)) is not bool:
            raise ValueError(f'invalid {kind} enabled')
    opening_field = 'opening_rules' if node else 'availability'
    for name in ('access', opening_field):
        value = record.get(name)
        if name not in record or (value is not None and type(value) is not str):
            label = 'opening rules' if name == 'opening_rules' else name
            raise ValueError(f'invalid {label}' if node else f'invalid edge {label}')
    if 'access_requirement' in record:
        requirement = record['access_requirement']
        if type(requirement) is not str or not valid_requirement(requirement):
            raise ValueError('invalid access_requirement')
    if 'access_area' in record and not _valid_access_area(record['access_area']):
        raise ValueError('invalid access_area')
    if 'access_conditionals' in record:
        tags = record['access_conditionals']
        if not isinstance(tags, Mapping) or not tags or any(
                type(key) is not str or not re.fullmatch(r'[^:\s]+(?::[^:\s]+)*:conditional', key)
                or type(value) is not str for key, value in tags.items()):
            raise ValueError('invalid access_conditionals')
    if 'opening_schedule' in record:
        return _validate_opening_schedule(record['opening_schedule'], explain_timezone_error=package_record)
    return None


def _validate_opening_schedule(schedule, *, explain_timezone_error):
    status = schedule.get('status') if isinstance(schedule, Mapping) else None
    if type(status) is not str or status not in ('known', 'unknown', 'unparsed'):
        raise ValueError('invalid opening_schedule status')
    if status != 'known':
        if set(schedule) != {'status'}:
            raise ValueError('unknown/unparsed opening_schedule has no invented windows')
        return
    if set(schedule) != {'status', 'timezone', 'windows'}:
        raise ValueError('invalid opening_schedule fields')
    timezone_key = schedule['timezone']
    if type(timezone_key) is not str:
        raise ValueError('opening_schedule requires an IANA timezone')
    windows = schedule['windows']
    if not isinstance(windows, (list, tuple)) or len(windows) > 64:
        raise ValueError('invalid opening windows')
    for window in windows:
        if not isinstance(window, Mapping) or set(window) != {'weekdays', 'start', 'end'}:
            raise ValueError('invalid opening window fields')
        days = window['weekdays']
        if not isinstance(days, (list, tuple)) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days) or len(set(days)) != len(days):
            raise ValueError('opening weekdays must be unique Monday=0..Sunday=6')
        for key in ('start', 'end'):
            value = window[key]
            if type(value) is not str or not (
                    re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value)
                    or (key == 'end' and value == '24:00')):
                raise ValueError('opening time must be HH:MM; 24:00 is end-only')
    try:
        return _zone(timezone_key)
    except ZoneInfoNotFoundError as error:
        if explain_timezone_error and not available_timezones():
            raise ValueError('IANA timezone data is unavailable; install the optional tzdata package') from error
        raise ValueError('opening_schedule has an unknown IANA timezone key') from error
    except ValueError as error:
        raise ValueError('opening_schedule requires an IANA timezone') from error


def access_requirement(record):
    if 'access_requirement' in record:
        return record['access_requirement']
    text = record.get('access')
    if text is None or not text.strip():
        return 'unknown'
    return ACCESS_TEXT.get(text.strip(), 'private' if text.strip() == 'private' else 'unparsed')


def opening_schedule(record, *, node=False):
    if 'opening_schedule' in record:
        return record['opening_schedule']
    text = record.get('opening_rules' if node else 'availability')
    if text is None or not text.strip():
        return {'status': 'unknown'}
    # No timezone can be inferred from geography. A timezone-free 24/7 is exact.
    if text.strip() in ('24/7', 'always'):
        return {'status': 'known', 'timezone': 'UTC', 'windows':
                [{'weekdays': list(range(7)), 'start': '00:00', 'end': '24:00'}]}
    return {'status': 'unparsed'}


def _boundary(day, wall, zone):
    if wall == '24:00':
        day, wall = day + timedelta(days=1), '00:00'
    naive = datetime.combine(day, time.fromisoformat(wall))
    candidates = set()
    for fold in (0, 1):
        utc = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(utc)
    # Ambiguous/nonexistent local boundaries require human review, never guessing.
    return next(iter(candidates)) if len(candidates) == 1 else None


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _boundary_bounds(day, wall, zone):
    """Bound an uncertain wall time without choosing a DST interpretation."""
    if wall == '24:00':
        day, wall = day + timedelta(days=1), '00:00'
    naive = datetime.combine(day, time.fromisoformat(wall))
    candidates = [naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)]
    return min(candidates), max(candidates)


def _opening_intervals(schedule, zone, begins, ends):
    day = begins.astimezone(zone).date() - timedelta(days=1)
    last = ends.astimezone(zone).date()
    known, uncertain = [], []
    while day <= last:
        for window in schedule['windows']:
            if day.weekday() not in window['weekdays']:
                continue
            end_day = day + timedelta(days=window['end'] <= window['start'] and window['end'] != '24:00')
            start = _boundary(day, window['start'], zone)
            end = _boundary(end_day, window['end'], zone)
            if start is None or end is None:
                # Only this window's possible extent is unknown. An unrelated
                # Sunday transition must not poison a Monday opening check.
                earliest, _ = _boundary_bounds(day, window['start'], zone)
                _, latest = _boundary_bounds(end_day, window['end'], zone)
                uncertain.append((earliest, latest))
            else:
                known.append((start, end))
        day += timedelta(days=1)
    return _merge(known), uncertain


def _covers(intervals, begins, ends):
    return any(start <= begins < end and ends <= end for start, end in intervals)


UNBOUNDED_OPEN = ((datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc)),)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    assumptions: tuple[str, ...] = ()

    @property
    def needs_review(self):
        return bool(self.assumptions)


@dataclass(frozen=True)
class OpeningAvailability:
    intervals: tuple
    assumptions: tuple[str, ...] = ()
    certain: tuple | None = None
    uncertain_note: str = ''

    def decision(self, begins, ends):
        if not _covers(self.intervals, begins, ends):
            return AccessDecision(False)
        notes = self.assumptions
        if self.certain is not None and not _covers(self.certain, begins, ends):
            notes = (*notes, self.uncertain_note)
        return AccessDecision(True, notes)


@dataclass(frozen=True)
class GraphAccessPolicy:
    credentials: frozenset[str] = frozenset()
    private_credentials: Mapping = field(default_factory=dict)
    treat_unparsed_access_as: str = 'blocked'
    treat_unknown_opening_as: str = 'blocked'

    def __post_init__(self):
        if not isinstance(self.credentials, (set, frozenset, tuple, list)) or any(not valid_requirement(c) or c in ('public', 'closed', 'unknown', 'unparsed') for c in self.credentials):
            raise ValueError('invalid graph access credentials')
        if not isinstance(self.private_credentials, Mapping):
            raise ValueError('private credentials must map graph_id[/area] to a requirement')
        for key, value in self.private_credentials.items():
            if type(key) is not str or not key.strip() or not valid_requirement(value) or value in ('public', 'closed', 'unknown', 'unparsed'):
                raise ValueError('private access must require an explicit credential')
        if self.treat_unparsed_access_as not in ('blocked', 'allowed') or self.treat_unknown_opening_as not in ('blocked', 'allowed'):
            raise ValueError('unknown access/opening treatment must be blocked or allowed')
        from types import MappingProxyType
        object.__setattr__(self, 'credentials', frozenset(self.credentials))
        object.__setattr__(self, 'private_credentials', MappingProxyType(dict(self.private_credentials)))

    @classmethod
    def from_settings(cls, settings):
        enabled = settings.get('graph_access_enabled', False)
        if type(enabled) is not bool:
            raise ValueError('graph_access_enabled must be boolean')
        if not enabled:
            return None  # Preserve existing adapter behaviour unless explicitly enabled.
        import json
        mappings = settings.get('graph_private_credentials', '{}')
        if isinstance(mappings, str):
            mappings = json.loads(mappings)
        return cls(settings.get('graph_access_credentials', ()), mappings,
                   settings.get('treat_unparsed_access_as', 'blocked'), settings.get('treat_unknown_opening_as', 'blocked'))

    def _has(self, requirement):
        return requirement in self.credentials or (requirement == 'campus_member' and bool(self.credentials & {'student', 'staff'}))

    def evaluate(self, record, graph_id, begins, ends=None, *, node=False):
        if begins.tzinfo is None or begins.utcoffset() is None:
            raise ValueError('graph opening evaluation requires an aware datetime')
        ends = begins if ends is None else ends
        if ends.tzinfo is None or ends.utcoffset() is None:
            raise ValueError('invalid graph traversal interval')
        begins, ends = begins.astimezone(timezone.utc), ends.astimezone(timezone.utc)
        if ends < begins:
            raise ValueError('invalid graph traversal interval')
        return self.availability(record, graph_id, begins, ends, node=node).decision(begins, ends)

    def availability(self, record, graph_id, begins, ends, *, node=False):
        """Compile continuous open intervals covering the requested search dates."""
        if not isinstance(record, Mapping) or record.get('enabled') is False:
            return OpeningAvailability(())
        try:
            zone = validate_access_fields(record, node=node)
        except ValueError:
            return OpeningAvailability(())
        requirement, notes = access_requirement(record), []
        identity = record.get('place_id', record.get('edge_id', '?'))
        if requirement == 'private':
            area = record.get('access_area', record.get('region'))
            requirement = self.private_credentials.get(graph_id + '/' + area) if area else None
            requirement = requirement or self.private_credentials.get(graph_id)
            if requirement is None:
                return OpeningAvailability(())  # Missing private mapping cannot be assumed away.
        if requirement == 'closed' or (requirement not in ('public', 'unknown', 'unparsed') and not self._has(requirement)):
            return OpeningAvailability(())
        # Conditions add uncertainty without overriding a baseline prohibition or credential.
        uncertainty = 'unparsed' if 'access_conditionals' in record else requirement
        if uncertainty in ('unknown', 'unparsed'):
            if self.treat_unparsed_access_as != 'allowed':
                return OpeningAvailability(())
            notes.append(f'{identity}: access {uncertainty}; assumed allowed by treat_unparsed_access_as')
        if 'osm_access_conflicts' in record:
            notes.append(f'{identity}: regenerated OSM access differs from the previous record; review required')
        schedule = opening_schedule(record, node=node)
        status = schedule['status']
        if status == 'known':
            if schedule['timezone'] == 'UTC' and any(
                    len(window['weekdays']) == 7 and window['start'] == '00:00' and window['end'] == '24:00'
                    for window in schedule['windows']):
                return OpeningAvailability(UNBOUNDED_OPEN, tuple(notes))
            known, uncertain = _opening_intervals(schedule, zone, begins, ends)
            if uncertain and self.treat_unknown_opening_as == 'allowed':
                return OpeningAvailability(tuple(_merge((*known, *uncertain))), tuple(notes), tuple(known),
                    f'{identity}: opening unknown; assumed allowed by treat_unknown_opening_as')
            return OpeningAvailability(tuple(known), tuple(notes))
        if self.treat_unknown_opening_as != 'allowed':
            return OpeningAvailability(())
        notes.append(f'{identity}: opening {status}; assumed allowed by treat_unknown_opening_as')
        return OpeningAvailability(UNBOUNDED_OPEN, tuple(notes))
