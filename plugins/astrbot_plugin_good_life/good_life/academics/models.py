"""Manual academic records and bounded, timezone-aware occurrence expansion.

These are domain records, separate from the frozen shared contracts. Dates and
wall times retain their local meaning; concrete boundaries are always UTC.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..kernel.codec import digest, freeze, integer, name, require, wire


@dataclass(frozen=True)
class AcademicsConfig:
    enabled: bool = False
    study_enabled: bool = False
    grades_enabled: bool = False

    def __post_init__(self):
        require(type(self.enabled) is bool, "academics.enabled must be boolean")
        require(type(self.study_enabled) is bool, "academics.study_enabled must be boolean")
        require(type(self.grades_enabled) is bool, "academics.grades_enabled must be boolean")


def fields(value, expected, label):
    require(isinstance(value, Mapping) and set(value) == set(expected.split()), label + ": invalid fields")


def day(value):
    require(type(value) is str and len(value) == 10, "date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        require(False, "date must be YYYY-MM-DD")
    require(parsed.isoformat() == value, "date must be YYYY-MM-DD")
    return parsed


def wall(value):
    require(type(value) is str and len(value) == 5, "time must be HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        require(False, "time must be HH:MM")
    require(parsed.strftime("%H:%M") == value, "time must be HH:MM")
    return parsed


def sequence(value, maximum, label):
    require(type(value) in (tuple, list) and len(value) <= maximum, label + ": invalid or oversized array")
    return value


def zone(value):
    name(value)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        require(False, "timezone must be an available IANA timezone")


def boundary(local_date, local_time, tz):
    """Reject nonexistent and ambiguous DST times instead of guessing a fold."""
    naive = datetime.combine(local_date, wall(local_time))
    possibilities = set()
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(tz).replace(tzinfo=None) == naive:
            possibilities.add(candidate)
    require(len(possibilities) == 1, "class boundary is ambiguous or nonexistent in timezone")
    return possibilities.pop()


def validate_term(record):
    fields(record, "term_id academic_year timezone start_date end_date week_starts holidays makeup_days source", "term")
    for key in ("term_id", "academic_year"):
        name(record[key])
    require(record["source"] == "manual", "term source must be manual")
    tz = zone(record["timezone"])
    first, last = day(record["start_date"]), day(record["end_date"])
    require(0 < (last - first).days <= 366, "term range must be one to 366 dates, end exclusive")
    weeks = [day(item) for item in sequence(record["week_starts"], 53, "week_starts")]
    require(weeks and weeks == sorted(set(weeks)), "teaching weeks must be unique and ordered")
    require(all(first <= item < last for item in weeks), "teaching week starts must lie inside the term")
    require(all((b - a).days >= 7 for a, b in zip(weeks, weeks[1:])), "teaching weeks overlap")
    holidays = [day(item) for item in sequence(record["holidays"], 366, "holidays")]
    require(len(set(holidays)) == len(holidays) and all(first <= item < last for item in holidays), "invalid holiday dates")
    makeup, teaching = set(), set()
    for item in sequence(record["makeup_days"], 366, "makeup_days"):
        fields(item, "date teaching_date", "makeup day")
        actual, original = day(item["date"]), day(item["teaching_date"])
        require(first <= actual < last and first <= original < last and actual != original, "makeup date outside term")
        require(actual not in makeup and original not in teaching and actual not in holidays, "duplicate or holiday makeup day")
        require(any(start <= original < start + timedelta(days=7) for start in weeks), "makeup has no teaching week")
        makeup.add(actual)
        teaching.add(original)
    require(not makeup.intersection(teaching), "makeup chains are not supported")
    # Check that local midnights exist as well (some zones skip whole dates).
    boundary(first, "00:00", tz)
    boundary(last, "00:00", tz)
    return freeze(record)


def validate_entry(record, term):
    fields(record, "slot_id term_id course weekday start_time end_time weeks week_rule place_id exceptions source", "timetable slot")
    name(record["slot_id"])
    require(record["term_id"] == term["term_id"] and record["source"] == "manual", "slot term/source mismatch")
    fields(record["course"], "course_id name kind credits teacher_ref", "course")
    course = record["course"]
    name(course["course_id"])
    name(course["name"])
    require(course["kind"] in ("required", "elective"), "course kind must be required/elective")
    require(type(course["credits"]) in (int, float) and math.isfinite(course["credits"])
            and 0 <= course["credits"] <= 100, "credits must be finite in 0..100")
    if course["teacher_ref"] is not None:
        name(course["teacher_ref"])
    require(1 <= integer(record["weekday"], 1) <= 7, "weekday must be 1..7")
    require(wall(record["start_time"]) < wall(record["end_time"]), "class must end after start on the same date")
    weeks = sequence(record["weeks"], 53, "weeks")
    require(weeks and len(set(weeks)) == len(weeks), "weeks must be nonempty and unique")
    require(all(1 <= integer(item, 1) <= len(term["week_starts"]) for item in weeks), "week outside teaching week map")
    require(record["week_rule"] in ("all", "odd", "even"), "unsupported week rule")
    name(record["place_id"])
    dates = set()
    for item in sequence(record["exceptions"], 366, "exceptions"):
        fields(item, "date cancelled start_time end_time place_id", "session exception")
        actual = day(item["date"])
        require(day(term["start_date"]) <= actual < day(term["end_date"]) and actual not in dates, "invalid exception date")
        dates.add(actual)
        require(type(item["cancelled"]) is bool, "cancelled must be boolean")
        require((item["start_time"] is None) == (item["end_time"] is None), "exception times must be paired")
        if item["start_time"] is not None:
            require(wall(item["start_time"]) < wall(item["end_time"]), "invalid exception times")
        if item["place_id"] is not None:
            name(item["place_id"])
    return freeze(record)


def expand(term, entries, *, revision=1):
    """Expand at most one bounded term. Makeup moves the logical teaching date.

    Session identity excludes room, wall time and revision, including makeup
    moves. A cancelled occurrence remains visible for stale task invalidation.
    """
    term = validate_term(term)
    tz = zone(term["timezone"])
    holidays = set(term["holidays"])
    moved = {item["teaching_date"]: item["date"] for item in term["makeup_days"]}
    makeup_dates = set(moved.values())
    result, ids = [], set()
    for raw in sequence(entries, 128, "timetable"):
        entry = validate_entry(raw, term)
        require(entry["slot_id"] not in ids, "duplicate slot_id")
        ids.add(entry["slot_id"])
        overrides = {item["date"]: item for item in entry["exceptions"]}
        used = set()
        for number, start in enumerate(term["week_starts"], 1):
            if number not in entry["weeks"] or (entry["week_rule"] == "odd" and number % 2 == 0) or (entry["week_rule"] == "even" and number % 2 == 1):
                continue
            week_start = day(start)
            logical = week_start + timedelta(days=(entry["weekday"] - week_start.isoweekday()) % 7)
            if logical >= day(term["end_date"]):
                continue  # A partial final teaching week ends at the term fence.
            original = logical.isoformat()
            if original in makeup_dates and original not in moved:
                continue  # The makeup day's normal timetable is replaced.
            actual = moved.get(original, original)
            if actual in holidays:
                continue
            exception = overrides.get(actual)
            if exception:
                used.add(actual)
            start_time = (exception or {}).get("start_time") or entry["start_time"]
            end_time = (exception or {}).get("end_time") or entry["end_time"]
            begins, ends = boundary(day(actual), start_time, tz), boundary(day(actual), end_time, tz)
            require(begins < ends, "class UTC end must follow start")
            session_id = "class-" + digest((term["term_id"], entry["slot_id"], original))[:40]
            result.append({"session_id": session_id, "slot_id": entry["slot_id"], "term_id": term["term_id"],
                "course_id": entry["course"]["course_id"], "course_name": entry["course"]["name"],
                "start_at": wire(begins), "end_at": wire(ends), "local_date": actual,
                "place_id": (exception or {}).get("place_id") or entry["place_id"], "revision": revision,
                "cancelled": (exception or {}).get("cancelled", False), "source": "manual"})
        require(used == set(overrides), "exception must address an expanded teaching date")
    result.sort(key=lambda item: (item["start_at"], item["session_id"]))
    live = [item for item in result if not item["cancelled"]]
    require(all(a["end_at"] <= b["start_at"] for a, b in zip(live, live[1:])), "timetable slots overlap")
    return tuple(freeze(item) for item in result)
