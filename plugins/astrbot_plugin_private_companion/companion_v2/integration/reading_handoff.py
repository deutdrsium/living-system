"""Authorize reading starts and supervised completions in the life store."""

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State, RoleReadingReceipt
from companion_v2.role_work.work_models import RoleWorkSpec

from . import activity_transition_authority, life_commands
from .natural_wake_handoff import NaturalWakeHandoff


class _ReadingHandoff(NaturalWakeHandoff):
    def _source(self, activity, at):
        work = activity.work
        if work is None or work.kind != "reading":
            raise TaskError("life_reading_invalid", "reading activity is unavailable")
        source = require(self.app.role_journal.sources.read(work.source, resolved=self.routine.context.actor,
            capability="role.reading.record", observed_at=at, authorizer=self.app.tasks.identity))
        if (type(source.payload) is not str or source.content_hash != work.content_hash
                or digest(source.payload) != work.content_hash or work.end_offset > len(source.payload)):
            raise TaskError("reading_source_changed", "reading must retain its original text")
        return source.payload

    def _request(self, activity, expected, at):
        context = self.routine.context
        return {"command_id": self._command_id(expected["operation_id"], at),
            "owner_key": digest(self.routine.owner), "context_key": context.key, "kind": "activity.transition",
            "expected_revision": activity.revision,
            "payload": {"activity_id": activity.activity_id, "operation_id": expected["operation_id"],
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(),
                "state": expected["state"], "observed_at": stamp(at), "checkpoint": expected["checkpoint"],
                "reading": expected.get("reading"), "work": expected["work"], "sleep": None,
                "event_consumers": expected["event_consumers"], "reading_control": expected["reading_control"]}}

    async def recover(self, request):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.routine.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.routine.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            self.abort(request, observed_at=self.app.now())
            return {"state": "aborted"}
        return await super().recover(request)


class ReadingStartHandoff(_ReadingHandoff):
    prefix = "life.activity.reading-start.v1:"

    def _command_id(self, operation_id, observed_at):
        return "activity-reading-start-" + digest([self.routine.context.key, operation_id, observed_at])

    def _expected_start(self, activity, request_id, at):
        if activity.state not in (State.PLANNED, State.PAUSED) or self.routine.settings is None or not self.routine.settings["enabled"]:
            raise TaskError("life_reading_invalid", "only an available reading may start")
        body = self.app.role_state.body_clock
        if body.pending_sleep(self.routine.context) or body.active(self.routine.context, "reading"):
            raise TaskError("life_reading_active", "the role is already sleeping or reading")
        text = self._source(activity, at)
        work = activity.work
        retry_id = self.app.role_reader.task_id(self.routine.context, activity) if activity.state is State.PAUSED else None
        approved = False
        if retry_id is not None:
            task = self.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (retry_id,))
            unknown = self.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id=? AND state IN ('reserved','dispatched','unknown')", (retry_id,))
            approved = bool(unknown and self.routine.approved_interrupted_reading_retry(activity, retry_id))
            if ((unknown and not approved) or (task is not None and task["state"] not in ("failed", "cancelled", "expired", "rejected"))):
                raise TaskError("life_reading_unresolved", "the original reading must settle before an explicit retry")
        metadata = self.provider.reading_metadata(context_key=self.routine.context.key, source=json.loads(encode(work.source)))
        pace = metadata["pace"]
        if pace is not None and not approved:
            until = datetime.fromisoformat(pace["started_at"]) + timedelta(
                seconds=60 * pace["characters"] / self.routine.settings["reading_speed_chars_per_minute"])
            if at < until:
                raise TaskError("life_reading_paced", "wait for the current reading time to finish")
        work = replace(work, request_id=request_id)
        if metadata["lazy"] and activity.state is State.PLANNED:
            size, start = self.routine.settings["reading_chunk_chars"], work.start_offset
            while start < len(text) and not text[start:start + size].strip():
                start += size
            if start >= len(text):
                raise TaskError("reading_source_empty", "the remaining original has no readable text")
            work = replace(work, start_offset=start, end_offset=min(len(text), start + size))
        result = {"state": "active", "checkpoint": "正在阅读实际提供的片段", "work": json.loads(encode(work)),
            "sleep": None, "operation_id": "read-start-" + digest([self.routine.context.key, request_id]),
            "event_consumers": [], "reading_control": {"mode": "start", "lazy": metadata["lazy"],
                "prior_pace": pace, "pace": {"started_at": stamp(at), "characters": work.end_offset - work.start_offset},
                "retry_task_id": retry_id, "retry_marker": self.provider.reading_retry(
                    context_key=self.routine.context.key, task_id=retry_id) if retry_id else None}}
        proposal = self.provider.propose_reading_start(activity=json.loads(encode(activity)), text=text, request_id=request_id,
            chunk_size=self.routine.settings["reading_chunk_chars"], observed_at=stamp(at))
        if proposal != result:
            raise TaskError("life_reading_invalid", "reading start differs from the original rules")
        return result

    def prepare(self, activity_id, *, expected_revision, request_id, observed_at):
        self._outside_host_transaction()
        if self.app.role_life.activity_owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "reading belongs to the life plugin")
        self.routine.refresh()
        with self.app.role_life.consistent_snapshot():
            self.routine.authorize()
            activity = self.routine.activity(activity_id)
            if activity.revision != expected_revision:
                raise TaskError("life_revision_conflict", "reading activity changed")
            expected = self._expected_start(activity, request_id, observed_at)
        request = self._request(activity, expected, observed_at)
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def _authorize(self, request, proposal, *, observed_at):
        self.routine.authorize()
        previous = activity_from_json(proposal["previous_json"])
        payload = request["payload"]
        at = datetime.fromisoformat(payload["observed_at"])
        expected = self._expected_start(previous, payload["work"]["request_id"], at)
        if (any(payload[key] != expected[key] for key in ("state", "checkpoint", "work", "sleep", "operation_id", "event_consumers", "reading_control"))
                or payload["reading"] is not None or request["command_id"] != self._command_id(expected["operation_id"], at)):
            raise TaskError("life_reading_invalid", "reading start inputs changed")
        return activity_transition_authority.authorize(self.app.tasks, self.app.role_journal.sources,
            self.routine.context, request, proposal, expected_operation_id=expected["operation_id"],
            expected_consumers=[], observed_at=observed_at)

    async def execute(self, activity_id, *, expected_revision, request_id):
        await self.routine.preflight()
        at = self.app.now()
        request = self.prepare(activity_id, expected_revision=expected_revision, request_id=request_id, observed_at=at)
        try:
            decision = self.decide(request, observed_at=at)
        except (TaskError, ValueError):
            self.abort(request, observed_at=at)
            raise
        result = self.apply(request, decision)
        self.complete(request, decision, observed_at=at)
        self.acknowledge(request, decision)
        self.app.notify()
        return activity_from_json(result["result"]["record_json"])


class ReadingCompletionHandoff(_ReadingHandoff):
    prefix = "life.activity.reading-completion.v1:"

    def __init__(self, app, routine, provider, *, run=None, reflection=None):
        super().__init__(app, routine, provider)
        self.run, self.reflection = run, reflection

    def _command_id(self, operation_id, observed_at):
        return "activity-reading-complete-" + digest([self.routine.context.key, operation_id, observed_at])

    def _expected_completion(self, activity, task_id, reflection, at):
        if activity.state is not State.ACTIVE:
            raise TaskError("life_reading_invalid", "reading is no longer active")
        text, work = self._source(activity, at), activity.work
        if not text[work.start_offset:work.end_offset].strip():
            raise TaskError("reading_span_empty", "completion requires the original span")
        receipt = RoleReadingReceipt(work.source, work.content_hash, len(text), work.start_offset, work.end_offset, at, reflection)
        metadata = self.provider.reading_metadata(context_key=self.routine.context.key, source=json.loads(encode(work.source)))
        following = None
        start = work.end_offset
        if metadata["lazy"]:
            while start < len(text):
                end = min(start + 6000, len(text))
                if text[start:end].strip():
                    identifier = "book-part-" + digest([work.source.version.lineage_id, start])
                    next_work = RoleWorkSpec("reading", work.context_key, identifier, work.source, work.content_hash, start, end)
                    following = RoleActivity(identifier, activity.role_id, activity.scope, at, State.PLANNED,
                        description=f"{activity.description.rsplit(' · ', 1)[0]} · {start + 1}–{end} / {len(text)} 字符", work=next_work)
                    break
                start = end
        result = {"state": "completed", "checkpoint": None, "work": None, "sleep": None,
            "reading": json.loads(encode(receipt)), "operation_id": "reading-complete-" + digest(task_id),
            "event_consumers": ["journal", "interest"], "reading_control": {"mode": "complete", "lazy": metadata["lazy"],
                "next_activity": json.loads(encode(following)) if following is not None else None}}
        proposal = self.provider.propose_reading_completion(activity=json.loads(encode(activity)), text=text,
            reflection=reflection, task_id=task_id, observed_at=stamp(at))
        if proposal != result:
            raise TaskError("life_reading_invalid", "reading completion differs from the original rules")
        return result

    def _parameters(self, request):
        payload = request["payload"]
        return digest([self.routine.owner, payload["activity_id"], request["expected_revision"],
                       State(payload["state"]), payload["checkpoint"], payload["reading"]])

    def _authorize(self, request, proposal, *, observed_at):
        if self.run is None or self.reflection is None:
            raise TaskError("life_activity_evidence_required", "reading model evidence is unavailable")
        self.routine.authorize()
        previous = activity_from_json(proposal["previous_json"])
        payload = request["payload"]
        at = datetime.fromisoformat(payload["observed_at"])
        expected = self._expected_completion(previous, self.run.task.task_id, self.reflection, at)
        if (any(payload[key] != expected[key] for key in ("state", "checkpoint", "reading", "work", "sleep", "operation_id", "event_consumers", "reading_control"))
                or request["command_id"] != self._command_id(expected["operation_id"], at)):
            raise TaskError("life_reading_invalid", "reading completion inputs changed")
        authorized = activity_transition_authority.authorize(self.app.tasks, self.app.role_journal.sources,
            self.routine.context, request, proposal, expected_operation_id=expected["operation_id"],
            expected_consumers=expected["event_consumers"], observed_at=observed_at,
            verify_completion=lambda *_: Outcome.success(True))
        self.run.validate()
        self.run.begin_effect()
        return authorized

    def execute_completion(self, activity, *, observed_at):
        self._outside_host_transaction()
        if self.app.role_life.activity_owner() != "good_life" or self.run is None:
            raise TaskError("life_activity_owner_invalid", "reading completion has no active executor")
        with self.app.role_life.consistent_snapshot():
            current = self.routine.activity(activity.activity_id)
            if current != activity:
                raise TaskError("life_revision_conflict", "reading activity changed")
            expected = self._expected_completion(activity, self.run.task.task_id, self.reflection, observed_at)
        request = self._request(activity, expected, observed_at)
        self.provider.prepare(request, prepared_at=observed_at)
        try:
            decision = self.decide(request, observed_at=observed_at)
        except (TaskError, ValueError):
            self.abort(request, observed_at=observed_at)
            raise
        result = self.apply(request, decision)
        self.complete(request, decision, observed_at=observed_at)
        self.acknowledge(request, decision)
        self.app.notify()
        return activity_from_json(result["result"]["record_json"])


def complete_supervised_reading(app, run, job, reflection):
    routine = app.life_runtime.routines.get(job.context.key)
    if (routine is None or routine.context != job.context or routine.instruction != job.instruction
            or routine._closed or routine._retired):
        raise TaskError("life_context_changed", "the original reading context is unavailable")
    return ReadingCompletionHandoff(app, routine, app.role_life.transition_provider(), run=run,
                                    reflection=reflection).execute_completion(job.activity, observed_at=app.now())
