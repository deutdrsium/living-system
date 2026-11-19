"""Run a bounded social session only in the role's own available time."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State
from companion_v2.role_work.work_models import RoleWorkSpec


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


class SmartSocial:
    def __init__(self, routine):
        self.routine, self.app = routine, routine.app
        self.next_check = None
        self.no_progress = 0

    def advance(self, records, now):
        routine, port = self.routine, getattr(self.app, "social_port", None)
        if port is None or not port.enabled_for(routine.context) or not any(port.available().values()):
            return
        if self.next_check is not None and now < self.next_check:
            return
        finished = [a.updated_at for a in records if a.work.kind == "social"
                    and a.state in (State.COMPLETED, State.PAUSED, State.CANCELLED)]
        if finished and now < max(finished) + timedelta(minutes=routine.settings["social_interval_minutes"]):
            return
        self.next_check = now + timedelta(minutes=1)
        local_now = now.astimezone(ZoneInfo(routine.settings["timezone"]))
        recent = sorted((a for a in records if a.work.kind == "social"), key=lambda a: a.started_at)
        try:
            calendar = self.app.calendar.occurrences(routine.context, start=now, end=now + timedelta(minutes=30))
            schedule = social_window(calendar, now, local_now,
                routine._next_sleep if routine.settings["sleep_enabled"] else None)
        except TaskError:
            return
        if not schedule:
            return
        request_id = "social-" + uuid4().hex
        identifier = "social-activity-" + digest([routine.context.key, request_id])
        activity = self.app.role_life.add_activity(RoleActivity(identifier, routine.owner.persona_id,
            routine.context.scope, now, State.ACTIVE, description="自主浏览外部内容",
            work=RoleWorkSpec("social", routine.context.key, request_id)))
        previous = tuple({"title": a.description, "checkpoint": a.checkpoint[:800]}
            for a in recent[-3:] if a.state is State.COMPLETED)
        routine.launch(identifier, lambda: self._browse(activity, schedule, previous))

    async def _browse(self, activity, schedule, previous):
        routine, result = self.routine, None
        try:
            await routine.preflight()
            event = routine.event("social-" + activity.activity_id,
                "生活执行端发现角色日程中有空档；这不是用户发来的聊天。")
            result = await self.app.role_social.consume(routine.context, activity,
                role_instruction=routine.instruction, event=event, schedule=schedule, previous=previous)
            return result
        finally:
            current = routine.activity(activity.activity_id)
            if current.state is State.ACTIVE:
                code = (result.failure.code if result and result.failure else
                        result.unwrap().stop_code if result else "interrupted")
                self.app.role_life.transition_activity(current.activity_id, owner=routine.owner,
                    expected_revision=current.revision, state=State.PAUSED,
                    operation_id="social-pause-" + digest([current.activity_id, current.work.request_id]),
                    observed_at=self.app.now(), checkpoint="浏览未完成：" + str(code or "interrupted"))
                current = routine.activity(activity.activity_id)
            if current.state is State.COMPLETED:
                self.no_progress, self.next_check = 0, None
            else:
                # A refusal or failure must not become a tight model/network loop.
                self.no_progress = min(self.no_progress + 1, 6)
                self.next_check = self.app.now() + timedelta(seconds=min(30 * 2 ** (self.no_progress - 1), 900))
