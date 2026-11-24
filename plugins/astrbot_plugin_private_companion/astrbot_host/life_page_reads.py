"""Read-only, authorized projections for the paired life page."""

from datetime import date, datetime, time, timedelta, timezone
import asyncio
import json
import sqlite3
from zoneinfo import ZoneInfo

from astrbot.api.web import request
from companion_v2.configuration.life_provider import LIFE_FIELDS
from companion_v2.execution.models import TaskError, digest, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.life.configuration import GROUP as LIFE_GROUP, target_for as life_target, values
from companion_v2.role_work.state.configuration import GROUP as STATE_GROUP, target_for as state_target

from .life_api import LifeWebAPI
from .life_overview import legacy_activity_outcome, overview_activities, overview_outcome
from .web_api import outcome_response


# Only the visible life workspace consumes these fields. The full host life API
# also serves journals and diary receipts and must keep its existing contract.
# The role's interests are the owner's own dashboard view of the role, so the
# interests panel's fields are forwarded as-is.
LIFE_PAGE_FIELDS = frozenset({
    "role_id", "host_state", "failure_code", "edit_token", "view_token",
    "next_sleep_at", "sleep_schedule", "body_sleep", "reading_plan",
    "activities", "next_activity", "next_date", "next_note_cursor", "refresh_active",
    "interests", "profile_hobbies", "disliked_items", "recent_exposures",
})
NOTE_FIELDS = {
    "reading_note": frozenset({"kind", "activity_id", "local_date", "completed_at", "state",
        "book_title", "start_offset", "end_offset", "body", "excerpt", "unavailable_reason"}),
    "social_note": frozenset({"kind", "activity_id", "local_date", "completed_at", "state", "body"}),
}
ACTIVITY_KINDS = frozenset({"reading", "sleep", "social"})
WALLET_FIELDS = frozenset({"enabled", "currency", "minor_unit_digits", "balance_minor", "reserved_minor",
    "available_minor", "next_before"})
RESERVATION_FIELDS = frozenset({"reservation_id", "activity_label", "limit_minor", "amount_remaining_minor",
    "status", "review_at"})
TRANSACTION_FIELDS = frozenset({"transaction_id", "kind", "amount_minor", "currency", "occurred_at", "activity_label"})
CLASS_FIELDS = ("course", "date", "start", "end", "place", "cancelled")


def wallet_page_data(data):
    """A separate allowlist also fences future domain fields at the web boundary."""
    if data.get("enabled") is not True:
        return {"enabled": False}
    result = {key: value for key, value in data.items() if key in WALLET_FIELDS}
    result["reservations"] = [{key: value for key, value in item.items() if key in RESERVATION_FIELDS}
                              for item in data.get("reservations", ())]
    result["transactions"] = [{key: value for key, value in item.items() if key in TRANSACTION_FIELDS}
                              for item in data.get("transactions", ())]
    return result


def life_page_data(data):
    """Fail closed for new host fields and never forward writing worker receipts."""
    result = {key: value for key, value in data.items() if key in LIFE_PAGE_FIELDS}
    result["config"] = {key: value for key, value in data.get("config", {}).items() if key in LIFE_FIELDS}
    result["activities"] = [item for item in data.get("activities", ()) if item.get("kind") in ACTIVITY_KINDS]
    if "overview_activities" in data:
        result["overview_activities"] = data["overview_activities"]
        result["activities"] = [legacy_activity_outcome(item) for item in result["activities"]]
    result["journals"] = [{key: value for key, value in item.items() if key in NOTE_FIELDS[item["kind"]]}
                          for item in data.get("journals", ()) if item.get("kind") in NOTE_FIELDS]
    # A diary-only archive page must not leak its hidden date cursor either.
    days = {item["local_date"] for item in result["journals"] if item.get("local_date")}
    result["next_date"] = min(days) if len(days) == 50 else None
    # The embedded legacy renderer unconditionally calls .map/.length on these
    # arrays even with its writing panel hidden. Empty placeholders keep it usable
    # without disclosing diary/dream operation IDs, failures or worker names.
    result.update(receipts=[], running=[])
    return result


def _query(allowed):
    query = request.query
    if set(query.keys()) - allowed or any(len(query.getlist(key)) != 1 for key in query.keys()):
        raise ValueError("unknown or repeated query fields")
    return query


def _wallet_before(query):
    if "before" not in query:
        return None
    raw = query["before"]
    if len(raw) > 2048:
        raise ValueError("invalid wallet cursor")
    before = json.loads(raw)
    if (type(before) is not list or len(before) != 2 or type(before[0]) is not str
            or len(before[0]) > 64 or type(before[1]) is not str or not 0 < len(before[1]) <= 256):
        raise ValueError("invalid wallet cursor")
    timestamp = datetime.fromisoformat(before[0])
    if timestamp.tzinfo is None or timestamp.astimezone(timezone.utc).isoformat() != before[0]:
        raise ValueError("invalid wallet cursor timestamp")
    return tuple(before)


def _day(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError("provide YYYY-MM-DD dates")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("provide YYYY-MM-DD dates")
    return result


def _midnight(day, zone):
    return datetime.combine(day, time.min, zone).astimezone(timezone.utc)


class LifePageReads:
    def __init__(self, controller):
        self.controller = controller

    async def _run(self, read, *, calendar=False):
        try:
            async with self.controller._control_lock:
                manager, operator = LifeWebAPI(self.controller).manager()
                app = manager.app
                with app.role_life.consistent_snapshot():
                    if calendar:
                        app.calendar.prepare_life_snapshot_read()
                    with app.runtime.read_snapshot():
                        access = manager.authorize(operator)
                        return outcome_response(Outcome.success(read(manager, operator, access)))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, KeyError, OverflowError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid life page query"))

    async def management(self, reader):
        try:
            before = _wallet_before(_query({"before"}))
            async with self.controller._control_lock:
                manager, operator = LifeWebAPI(self.controller).manager()
                manager.authorize(operator)
                scope = {"owner_key": digest(manager.owner), "context_key": manager.context.key, "now": manager.app.now()}
            data = await asyncio.to_thread(reader, **scope, before=before)
            activities = overview_activities(data["activities"], management=True)
            wallet = data["wallet"]
            return outcome_response(Outcome.success({
                "features": {key: overview_outcome(**data["features"][key], management=True)
                             for key in ("class", "attendance", "city_trip", "wallet")},
                "activities": activities,
                "wallet": wallet_page_data(wallet) if wallet is not None else None,
            }))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except sqlite3.Error:
            return outcome_response(Outcome.failed("provider_unavailable", "life storage is unavailable"))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid life page query"))

    async def wallet(self, reader):
        try:
            query = _query({"limit", "before"})
            limit = int(query.get("limit", "20"))
            if not 1 <= limit <= 50:
                raise ValueError("invalid page size")
            before = _wallet_before(query)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid wallet query"))

        try:
            async with self.controller._control_lock:
                manager, operator = LifeWebAPI(self.controller).manager()
                manager.authorize(operator)
                scope = {"owner_key": digest(manager.owner), "context_key": manager.context.key}
            # Like management(), acquire the provider's read lock off-loop. Even
            # waiting for another life snapshot must not stall unrelated requests.
            data = {"enabled": False}
            if callable(reader):
                try:
                    data = await asyncio.to_thread(reader, **scope, limit=limit, before=before)
                except ValueError as error:
                    # Provider KernelError carries a code. Preserve it before
                    # the query fallback without importing the optional plugin.
                    code = getattr(error, "code", None)
                    if type(code) is not str or not code:
                        raise
                    raise TaskError(code, str(error)) from error
            return outcome_response(Outcome.success(wallet_page_data(data)))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except sqlite3.Error:
            return outcome_response(Outcome.failed("wallet_unavailable", "wallet storage is unavailable"))
        except (ValueError, TypeError, KeyError, OverflowError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid wallet query"))

    async def calendar_occurrences(self):
        try:
            query = _query({"start", "end", "include_cancelled"})
            first, last = _day(query.get("start")), _day(query.get("end"))
            if query.get("include_cancelled") not in (None, "1"):
                raise ValueError("invalid include_cancelled flag")
            options = {"include_cancelled": True} if "include_cancelled" in query else {}
            if not 0 < (last - first).days <= 31:
                raise ValueError("query one to 31 local dates; end is exclusive")
        except (ValueError, TypeError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid calendar date range"))

        def read(manager, operator, _access):
            app, context = manager.app, manager.context
            config = require(app.configuration.read(operator, target=state_target(context), group_id=STATE_GROUP,
                                                    observed_at=app.now()))
            zone_name = config.values[STATE_GROUP + ".planning_timezone"]
            zone = ZoneInfo(zone_name)
            start, end = _midnight(first, zone), _midnight(last, zone)
            cursor, pages = start, []
            # A 31-date month can exceed 31 elapsed days over a DST fall-back.
            # Keep each existing calendar query within its own elapsed-time bound.
            while cursor < end:
                until = min(end, cursor + timedelta(days=31))
                pages.append(app.calendar.occurrences(context, start=cursor, end=until,
                                                      operator=operator, diagnostics=True, **options))
                cursor = until
            items = {(item["id"], item["revision"], item["sequence"]): item
                     for page in pages for item in page["items"]}
            ordered = sorted(items.values(), key=lambda item: (item["start"], item["id"], item["sequence"]))
            if options:
                ordered = [{**item, "outcome": overview_outcome(item["state"], reason=item["state"])}
                           if item["state"] in ("cancelled", "skipped") else item for item in ordered]
            overflow = max(0, len(ordered) - 64)
            reasons = {key: max(page.get("skipped_reasons", {}).get(key, 0) for page in pages)
                       for page in pages for key in page.get("skipped_reasons", {})}
            return {"start": first.isoformat(), "end": last.isoformat(), "timezone": zone_name,
                "items": ordered[:64], "partial": bool(overflow) or any(page["partial"] for page in pages),
                "skipped": max(page["skipped"] for page in pages), "skipped_reasons": reasons,
                "scan_truncated": any(page["scan_truncated"] for page in pages),
                "occurrences_truncated": overflow + sum(page["occurrences_truncated"] for page in pages)}

        return await self._run(read, calendar=True)

    async def class_sessions(self):
        """The role's class sessions on [start, end) local dates in its planning timezone.

        Fails with missing_dependency while academics is unavailable, so the page
        keeps treating blank time as unknown instead of free.
        """
        try:
            query = _query({"start", "end"})
            first, last = _day(query.get("start")), _day(query.get("end"))
            if not 0 < (last - first).days <= 7:
                raise ValueError("query one to 7 local dates; end is exclusive")
        except (ValueError, TypeError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid class date range"))

        def read(manager, operator, _access):
            app, context = manager.app, manager.context
            config = require(app.configuration.read(operator, target=state_target(context), group_id=STATE_GROUP,
                                                    observed_at=app.now()))
            zone_name = config.values[STATE_GROUP + ".planning_timezone"]
            zone = ZoneInfo(zone_name)
            sessions = app.calendar.class_sessions(context, _midnight(first, zone), _midnight(last, zone))
            if sessions is None:
                raise TaskError("missing_dependency", "academics is unavailable")
            items = [{key: item.get(key) for key in CLASS_FIELDS} for item in sessions
                     if first.isoformat() <= str(item.get("date")) < last.isoformat()]
            return {"start": first.isoformat(), "end": last.isoformat(), "timezone": zone_name, "items": items}

        return await self._run(read, calendar=True)

    @staticmethod
    def _history_activity(manager, activity, operator, access, settings):
        if activity.work.kind != "reading":
            return manager._project_activity(activity, operator, access, settings)
        # The current shelf projection can resize a lazy planned reading span
        # and look up its current task. A historical revision must retain the
        # original saved title/span and cannot invent a historical task receipt.
        check = manager.app.role_journal.sources.validate_reference_for_audience(activity.work.source,
            recipient=manager.context.actor.subject, observed_at=manager.app.now())
        unavailable = check.failure.code if check.failure else None
        return {"activity_id": activity.activity_id, "revision": activity.revision, "kind": "reading",
            "current_context": activity.work.context_key == manager.context.key,
            "state": activity.state.value, "title": activity.description, "checkpoint": activity.checkpoint,
            "started_at": None if activity.state.value == "planned" else activity.started_at.isoformat(),
            "updated_at": activity.updated_at.isoformat(), "due_at": None,
            "start_offset": activity.work.start_offset, "end_offset": activity.work.end_offset, "sleep": None,
            "reflection": activity.reading.reflection if activity.reading and unavailable is None else None,
            "unavailable_reason": unavailable, "task": None,
            "view_token": manager.token(operator, access, activity.activity_id, activity.revision)}

    async def activity_history(self):
        try:
            query = _query({"date", "before", "limit"})
            day = _day(query.get("date"))
            limit = int(query.get("limit", "50"))
            if not 1 <= limit <= 100:
                raise ValueError("invalid page size")
            before = None
            if "before" in query:
                raw = query["before"]
                if len(raw) > 2048:
                    raise ValueError("invalid history cursor")
                before = json.loads(raw)
                if (type(before) is not list or len(before) != 3 or type(before[0]) is not str
                        or len(before[0]) > 64 or type(before[1]) is not str or not 0 < len(before[1]) <= 512
                        or type(before[2]) is not int or before[2] < 1):
                    raise ValueError("invalid history cursor")
                timestamp = datetime.fromisoformat(before[0])
                if timestamp.tzinfo is None or stamp(timestamp) != before[0]:
                    raise ValueError("invalid history cursor timestamp")
                before = tuple(before)
        except (ValueError, TypeError, OverflowError, RecursionError):
            return outcome_response(Outcome.failed("life_query_invalid", "invalid activity history query"))

        def read(manager, operator, access):
            app, context = manager.app, manager.context
            config = require(app.configuration.read(operator, target=life_target(context), group_id=LIFE_GROUP,
                                                    observed_at=app.now()))
            settings = values(config)
            zone = ZoneInfo(settings["timezone"])
            start, end = _midnight(day, zone), _midnight(day + timedelta(days=1), zone)
            # The ledger revision API uses (after, at]; translate a local [start,
            # end) day exactly, including revisions recorded at midnight.
            visible, position = [], before
            while len(visible) <= limit:
                rows = app.role_life.activity_revisions(owner=manager.owner, after=start - timedelta(microseconds=1),
                    at=end - timedelta(microseconds=1), before=position, limit=100)
                for row in rows:
                    activity = activity_from_json(row["record_json"])
                    if (activity.scope == context.scope and activity.work
                            and activity.work.context_key == context.key and activity.work.kind in ACTIVITY_KINDS):
                        visible.append((row, activity))
                        if len(visible) > limit:
                            break
                if len(visible) > limit or len(rows) < 100:
                    break
                position = (rows[-1]["updated_at"], rows[-1]["activity_id"], rows[-1]["revision"])
            items = [self._history_activity(manager, activity, operator, access, settings)
                     for _row, activity in visible[:limit]]
            # Never expose the raw position of a filtered host activity or a
            # different private context in a client-visible cursor.
            last = visible[limit - 1][0] if len(visible) > limit else None
            cursor = (json.dumps([last["updated_at"], last["activity_id"], last["revision"]],
                                 ensure_ascii=False, separators=(",", ":")) if last else None)
            return {"date": day.isoformat(), "timezone": settings["timezone"], "items": items,
                    "next_cursor": cursor}

        return await self._run(read)
