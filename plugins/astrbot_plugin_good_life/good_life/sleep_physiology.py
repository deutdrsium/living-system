"""Sleep calculations migrated from private_companion/companion_v2/role_work/life."""

from datetime import datetime, timezone

from .reading_choices import _digest


def _time(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("sleep observation must be timezone-aware")
    return result.astimezone(timezone.utc)


def actual_sleep_seconds(activity, now):
    receipt = activity["sleep"]
    recorded = receipt["slept_seconds"] if receipt else 0
    if activity["state"] == "active":
        return min(172800, recorded + max(0, round((_time(now) - _time(activity["updated_at"])).total_seconds())))
    if receipt:
        return recorded
    if activity["state"] in ("completed", "cancelled"):
        return max(0, round((_time(activity["updated_at"]) - _time(activity["started_at"])).total_seconds()))
    return 0


def sleep_profile(episodes, *, workload_score, now):
    now = _time(now)
    episodes = tuple(sorted(episodes, key=lambda item: (_time(item["started_at"]), item["activity_id"])))
    debt = 0.0
    for activity in episodes:
        if activity["state"] in ("active", "paused", "planned"):
            continue
        receipt, work = activity["sleep"], activity["work"]
        kind = receipt["kind"] if receipt else "night"
        if receipt:
            base = receipt["base_need_minutes"]
        elif work and work["due_at"]:
            base = max(1, min(1440, round((_time(work["due_at"]) - _time(activity["started_at"])).total_seconds() / 60)))
        else:
            base = 480
        if kind == "manual" and receipt and receipt["target_minutes"] <= 60:
            kind = "nap"
        slept = actual_sleep_seconds(activity, now) / 60
        quality = (receipt["quality_min"] + receipt["quality_max"]) / 200 if receipt else 0.78
        if kind == "nap":
            debt = max(0.0, debt - slept * quality * 0.12)
        else:
            debt += max(0.0, base - slept)
            debt = max(0.0, debt - min(debt, max(0.0, slept - base) * quality * 0.75))
        debt = min(720.0, debt)
    workload_score = max(0, min(100, int(workload_score)))
    latest = episodes[-1] if episodes else None
    if latest is None:
        awake_minutes, pressure = None, None
    elif latest["state"] == "active":
        awake_minutes, pressure = 0, 0
    else:
        awake_minutes = max(0, round((now - _time(latest["updated_at"])).total_seconds() / 60))
        pressure = min(100, round(awake_minutes / 60 * 4.0 + debt * 0.14 + workload_score * 0.12))
    return {"sleep_pressure": pressure, "sleep_debt_minutes": round(debt), "workload_score": workload_score,
            "awake_minutes": awake_minutes, "has_sleep_history": latest is not None}


def sleep_quality_range(workload_score, sleep_debt_minutes, interruptions=0, early_interruptions=0):
    workload = max(0, min(100, int(workload_score)))
    debt = max(0, min(720, int(sleep_debt_minutes)))
    interruptions = max(0, min(20, int(interruptions)))
    early_interruptions = max(0, min(interruptions, int(early_interruptions)))
    penalty = workload * 0.18 + min(20, debt * 0.025) + interruptions * 7 + early_interruptions * 5
    lower = max(25, min(92, round(82 - penalty)))
    upper = max(lower + 3, min(98, round(95 - workload * 0.10 - min(12, debt * 0.015)
                                     - interruptions * 4 - early_interruptions * 3)))
    return lower, upper


def recommended_sleep_minutes(base_need_minutes, sleep_debt_minutes, workload_score):
    base = max(1, min(1440, int(base_need_minutes)))
    recovery = min(120, round(max(0, sleep_debt_minutes) * 0.45))
    workload = 30 if workload_score >= 80 else 15 if workload_score >= 55 else 0
    return min(1440, base + recovery + workload)


def night_sleep_ready(profile):
    pressure = profile.get("sleep_pressure")
    if pressure is None:
        return True
    threshold = max(48, 66 - profile.get("workload_score", 0) * 0.12
                    - profile.get("sleep_debt_minutes", 0) * 0.035)
    return pressure >= threshold


def nap_ready(profile, *, local_minutes):
    pressure = profile.get("sleep_pressure")
    return (profile.get("has_sleep_history") is True and profile.get("awake_minutes") is not None
            and profile["awake_minutes"] >= 300 and 660 <= local_minutes < 960
            and pressure is not None and pressure >= 40
            and (profile.get("workload_score", 0) >= 40 or profile.get("sleep_debt_minutes", 0) >= 90))


def oversleep_extension(activity, *, sleep_debt_minutes, roll=None):
    receipt = activity["sleep"]
    if (receipt is None or receipt["oversleep_minutes"] or receipt["kind"] == "nap"
            or receipt["kind"] == "manual" and receipt["target_minutes"] <= 60):
        return 0
    chance = min(70, 8 + min(30, sleep_debt_minutes // 10)
                 + receipt["workload_score"] // 4 + receipt["interruptions"] * 8)
    if roll is None:
        roll = int(_digest([activity["activity_id"], "oversleep"])[:8], 16) % 100
    if roll >= chance:
        return 0
    return 30 if chance >= 45 else 15
