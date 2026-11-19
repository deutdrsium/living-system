"""Transfer an authorized reading cancellation to the life ledger."""

from dataclasses import replace
from datetime import datetime
import hashlib
import json

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State, advance_activity
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from . import activity_transition_authority, life_commands
from .manual_wake_handoff import ManualCancelSleepHandoff


class ManualCancelReadingHandoff(ManualCancelSleepHandoff):
    command_prefix = "activity-cancel-reading-"
    checkpoint = None

    def already_ended(self, operator, data, *, observed_at):
        self._outside_host_transaction()
        operation_id, command_id = self._identity(operator, data)
        context = self.manager.context
        if self.provider.request_for_replay(command_id=command_id, owner_key=digest(self.manager.owner),
                                            context_key=context.key) is not None:
            return None
        with self.manager.app.role_life.consistent_snapshot():
            activity = self.manager._activity(data["activity_id"], current=False)
            if activity.revision != data["expected_revision"]:
                raise TaskError("life_revision_conflict", "reading activity changed")
            if activity.state not in (State.COMPLETED, State.CANCELLED):
                return None
            with self.runtime.transaction():
                self._identity(operator, data)
                operation = OperationRef(operation_id, "role.life.management", context.scope, VersionRef(operation_id, 1), operation_id)
                receipt = require(OperationRepository(self.runtime).reserve(operation, parameter_hash=digest(data), observed_at=observed_at))
                if receipt.replayed:
                    return {"operation_id": operation_id, "state": receipt.state.value, "replayed": True}
                require(OperationRepository(self.runtime).transition(operation_id, expected_state=OperationState.ACCEPTED,
                                                                      next_state=OperationState.COMPLETED, observed_at=observed_at))
        return {"operation_id": operation_id, "activity_id": activity.activity_id}

    def _expected_reading(self, activity, operation_id, at):
        work, context = activity.work, self.manager.context
        if (work is None or work.kind != "reading" or activity.scope != context.scope
                or activity.state not in (State.PLANNED, State.ACTIVE, State.PAUSED)):
            raise TaskError("life_reading_invalid", "reading cannot be cancelled in this context")
        if work.context_key != context.key:
            # The legacy manager can cancel a prior session in the same window,
            # but does not enqueue a further span under the current session.
            return {"state": "cancelled", "checkpoint": None, "work": None, "sleep": None,
                    "reading": None, "operation_id": operation_id + self.role_suffix, "event_consumers": []}
        source = self.manager.app.role_journal.sources.read(work.source, resolved=context.actor,
            capability="role.reading.record", observed_at=at, authorizer=self.manager.app.tasks.identity)
        text = source.unwrap().payload if source.is_success else None
        if (text is not None and (type(text) is not str or source.unwrap().content_hash != work.content_hash
                                 or digest(text) != work.content_hash)):
            text = None
        metadata = self.provider.reading_metadata(context_key=context.key, source=json.loads(encode(work.source)))
        chunk = 1
        if text is not None and metadata["lazy"] and activity.state is State.PLANNED:
            self.manager.life.refresh()
            chunk = self.manager.life.settings["reading_chunk_chars"]
        changed = work
        if text is not None and metadata["lazy"] and activity.state is State.PLANNED:
            changed = replace(work, end_offset=min(len(text), work.start_offset + chunk))
        following = None
        start = changed.end_offset
        if metadata["lazy"] and text is not None:
            while start < len(text):
                end = min(start + 6000, len(text))
                if text[start:end].strip():
                    identifier = "book-part-" + digest([work.source.version.lineage_id, start])
                    next_work = RoleWorkSpec("reading", context.key, identifier, work.source, work.content_hash, start, end)
                    following = RoleActivity(identifier, activity.role_id, activity.scope, at, State.PLANNED,
                        description=f"{activity.description.rsplit(' · ', 1)[0]} · {start + 1}–{end} / {len(text)} 字符",
                        work=next_work)
                    break
                start = end
        result = {"state": "cancelled", "checkpoint": None, "work": json.loads(encode(changed)) if changed != work else None,
            "sleep": None, "reading": None, "operation_id": operation_id + self.role_suffix, "event_consumers": [],
            "reading_control": {"mode": "cancel", "lazy": metadata["lazy"],
                "next_activity": json.loads(encode(following)) if following is not None else None,
                "text_length": len(text) if text is not None else None}}
        proposal = self.provider.propose_reading_cancel(activity=json.loads(encode(activity)), text=text,
            chunk_size=chunk, operation_id=operation_id + self.role_suffix, observed_at=stamp(at))
        if proposal != result:
            raise TaskError("life_reading_invalid", "reading cancellation differs from the original rules")
        return result

    def prepare(self, operator, data, *, observed_at):
        self._outside_host_transaction()
        operation_id, command_id = self._identity(operator, data)
        context, owner = self.manager.context, self.manager.owner
        original = self.provider.request_for_replay(command_id=command_id, owner_key=digest(owner), context_key=context.key)
        if original is not None:
            if original["payload"].get("management") != {"operation_id": operation_id, "parameter_hash": digest(data)}:
                raise TaskError("life_reading_invalid", "reading cancellation conflicts with its original request")
            return original
        with self.manager.app.role_life.consistent_snapshot():
            activity = self.manager._activity(data["activity_id"], current=False)
            if activity.revision != data["expected_revision"]:
                raise TaskError("life_revision_conflict", "reading activity changed")
            expected = self._expected_reading(activity, operation_id, observed_at)
        request = {"command_id": command_id, "owner_key": digest(owner), "context_key": context.key,
            "kind": "activity.transition", "expected_revision": activity.revision,
            "payload": {"activity_id": activity.activity_id, "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(),
                "observed_at": stamp(observed_at), **expected,
                "management": {"operation_id": operation_id, "parameter_hash": digest(data)}}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def _authorize(self, operator, data, request, proposal, *, observed_at):
        operation_id, command_id = self._identity(operator, data)
        previous = activity_from_json(proposal["previous_json"])
        payload = request["payload"]
        at = datetime.fromisoformat(payload["observed_at"])
        expected = self._expected_reading(previous, operation_id, at)
        if (request["command_id"] != command_id or request["expected_revision"] != data["expected_revision"]
                or payload["activity_id"] != data["activity_id"]
                or payload.get("management") != {"operation_id": operation_id, "parameter_hash": digest(data)}
                or any(payload[name] != value for name, value in expected.items())):
            raise TaskError("life_reading_invalid", "reading cancellation inputs changed")
        changed = activity_from_json(proposal["result_json"])
        expected_activity = advance_activity(previous, expected_revision=previous.revision, state=State.CANCELLED,
            observed_at=at, work=changed.work if payload["work"] is not None else None)
        if changed != expected_activity:
            raise TaskError("life_reading_invalid", "reading cancellation result changed")
        return activity_transition_authority.authorize(self.manager.app.tasks, self.manager.app.role_journal.sources,
            self.manager.context, request, proposal, expected_operation_id=operation_id + self.role_suffix,
            expected_consumers=[], observed_at=observed_at, require_life_authority=False, allow_prior_session_cancel=True)

    def _role_parameters(self, request):
        payload = request["payload"]
        parameters = [self.manager.owner, payload["activity_id"], request["expected_revision"], State.CANCELLED, None]
        if payload["work"] is not None:
            parameters.append(payload["work"])
        return digest(parameters)

    def stop_worker(self, request):
        return self.provider.stop_cancelled_reading(command_id=request["command_id"], owner_key=request["owner_key"],
            context_key=request["context_key"], verify_host_completion=lambda evidence: life_commands.verify_completion(self.runtime, evidence))

    async def recover(self, request):
        self._outside_host_transaction()
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        decision = life_commands.lookup(self.runtime, context_key=context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            result = self.abort(request, observed_at=self.manager.app.now())
        elif decision["decision"] == "abort":
            result = self.provider.resolve(decision,
                verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        else:
            self._check(None, request, decision)
            result = self.apply(None, request, decision)
            self.complete(None, request, decision, observed_at=self.manager.app.now())
            self.stop_worker(request)
            self.acknowledge(None, request, decision)
        self.manager.app.notify()
        return result
