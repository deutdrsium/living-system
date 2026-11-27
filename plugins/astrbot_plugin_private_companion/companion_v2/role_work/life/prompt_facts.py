"""Read-time views for foreground and proactive prompts. Receipts stay intact."""

import re


_READING_DESCRIPTION = re.compile(r"(.+) · \d+–\d+ / \d+ 字符")


def activity_facts(activity):
    fields = ("basis", "kind", "state", "started_at", "updated_at", "due_at", "planned_wake_at",
              "planned_reading_span", "id", "revision", "sources")
    facts = {key: activity[key] for key in fields if key in activity}
    if activity.get("kind") == "reading":
        match = _READING_DESCRIPTION.fullmatch(activity.get("description") or "")
        if match:
            facts["title"] = match[1]
    if isinstance(reading := activity.get("reading"), dict):
        facts["reading"] = {key: reading[key] for key in (
            "basis", "source_key", "start_offset", "end_offset", "text_length", "covers_source", "completed_at"
        ) if key in reading}
    return facts


def state_facts(state):
    values = state.get("values") or {}
    facts = {key: values[key] for key in ("energy", "online_mode") if values.get(key) is not None}
    for key in ("cycle", "mood"):
        if isinstance(value := values.get(key), dict):
            facts[key] = {name: value[name] for name in (
                ("applicability",) if key == "cycle" else
                ("joy", "anger", "sorrow", "fear", "baseline", "intensity")
            ) if name in value}
    if values.get("conditions"):
        facts["conditions"] = [{"kind": item["kind"], "expires_at": item.get("expires_at")}
                               for item in values["conditions"]]
    result = {key: state[key] for key in ("state", "basis", "revision", "updated_at", "expired_conditions",
              "mood_dynamics", "sleep_execution", "cycle_timeline", "cycle_feeling") if key in state}
    result["values"] = facts
    if isinstance(feeling := result.get("cycle_feeling"), dict):
        result["cycle_feeling"] = {key: value for key, value in feeling.items() if key != "description"}
    return result


def laya_state_facts(state):
    projected = state_facts(state)
    values = projected["values"]
    facts = {key: values[key] for key in ("energy", "online_mode", "mood") if values.get(key) is not None}
    if sleep := projected.get("sleep_execution"):
        facts["sleep"] = sleep["state"]
    return facts
