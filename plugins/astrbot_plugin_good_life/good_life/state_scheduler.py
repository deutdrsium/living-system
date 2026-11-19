"""Keep the existing state-maintenance schedule in the life database."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stamp(value):
    if value.utcoffset() is None:
        raise ValueError("state schedule time must be aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class StateSchedules:
    def __init__(self, storage):
        self.storage = storage

    def install(self):
        with self.storage.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS life_state_schedule_claims (
                request_id TEXT PRIMARY KEY, claim_json TEXT NOT NULL, result_json TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS life_state_manual_requests (
                request_id TEXT PRIMARY KEY, schedule_key TEXT NOT NULL, due_at TEXT NOT NULL)""")

    def dispatch(self, operation, **request):
        if operation == "force_due":
            if (set(request) != {"request_id", "schedule_key", "due_at"}
                    or any(type(value) is not str or not value for value in request.values())):
                raise ValueError("invalid state maintenance request")
            with self.storage.transaction() as db:
                prior = db.execute("SELECT schedule_key,due_at FROM life_state_manual_requests WHERE request_id=?",
                                   (request["request_id"],)).fetchone()
                if prior is not None:
                    if tuple(prior) != (request["schedule_key"], request["due_at"]):
                        raise ValueError("state maintenance request changed")
                    return True
                changed = db.execute("UPDATE staged_role_state_schedules SET next_at=?,revision=revision+1 WHERE context_key=?",
                                     (request["due_at"], request["schedule_key"]))
                if not changed.rowcount:
                    return False
                db.execute("INSERT INTO life_state_manual_requests VALUES (?,?,?)",
                           (request["request_id"], request["schedule_key"], request["due_at"]))
                return True
        if operation != "verify_claim" or set(request) != {"claim"}:
            raise ValueError("unknown state schedule operation")
        claim = request["claim"]
        if type(claim) is not dict or type(claim.get("request_id")) is not str:
            return False
        with self.storage._lock:
            row = self.storage._connection.execute("SELECT claim_json FROM life_state_schedule_claims WHERE request_id=?",
                                                   (claim["request_id"],)).fetchone()
            return row is not None and json.loads(row[0]) == claim

    def bind(self, item, now):
        required = {"context_key", "owner", "schedule_key", "context_json", "settings_json", "enabled", "can_anchor"}
        if (type(item) is not dict or set(item) != required or type(item["owner"]) is not dict
                or type(item["enabled"]) is not bool or type(item["can_anchor"]) is not bool):
            raise ValueError("state schedule binding is invalid")
        owner, schedule_key = item["owner"], item["schedule_key"]
        context_json, encoded = item["context_json"], item["settings_json"]
        settings = json.loads(encoded)
        if (type(settings) is not dict or type(settings.get("values")) is not dict
                or settings["values"].get("enabled") is not item["enabled"]
                or type(settings.get("token")) is not str or type(settings.get("instruction")) is not str):
            raise ValueError("state schedule settings are invalid")
        context = json.loads(context_json)
        if (context["role_binding"]["scope"]["installation_id"] != owner["installation_id"]
                or context["role_binding"]["scope"]["bot_id"] != owner["bot_id"]
                or context["role_binding"]["role"]["subject_id"] != owner["persona_id"]):
            raise ValueError("state schedule owner changed")
        with self.storage.transaction() as db:
            row = db.execute("SELECT * FROM staged_role_state_schedules WHERE context_key=?", (schedule_key,)).fetchone()
            legacy = db.execute("""SELECT * FROM staged_role_state_schedules
                WHERE json_extract(context_json,'$.role_binding.scope.installation_id')=?
                AND json_extract(context_json,'$.role_binding.scope.bot_id')=?
                AND json_extract(context_json,'$.role_binding.role.subject_id')=?
                AND context_key!=? ORDER BY next_at,context_key""",
                (owner["installation_id"], owner["bot_id"], owner["persona_id"], schedule_key)).fetchall()
            if row is None and legacy:
                next_at, result, revision = min(x["next_at"] for x in legacy), next((x["result_json"] for x in legacy if x["result_json"]), None), max(x["revision"] for x in legacy)
            elif row is not None:
                next_at, result, revision = row["next_at"], row["result_json"], row["revision"]
            else:
                next_at, result, revision = _stamp(now), None, 0
            if row is not None and row["context_json"] != context_json and (not item["enabled"] or not item["can_anchor"]):
                context_json = row["context_json"]
            elif row is None and legacy and item["enabled"] and not item["can_anchor"]:
                context_json = min(legacy, key=lambda x: x["next_at"])["context_json"]
            same = row is not None and row["settings_json"] == encoded
            if same and not legacy:
                if row["context_json"] != context_json:
                    db.execute("UPDATE staged_role_state_schedules SET context_json=? WHERE context_key=?", (context_json, schedule_key))
                return
            if row is None and not legacy and not item["enabled"]:
                return
            due = next_at if same or row is None and legacy else _stamp(now)
            result = result if same or row is None and legacy else None
            db.execute("""INSERT INTO staged_role_state_schedules VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(context_key) DO UPDATE SET context_json=excluded.context_json,settings_json=excluded.settings_json,
                next_at=excluded.next_at,revision=excluded.revision,enabled=excluded.enabled,result_json=excluded.result_json""",
                (schedule_key, context_json, encoded, due, revision + 1, int(item["enabled"]), result))
            db.executemany("DELETE FROM staged_role_state_schedules WHERE context_key=?", ((x["context_key"],) for x in legacy))

    def due(self, now, after=None):
        with self.storage._lock:
            return tuple(dict(row) for row in self.storage._connection.execute("""SELECT * FROM staged_role_state_schedules
                WHERE enabled=1 AND next_at<=? AND (? IS NULL OR next_at>? OR (next_at=? AND context_key>?))
                ORDER BY next_at,context_key LIMIT 16""",
                (_stamp(now), after[0], after[0], after[0], after[1]) if after else (_stamp(now), None, None, None, None)))

    def claim(self, row, selected, now):
        minutes = json.loads(row["settings_json"])["values"]["maintenance_minutes"]
        if type(minutes) is not int or minutes < 1:
            raise ValueError("state maintenance interval is invalid")
        request_id = "state-period-" + _digest([selected["context_key"], row["revision"], row["next_at"]])
        claim = {"request_id": request_id, "context_key": selected["context_key"], "owner_key": selected["owner_key"],
                 "schedule_key": row["context_key"], "schedule_revision": row["revision"], "due_at": row["next_at"],
                 "token": selected["token"]}
        with self.storage.transaction() as db:
            changed = db.execute("""UPDATE staged_role_state_schedules SET next_at=?
                WHERE context_key=? AND revision=? AND next_at=? AND settings_json=? AND enabled=1""",
                (_stamp(now + timedelta(minutes=minutes)), row["context_key"], row["revision"], row["next_at"], row["settings_json"]))
            if not changed.rowcount:
                return None
            db.execute("INSERT INTO life_state_schedule_claims VALUES (?, ?, NULL)",
                       (request_id, json.dumps(claim, sort_keys=True, separators=(",", ":"))))
        return claim

    def finish(self, claim, result):
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.storage.transaction() as db:
            changed = db.execute("UPDATE life_state_schedule_claims SET result_json=? WHERE request_id=? AND result_json IS NULL",
                                 (encoded, claim["request_id"]))
            if changed.rowcount:
                db.execute("UPDATE staged_role_state_schedules SET result_json=? WHERE context_key=? AND revision=?",
                           (encoded, claim["schedule_key"], claim["schedule_revision"]))


class StateScheduler:
    def __init__(self, storage, capabilities):
        from .state_patrol import StatePatrol
        self.schedules, self.capabilities = StateSchedules(storage), capabilities
        self.patrol = StatePatrol(storage)
        self.schedules.install()
        self.bindings, self.worker = {}, None
        self.claim = None
        self.scan_after = None
        self.manual_id = None
        self.manual_next = {}
        self.patrol_checked_at, self.patrol_results = None, {}

    def advance(self, now):
        if self.worker is not None:
            if not self.worker.done():
                return
            failed = False
            try:
                result = self.worker.result()
            except BaseException as error:
                failed = True
                result = {"state": "failed", "code": getattr(error, "code", "state_worker_failed")}
            if self.claim is not None:
                self.schedules.finish(self.claim, result)
            elif self.manual_id is not None and failed:
                self.manual_next[self.manual_id] = now + timedelta(seconds=30)
            self.worker = self.claim = None
            self.manual_id = None
        active = set()
        bindings = self.capabilities["state_bindings"]()
        patrol_due = (callable(self.capabilities.get("state_patrol_snapshot")) and
            (self.patrol_checked_at is None or (now - self.patrol_checked_at).total_seconds() >= 60))
        if patrol_due:
            self.patrol_checked_at = now
        for item in bindings:
            active.add(item["context_key"])
            signature = _digest(item)
            if self.bindings.get(item["context_key"]) != signature:
                self.schedules.bind(item, now)
                self.bindings[item["context_key"]] = signature
                self.scan_after = None
            if patrol_due:
                try:
                    settings = json.loads(item["settings_json"])["values"]
                    if self.patrol.due(item["owner"], settings, now):
                        state = self.capabilities["state_patrol_snapshot"](item["context_key"], _digest(item["owner"]))
                        self.patrol_results[item["context_key"]] = self.patrol.record(item["owner"], settings, now, state)
                except Exception as error:
                    self.patrol_results[item["context_key"]] = {"state": "failed", "code": getattr(error, "code", "state_patrol_failed")}
        self.bindings = {key: value for key, value in self.bindings.items() if key in active}
        recover_manual = self.capabilities.get("state_recover_manual")
        if callable(recover_manual) and recover_manual():
            self.scan_after = None
        if all(callable(self.capabilities.get(name)) for name in ("state_intervention_pending", "state_intervention_run")):
            for item in self.capabilities["state_intervention_pending"]():
                if (type(item) is dict and set(item) == {"operation_id", "context_key", "owner_key"}
                        and all(type(value) is str and value for value in item.values())
                        and now >= self.manual_next.get(item["operation_id"], now)):
                    self.manual_id = item["operation_id"]
                    self.worker = asyncio.create_task(self.capabilities["state_intervention_run"](item["context_key"], item["operation_id"]),
                                                      name="good-life:state-intervention")
                    return
        rows = self.schedules.due(now, self.scan_after)
        for row in rows:
            self.scan_after = (row["next_at"], row["context_key"])
            try:
                selected = self.capabilities["state_preflight"](row)
            except Exception:
                continue
            if selected is None:
                continue
            claim = self.schedules.claim(row, selected, now)
            if claim is not None:
                self.claim = claim
                self.worker = asyncio.create_task(self.capabilities["state_run"](claim), name="good-life:state-maintenance")
                return
        if len(rows) < 16:
            self.scan_after = None

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = self.claim = None
