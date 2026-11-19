"""Publish the original source and create its reading queue across the two stores."""

from datetime import datetime
import json
from threading import get_ident

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, SourceRef, VersionRef
from companion_v2.role_work.life.configuration import GROUP, target_for, values
from companion_v2.role_work.life.epub_import import MAX_EPUB_TEXT
from companion_v2.role_work.life.reading_queue import queue_key
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleArtifact, RoleArtifactJournalMeaning, RoleArtifactKind
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import life_commands


PREFIX = "life.activity.reading-import.v1:"


class ReadingImportHandoff:
    def __init__(self, manager, provider):
        self.manager, self.provider, self.runtime = manager, provider, manager.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_activity_transaction_active", "reading import needs an independent host transaction")

    def _identity(self, operator, data):
        if type(data) is not dict or data.get("action") not in ("add_text", "add_epub"):
            raise ValueError("not a reading import")
        key, identifier, revision = data["operation_key"], data.get("activity_id", "life"), data.get("expected_revision", 0)
        if (type(key) is not str or not key.strip() or len(key) > 128
                or type(identifier) is not str or not identifier or type(revision) is not int or revision < 0):
            raise ValueError("invalid reading import identity")
        access = self.manager.authorize(operator)
        if data.get("view_token") != self.manager.token(operator, access, identifier, revision):
            raise TaskError("life_view_stale", "refresh before applying this change")
        operation_id = self.manager.operation_id("life-command-", operator, key)
        return operation_id, "activity-reading-import-" + digest([self.manager.context.key, operation_id])

    @staticmethod
    def _content(data, epub):
        content = epub if data["action"] == "add_epub" else data
        text, title, author = content["text"], content["title"], content.get("author", "")
        maximum = MAX_EPUB_TEXT if data["action"] == "add_epub" else 262144
        if (type(text) is not str or not text.strip() or len(text) > maximum or type(title) is not str
                or not title.strip() or len(title) > 160 or not isinstance(author, str) or len(author) > 120):
            raise ValueError("provide a title and valid original text")
        return text, f"{title}（{author}）" if author.strip() else title

    def _expected(self, operation_id, text, title, at):
        context, owner = self.manager.context, self.manager.owner
        source = SourceRef("book-" + digest(operation_id), GROUP, context.scope, VersionRef(operation_id, 1), at)
        start = 0
        while start < len(text):
            end = min(start + 6000, len(text))
            if text[start:end].strip():
                break
            start = end
        else:
            raise ValueError("original text has no readable span")
        identifier = "book-part-" + digest([source.version.lineage_id, start])
        work = RoleWorkSpec("reading", context.key, identifier, source, digest(text), start, end)
        activity = RoleActivity(identifier, owner.persona_id, context.scope, at, RoleActivityState.PLANNED,
            description=f"{title} · {start + 1}–{end} / {len(text)} 字符", work=work)
        artifact = RoleArtifact("book-" + digest(operation_id), identifier, RoleArtifactKind.TEXT, source, at,
                                owner=owner, journal_meaning=RoleArtifactJournalMeaning.READING)
        metadata = {"artifact": json.loads(encode(artifact)), "queue_key": queue_key(context.key, source), "text_length": len(text)}
        return source, json.loads(encode(activity)), metadata

    def prepare(self, operator, data, epub, *, observed_at):
        self._outside_host_transaction()
        operation_id, command_id = self._identity(operator, data)
        owner, context = self.manager.owner, self.manager.context
        original = self.provider.request_for_replay(command_id=command_id, owner_key=digest(owner), context_key=context.key)
        if original is not None:
            if original["payload"].get("management") != {"operation_id": operation_id, "parameter_hash": digest(data)}:
                raise TaskError("life_activity_creation_invalid", "reading import conflicts with its original request")
            return original
        text, title = self._content(data, epub)
        source, activity, metadata = self._expected(operation_id, text, title, observed_at)
        proposal = self.provider.propose_reading_import(owner=json.loads(encode(owner)), scope=json.loads(encode(context.scope)),
            context_key=context.key, operation_id=operation_id, source=json.loads(encode(source)), text=text,
            title=title, observed_at=stamp(observed_at))
        if proposal != {"activity": activity, "reading_import": metadata}:
            raise TaskError("life_activity_creation_invalid", "reading import proposal changed")
        request = {"command_id": command_id, "owner_key": digest(owner), "context_key": context.key,
            "kind": "activity.create", "expected_revision": 0,
            "payload": {"owner": json.loads(encode(owner)), "activity": activity, "reading_import": metadata,
                        "management": {"operation_id": operation_id, "parameter_hash": digest(data)}}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def decide(self, operator, data, epub, request, *, observed_at):
        self._outside_host_transaction()
        context, app = self.manager.context, self.manager.app
        _, request_hash = life_commands._request(context, request)
        existing = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"], request_hash=request_hash)
        if existing is not None:
            self._check(request, existing)
            return existing
        operation_id, command_id = self._identity(operator, data)
        text, title = self._content(data, epub)
        at = datetime.fromisoformat(request["payload"]["activity"]["started_at"])
        source, activity, metadata = self._expected(operation_id, text, title, at)
        if (request["command_id"] != command_id or request["payload"] != {"owner": json.loads(encode(self.manager.owner)),
                "activity": activity, "reading_import": metadata,
                "management": {"operation_id": operation_id, "parameter_hash": digest(data)}}):
            raise TaskError("life_activity_creation_invalid", "reading import identity changed")
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])

        def authorize(_context, _at):
            self._identity(operator, data)
            return Outcome.success(True)

        def apply(connection, _request):
            receipt = require(OperationRepository(self.runtime).reserve(OperationRef(operation_id, "role.life.management", context.scope,
                VersionRef(operation_id, 1), operation_id), parameter_hash=digest(data), observed_at=observed_at))
            if receipt.replayed:
                raise TaskError("life_activity_creation_unconfirmed", "reading import lacks its original decision")
            require(app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=observed_at))
            evidence = {"request_hash": request_hash, "operation_id": operation_id,
                        "parameter_hash": digest(data), "scope_hash": digest(context.scope), "source_hash": digest(text)}
            connection.execute("INSERT INTO runtime_metadata VALUES (?,?)", (PREFIX + decision_id, encode(evidence)))

        with app.role_life.consistent_snapshot():
            return life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=observed_at,
                verify_prepared=self.provider.verify_prepared, authorize=authorize, apply=apply)

    def _check(self, request, decision):
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        if (decision.get("decision") != "commit" or decision.get("request_hash") != request_hash
                or not life_commands.verify_decision(self.runtime, decision)):
            raise TaskError("life_activity_creation_invalid", "reading import decision changed")
        metadata, work = request["payload"]["management"], request["payload"]["activity"]["work"]
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        expected = {"request_hash": request_hash, "operation_id": metadata["operation_id"],
                    "parameter_hash": metadata["parameter_hash"], "scope_hash": digest(context.scope), "source_hash": work["content_hash"]}
        if row is None or json.loads(row["value"]) != expected:
            raise TaskError("life_activity_creation_unconfirmed", "reading import evidence is unavailable")
        receipt = require(OperationRepository(self.runtime).get(metadata["operation_id"]))
        source = self.runtime.read_one("SELECT content_hash FROM sources WHERE source_id=? AND lineage_id=? AND source_revision=?",
            (work["source"]["source_id"], work["source"]["version"]["lineage_id"], work["source"]["version"]["revision"]))
        if (receipt.operation.owner != "role.life.management" or receipt.operation.scope != context.scope
                or receipt.parameter_hash != metadata["parameter_hash"] or receipt.commit_state is not CommitState.COMMITTED
                or receipt.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)
                or source is None or source["content_hash"] != work["content_hash"]):
            raise TaskError("life_activity_creation_unconfirmed", "reading import source or receipt changed")
        return metadata["operation_id"]

    def abort(self, request, *, observed_at):
        decision = life_commands.decide(self.runtime, context=self.manager.context, request=request, outcome="abort",
            observed_at=observed_at, verify_prepared=self.provider.verify_prepared, authorize=lambda *_: None, apply=lambda *_: None)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def finish(self, request, decision, *, observed_at):
        operation_id = self._check(request, decision)
        result = self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        def finalize(_connection, _decision):
            self._check(request, decision)
            require(OperationRepository(self.runtime).transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=observed_at))
        life_commands.complete(self.runtime, decision, observed_at=observed_at,
                               verify_life_commit=self.provider.verify_commit, finalize=finalize)
        self.provider.acknowledge(decision["command_id"], verify_host_completion=lambda value:
            life_commands.verify_completion(self.runtime, value) and
            require(OperationRepository(self.runtime).get(operation_id)).state is OperationState.COMPLETED)
        self.manager.app.notify()
        return result

    def execute(self, operator, data, epub, *, observed_at):
        request = self.prepare(operator, data, epub, observed_at=observed_at)
        try:
            decision = self.decide(operator, data, epub, request, observed_at=observed_at)
        except (TaskError, ValueError):
            _, request_hash = life_commands._request(self.manager.context, request)
            if life_commands.lookup(self.runtime, context_key=self.manager.context.key,
                                    command_id=request["command_id"], request_hash=request_hash) is None:
                self.abort(request, observed_at=observed_at)
            raise
        result = self.finish(request, decision, observed_at=observed_at)
        text, _title = self._content(data, epub)
        snapshot = require(self.manager.app.configuration.read(self.manager.context.actor, target=target_for(self.manager.context),
            group_id=GROUP, observed_at=observed_at))
        size = values(snapshot)["reading_chunk_chars"]
        response = {"operation_id": request["payload"]["management"]["operation_id"],
                    "activity_ids": [request["payload"]["activity"]["activity_id"]],
                    "total_segments": sum(bool(text[start:start + size].strip()) for start in range(0, len(text), size))}
        if data["action"] == "add_epub":
            response["title"] = epub["title"]
        if result["state"] == "acknowledged":
            response.update(state="completed", replayed=True)
        return response

    async def recover(self, request):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.manager.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.manager.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            self.abort(request, observed_at=self.manager.app.now())
            return {"state": "aborted"}
        return self.finish(request, decision, observed_at=self.manager.app.now())
