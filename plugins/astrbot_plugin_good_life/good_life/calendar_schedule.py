"""Recurring calendar schedule, migrated from private_companion/companion_v2/agenda/models.py.

This module is independent of the host plugin; role identity and authorization
remain the host's responsibility.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class AgendaError(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        super().__init__(reason)


def opaque(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise AgendaError("invalid_agenda_id", "identifier must be nonblank and at most 512 characters")
    return value


def normalize_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


class Frequency(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class GapPolicy(str, Enum):
    REJECT = "reject"
    SHIFT_FORWARD = "shift_forward"


@dataclass(frozen=True)
class Schedule:
    anchor: datetime
    timezone_name: str
    frequency: Frequency = Frequency.ONCE
    interval: int = 1
    count: int | None = None
    fold: int = 0
    gap_policy: GapPolicy = GapPolicy.REJECT

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor", normalize_utc(self.anchor, field="schedule anchor"))
        if (not isinstance(self.frequency, Frequency) or not isinstance(self.gap_policy, GapPolicy)
                or type(self.interval) is not int or self.interval < 1
                or (self.count is not None and (type(self.count) is not int or self.count < 1))
                or type(self.fold) is not int or self.fold not in (0, 1)):
            raise AgendaError("invalid_schedule", "recurrence requires positive intervals/counts and an explicit time policy")
        self.zone()

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(opaque(self.timezone_name))
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise AgendaError("timezone_unavailable", "timezone is unknown or its database is unavailable") from error

    def at(self, sequence: int) -> datetime | None:
        if type(sequence) is not int or sequence < 0:
            raise AgendaError("invalid_occurrence", "sequence must be a nonnegative integer")
        if ((self.count is not None and sequence >= self.count)
                or (self.frequency is Frequency.ONCE and sequence > 0)):
            return None
        if sequence == 0:
            return self.anchor
        zone = self.zone()
        local = self.anchor.astimezone(zone).replace(tzinfo=None)
        delta = sequence * self.interval
        try:
            if self.frequency in (Frequency.DAILY, Frequency.WEEKLY):
                wall = local + timedelta(days=delta * (7 if self.frequency is Frequency.WEEKLY else 1))
            else:
                months = delta * (12 if self.frequency is Frequency.YEARLY else 1)
                year, month_zero = divmod(local.year * 12 + local.month - 1 + months, 12)
                month = month_zero + 1
                wall = local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]))
        except (ValueError, OverflowError) as error:
            raise AgendaError("schedule_out_of_range", "next occurrence is outside the supported calendar") from error
        candidate = wall.replace(tzinfo=zone, fold=self.fold).astimezone(timezone.utc)
        roundtrip = candidate.astimezone(zone).replace(tzinfo=None)
        if roundtrip != wall:
            if self.gap_policy is GapPolicy.REJECT:
                raise AgendaError("schedule_needs_resolution", "next local time does not exist under the selected timezone")
            choices = [wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)]
            candidate = min(item for item in choices if item.astimezone(zone).replace(tzinfo=None) > wall)
        return candidate

    def first_at_or_after(self, instant: datetime, *, minimum: int = 0) -> int | None:
        """Jump from the original calendar anchor; never walk every missed occurrence."""
        instant = normalize_utc(instant, field="schedule lower bound")
        if type(minimum) is not int or minimum < 0:
            raise AgendaError("invalid_occurrence", "minimum sequence must be nonnegative")
        anchor, local = self.anchor.astimezone(self.zone()), instant.astimezone(self.zone())
        if self.frequency is Frequency.ONCE:
            estimate = 0
        elif self.frequency in (Frequency.DAILY, Frequency.WEEKLY):
            step = self.interval * (7 if self.frequency is Frequency.WEEKLY else 1)
            estimate = (local.date() - anchor.date()).days // step
        else:
            step = self.interval * (12 if self.frequency is Frequency.YEARLY else 1)
            estimate = ((local.year - anchor.year) * 12 + local.month - anchor.month) // step
        sequence = max(minimum, estimate, 0)
        due = self.at(sequence)
        if due is not None and due < instant:
            sequence += 1
            due = self.at(sequence)
        return sequence if due is not None else None

    @classmethod
    def restore(cls, data: dict | None) -> Schedule | None:
        if data is None:
            return None
        if type(data) is not dict:
            raise AgendaError("invalid_schedule", "schedule must be an object")
        fold = data.get("fold", 0)
        # JSON false/true is a common calendar-tool spelling for fold 0/1; keep
        # the stored contract integer-only but accept the boolean spelling.
        if type(fold) is bool:
            fold = int(fold)
        return cls(datetime.fromisoformat(data["anchor"]), data["timezone_name"],
                   Frequency(data.get("frequency", "once")), data.get("interval", 1),
                   data.get("count"), fold, GapPolicy(data.get("gap_policy", "reject")))
