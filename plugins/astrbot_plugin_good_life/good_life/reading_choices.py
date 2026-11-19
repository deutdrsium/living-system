"""Reading window and candidate selection migrated from the companion plugin.

Sources remain owned by the companion. The caller must supply a synchronous
audience-authorization callback; an earlier paused segment blocks its book.
"""

from datetime import datetime
import hashlib
import json


READING_WORDS = ("阅读", "读书", "看书", "翻书", "读几页", "读几章", "读几段", "慢慢读", "读一会", "读一读", "读小说", "读诗", "看小说", "翻阅")


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def choice_slot(records, day):
    completed = sorted((a["activity_id"], a["revision"]) for a in records
                       if a["state"] == "completed" and a["work"]["kind"] in ("reading", "social", "sleep"))
    return day + ":" + _digest(completed)


def reading_window(calendar, now, local_now, next_sleep):
    if calendar.get("partial"):
        return None
    if next_sleep is not None and next_sleep <= now:
        return None
    active = []
    for item in calendar.get("items", ()):
        if item.get("state") != "accepted" or item.get("kind") not in ("role_plan", "commitment"):
            continue
        try:
            start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
        except (ValueError, KeyError, TypeError):
            return None
        if start <= now < end:
            active.append(item)
    def is_reading(item):
        description = str(item.get("title", "")) + " " + str(item.get("detail", ""))
        return item.get("kind") == "role_plan" and any(word in description for word in READING_WORDS)
    if active:
        if not all(is_reading(item) for item in active):
            return None
        return "正在进行的阅读安排：" + "；".join(str(item.get("title", "")) for item in active)[:250]
    return "日历中没有其他角色安排的空档"


def candidates(records, day, *, selected_source=None, authorize_source):
    by_source = {}
    for activity in records:
        work = activity["work"]
        if work["kind"] != "reading" or activity["state"] not in ("planned", "paused"):
            continue
        key = work["source"]["source_id"]
        if key not in by_source or work["start_offset"] < by_source[key]["work"]["start_offset"]:
            by_source[key] = activity
    choices = sorted((a for a in by_source.values() if a["state"] == "planned" and
                      (selected_source is None or a["work"]["source"]["source_id"] == selected_source)),
                     key=lambda a: a["work"]["source"]["source_id"])
    if len(choices) > 8:
        offset = int(_digest(day)[:8], 16) % len(choices)
        choices = choices[offset:] + choices[:offset]
    available = []
    for item in choices:
        if authorize_source(item["work"]["source"]) is True:
            available.append(item)
            if len(available) == 8:
                break
    return available
