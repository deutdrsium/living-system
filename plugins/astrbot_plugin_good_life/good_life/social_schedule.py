"""Browsing timing migrated from private_companion/companion_v2/role_work/life.

Platform access and real interaction receipts remain host-owned capabilities.
"""

from datetime import datetime, timedelta

from .reading_choices import _digest


SOCIAL_WORDS = ("上网", "网上", "浏览", "社交", "刷", "看视频", "搜一搜", "搜索")


def social_window(calendar, now, local_now, next_sleep):
    if calendar.get("partial") or (next_sleep is not None and next_sleep <= now):
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
            active.append((item, end))
    if active:
        if any(item.get("kind") != "role_plan" or not any(word in str(item.get("title", "")) + " " +
                str(item.get("detail", "")) for word in SOCIAL_WORDS) for item, _ in active):
            return None
        return "正在进行的上网安排：" + "；".join(str(item.get("title", "")) for item, _ in active)[:250]
    return "日历中没有其他角色安排的空档"


def browsing_ready(records, now, interval_minutes):
    finished = [datetime.fromisoformat(a["updated_at"]) for a in records
                if a["work"]["kind"] == "social" and a["state"] in ("completed", "paused", "cancelled")]
    return not finished or now >= max(finished) + timedelta(minutes=interval_minutes)


def social_first(owner, day, interests, records):
    first = bool(interests) and int(_digest([owner, day])[:8], 16) % 5 < 2
    completed = [a for a in records if a["state"] == "completed" and a["work"]["kind"] in ("reading", "social")]
    if completed:
        last = max(completed, key=lambda a: (datetime.fromisoformat(a["updated_at"]), a["activity_id"]))
        first = last["work"]["kind"] == "reading"
    return first
