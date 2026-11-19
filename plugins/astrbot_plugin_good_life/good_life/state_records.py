"""Read the imported role-state records for the authorized companion host."""

import json

from .storage import StorageReader


class StateTransitionPending(Exception):
    code = "life_state_pending"


class StagedStateRecords:
    def __init__(self, storage):
        self.reader = StorageReader(storage)

    def dispatch(self, operation, **request):
        if operation not in ("get", "get_raw", "current_raw", "version", "receipt", "shared_source", "history_at", "dialogue_cursor", "schedule_status", "schedule_anchor", "maintenance_instruction", "reply_blocks", "compute"):
            raise ValueError("unknown state read operation")
        with self.reader.locked():
            journal = self.reader.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'", ())
            if journal and self.reader.one("""SELECT 1 FROM life_prepared_commands WHERE kind='state.change' AND state='committed'
                AND (json_extract(payload_json,'$.storage_key')=? OR json_extract(payload_json,'$.legacy_key')=?) LIMIT 1""",
                (request.get("owner_key"), request.get("owner_key"))):
                raise StateTransitionPending("state change awaits host confirmation")
            return getattr(self, operation)(**request)

    def maintenance_instruction(self, *, owner_key, settings, mood_round, has_growth_refs, skills_partial, intervention):
        from .state_text import maintenance_instruction
        return maintenance_instruction(settings, mood_round, has_growth_refs=has_growth_refs,
                                       skills_partial=skills_partial, intervention=intervention)

    def reply_blocks(self, *, owner_key, simulation, intensity):
        from .state_text import reply_blocks
        return reply_blocks(simulation, intensity)

    def compute(self, *, owner_key, domain, function, args, options):
        from . import state_mood, state_cycle, state_cycle_feelings
        permitted = {"mood": (state_mood, {"validate", "previous_axes", "axes", "validate_update", "document", "project", "baseline_of", "bounds"}),
                     "cycle": (state_cycle, {"validate_assertion", "validate_alignment", "evolve", "project"}),
                     "feelings": (state_cycle_feelings, {"update"})}
        if (domain not in permitted or function not in permitted[domain][1]
                or type(args) not in (tuple, list) or type(options) is not dict):
            raise ValueError("unknown state algorithm")
        return getattr(permitted[domain][0], function)(*args, **options)

    def current_raw(self, *, owner_key, legacy_key):
        return (self.get_raw(owner_key=owner_key, identifier="current") or
                self.get_raw(owner_key=legacy_key, identifier="current"))

    def get(self, *, owner_key, identifier):
        raw = self.get_raw(owner_key=owner_key, identifier=identifier)
        return json.loads(raw) if raw is not None else None

    def get_raw(self, *, owner_key, identifier):
        row = self.reader.one("SELECT record_json FROM staged_role_state_records WHERE owner_key=? AND record_id=?",
                              (owner_key, identifier))
        return row[0] if row else None

    def version(self, *, owner_key, identifier, revision):
        if type(revision) is not int or revision < 1:
            raise ValueError("state revision is invalid")
        row = self.reader.one("""SELECT record_json FROM staged_role_state_history
            WHERE owner_key=? AND record_id=? AND revision=?""", (owner_key, identifier, revision))
        if row is None:
            raise KeyError((identifier, revision))
        return json.loads(row[0])

    def receipt(self, *, owner_key, operation_id):
        row = self.reader.one("SELECT result_json FROM staged_role_state_receipts WHERE owner_key=? AND operation_id=?",
                              (owner_key, operation_id))
        if row is None:
            raise KeyError(operation_id)
        return json.loads(row[0])

    def shared_source(self, *, owner_key, source_id):
        row = self.reader.one("""SELECT record_json FROM staged_role_state_history
            WHERE owner_key=? AND record_id='current'
            AND (json_extract(record_json,'$.sources[0].source_id')=?
                 OR json_extract(record_json,'$.cycle_sources[0].source_id')=?)
            ORDER BY revision DESC LIMIT 1""", (owner_key, source_id, source_id))
        return json.loads(row[0]) if row else None

    def history_at(self, *, owner_key, identifier, at):
        row = self.reader.one("""SELECT record_json FROM staged_role_state_history
            WHERE owner_key=? AND record_id=? AND json_extract(record_json,'$.updated_at')<=?
            ORDER BY revision DESC LIMIT 1""", (owner_key, identifier, at))
        return json.loads(row[0]) if row else None

    def dialogue_cursor(self, *, owner_key, context_key):
        row = self.reader.one("SELECT value FROM staged_state_metadata WHERE key=?",
                              ("role.state.dialogue-cursor.v1:" + context_key,))
        return json.loads(row[0]) if row else None

    def schedule_status(self, *, owner_key, schedule_key):
        row = self.reader.one("SELECT next_at,enabled,result_json FROM staged_role_state_schedules WHERE context_key=?",
                              (schedule_key,))
        return dict(row) if row else None

    def schedule_anchor(self, *, owner_key, schedule_key):
        row = self.reader.one("SELECT context_json FROM staged_role_state_schedules WHERE context_key=?", (schedule_key,))
        return row[0] if row else None
