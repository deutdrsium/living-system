"""Narrow host callbacks for the registered life provider's scheduler."""

import json
from datetime import datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskContext, TaskError, digest, encode, stamp
from companion_v2.integration.natural_wake_handoff import NaturalWakeHandoff
from companion_v2.integration.interrupted_work_handoff import InterruptedWorkHandoff
from companion_v2.integration.state_write_handoff import StateWriteHandoff
from companion_v2.integration.reading_handoff import ReadingCompletionHandoff, ReadingStartHandoff
from companion_v2.integration.reading_import_handoff import ReadingImportHandoff
from companion_v2.integration.scheduled_sleep_handoff import ScheduledSleepHandoff
from companion_v2.integration.sleep_interruption_handoff import SleepInterruptionHandoff
from companion_v2.integration import sleep_interruption_queue
from companion_v2.integration import life_state_schedule
from companion_v2.integration import manual_reading_queue
from companion_v2.integration import manual_calendar_queue
from companion_v2.integration import state_intervention_queue
from companion_v2.role_work.state.maintenance import KIND as STATE_KIND
from companion_v2.role_work.state.ownership import role_owner, role_schedule_key
from companion_v2.role_work.life.smart_reading import choice_slot, reading_window
from companion_v2.role_work.codec import activity_from_json
from companion_v2.sources.dependencies import validation_batch
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository


def capabilities_for(controller, registry, *, plugin, registration_id):
    if not registry.owns_registration(registration_id, plugin):
        raise TaskError("life_provider_unavailable", "the registered life provider is unavailable")

    def current():
        if getattr(controller, "_terminated", False) or not registry.owns_registration(registration_id, plugin):
            raise TaskError("life_provider_unavailable", "the registered life provider is unavailable")
        session = getattr(controller, "session", None)
        app = getattr(session, "application", None)
        if app is None:
            raise TaskError("life_provider_unavailable", "the companion session is unavailable")
        app.require_running()
        if app.role_life.activity_owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "life execution has not transferred")
        return app

    def contexts():
        app = current()
        result, seen = [], set()
        for routine in tuple(app.life_runtime.routines.values()):
            if (id(routine) in seen or routine.context is None or routine.instruction is None
                    or routine._closed or routine._retired):
                continue
            seen.add(id(routine))
            try:
                routine.refresh()
                if not routine.settings or not routine.settings["enabled"]:
                    continue
                routine.authorize()
            except (TaskError, ValueError, KeyError):
                continue
            result.append({"context_key": routine.context.key, "owner_key": digest(routine.owner)})
        return tuple(result)

    def calendar_contexts():
        app = current()
        result, seen = [], set()
        for routine in tuple(app.life_runtime.routines.values()):
            if (id(routine) in seen or routine.context is None or routine.instruction is None
                    or routine._closed or routine._retired):
                continue
            seen.add(id(routine))
            result.append({"context_key": routine.context.key, "owner_key": digest(routine.owner)})
        known = {item["context_key"] for item in result}
        for context, _instruction, _guard in tuple(app.state_maintenance.bindings.values()):
            if context.key not in known:
                result.append({"context_key": context.key, "owner_key": digest(role_owner(context))})
                known.add(context.key)
        return tuple(result)

    def routine_for(context_key):
        app = current()
        if type(context_key) is not str or not context_key or len(context_key) > 256:
            raise TaskError("life_context_changed", "the original life context is unavailable")
        routine = app.life_runtime.routines.get(context_key)
        if (routine is None or routine.context is None or routine.context.key != context_key
                or routine.instruction is None or routine._closed or routine._retired):
            raise TaskError("life_context_changed", "the original life context is unavailable")
        return app, routine

    async def wake(context_key, activity_id, revision):
        app, routine = routine_for(context_key)
        handoff = NaturalWakeHandoff(app, routine, app.role_life.transition_provider())
        result = await handoff.execute(activity_id, expected_revision=revision)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover(context_key, request):
        app, routine = routine_for(context_key)
        if (type(request) is not dict or request.get("kind") != "activity.transition"
                or type(request.get("command_id")) is not str or not request["command_id"].startswith("activity-due-")):
            raise TaskError("life_command_invalid", "only the original due-sleep command can be recovered")
        handoff = NaturalWakeHandoff(app, routine, app.role_life.transition_provider())
        result = await handoff.recover(request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    def sleep_snapshot(context_key):
        app, routine = routine_for(context_key)
        return ScheduledSleepHandoff(app, routine, app.role_life.creation_provider()).snapshot()

    async def start_sleep(context_key, plan):
        app, routine = routine_for(context_key)
        handoff = ScheduledSleepHandoff(app, routine, app.role_life.creation_provider())
        result = await handoff.execute(plan)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover_sleep_creation(context_key, request):
        app, routine = routine_for(context_key)
        if (type(request) is not dict or request.get("kind") != "activity.create"
                or type(request.get("command_id")) is not str or not request["command_id"].startswith("activity-scheduled-")):
            raise TaskError("life_command_invalid", "only scheduled sleep creation can be recovered")
        handoff = ScheduledSleepHandoff(app, routine, app.role_life.creation_provider())
        result = await handoff.recover(request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover_manual_sleep(context_key, request):
        from companion_v2.integration.manual_sleep_handoff import ManualSleepHandoff
        from companion_v2.integration.manual_wake_handoff import ManualCancelSleepHandoff, ManualWakeHandoff
        from companion_v2.role_work.life.management import RoleLifeManagement
        app, routine = routine_for(context_key)
        if type(request) is not dict:
            raise TaskError("life_command_invalid", "manual sleep recovery command is invalid")
        command_id = request.get("command_id")
        manager = RoleLifeManagement(app, routine.context)
        if type(command_id) is str and request.get("kind") == "activity.create" and command_id.startswith("activity-create-"):
            handoff = ManualSleepHandoff(manager, app.role_life.creation_provider())
        elif type(command_id) is str and request.get("kind") == "activity.transition" and command_id.startswith("activity-wake-"):
            handoff = ManualWakeHandoff(manager, app.role_life.transition_provider())
        elif type(command_id) is str and request.get("kind") == "activity.transition" and command_id.startswith("activity-cancel-sleep-"):
            handoff = ManualCancelSleepHandoff(manager, app.role_life.transition_provider())
        else:
            raise TaskError("life_command_invalid", "manual sleep recovery command is invalid")
        result = await handoff.recover(request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover_reading_deletion(context_key, request):
        from companion_v2.integration.activity_deletion_handoff import ActivityDeletionHandoff
        from companion_v2.role_work.life.management import RoleLifeManagement
        app, routine = routine_for(context_key)
        if (type(request) is not dict or request.get("kind") != "activity.delete"
                or type(request.get("command_id")) is not str
                or not request["command_id"].startswith("activity-delete-")):
            raise TaskError("life_command_invalid", "reading deletion recovery command is invalid")
        manager = RoleLifeManagement(app, routine.context)
        result = await ActivityDeletionHandoff(manager, app.role_life.deletion_provider()).recover(request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover_reading_cancel(context_key, request):
        from companion_v2.integration.manual_cancel_reading_handoff import ManualCancelReadingHandoff
        from companion_v2.role_work.life.management import RoleLifeManagement
        app, routine = routine_for(context_key)
        if (type(request) is not dict or request.get("kind") != "activity.transition"
                or type(request.get("command_id")) is not str
                or not request["command_id"].startswith("activity-cancel-reading-")):
            raise TaskError("life_command_invalid", "reading cancellation recovery command is invalid")
        manager = RoleLifeManagement(app, routine.context)
        result = await ManualCancelReadingHandoff(manager, app.role_life.transition_provider()).recover(request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    def sleep_interruptions():
        return sleep_interruption_queue.pending(current().runtime)

    async def interrupt_sleep(context_key, intent_id):
        app, routine = routine_for(context_key)
        handoff = SleepInterruptionHandoff(app, routine, app.role_life.transition_provider(), intent_id)
        result = await handoff.execute_intent()
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover_calendar(context_key, request):
        app = current()
        routine = app.life_runtime.routines.get(context_key)
        binding = app.state_maintenance.bindings.get(context_key)
        context = (routine.context if routine is not None and routine.context is not None and routine.context.key == context_key
                   else binding[0] if binding else None)
        if context is None or context.key != context_key:
            raise TaskError("life_context_changed", "the original calendar context is unavailable")
        calendar = app.calendar
        calendar._require_life_boundary()
        with calendar._life_bridge_lock:
            if calendar._life_calendar_owner() != "good_life":
                raise TaskError("life_calendar_owner_invalid", "calendar execution has not transferred")
            calendar._resolve_life_bridge()
            bridge = calendar._life_command_bridge
            if bridge is None:
                raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
            if type(request) is dict and request.get("kind") == "calendar.batch":
                from companion_v2.integration.calendar_batch_handoff import CalendarBatchHandoff
                result = CalendarBatchHandoff(calendar, bridge.commands).recover(context, request)
            else:
                result = bridge.recover(context, request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return result

    def planning_bindings():
        app = current()
        if app.calendar._life_calendar_owner() != "good_life":
            return ()
        app.calendar.prepare_life_snapshot_read()
        with app.role_life.consistent_snapshot():
            groups = app.calendar_planning._role_bindings()
            result = []
            for role_key, bindings in sorted(groups.items()):
                context, instruction, _guard = app.calendar_planning._role_anchor(bindings)
                if instruction:
                    result.append({"role_key": role_key, "context_key": context.key})
            return tuple(result)

    def planning_check(context_key):
        from companion_v2.agenda.planning import KIND
        from companion_v2.agenda.planning_config import SCHEDULE_MODE_ORCHESTRATED, planning_values
        app = current()
        binding = app.state_maintenance.bindings.get(context_key)
        if binding is None or binding[0].key != context_key or not binding[1]:
            raise TaskError("planning_role_changed", "the original planning context is unavailable")
        context, planner = binding[0], app.calendar_planning
        app.calendar.prepare_life_snapshot_read()
        with app.role_life.consistent_snapshot():
            settings = planning_values(app.role_state.settings(context)[0])
            if (KIND not in app.gateway.router.profiles or settings["schedule_simulation_mode"] != SCHEDULE_MODE_ORCHESTRATED
                    or not settings["planning_enabled"] or not settings["calendar_enabled"]
                    or app.state_maintenance.foreground_busy(context)):
                return None
            deferred = app.gateway.ledger.background_deferral(context, KIND, at=planner.tasks.now())
            if deferred:
                planner._set_result(context, deferred)
                return None
            opportunity = planner.opportunity(context, settings, planner.tasks.now())
            if opportunity is None:
                return None
            mode, slot, occurrence = opportunity
            return {"mode": mode, "slot": slot, "occurrence": occurrence}

    async def planning_run(context_key, opportunity):
        app = current()
        if type(opportunity) is not dict or set(opportunity) != {"mode", "slot", "occurrence"}:
            raise TaskError("planning_job_unavailable", "planning opportunity is invalid")
        if planning_check(context_key) != opportunity:
            raise TaskError("planning_inputs_changed", "planning opportunity changed")
        binding = app.state_maintenance.bindings.get(context_key)
        if binding is None:
            raise TaskError("planning_role_changed", "the original planning context is unavailable")
        app.calendar.prepare_life_snapshot_read()
        with app.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            return await app.calendar_planning.run(binding[0], binding[1], mode=opportunity["mode"], slot=opportunity["slot"],
                occurrence=opportunity["occurrence"], automatic=True, life=True)

    def planning_manual_pending():
        app = current()
        return tuple(item for item in manual_calendar_queue.pending(app)
            if (binding := app.state_maintenance.bindings.get(item["context_key"])) is not None
            and binding[0].key == item["context_key"] and digest(role_owner(binding[0])) == item["owner_key"])

    async def planning_manual_run(context_key, operation_id):
        app = current()
        binding = app.state_maintenance.bindings.get(context_key)
        if binding is None or binding[0].key != context_key or not binding[1]:
            raise TaskError("planning_role_changed", "the original planning context is unavailable")
        app.calendar.prepare_life_snapshot_read()
        with app.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            return await manual_calendar_queue.run(app, binding[0], binding[1], operation_id)

    async def recover_state(context_key, request):
        app, routine = routine_for(context_key)
        service = app.role_state
        with service._life_state_lock:
            if service.state_owner() != "good_life" or service._life_records is None or service._life_commands is None:
                raise TaskError("life_provider_unavailable", "the state life provider is unavailable")
            result = StateWriteHandoff(service, service._life_records, service._life_commands).recover(routine.context, request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return result

    def state_bindings():
        app = current()
        if app.role_state.state_owner() != "good_life":
            return ()
        result = []
        for context, instruction, _guard in tuple(app.state_maintenance.bindings.values()):
            if not instruction:
                continue
            try:
                settings, token = app.role_state.settings(context)
                result.append({"context_key": context.key, "owner": json.loads(encode(role_owner(context))),
                    "schedule_key": role_schedule_key(context), "context_json": encode(context),
                    "settings_json": encode({"values": settings, "token": token, "instruction": instruction}),
                    "enabled": bool(settings["enabled"]),
                    "can_anchor": app.state_maintenance._can_anchor_schedule(context)})
            except (TaskError, ValueError, KeyError):
                continue
        return tuple(result)

    def state_preflight(row):
        app = current()
        if app.role_state.state_owner() != "good_life":
            return None
        context = TaskContext.restore(json.loads(row["context_json"]))
        if role_schedule_key(context) != row["context_key"]:
            raise TaskError("state_schedule_invalid", "state schedule owner changed")
        maintenance = app.state_maintenance
        binding = maintenance._current_role_binding(context)
        if binding is None:
            return None
        context, instruction, guard = binding
        saved = json.loads(row["settings_json"])
        maintenance.service.access.check(context, STATE_KIND)
        settings, token = maintenance.service.settings(context)
        now = maintenance.tasks.now()
        if (not settings["enabled"] or token != saved["token"] or row["next_at"] > stamp(now)
                or maintenance.foreground_busy(context) or maintenance.workers):
            return None
        with app.role_life.consistent_snapshot():
            app.calendar.prepare_life_snapshot_read()
            context, instruction, guard = maintenance._conversation_anchor((context, instruction, guard), now)
        selected_settings, selected_token = maintenance.service.settings(context)
        if not selected_settings["enabled"] or maintenance.gateway.ledger.background_deferral(context, STATE_KIND, at=now):
            return None
        return {"context_key": context.key, "owner_key": digest(role_owner(context)), "token": selected_token}

    def state_patrol_snapshot(context_key, owner_key):
        app = current()
        binding = app.state_maintenance.bindings.get(context_key)
        if (app.role_state.state_owner() != "good_life" or binding is None
                or binding[0].key != context_key or digest(role_owner(binding[0])) != owner_key):
            raise TaskError("state_role_context_unavailable", "state patrol context is unavailable")
        with app.role_life.consistent_snapshot():
            state = app.role_state.read(binding[0])
        values = state.get("values") if isinstance(state.get("values"), dict) else {}
        return {"state": state.get("state"), "revision": state.get("revision"),
                "live_conditions": len(values.get("conditions") or ()),
                "expired_conditions": state.get("expired_conditions")}

    async def state_run(claim):
        app = current()
        required = {"request_id", "context_key", "owner_key", "schedule_key", "schedule_revision", "due_at", "token"}
        if (type(claim) is not dict or set(claim) != required
                or claim["request_id"] != "state-period-" + digest([claim["context_key"], claim["schedule_revision"], claim["due_at"]])
                or registry.lease().dispatch("state_schedule", "verify_claim", claim=claim) is not True):
            raise TaskError("state_schedule_invalid", "state schedule claim is unavailable")
        binding = app.state_maintenance.bindings.get(claim["context_key"])
        if binding is None:
            raise TaskError("state_role_context_unavailable", "state context is unavailable")
        context, instruction, _guard = binding
        settings, token = app.role_state.settings(context)
        if (app.role_state.state_owner() != "good_life" or not settings["enabled"] or token != claim["token"]
                or role_schedule_key(context) != claim["schedule_key"] or digest(role_owner(context)) != claim["owner_key"]
                or claim["due_at"] > stamp(app.state_maintenance.tasks.now())
                or not app.state_maintenance._can_anchor_schedule(context)
                or app.state_maintenance.foreground_busy(context)):
            raise TaskError("state_schedule_changed", "state schedule is no longer authorized")
        result = await app.state_maintenance.run_life(context, instruction, claim["request_id"])
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return result

    def state_recover_manual():
        return life_state_schedule.recover(current())

    def state_intervention_pending():
        app = current()
        if app.role_state.state_owner() != "good_life":
            return ()
        return tuple(item for item in state_intervention_queue.pending(app)
            if (binding := app.state_maintenance.bindings.get(item["context_key"])) is not None
            and binding[0].key == item["context_key"] and digest(role_owner(binding[0])) == item["owner_key"])

    async def state_intervention_run(context_key, operation_id):
        app = current()
        binding = app.state_maintenance.bindings.get(context_key)
        if binding is None or binding[0].key != context_key or not binding[1]:
            raise TaskError("state_not_bound", "the original state context is unavailable")
        return await state_intervention_queue.run(app, binding[0], binding[1], operation_id)

    async def pause_interrupted_work(context_key, activity_id, revision):
        app, routine = routine_for(context_key)
        handoff = InterruptedWorkHandoff(app, routine, app.role_life.transition_provider())
        result = await handoff.execute(activity_id, expected_revision=revision)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    async def recover_interrupted_work(context_key, request):
        app, routine = routine_for(context_key)
        if (type(request) is not dict or request.get("kind") != "activity.transition"
                or type(request.get("command_id")) is not str or not request["command_id"].startswith("activity-interrupted-")):
            raise TaskError("life_command_invalid", "only the original interrupted activity can be recovered")
        handoff = InterruptedWorkHandoff(app, routine, app.role_life.transition_provider())
        result = await handoff.recover(request)
        if current() is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    def reading_snapshot(context_key):
        app, routine = routine_for(context_key)
        routine.refresh()
        routine.authorize()
        settings = routine.settings
        if not settings or not settings["enabled"]:
            raise TaskError("life_disabled", "the original life routine is disabled")
        at = app.now()
        app.calendar.prepare_life_snapshot_read()
        with app.role_life.consistent_snapshot():
            records = routine.activities()
            calendar = app.calendar.occurrences(routine.context, start=at, end=at + timedelta(minutes=30))

            @cache
            def source_visible(source):
                return app.role_journal.sources.validate_reference_for_audience(
                    source, recipient=routine.context.actor.subject, observed_at=at).is_success

            with validation_batch(app.runtime, at):
                authorized = [activity.work.source.source_id for activity in records
                    if activity.work.kind == "reading" and activity.state.value == "planned"
                    and source_visible(activity.work.source)]
            approved_paused = [activity.activity_id for activity in records
                if activity.work.kind == "reading" and activity.state.value == "paused"
                and routine.approved_interrupted_reading_retry(activity, app.role_reader.task_id(routine.context, activity))]
            busy = (any(not worker.done() for worker in routine._workers.values())
                    or any(activity.state.value == "active" for activity in records)
                    or bool(app.role_state.body_clock.pending_sleep(routine.context)))
            reading = next((item for item in records if item.work.kind == "reading"), None)
            pace = (app.role_life.transition_provider().reading_metadata(context_key=context_key,
                    source=json.loads(encode(reading.work.source)))["pace"] if reading is not None else None)
        pace_until = (datetime.fromisoformat(pace["started_at"]) + timedelta(
            seconds=60 * pace["characters"] / settings["reading_speed_chars_per_minute"])) if pace else None
        return {"context_key": context_key, "owner_key": digest(routine.owner), "observed_at": stamp(at),
                "timezone": settings["timezone"], "reading_enabled": settings["reading_enabled"],
                "sleep_enabled": settings["sleep_enabled"], "busy": bool(busy),
                "pace_until": stamp(pace_until) if pace_until else None,
                "records": json.loads(encode(records)), "authorized_sources": authorized,
                "approved_paused": approved_paused, "calendar": calendar}

    def reading_choice_result(context_key, slot):
        app, routine = routine_for(context_key)
        if type(slot) is not str or not slot or len(slot) > 256:
            raise TaskError("life_reading_invalid", "reading choice slot is invalid")
        task_id = app.role_reader.choice_id(routine.context, slot)
        operation = OperationRepository(app.runtime).get(task_id)
        if not operation.is_success:
            return {"state": "absent"}
        receipt = operation.value
        if receipt.operation.scope != routine.context.scope or receipt.operation.owner != "role.reading.choice":
            raise TaskError("life_reading_invalid", "reading choice authority changed")
        if receipt.state is OperationState.COMPLETED and receipt.commit_state is CommitState.COMMITTED:
            row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                       ("life.reading.choice.evidence.v1:" + task_id,))
            evidence = json.loads(row["value"]) if row else None
            if (type(evidence) is not dict or set(evidence) != {"context_key", "slot", "value"}
                    or evidence["context_key"] != context_key or evidence["slot"] != slot
                    or type(evidence["value"]) is not dict):
                raise TaskError("life_reading_invalid", "reading choice evidence is unavailable")
            return {"state": "committed", "value": evidence["value"]}
        row = app.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (task_id,))
        state = row["state"] if row else None
        if state in ("failed", "cancelled", "expired", "rejected"):
            return {"state": "failed"}
        return {"state": "unknown" if state == "uncertain" else "pending"}

    def _reading_window(snapshot, next_sleep):
        now = datetime.fromisoformat(snapshot["observed_at"])
        if next_sleep is not None:
            next_sleep = datetime.fromisoformat(next_sleep)
            if next_sleep.utcoffset() is None:
                raise TaskError("life_reading_invalid", "sleep schedule has no timezone")
        return reading_window(snapshot["calendar"], now, now.astimezone(ZoneInfo(snapshot["timezone"])),
                              next_sleep if snapshot["sleep_enabled"] else None)

    async def choose_reading(context_key, identifiers, slot, schedule, next_sleep):
        app, routine = routine_for(context_key)
        await routine.preflight()
        snapshot = reading_snapshot(context_key)
        now = datetime.fromisoformat(snapshot["observed_at"])
        day = now.astimezone(ZoneInfo(snapshot["timezone"])).date().isoformat()
        with app.role_life.consistent_snapshot():
            records = routine.activities()
            base = choice_slot(records, day) + ":"
            if (snapshot["busy"] or not snapshot["reading_enabled"] or _reading_window(snapshot, next_sleep) != schedule
                    or type(slot) is not str or not slot.startswith(base) or not slot[len(base):].isascii() or not slot[len(base):].isdigit()
                    or type(identifiers) is not list):
                raise TaskError("life_reading_invalid", "reading choice is no longer due")
            choices = routine._smart_reading.candidates(records, day)
            if identifiers != [item.activity_id for item in choices]:
                raise TaskError("life_reading_invalid", "reading candidates changed")
        with app.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            await app.role_reader.select_book(routine.context, choices, role_instruction=routine.instruction,
                                              day=slot, schedule=schedule, life=True)
        return reading_choice_result(context_key, slot)

    async def start_reading(context_key, activity_id, revision, next_sleep, resume=False):
        app, routine = routine_for(context_key)
        snapshot = reading_snapshot(context_key)
        if (snapshot["busy"] or not snapshot["reading_enabled"] or not _reading_window(snapshot, next_sleep)):
            raise TaskError("life_reading_invalid", "reading is no longer due")
        activity = routine.activity(activity_id)
        with app.role_life.consistent_snapshot():
            if resume:
                task_id = app.role_reader.task_id(routine.context, activity)
                if activity.state.value != "paused" or not routine.approved_interrupted_reading_retry(activity, task_id):
                    raise TaskError("life_reading_unresolved", "reading retry is not approved")
                request_id = "resume-interrupted-" + digest([task_id, activity.revision])
            else:
                records = routine.activities()
                day = datetime.fromisoformat(snapshot["observed_at"]).astimezone(ZoneInfo(snapshot["timezone"])).date().isoformat()
                candidates = routine._smart_reading.candidates(records, day, activity.work.source.source_id)
                if activity not in candidates:
                    raise TaskError("life_reading_invalid", "an earlier reading segment is still pending")
                request_id = activity.work.request_id
        started = await ReadingStartHandoff(app, routine, app.role_life.transition_provider()).execute(
            activity_id, expected_revision=revision, request_id=request_id)
        return {"activity_id": started.activity_id, "revision": started.revision}

    async def execute_reading(context_key, activity_id, revision):
        app, routine = routine_for(context_key)
        await routine.preflight()
        activity = routine.activity(activity_id)
        if activity.revision != revision or activity.state.value != "active" or activity.work.kind != "reading":
            raise TaskError("life_reading_invalid", "reading activity changed before execution")
        app.calendar.prepare_life_snapshot_read()
        with app.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            result = await app.role_reader.consume(routine.context, activity, role_instruction=routine.instruction, life=True)
        if result.is_success and result.unwrap().task and result.unwrap().task.state == "succeeded":
            return {"state": "completed"}
        try:
            current_activity = routine.activity(activity_id)
            if current_activity.state.value == "active":
                await InterruptedWorkHandoff(app, routine, app.role_life.transition_provider()).execute(
                    activity_id, expected_revision=current_activity.revision)
        except (TaskError, ValueError, KeyError):
            pass
        return {"state": "paused"}

    async def recover_reading(context_key, request):
        app, routine = routine_for(context_key)
        command_id = request.get("command_id", "") if type(request) is dict else ""
        if command_id.startswith("activity-reading-start-"):
            handoff = ReadingStartHandoff(app, routine, app.role_life.transition_provider())
        elif command_id.startswith("activity-reading-complete-"):
            handoff = ReadingCompletionHandoff(app, routine, app.role_life.transition_provider())
        else:
            raise TaskError("life_command_invalid", "reading recovery command is invalid")
        return await handoff.recover(request)

    async def recover_reading_import(context_key, request):
        app, routine = routine_for(context_key)
        if (type(request) is not dict or request.get("kind") != "activity.create"
                or not str(request.get("command_id", "")).startswith("activity-reading-import-")):
            raise TaskError("life_command_invalid", "reading import recovery command is invalid")
        from companion_v2.role_work.life.management import RoleLifeManagement
        return await ReadingImportHandoff(RoleLifeManagement(app, routine.context), app.role_life.creation_provider()).recover(request)

    async def recover_social(context_key, request):
        app, routine = routine_for(context_key)
        if type(request) is not dict:
            raise TaskError("life_command_invalid", "browsing recovery command is invalid")
        command_id = request.get("command_id", "")
        from companion_v2.integration.social_handoff import SocialCompletionHandoff, SocialStartHandoff
        if request.get("kind") == "activity.create" and str(command_id).startswith("activity-social-start-"):
            handoff = SocialStartHandoff(app, routine, app.role_life.creation_provider(), None, None)
        elif request.get("kind") == "activity.transition" and str(command_id).startswith("activity-social-complete-"):
            handoff = SocialCompletionHandoff(app, routine, app.role_life.transition_provider())
        else:
            raise TaskError("life_command_invalid", "browsing recovery command is invalid")
        return await handoff.recover(request)

    def social_snapshot(context_key):
        from companion_v2.role_work.interests import visible_interests
        app, routine = routine_for(context_key)
        snapshot = reading_snapshot(context_key)
        port = getattr(app, "social_port", None)
        snapshot.update(owner=json.loads(encode(routine.owner)), social_interval_minutes=routine.settings["social_interval_minutes"],
            social_available=bool(port is not None and port.enabled_for(routine.context) and any(port.available().values())),
            interests=bool(visible_interests(app, routine.context, app.now())))
        return snapshot

    async def start_social(context_key, plan):
        app, routine = routine_for(context_key)
        from companion_v2.integration.social_handoff import SocialStartHandoff
        handoff = SocialStartHandoff(app, routine, app.role_life.creation_provider(), lambda: social_snapshot(context_key), plan)
        result = await handoff.execute()
        activity = activity_from_json(result["result"]["record_json"])
        return {"activity_id": activity.activity_id, "revision": activity.revision}

    async def execute_social(context_key, activity_id, revision, schedule):
        app, routine = routine_for(context_key)
        await routine.preflight()
        activity = routine.activity(activity_id)
        if (activity.revision != revision or activity.state.value != "active" or activity.work.kind != "social"
                or type(schedule) is not str or not schedule or len(schedule) > 400
                or not app.social_port.enabled_for(routine.context)):
            raise TaskError("life_social_invalid", "browsing activity changed before execution")
        recent = sorted((item for item in routine.activities() if item.work.kind == "social"), key=lambda item: item.started_at)
        previous = tuple({"title": item.description, "checkpoint": item.checkpoint[:800]}
                         for item in recent[-3:] if item.state.value == "completed")
        event = routine.event("social-" + activity.activity_id, "生活执行端发现角色日程中有空档；这不是用户发来的聊天。")
        app.calendar.prepare_life_snapshot_read()
        with app.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            result = await app.role_social.consume(routine.context, activity, role_instruction=routine.instruction,
                                                   event=event, schedule=schedule, previous=previous)
        try:
            current_activity = routine.activity(activity_id)
            if current_activity.state.value == "active":
                await InterruptedWorkHandoff(app, routine, app.role_life.transition_provider()).execute(
                    activity_id, expected_revision=current_activity.revision)
                return {"state": "paused"}
            return {"state": current_activity.state.value}
        except (TaskError, ValueError, KeyError):
            return {"state": "pending"}

    async def recover_configuration(request):
        from companion_v2.configuration.life_handoff import LifeConfigurationHandoff
        if getattr(controller, "_terminated", False) or not registry.owns_registration(registration_id, plugin):
            raise TaskError("life_provider_unavailable", "the registered life provider is unavailable")
        session = getattr(controller, "session", None)
        app = getattr(session, "application", None)
        if app is None or app.state not in ("new", "running"):
            raise TaskError("life_provider_unavailable", "the companion session is unavailable")
        result = LifeConfigurationHandoff(app.configuration).recover(request)
        if getattr(getattr(controller, "session", None), "application", None) is not app:
            raise TaskError("life_provider_unavailable", "the companion session changed")
        return {"state": result["state"]}

    def reading_manual_pending():
        return manual_reading_queue.pending(current())

    async def execute_manual_reading(context_key, operation_id):
        app, routine = routine_for(context_key)
        await routine.preflight()
        started = await manual_reading_queue.start(app, routine, operation_id)
        if started.get("state") == "rejected":
            return {"state": "rejected"}
        if started["active"]:
            return await execute_reading(context_key, started["activity_id"], started["revision"])
        return {"state": "settled"}

    return {"api_version": "companion.life-host.v1", "contexts": contexts, "wake": wake, "recover": recover,
            "sleep_snapshot": sleep_snapshot, "start_sleep": start_sleep, "recover_sleep_creation": recover_sleep_creation,
            "recover_manual_sleep": recover_manual_sleep,
            "recover_reading_deletion": recover_reading_deletion,
            "recover_reading_cancel": recover_reading_cancel,
            "sleep_interruptions": sleep_interruptions, "interrupt_sleep": interrupt_sleep,
            "calendar_contexts": calendar_contexts, "recover_calendar": recover_calendar, "recover_state": recover_state,
            "recover_configuration": recover_configuration,
            "planning_bindings": planning_bindings, "planning_check": planning_check, "planning_run": planning_run,
            "planning_manual_pending": planning_manual_pending, "planning_manual_run": planning_manual_run,
            "state_bindings": state_bindings, "state_preflight": state_preflight, "state_run": state_run,
            "state_patrol_snapshot": state_patrol_snapshot,
            "state_recover_manual": state_recover_manual,
            "state_intervention_pending": state_intervention_pending, "state_intervention_run": state_intervention_run,
            "reading_snapshot": reading_snapshot, "reading_choice_result": reading_choice_result,
            "choose_reading": choose_reading, "start_reading": start_reading,
            "execute_reading": execute_reading, "recover_reading": recover_reading,
            "recover_reading_import": recover_reading_import,
            "recover_social": recover_social,
            "social_snapshot": social_snapshot, "start_social": start_social, "execute_social": execute_social,
            "reading_manual_pending": reading_manual_pending, "execute_manual_reading": execute_manual_reading,
            "pause_interrupted_work": pause_interrupted_work, "recover_interrupted_work": recover_interrupted_work}
