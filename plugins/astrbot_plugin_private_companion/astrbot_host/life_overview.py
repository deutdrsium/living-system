"""Outcome vocabulary for the authorized Good Life overview read model."""

REASON_LABELS = {
    "cancelled": "被取消",
    "skipped": "已跳过",
    "abandoned": "已放弃",
    "delay_abandoned": "延误后放弃",
    "departure_missed": "错过出发时间",
    "insufficient_resource": "资源不足",
    "missing_dependency": "缺少所需能力",
    "no_time_window": "没有可用时段",
    "source_unavailable": "资料不可用",
    "provider_unavailable": "服务不可用",
    "result_unknown": "结果尚未确认",
    "invalid_config": "配置无效",
    "stale_version": "记录已变更",
    "unauthorized": "没有执行权限",
    "class_absent": "缺课",
    "overslept": "睡过头",
}


MANAGEMENT_REASONS = {**REASON_LABELS, "delayed_en_route": "路上耽搁", "unwell": "身体不适",
                      "capability_disabled": "功能已关闭"}


def overview_outcome(state, *, reason=None, simulated=False, management=False):
    labels = MANAGEMENT_REASONS if management else REASON_LABELS
    result = {"state": state}
    if reason in labels:
        result.update(reason_code=reason, reason_label=labels[reason])
    if simulated:
        result["source"] = "simulated"
    return result


def legacy_activity_outcome(item):
    state = item.get("state")
    if state == "cancelled":
        return {**item, "outcome": overview_outcome(state, reason=state)}
    if item["kind"] != "reading" and state == "active":
        return {**item, "outcome": overview_outcome("in_progress")}
    return item


ACTIVITY_FIELDS = ("activity_id", "kind", "title", "started_at", "updated_at", "due_at", "ends_at")
ACTIVITY_STATES = {
    "preparing": ("active", "in_progress"),
    "running": ("active", "in_progress"),
    "paused": ("paused", "paused"),
    "planned": ("planned", "planned"),
    "completed": ("completed", "completed"),
    "cancelled": ("cancelled", "cancelled"),
    "skipped": ("skipped", "skipped"),
    "failed": ("failed", "failed"),
    "unknown": ("unknown", "unknown"),
}


def overview_activities(data, *, management=False):
    labels = MANAGEMENT_REASONS if management else REASON_LABELS
    items = []
    for row in data["items"]:
        state, outcome = ACTIVITY_STATES[row["state"]]
        fields = {key: row[key] for key in ACTIVITY_FIELDS if key in row}
        reason = row.get("reason_code")
        if reason not in labels and state in ("cancelled", "skipped"):
            reason = state
        if management and row.get("attendance"):
            fields["attendance"] = {key: row["attendance"][key] for key in ("outcome", "minutes_missed")}
        items.append({**fields, "state": state,
            "outcome": overview_outcome(outcome, reason=reason, simulated=row.get("simulated") is True, management=management)})
    return {"items": items, "partial": data["partial"] is True}
