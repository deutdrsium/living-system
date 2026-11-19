"""Read shared and window state through a revocable life-provider lease."""

import json
from threading import get_ident
from types import SimpleNamespace

from companion_v2.execution.models import TaskError, digest
from companion_v2.identity_policy.private_domain import source_ref
from companion_v2.role_work.state.ownership import role_owner, role_schedule_key
from companion_v2.storage.versioned_records import owner_key


class LifeStateRecords:
    def __init__(self, service, lease):
        self.service, self.lease = service, lease
        self.legacy = SimpleNamespace(owner_key=owner_key)

    def _call(self, operation, **request):
        if self.service.state_owner() != "good_life":
            raise TaskError("life_state_owner_changed", "state ownership changed")
        runtime = self.service.runtime
        diagnostic = getattr(getattr(runtime, "_read_snapshot_diagnostics", None), "current", None)
        if (getattr(runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None):
            if not self.service.life.is_pinned_provider(self.lease):
                raise TaskError("life_state_transaction_active", "state access requires a coordinated life snapshot")
        try:
            return self.lease.dispatch("state_read", operation, **request)
        except (TaskError, KeyError):
            raise
        except Exception as error:
            if getattr(error, "code", None) == "life_state_pending":
                raise TaskError("life_state_pending", "state change awaits host confirmation") from error
            if str(getattr(error, "code", "")).startswith(("role_mood_", "role_cycle_")):
                raise TaskError(error.code, str(error)) from error
            raise TaskError("life_provider_unavailable", "state life provider is unavailable") from error

    def owner_key(self, context):
        return digest(["role-body.v1", role_owner(context)])

    def operation_key(self, context):
        return owner_key(context)

    @staticmethod
    def _record(record, identifier, revision=None):
        if (type(record) is not dict or record.get("id") != identifier
                or type(record.get("revision")) is not int or record["revision"] < 1
                or type(record.get("updated_at")) is not str
                or (revision is not None and record["revision"] != revision)):
            raise TaskError("life_provider_data_invalid", "state record identity is invalid")
        return record

    def _get(self, key, identifier):
        record = self._call("get", owner_key=key, identifier=identifier)
        return self._record(record, identifier) if record is not None else None

    def storage(self, context):
        return "role" if self._get(self.owner_key(context), "current") else "window"

    def get(self, context, identifier, *, storage_key=None):
        if storage_key is not None:
            return self._get(storage_key, identifier)
        return self._get(self.owner_key(context), identifier) or self._get(owner_key(context), identifier)

    def version(self, context, identifier, revision, *, storage=None):
        key = owner_key(context) if storage == "window" or storage is None and self.storage(context) == "window" else self.owner_key(context)
        try:
            return self._record(self._call("version", owner_key=key, identifier=identifier, revision=revision), identifier, revision)
        except KeyError as error:
            raise TaskError("record_version_unavailable", "original record version is unavailable") from error

    def receipt(self, context, operation_id):
        for key in (self.owner_key(context), owner_key(context)):
            try:
                result = self._call("receipt", owner_key=key, operation_id=operation_id)
            except KeyError:
                continue
            if type(result) is not dict or result.get("operation_id") != operation_id:
                raise TaskError("life_provider_data_invalid", "state receipt is invalid")
            return result
        raise TaskError("operation_unavailable", "original domain operation is unavailable")

    def shared_source(self, context, reference):
        if reference.owner not in {"role.state", "role.state.cycle"}:
            return False
        record = self._call("shared_source", owner_key=self.owner_key(context), source_id=reference.source_id)
        if record is None:
            return False
        record = self._record(record, "current")
        return any(reference in tuple(source_ref(item) for item in record.get(field, ()))
                   for field in ("sources", "cycle_sources"))

    def history_at(self, context, identifier, at):
        key = self.owner_key(context) if self.storage(context) == "role" else owner_key(context)
        record = self._call("history_at", owner_key=key, identifier=identifier, at=at)
        return self._record(record, identifier) if record is not None else None

    def dialogue_cursor(self, context):
        return self._call("dialogue_cursor", owner_key=self.owner_key(context), context_key=context.key)

    def maintenance_instruction(self, context, *, settings, mood_round, has_growth_refs, skills_partial, intervention):
        result = self._call("maintenance_instruction", owner_key=self.owner_key(context), settings=settings,
            mood_round=mood_round, has_growth_refs=has_growth_refs, skills_partial=skills_partial, intervention=intervention)
        if type(result) is not str:
            raise TaskError("life_provider_data_invalid", "state maintenance instruction is invalid")
        return result

    def reply_blocks(self, context, *, simulation, intensity):
        result = self._call("reply_blocks", owner_key=self.owner_key(context), simulation=simulation, intensity=intensity)
        if (type(result) is not dict or set(result) != {"base", "humanized"}
                or any(type(value) is not str for value in result.values())):
            raise TaskError("life_provider_data_invalid", "state reply text is invalid")
        return result

    def compute(self, context, domain, function, *args, **options):
        return self._call("compute", owner_key=self.owner_key(context), domain=domain,
                          function=function, args=args, options=options)

    def schedule_status(self, context):
        return self._call("schedule_status", owner_key=self.owner_key(context), schedule_key=role_schedule_key(context))

    def schedule_anchor(self, context):
        from companion_v2.execution.models import TaskContext
        raw = self._call("schedule_anchor", owner_key=self.owner_key(context), schedule_key=role_schedule_key(context))
        if raw is None:
            return None
        try:
            anchor = TaskContext.restore(json.loads(raw))
            if role_schedule_key(anchor) != role_schedule_key(context):
                raise ValueError("state schedule belongs to another role")
            return anchor
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_provider_data_invalid", "state schedule anchor is invalid") from error

    def change(self, *_args, **_kwargs):
        raise TaskError("life_state_write_unavailable", "state writes have not been transferred")
