"""Authorize and audit configuration writes owned by the life plugin."""

from contextlib import nullcontext
from datetime import datetime
import json
from types import SimpleNamespace

from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.integration import life_commands
from companion_v2.storage.codec import encode_timestamp
from companion_v2.storage.models import CommitState, OperationState

from . import local_journal
from .life_provider import LIFE_FIELDS, LifeConfigurationStore, transferred
from .life_write_plan import build_plan
from .models import ConfigLayer, ConfigTarget, ConfigurationError, encode, fingerprint


EFFECT_PREFIX = "life.configuration.handoff.v1:"
APPLIED_PREFIX = "life.configuration.effects.v1:"


def _required(outcome):
    if outcome.failure is not None:
        raise ConfigurationError(outcome.failure.code, outcome.failure.reason)
    return outcome.unwrap()


def _subject(value):
    if value is None:
        return None
    return {"installation_id": value.installation_id, "kind": value.kind.value, "subject_id": value.subject_id}


def _restore_subject(value):
    return None if value is None else SubjectRef(value["installation_id"], SubjectKind(value["kind"]), value["subject_id"])


def _target(value):
    return {"installation_id": value.installation_id, "layer": value.layer.value, "role": _subject(value.role),
            "scope": value.scope.document() if value.scope else None, "bot_id": value.bot_id, "subject": _subject(value.subject)}


def _restore_target(value):
    return ConfigTarget(value["installation_id"], ConfigLayer(value["layer"]), _restore_subject(value["role"]),
                        ScopeRef.restore(value["scope"]) if value["scope"] else None, value["bot_id"],
                        _restore_subject(value["subject"]))


def _context(identity, key):
    scope = ScopeRef(identity["installation_id"], "companion", ScopeKind.SYSTEM,
                     "configuration.handoff", bot_id=identity["bot_id"])
    role = SubjectRef(identity["installation_id"], SubjectKind.BOT, identity["role_id"])
    return SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), key=key)


class LifeConfigurationHandoff:
    def __init__(self, service):
        self.service, self.store, self.runtime = service, service.store, service.runtime
        self.lease = getattr(self.store, "_life_configuration_lease", None)
        if self.lease is None:
            raise TaskError("life_provider_unavailable", "life configuration provider is unavailable")

    def _call(self, name, *args, **kwargs):
        return self.lease.dispatch("configuration_command", name, *args, **kwargs)

    def _state_effect(self, owner, target, fields, changes, reset, observed_at):
        participant, proposed, changed = owner.prepare_write(target, fields, changes=changes, reset=reset,
                                                             observed_at=observed_at)
        return {"target": _target(target), "participant": _subject(participant),
                "proposed": proposed, "changed": sorted(changed)}

    def _effect(self, request, decision):
        row = self.store.read_one("SELECT value FROM runtime_metadata WHERE key=?", (EFFECT_PREFIX + decision["decision_id"],))
        if row is None or not life_commands.verify_decision(self.store, decision):
            raise TaskError("life_configuration_unconfirmed", "configuration decision is unavailable")
        try:
            effect = json.loads(row["value"])
            if (effect["request_hash"] != decision["request_hash"]
                    or effect["operation_id"] != request["payload"]["operation_id"]
                    or effect["parameter_hash"] != request["payload"]["parameter_hash"]):
                raise ValueError("configuration effect differs")
            return effect
        except (KeyError, TypeError, ValueError) as error:
            raise TaskError("life_configuration_unconfirmed", "configuration decision is invalid") from error

    def _apply_state_effect(self, effect, decision_id, observed_at):
        state = effect["state_effect"]
        if state is None:
            return
        expected = digest(state)
        with self.runtime.transaction() as connection:
            row = connection.execute("SELECT value FROM runtime_metadata WHERE key=?", (APPLIED_PREFIX + decision_id,)).fetchone()
            if row is not None:
                if row["value"] != expected:
                    raise TaskError("life_configuration_conflict", "configuration effect changed")
                return
            owner = self.service._owners["role.state.settings"]
            owner.apply_write_effects(_restore_target(state["target"]), participant=_restore_subject(state["participant"]),
                proposed=state["proposed"], changed=set(state["changed"]), observed_at=observed_at)
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (APPLIED_PREFIX + decision_id, expected))

    def _finish(self, request, decision):
        effect = self._effect(request, decision)
        result = self._call("resolve", decision, verify_host_decision=lambda value: life_commands.verify_decision(self.store, value))
        if result["state"] not in ("committed", "acknowledged"):
            raise TaskError("life_configuration_unconfirmed", "life configuration was not committed")
        observed_at = datetime.fromisoformat(request["payload"]["observed_at"])

        def finalize(connection, _decision):
            self._effect(request, decision)
            plan = effect["plan"]
            timestamp = encode_timestamp(observed_at)
            for target in plan["host_targets"]:
                connection.execute("INSERT OR IGNORE INTO configuration_targets VALUES (?,?,0,?)",
                                   (target["key"], encode(target["document"]), timestamp))
                revision = connection.execute("SELECT revision FROM configuration_targets WHERE target_key=?", (target["key"],)).fetchone()[0]
                for item in target["changes"]:
                    previous = connection.execute("SELECT value_json FROM configuration_values WHERE target_key=? AND field_id=?",
                                                  (target["key"], item["field_id"])).fetchone()
                    if (previous[0] if previous else None) != item["previous_json"]:
                        raise TaskError("life_configuration_conflict", "host configuration changed during handoff")
                    if item["value_json"] is None:
                        connection.execute("DELETE FROM configuration_values WHERE target_key=? AND field_id=?",
                                           (target["key"], item["field_id"]))
                    else:
                        connection.execute("""INSERT INTO configuration_values VALUES (?,?,?,?)
                            ON CONFLICT(target_key,field_id) DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision""",
                            (target["key"], item["field_id"], item["value_json"], revision + 1))
                connection.execute("UPDATE configuration_targets SET revision=?,updated_at=? WHERE target_key=?",
                                   (revision + 1, timestamp, target["key"]))
            self._apply_state_effect(effect, decision["decision_id"], observed_at)
            connection.execute("INSERT OR IGNORE INTO configuration_targets VALUES (?,?,0,?)",
                               (effect["target_key"], encode(effect["target_document"]), timestamp))
            revisions = dict(plan["life_revisions"])
            for target in plan["life_targets"]:
                if target["key"] in revisions and target["changes"]:
                    revisions[target["key"]] += 1
            revision = sum(revisions.values())
            for key in plan["host_revision_keys"]:
                row = connection.execute("SELECT revision FROM configuration_targets WHERE target_key=?", (key,)).fetchone()
                revision += row[0] if row else 0
            receipt = {"operation_id": effect["operation_id"], "target_key": effect["target_key"],
                       "revision": revision, "changed_fields": plan["changed_fields"]}
            connection.execute("""INSERT INTO configuration_changes(operation_id,target_key,group_id,changed,result_json,created_at)
                VALUES (?,?,?,?,?,?)""", (effect["operation_id"], effect["target_key"], effect["group_id"],
                int(bool(plan["changed_fields"])), encode(receipt), timestamp))
            if (undo := effect.get("local_undo")) is not None:
                if plan["changed_fields"]:
                    local_journal.append(connection, target_key=effect["target_key"], revision=revision,
                                         changed_at=observed_at.isoformat(), **undo)
                # A pre-finalization recovery needs the before-image. After the
                # receipt commits, keep it only in the bounded undo journal.
                del effect["local_undo"]
                connection.execute("UPDATE runtime_metadata SET value=? WHERE key=?",
                                   (encode(effect), EFFECT_PREFIX + decision["decision_id"]))
            _required(self.service.operations.transition(effect["operation_id"], expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=observed_at))

        # State finalization also writes the runtime DB. Match the Dashboard's
        # runtime-before-config lock order, including when recovering a decision.
        # Take only the lock here so the state effect keeps its existing commit
        # boundary and is durable before the configuration receipt completes.
        runtime_lock = self.runtime._lock if effect["state_effect"] is not None else nullcontext()
        with runtime_lock:
            life_commands.complete(self.store, decision, observed_at=observed_at,
                                   verify_life_commit=lambda value: self._call("verify_commit", value), finalize=finalize)
        self._call("acknowledge", request["command_id"], verify_host_completion=lambda evidence:
                   life_commands.verify_completion(self.store, evidence))
        row = self.store.read_one("SELECT result_json FROM configuration_changes WHERE operation_id=?", (effect["operation_id"],))
        if row is None:
            raise TaskError("life_configuration_unconfirmed", "configuration receipt is unavailable")
        return json.loads(row["result_json"])

    def recover(self, request):
        if (type(request) is not dict or request.get("kind") != "configuration.patch"
                or not str(request.get("command_id", "")).startswith("life-config-")):
            raise TaskError("life_configuration_invalid", "invalid configuration recovery request")
        context = _context(request["payload"]["identity"], request["context_key"])
        _, request_hash = life_commands._request(context, request)
        decision = life_commands.lookup(self.store, context_key=context.key, command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            decision = life_commands.decide(self.store, context=context, request=request, outcome="abort",
                observed_at=datetime.fromisoformat(request["payload"]["observed_at"]),
                verify_prepared=lambda value: self._call("verify_prepared", value), authorize=lambda *_: None, apply=lambda *_: None)
        if decision["decision"] == "abort":
            self._call("resolve", decision, verify_host_decision=lambda value: life_commands.verify_decision(self.store, value))
            return {"state": "aborted"}
        return {"state": "completed", "receipt": self._finish(request, decision)}

    def patch(self, actor, *, target, group_id, operation, expected_edit_token, changes, reset_to_inherit, observed_at):
        return self._patch(actor, target=target, group_id=group_id, operation=operation,
                           expected_edit_token=expected_edit_token, changes=changes,
                           reset_to_inherit=reset_to_inherit, observed_at=observed_at)

    def patch_local(self, actor, *, target, group_id, operation, expected_edit_token, changes, reset_to_inherit,
                    observed_at, expected_revision, change_id, reverts, check_generation):
        """Fence a local operator edit at the normal handoff decision and journal it on recovery."""
        names = tuple(changes) + reset_to_inherit
        if len(names) != 1 or operation.operation_id != "companion-config:" + change_id:
            raise ConfigurationError("invalid_configuration_patch", "local edit requires one field and an operation ID")
        return self._patch(actor, target=target, group_id=group_id, operation=operation,
                           expected_edit_token=expected_edit_token, changes=changes,
                           reset_to_inherit=reset_to_inherit, observed_at=observed_at,
                           local_edit=(expected_revision, change_id, reverts, names[0], check_generation))

    def _local_undo(self, local_edit, target, domain, owner, fields, revision):
        expected_revision, change_id, reverts, name, check_generation = local_edit
        _required(check_generation())
        if revision != expected_revision:
            raise ConfigurationError("stale_revision", "configuration target changed before the decision")
        if domain == "state" or name.rsplit(".", 1)[-1] in LIFE_FIELDS:
            provider = LifeConfigurationStore(self.store, domain, target, owner.role_target(target),
                                              tuple(field for field in fields if field.field_id == name))
            rows = provider.read_all("SELECT field_id, value_json, revision FROM configuration_values",
                                     (target.key, name))
            previous = rows[0]["value_json"] if rows else None
        else:
            previous = self.store.read_one("SELECT value_json FROM configuration_values WHERE target_key=? AND field_id=?",
                                           (target.key, name))
            previous = previous[0] if previous else None
        return dict(change_id=change_id, reverts=reverts, field_id=name, old_json=local_journal.old_value(previous))

    def _patch(self, actor, *, target, group_id, operation, expected_edit_token, changes, reset_to_inherit, observed_at,
               local_edit=None):
        service = self.service
        domain = "life" if group_id == "role.life" else "state"
        with self.store.read_snapshot():
            snapshot = service._snapshot(actor, target, group_id, observed_at, "configuration.write")
            fields = service.registry.group(group_id)
            owner = service._owners[fields[0].owner_id]
            if operation.scope != target.authorization_scope:
                raise ConfigurationError("operation_scope_mismatch", "operation is not owned by this configuration target")
            if operation.owner != owner.owner_id:
                existing = service.operations.get(operation.operation_id)
                if (operation.owner not in tuple(getattr(owner, "legacy_owner_ids", ())) or not existing.is_success
                        or existing.value.operation != operation):
                    raise ConfigurationError("operation_scope_mismatch", "operation is not owned by this configuration target")
            normalized = service._normalize_patch(target, fields, changes, reset_to_inherit)
            parameter_hash = fingerprint({"target": target.document(), "group": group_id, "schema": service.registry.fingerprint,
                "actor": [actor.principal.host_id, actor.principal.platform, actor.principal.native_account_id,
                          actor.binding_id, actor.binding_revision],
                "set": normalized, "reset": sorted(reset_to_inherit), "expected": expected_edit_token})
            identity = {"installation_id": target.installation_id, "bot_id": target.bot_id or "configuration-default",
                        "role_id": target.role.subject_id}
            context = _context(identity, target.key)
            owner_key = digest(RoleOwnerRef(identity["installation_id"], identity["bot_id"], identity["role_id"]))
            command_id = "life-config-" + fingerprint([target.key, operation.operation_id])
            prior = self._call("request_for_replay", command_id=command_id, owner_key=owner_key, context_key=target.key)
            if prior is not None:
                if (prior["payload"]["operation_id"] != operation.operation_id
                        or prior["payload"]["parameter_hash"] != parameter_hash):
                    raise ConfigurationError("idempotency_key_conflict", "configuration operation was already used")
                # Recovery writes; perform it after leaving the host read snapshot.
                plan = state_effect = None
            else:
                existing = service.operations.get(operation.operation_id)
                if existing.is_success:
                    value = existing.value
                    if value.operation != operation or value.parameter_hash != parameter_hash:
                        raise ConfigurationError("idempotency_key_conflict", "configuration operation was already used")
                    row = self.store.read_one("SELECT result_json FROM configuration_changes WHERE operation_id=?", (operation.operation_id,))
                    if row is None or value.state is not OperationState.COMPLETED or value.commit_state is not CommitState.COMMITTED:
                        raise ConfigurationError("configuration_operation_incomplete", "original configuration result is unavailable")
                    return Outcome.success(service._receipt(json.loads(row["result_json"]), replayed=True))
                if snapshot.edit_token != expected_edit_token:
                    raise ConfigurationError("configuration_revision_conflict", "configuration, inheritance, binding, or policy changed")
                state_effect = (self._state_effect(owner, target, fields, normalized, reset_to_inherit, observed_at)
                                if domain == "state" else None)
                plan = build_plan(owner, domain=domain, target=target, fields=fields, changes=normalized,
                                  reset=reset_to_inherit, observed_at=observed_at)
        if prior is not None:
            recovered = self.recover(prior)
            if recovered["state"] != "completed":
                raise ConfigurationError("configuration_operation_incomplete", "original configuration operation was interrupted")
            return Outcome.success(service._receipt(recovered["receipt"], replayed=True))
        if local_edit is not None:
            name = local_edit[3]
            for item in (*plan["life_targets"], *plan["host_targets"]):
                if any(item["key"] != target.key or field["field_id"] != name for field in item["changes"]):
                    raise ConfigurationError("edit_affects_other_fields", "the edit affects another field or target")
            # A normal handoff reserves the edited rows. A CLI compare-and-set
            # also pins the other provider row counted by its numeric revision.
            reserved_keys = {item["key"] for item in plan["life_targets"]}
            for layer in {target, owner.role_target(target)}:
                if layer.key not in reserved_keys:
                    plan["life_targets"].append({"key": layer.key, "document": layer.document(),
                        "revision": plan["life_revisions"][layer.key], "changes": []})
            plan["life_targets"].sort(key=lambda item: item["key"])
        request = {"command_id": command_id, "owner_key": owner_key, "context_key": target.key,
                   "kind": "configuration.patch", "expected_revision": 0,
                   "payload": {"domain": domain, "targets": plan["life_targets"], "identity": identity,
                       "operation_id": operation.operation_id, "parameter_hash": parameter_hash,
                       "observed_at": observed_at.isoformat()}}
        self._call("prepare", request, prepared_at=observed_at)
        _, request_hash = life_commands._request(context, request)
        decision_id = "life-decision-" + digest([context.key, command_id])

        undo = None

        def authorize(_context, at):
            nonlocal undo
            if not transferred(self.store):
                raise ConfigurationError("life_configuration_owner_changed", "configuration ownership changed")
            current = service._snapshot(actor, target, group_id, at, "configuration.write")
            if local_edit is not None:
                undo = self._local_undo(local_edit, target, domain, owner, fields, current.revision)
            if current.edit_token != expected_edit_token:
                raise ConfigurationError("configuration_revision_conflict", "configuration changed before authorization")
            if domain == "state" and self._state_effect(owner, target, fields, normalized, reset_to_inherit, at) != state_effect:
                raise ConfigurationError("configuration_revision_conflict", "state target changed before authorization")
            return Outcome.success(True)

        def apply(connection, _request):
            reserved = _required(service.operations.reserve(operation, parameter_hash=parameter_hash, observed_at=observed_at))
            if reserved.replayed:
                raise ConfigurationError("configuration_operation_incomplete", "configuration operation has no matching decision")
            effect = {"request_hash": request_hash, "operation_id": operation.operation_id, "parameter_hash": parameter_hash,
                "target_key": target.key, "target_document": target.document(), "group_id": group_id,
                "plan": plan, "state_effect": state_effect}
            if undo is not None:
                effect["local_undo"] = undo
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (EFFECT_PREFIX + decision_id, encode(effect)))

        try:
            decision = life_commands.decide(self.store, context=context, request=request, outcome="commit", observed_at=observed_at,
                verify_prepared=lambda value: self._call("verify_prepared", value), authorize=authorize, apply=apply)
            return Outcome.success(service._receipt(self._finish(request, decision), replayed=False))
        except Exception:
            if life_commands.lookup(self.store, context_key=context.key, command_id=command_id, request_hash=request_hash) is None:
                try:
                    self.recover(request)
                except Exception:
                    pass
            raise
