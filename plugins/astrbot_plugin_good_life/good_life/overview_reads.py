"""Bounded read of persisted outcomes for an already authorized life page."""

from datetime import datetime, timedelta
import json

from .kernel.codec import wire
from .storage import StorageReader


LIMIT = 64
TEMPLATES = {
    "class_session_local": ("class", "上课"),
    "study_local": ("study", "学习"),
    "city_trip": ("city_trip", "城市出行"),
    "purchase_local": ("purchase", "购买"),
    "local_timed_units": ("activity", "整理"),
}


def _trip_reason(checkpoint):
    # The structured explanation codes are ordered in the execution record.
    # Distinguish a missed departure from a later explicit cancellation after a delay.
    if not checkpoint.get("cancel_requested"):
        return "cancelled"
    delayed = False
    for explanation in checkpoint.get("explanations", ()):
        code = explanation["reason_code"]
        if code == "no_time_window":
            return "departure_missed"
        if code == "provider_unavailable":
            delayed = True
        if code == "cancelled" and delayed:
            return "delay_abandoned"
    return "cancelled"


def _instance(record, template, *, refused=False):
    executor = template["executor"].partition("@")[0]
    kind, title = TEMPLATES.get(executor, ("activity", "生活活动"))
    session = template["params"].get("session") if kind == "class" else None
    checkpoint = record["checkpoint"]
    if kind == "class" and record["phase"] == "travel" and record["status"] != "completed":
        kind, title = "campus_trip", "校内出行"
    elif record["template_id"] == "buy_drink":
        title = "买饮料"
    state = record["status"]
    result = record.get("outcome") or {}
    reason = result.get("reason_code") or record.get("reason_code")
    if state == "cancelled" and kind == "city_trip":
        reason = _trip_reason(checkpoint)
    elif state == "cancelled" and refused:
        reason = "insufficient_resource"
    elif state == "failed" and reason == "invalid_config" and result.get("summary", "").startswith("Local result abandoned: "):
        # Existing abandon_result records use this prefix as their only durable marker.
        reason = "abandoned"
    elif state == "completed" and session:
        if template["params"]["decision"] == "skip" or checkpoint.get("attended_s") == 0:
            state, reason = "skipped", "class_absent"
    return {"activity_id": "kernel:" + record["instance_id"], "kind": kind,
        "title": session["course_name"] if session and kind == "class" else title,
        "state": state, "started_at": record["started_at"], "updated_at": record["updated_at"],
        "simulated": (template.get("presentation") or {}).get("provenance") == "simulated",
        "reason_code": reason}


def read_activities(storage, *, owner_key, context_key, now, kernel, academics, management=False):
    # Include today's snapshots across all supported planning timezones. The page
    # uses its own role-local day to display timed rows, independent of host TZ.
    start, end = wire(now - timedelta(hours=26)), wire(now + timedelta(hours=26))
    items, executed_sessions = [], set()
    partial = False
    reader = StorageReader(storage)
    with reader.locked():
        if kernel:
            wallet_audit = reader.one("SELECT 1 FROM sqlite_master WHERE name='life_wallet_recovery_audit'", ())
            rows = reader.all("""SELECT record_json, template_json FROM life_kernel_instances AS i
                WHERE owner_key=? AND json_extract(record_json,'$.context_key')=?
                  AND EXISTS (SELECT 1 FROM json_each(i.audience_json,'$.context_keys') WHERE value=?)
                  AND (NOT ? OR json_extract(template_json,'$.executor') IN
                       ('class_session_local@1', 'city_trip@1', 'purchase_local@1', 'purchase_local@2'))
                  AND (? OR status IN ('preparing','running','paused','unknown')
                       OR julianday(json_extract(record_json,'$.updated_at')) >= julianday(?))
                ORDER BY (status IN ('preparing','running','paused','unknown')) DESC,
                         julianday(json_extract(record_json,'$.updated_at')) DESC, instance_id LIMIT ?""",
                (owner_key, context_key, context_key, management, management, start, LIMIT + 1))
            partial = len(rows) > LIMIT
            for row in rows[:LIMIT]:
                record, template = json.loads(row[0]), json.loads(row[1])
                refused = (wallet_audit is not None and record["template_id"] == "buy_drink"
                    and record["status"] == "cancelled" and reader.one("""SELECT 1 FROM life_wallet_recovery_audit
                    WHERE owner_key=? AND instance_id=? AND kind='reservation_refused'
                      AND json_extract(record_json,'$.context_key')=?
                      AND json_extract(record_json,'$.terminal_revision')=? LIMIT 1""",
                    (owner_key, record["instance_id"], context_key, record["revision"])) is not None)
                item = _instance(record, template, refused=refused)
                if template["executor"].partition("@")[0] == "class_session_local":
                    session = template["params"]["session"]
                    executed_sessions.add((session["session_id"], session["revision"]))
                    if academics:
                        participation = reader.one("""SELECT record_json FROM life_academic_participation
                            WHERE owner_key=? AND session_id=? AND instance_id=?""",
                            (owner_key, session["session_id"], record["instance_id"]))
                        grade = json.loads(participation[0]).get("attendance") if participation else None
                        if management and grade:
                            item["attendance"] = grade
                        if grade and grade["cause_code"] not in (None, "missed"):
                            item["reason_code"] = grade["cause_code"]
                        if grade and grade["outcome"] == "absent":
                            item.update(kind="class", title=session["course_name"], state="skipped",
                                        reason_code=item["reason_code"] or "class_absent")
                items.append(item)
        if academics:
            rows = reader.all("""SELECT s.record_json, p.record_json FROM life_academic_sessions AS s
                JOIN life_academic_owners AS o USING (owner_key)
                LEFT JOIN life_academic_participation AS p USING (owner_key, session_id)
                WHERE s.owner_key=? AND o.context_key=?
                  AND ((? AND json_extract(p.record_json,'$.attendance') IS NOT NULL)
                       OR (julianday(json_extract(s.record_json,'$.start_at')) < julianday(?)
                       AND julianday(json_extract(s.record_json,'$.end_at')) > julianday(?)))
                ORDER BY CASE WHEN ? THEN -julianday(json_extract(s.record_json,'$.start_at'))
                              END, json_extract(s.record_json,'$.start_at'), s.session_id LIMIT ?""",
                (owner_key, context_key, management, end, start, management, LIMIT + 1))
            partial = partial or len(rows) > LIMIT
            for row in rows[:LIMIT]:
                session = json.loads(row[0])
                session_id = session["session_id"]
                state, reason = "planned", None
                grade = json.loads(row[1]).get("attendance") if row[1] else None
                if session["cancelled"]:
                    state, reason = "cancelled", "cancelled"
                elif (session_id, session["revision"]) in executed_sessions:
                    continue
                elif grade:
                    state = "skipped" if grade["outcome"] == "absent" else "completed"
                    reason = "class_absent" if grade["cause_code"] == "missed" else grade["cause_code"]
                elif session["end_at"] <= wire(now.replace(microsecond=0)):
                    state, reason = "unknown", "result_unknown"
                items.append({"activity_id": "class:" + session_id, "kind": "class", "title": session["course_name"],
                    "state": state, "started_at": session["start_at"], "due_at": session["start_at"],
                    **({"attendance": grade} if management and grade else {}),
                    "ends_at": session["end_at"], "reason_code": reason, **({"simulated": True} if grade else {})})
    if management:
        items.sort(key=lambda item: (
            "updated_at" in item and item["state"] in ("preparing", "running", "paused", "unknown"),
            datetime.fromisoformat(item.get("updated_at", item["started_at"]))), reverse=True)
    return {"items": items[:LIMIT], "partial": partial or len(items) > LIMIT}
