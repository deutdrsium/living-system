"""Coordinate an isolated reading deletion with host source governance.

Related reading is cancelled with the deletion; the life scheduler receives a
stop signal for any affected worker before the host acknowledges the handoff.
"""

import hashlib
import json
from threading import get_ident

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, VersionRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.role_work.models import RoleActivityState, advance_activity

from . import activity_deletion_authority, life_commands


PREFIX = "life.activity.deletion.v1:"


class ActivityDeletionHandoff:
    def __init__(self, manager, provider):
        self.manager, self.provider, self.runtime = manager, provider, manager.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_activity_transaction_active", "reading deletion needs an independent host transaction")

    def prepare(self, operator, data, *, observed_at):
        self._outside_host_transaction()
        try:
            if type(data) is not dict or data.get("action") not in ("remove", "withdraw"):
                raise ValueError("not a removal")
            withdraw = data["action"] == "withdraw"
            identifier, revision, key = data["activity_id"], data["expected_revision"], data["operation_key"]
            if (type(identifier) is not str or not identifier or type(revision) is not int or revision < 1
                    or type(key) is not str or not key.strip() or len(key) > 128):
                raise ValueError("invalid removal identity")
            access = self.manager.authorize(operator)
            if data.get("view_token") != self.manager.token(operator, access, identifier, revision):
                raise TaskError("life_view_stale", "refresh before applying this change")
            context = self.manager.context
            owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
            operation_id = self.manager.operation_id("life-command-", operator, key)
            command_id = "activity-delete-" + digest([context.key, operation_id])
            original = self.provider.request_for_replay(command_id=command_id, owner_key=digest(owner), context_key=context.key)
            if original is not None:
                payload = original["payload"]
                if (original["expected_revision"] != revision or payload["activity_id"] != identifier
                        or payload["management_operation_id"] != operation_id
                        or payload["management_parameter_hash"] != digest(data)):
                    raise ValueError("management request conflicts with its original deletion")
                return original
            proposal = self.provider.describe(owner_key=digest(owner), activity_id=identifier, include_target=withdraw)
            activity = activity_from_json(proposal["previous_json"])
            related = []
            for raw in proposal["related_json"]:
                item = activity_from_json(raw)
                cancelled = advance_activity(item, expected_revision=item.revision,
                    state=RoleActivityState.CANCELLED, observed_at=observed_at)
                related.append({"activity_id": item.activity_id, "revision": item.revision,
                    "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "operation_id": operation_id + (":cancel" if withdraw and item.activity_id == identifier else ":" + digest(item.activity_id)),
                    "result_hash": hashlib.sha256(encode(cancelled).encode("utf-8")).hexdigest()})
            request = {"command_id": command_id,
                "owner_key": digest(owner), "context_key": context.key, "kind": "activity.delete",
                "expected_revision": revision,
                "payload": {"activity_id": identifier, "management_operation_id": operation_id,
                    "management_parameter_hash": digest(data), "rows_hash": proposal["rows_hash"],
                    "source_hash": digest(activity.work.source),
                    "observed_at": stamp(observed_at), "related": related,
                    "previous_hash": hashlib.sha256(proposal["previous_json"].encode("utf-8")).hexdigest()}}
            if withdraw:
                request["payload"]["mode"] = "withdraw"
            activity_deletion_authority.authorize(self.manager, operator, data, request, proposal, observed_at=observed_at)
            self.provider.prepare(request, prepared_at=observed_at)
            return request
        except TaskError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_activity_deletion_invalid", "reading removal cannot be prepared") from error

    def decide(self, operator, data, request, *, observed_at):
        self._outside_host_transaction()
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        if digest(data) != request["payload"]["management_parameter_hash"]:
            raise TaskError("life_activity_deletion_invalid", "management request changed")
        existing = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"],
                                        request_hash=request_hash)
        if existing is not None:
            return existing
        proposal = self.provider.proposal(request)
        activity = activity_from_json(proposal["previous_json"])
        operation_id = request["payload"]["management_operation_id"]
        operation = OperationRef(operation_id, "role.life.management", context.scope, VersionRef(operation_id, 1), operation_id)
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])

        def authorize(_context, _at):
            return activity_deletion_authority.authorize(self.manager, operator, data, request, proposal, observed_at=_at)

        def reserve_and_revoke(connection, _request):
            receipt = require(OperationRepository(self.runtime).reserve(operation,
                parameter_hash=request["payload"]["management_parameter_hash"], observed_at=observed_at))
            if receipt.replayed:
                raise TaskError("life_activity_deletion_unconfirmed", "management operation lacks its original decision")
            owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
            for item in request["payload"].get("related", []):
                related_operation = OperationRef(item["operation_id"], "role.life", context.scope,
                    VersionRef(item["operation_id"], 1), item["operation_id"])
                related_hash = digest([owner, item["activity_id"], item["revision"], RoleActivityState.CANCELLED, None])
                related_receipt = require(OperationRepository(self.runtime).reserve(related_operation,
                    parameter_hash=related_hash, observed_at=observed_at))
                if related_receipt.replayed:
                    raise TaskError("life_activity_deletion_unconfirmed", "related operation lacks its original decision")
            source = activity.work.source
            source_row = connection.execute("SELECT governance_revision FROM sources WHERE source_id=?",
                                            (source.source_id,)).fetchone() if source else None
            if request["payload"].get("mode") == "withdraw" and source_row is None:
                raise TaskError("life_activity_deletion_invalid", "reading source is unavailable")
            if source_row is not None:
                require(self.manager.app.role_journal.sources.revoke(source,
                    expected_governance_revision=source_row["governance_revision"], observed_at=observed_at))
            evidence = {"request_hash": request_hash, "operation_id": operation_id,
                        "parameter_hash": request["payload"]["management_parameter_hash"],
                        "scope_hash": digest(context.scope), "source": json.loads(encode(source)) if source else None}
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)",
                               (PREFIX + decision_id, encode(evidence)))

        return life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=observed_at,
            verify_prepared=self.provider.verify_prepared, authorize=authorize, apply=reserve_and_revoke)

    def _check(self, request, decision):
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        if decision.get("request_hash") != request_hash or not life_commands.verify_decision(self.runtime, decision):
            raise TaskError("life_activity_deletion_invalid", "reading removal decision changed")
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        try:
            record = json.loads(row["value"])
            if (set(record) != {"request_hash", "operation_id", "parameter_hash", "scope_hash", "source"}
                    or record["request_hash"] != request_hash or record["scope_hash"] != digest(context.scope)
                    or record["operation_id"] != request["payload"]["management_operation_id"]
                    or record["parameter_hash"] != request["payload"]["management_parameter_hash"]
                    or digest(record["source"]) != request["payload"]["source_hash"]):
                raise ValueError("removal evidence changed")
            source = record["source"]
            if source is not None:
                current = self.runtime.read_one("""SELECT state FROM sources WHERE source_id=? AND lineage_id=?
                    AND source_revision=?""", (source["source_id"], source["version"]["lineage_id"], source["version"]["revision"]))
                if current is not None and current["state"] != "revoked":
                    raise ValueError("source withdrawal is not durable")
            receipt = OperationRepository(self.runtime).get(record["operation_id"])
            if (not receipt.is_success or receipt.value.operation.owner != "role.life.management"
                    or receipt.value.operation.scope != context.scope or receipt.value.parameter_hash != record["parameter_hash"]
                    or receipt.value.commit_state is not CommitState.COMMITTED
                    or receipt.value.operation.version != VersionRef(record["operation_id"], 1)
                    or receipt.value.operation.idempotency_key != record["operation_id"]
                    or receipt.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                raise ValueError("management receipt changed")
            owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
            for item in request["payload"].get("related", []):
                receipt = OperationRepository(self.runtime).get(item["operation_id"])
                parameters = digest([owner, item["activity_id"], item["revision"], RoleActivityState.CANCELLED, None])
                if (not receipt.is_success or receipt.value.operation.owner != "role.life"
                        or receipt.value.operation.scope != context.scope or receipt.value.parameter_hash != parameters
                        or receipt.value.commit_state is not CommitState.COMMITTED
                        or receipt.value.operation.version != VersionRef(item["operation_id"], 1)
                        or receipt.value.operation.idempotency_key != item["operation_id"]
                        or receipt.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                    raise ValueError("related reading receipt changed")
            return record
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_activity_deletion_unconfirmed", "reading removal host evidence is unavailable") from error

    def apply(self, request, decision):
        self._outside_host_transaction()
        self._check(request, decision)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def abort(self, request, *, observed_at):
        """Release a preparation only if the host never decided to commit it."""
        self._outside_host_transaction()
        decision = life_commands.decide(self.runtime, context=self.manager.context, request=request, outcome="abort",
            observed_at=observed_at, verify_prepared=self.provider.verify_prepared,
            authorize=lambda *_: None, apply=lambda *_: None)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def complete(self, request, decision, *, observed_at):
        self._outside_host_transaction()
        record = self._check(request, decision)
        def finalize(_connection, _decision):
            self._check(request, decision)
            for item in request["payload"].get("related", []):
                require(OperationRepository(self.runtime).transition(item["operation_id"], expected_state=OperationState.ACCEPTED,
                    next_state=OperationState.COMPLETED, observed_at=observed_at))
            require(OperationRepository(self.runtime).transition(record["operation_id"], expected_state=OperationState.ACCEPTED,
                                                                  next_state=OperationState.COMPLETED, observed_at=observed_at))
        return life_commands.complete(self.runtime, decision, observed_at=observed_at,
                                      verify_life_commit=self.provider.verify_commit, finalize=finalize)

    def acknowledge(self, request, decision):
        self._outside_host_transaction()
        record = self._check(request, decision)
        def verify(evidence):
            self._check(request, decision)
            receipt = OperationRepository(self.runtime).get(record["operation_id"])
            related = [OperationRepository(self.runtime).get(item["operation_id"]) for item in request["payload"].get("related", [])]
            return (life_commands.verify_completion(self.runtime, evidence) and receipt.is_success
                    and receipt.value.state is OperationState.COMPLETED and receipt.value.parameter_hash == record["parameter_hash"]
                    and all(item.is_success and item.value.state is OperationState.COMPLETED for item in related))
        return self.provider.acknowledge(decision["command_id"], verify_host_completion=verify)

    def stop_workers(self, request, decision):
        self._check(request, decision)
        return self.provider.stop_cancelled_readings(command_id=request["command_id"], owner_key=request["owner_key"],
            context_key=request["context_key"], verify_host_completion=lambda evidence: life_commands.verify_completion(self.runtime, evidence))

    async def recover(self, request):
        """Finish an admitted removal or abort a preparation with no host decision."""
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
            result = self.apply(request, decision)
            self.complete(request, decision, observed_at=self.manager.app.now())
            self.stop_workers(request, decision)
            self.acknowledge(request, decision)
        self.manager.app.notify()
        return result
