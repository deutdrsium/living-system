"""Durable simulated expenses accepted through the life calendar batch."""

from datetime import datetime, time, timedelta, timezone
import json
import logging
from zoneinfo import ZoneInfo

from ..calendar_schedule import Schedule
from ..storage import StorageReader
from .access import purchase_labels
from .wallet import MAX_MINOR


logger = logging.getLogger(__name__)
DUPLICATE_WINDOW = timedelta(hours=1)
DEFAULT_ENTRY_CAP = 500
DEFAULT_DAILY_CAP = 1000


def _stamp(at):
    return at.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _day(at, zone):
    day = at.astimezone(zone).date()
    return (datetime.combine(day, time(), zone), datetime.combine(day + timedelta(days=1), time(), zone))


def _occurrence(record, sequence):
    if record["state"] == "cancelled":
        return None
    if str(sequence) in record["exceptions"]:
        at = record["exceptions"][str(sequence)]
        return datetime.fromisoformat(at) if at else None
    return Schedule.restore(record["schedule"]).at(sequence)


def _link(db, keys, item, at):
    for key in keys:
        row = db.execute("SELECT record_json FROM staged_calendar_records WHERE owner_key=? AND record_id=?",
                         (key, item)).fetchone()
        if row is None:
            continue
        record = json.loads(row[0])
        if record["kind"] != "role_plan" or record["state"] != "accepted":
            break
        schedule = Schedule.restore(record["schedule"])
        following = schedule.first_at_or_after(at)
        candidates = [(int(index), datetime.fromisoformat(moved))
                      for index, moved in record["exceptions"].items() if moved]
        preceding = following - 1 if following is not None else (schedule.count or 1) - 1
        while str(preceding) in record["exceptions"]:
            preceding -= 1
        while following is not None and str(following) in record["exceptions"]:
            following += 1
        for index in (preceding, following):
            if index is not None and index >= 0 and (due := schedule.at(index)) is not None:
                candidates.append((index, due))
        if candidates:
            index, due = min(candidates, key=lambda pair: (abs(pair[1] - at), pair[0]))
            return key, index, (at - due) // timedelta(microseconds=1)
        break
    raise ValueError("no linked plan occurrence")


class PlannedSpending:
    def __init__(self, storage, *, enabled=False, entry_cap=DEFAULT_ENTRY_CAP, daily_cap=DEFAULT_DAILY_CAP, wallet):
        self.storage, self.wallet = storage, wallet
        self.reader = StorageReader(storage)
        self.enabled = enabled
        if not enabled:
            entry_cap, daily_cap = DEFAULT_ENTRY_CAP, DEFAULT_DAILY_CAP
        elif (type(entry_cap) is not int or type(daily_cap) is not int
                or not 0 < entry_cap <= daily_cap <= MAX_MINOR // 100):
            raise ValueError("require 0 < planned_spending_entry_cap <= planned_spending_daily_cap in whole yuan")
        self.entry_cap = entry_cap * 100
        self.daily_cap = daily_cap * 100
        if enabled:
            with storage.transaction() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS life_planned_spending (
                    expense_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, owner_key TEXT NOT NULL,
                    context_key TEXT NOT NULL, label TEXT NOT NULL, label_key TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL, at TEXT NOT NULL, timezone TEXT NOT NULL,
                    plan_owner_key TEXT, plan_id TEXT, sequence INTEGER, offset_us INTEGER,
                    status TEXT NOT NULL, resolved_at TEXT, reason TEXT, transaction_id TEXT)""")
                db.execute("""CREATE INDEX IF NOT EXISTS life_planned_spending_due
                    ON life_planned_spending(status, at)""")
                db.execute("""CREATE INDEX IF NOT EXISTS life_planned_spending_owner
                    ON life_planned_spending(owner_key, at)""")
                db.execute("""CREATE INDEX IF NOT EXISTS life_planned_spending_plan
                    ON life_planned_spending(plan_owner_key, plan_id, status)""")

    def _active_wallet(self):
        wallet = self.wallet() if self.enabled else None
        return wallet if wallet is not None and wallet.currency == "CNY" else None

    @staticmethod
    def _day_rows(db, owner, at, zone):
        start, end = map(_stamp, _day(at, zone))
        return db.execute("""SELECT * FROM life_planned_spending
            WHERE owner_key=? AND at>=? AND at<? AND status!='cancelled' ORDER BY at,expense_id""",
            (owner, start, end)).fetchall()

    @staticmethod
    def _total(rows, *, excluding=None):
        return sum(row["amount_minor"] for row in rows
                   if row["status"] in ("pending", "charged") and row["expense_id"] != excluding)

    @staticmethod
    def _duplicate(rows, label_key, at, *, excluding=None):
        return any(row["expense_id"] != excluding and row["label_key"] == label_key
                   and abs(at - datetime.fromisoformat(row["at"])) <= DUPLICATE_WINDOW for row in rows)

    def add(self, db, request):
        draft = request["payload"].get("spending")
        if draft is None or not self.enabled:
            return
        owner, context = request["owner_key"], request["context_key"]
        zone = ZoneInfo(draft["timezone"])
        now, end = map(datetime.fromisoformat, (draft["now"], draft["end"]))
        for index, entry in enumerate(draft["entries"]):
            try:
                label, minor = entry["label"], entry["amount_minor"]
                at = datetime.fromisoformat(entry["at"])
                if (type(label) is not str or not 1 <= len(label.strip()) <= 60 or not label.strip().isprintable()
                        or type(minor) is not int or not 0 < minor <= self.entry_cap
                        or at.utcoffset() is None or not now <= at < end):
                    raise ValueError("invalid amount, label or time")
                item = entry.get("item")
                key, sequence, offset = (_link(db, draft["calendar_keys"], item, at)
                                         if item is not None else (None, None, None))
                rows = self._day_rows(db, owner, at, zone)
                # Same local day and label, ignoring case/whitespace, within one hour.
                label_key = "".join(label.casefold().split())
                if self._duplicate(rows, label_key, at):
                    raise ValueError("duplicate expense")
                if self._total(rows) + minor > self.daily_cap:
                    raise ValueError("daily cap exceeded")
            except (ValueError, TypeError, OverflowError) as error:
                logger.info("Dropped planned spending entry %d (%s)", index, type(error).__name__)
                continue
            db.execute("""INSERT INTO life_planned_spending
                (expense_id,command_id,owner_key,context_key,label,label_key,amount_minor,at,timezone,
                 plan_owner_key,plan_id,sequence,offset_us,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')""",
                (f"{request['command_id']}:{index}", request["command_id"], owner, context, label.strip(), label_key,
                 minor, _stamp(at), zone.key, key, item, sequence, offset))

    def calendar_changed(self, db, payload, record, previous, *, command_id):
        if payload["action"] not in ("cancel", "exception", "revise") or not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='life_planned_spending'").fetchone():
            return
        rows = db.execute("""SELECT * FROM life_planned_spending WHERE plan_owner_key=? AND plan_id=?
            AND status='pending' ORDER BY at,expense_id""", (payload["storage_key"], record["id"])).fetchall()
        for row in rows:
            sequence, identifier = row["sequence"], row["expense_id"]
            if payload["action"] == "exception" and payload["data"]["sequence"] != sequence:
                continue
            try:
                due = _occurrence(record, sequence)
                before = _occurrence(previous, sequence)
            except ValueError:
                due, before = None, None
            if due is None:
                db.execute("""UPDATE life_planned_spending SET command_id=?,status='cancelled',resolved_at=?
                    WHERE expense_id=?""", (command_id, record["updated_at"], identifier))
            elif due != before:
                at = due + timedelta(microseconds=row["offset_us"])
                zone = ZoneInfo(payload.get("planning_timezone", row["timezone"]))
                day_rows = self._day_rows(db, row["owner_key"], at, zone)
                status, resolved_at, reason = "pending", None, None
                if self._duplicate(day_rows, row["label_key"], at, excluding=identifier):
                    status, resolved_at, reason = "cancelled", record["updated_at"], "duplicate"
                elif self._total(day_rows, excluding=identifier) + row["amount_minor"] > self.daily_cap:
                    status, resolved_at, reason = "refused", record["updated_at"], "daily_cap"
                db.execute("""UPDATE life_planned_spending SET command_id=?,at=?,status=?,resolved_at=?,reason=?
                    WHERE expense_id=?""", (command_id, _stamp(at), status, resolved_at, reason, identifier))

    def tick(self, now, *, before_charge=None):
        wallet = self._active_wallet()
        if wallet is None:
            return
        # Each expense references the latest command that changed its due time.
        due_sql = """SELECT s.* FROM life_planned_spending s JOIN life_prepared_commands c USING(command_id)
            WHERE s.status='pending' AND s.at<=? AND c.state='acknowledged' ORDER BY s.at,s.expense_id LIMIT 100"""
        stamp = _stamp(now)
        if self.reader.one(due_sql, (stamp,)) is None:
            return
        if before_charge is not None:
            before_charge(now)
        with self.storage.transaction() as db:
            for row in db.execute(due_sql, (stamp,)).fetchall():
                owner, identifier = row["owner_key"], row["expense_id"]
                quote = wallet._quote(db, owner)
                if row["amount_minor"] > quote.available_minor:
                    db.execute("""UPDATE life_planned_spending SET status='refused',resolved_at=?,reason='insufficient_funds'
                        WHERE expense_id=?""", (stamp, identifier))
                    continue
                result = wallet._spend_planned(db, owner, identifier, row["amount_minor"],
                                               datetime.fromisoformat(row["at"]), quote.revision)
                db.execute("""UPDATE life_planned_spending SET status='charged',resolved_at=?,transaction_id=?
                    WHERE expense_id=?""", (stamp, result.transaction_id, identifier))

    def read(self, *, owner_key, context_key, at, timezone_name=None):
        wallet = self._active_wallet()
        if wallet is None:
            return None
        with self.reader.locked():
            db = self.reader._connection
            recent = db.execute("""SELECT * FROM life_planned_spending WHERE owner_key=? AND context_key=?
                AND status IN ('charged','refused') AND resolved_at BETWEEN ? AND ?
                ORDER BY resolved_at DESC,expense_id DESC LIMIT 5""",
                (owner_key, context_key, _stamp(at - timedelta(days=1)), _stamp(at))).fetchall()
            if timezone_name is None:
                return [_project(row) for row in recent]
            zone = ZoneInfo(timezone_name)
            today, _ = _day(at, zone)
            month_start, day_start, observed_at = map(_stamp, (today.replace(day=1), today, at))
            totals = dict(db.execute("""SELECT kind, SUM(amount_minor) FROM life_wallet_transactions WHERE owner_key=?
                AND julianday(effective_at)>=julianday(?) AND julianday(effective_at)<=julianday(?) GROUP BY kind""",
                (owner_key, month_start, observed_at)))
            rows = self._day_rows(db, owner_key, at, zone)
            visible = [_project(row) for row in rows if row["context_key"] == context_key]
            other_spends = db.execute("""SELECT t.amount_minor,t.effective_at,t.instance_id FROM life_wallet_transactions t
                LEFT JOIN life_planned_spending s ON s.expense_id=t.source_ref AND s.transaction_id=t.transaction_id
                WHERE t.owner_key=? AND t.kind='spend' AND s.expense_id IS NULL
                AND julianday(t.effective_at)>=julianday(?) AND julianday(t.effective_at)<=julianday(?)
                ORDER BY t.effective_at DESC,t.transaction_id DESC LIMIT 25""",
                (owner_key, day_start, observed_at)).fetchall()
            labels = purchase_labels(db, owner_key, context_key, (row["instance_id"] for row in other_spends))
            visible.extend({"label": labels.get(row["instance_id"]) or "钱包支出", "amount_minor": row["amount_minor"],
                            "at": row["effective_at"], "status": "charged"} for row in other_spends)
            return {"currency": "CNY", "available_minor": wallet._quote(db, owner_key).available_minor,
                    "month_income_minor": totals.get("income", 0), "month_spend_minor": totals.get("spend", 0),
                    "entry_cap_minor": self.entry_cap, "daily_cap_minor": self.daily_cap,
                    "today_planned_minor": self._total(rows),
                    "today_pending_minor": sum(row["amount_minor"] for row in rows if row["status"] == "pending"),
                    "today": visible[:24], "today_partial": len(visible) > 24,
                    "recent": [_project(row) for row in recent if datetime.fromisoformat(row["at"]) < today],
                    "pending_plan_ids": [row["plan_id"] for row in rows if row["status"] == "pending" and row["plan_id"]]}


def _project(row):
    return {"label": row["label"], "amount_minor": row["amount_minor"], "at": row["at"], "status": row["status"],
            **({"reason": row["reason"]} if row["reason"] else {})}
