"""Core-only, versioned management of one original private role and participant."""

import asyncio
from dataclasses import replace
from datetime import date
import json
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, ScopeKind, SourceRef, VersionRef
from companion_v2.identity_policy.models import AuthorizationTier, GrantState
from companion_v2.identity_policy.object_access import ObjectAccessPolicy
from companion_v2.sources.models import SourceRecord
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.role_work.models import (RoleActivityState as State, RoleArtifact,
    RoleArtifactKind, RoleArtifactJournalMeaning)
from companion_v2.role_work.journal import codec
from companion_v2.role_work.journal.models import (DreamRecall, JournalContext, JournalDraft, JournalKey,
    JournalKind, JournalOrigin, JournalState)

from .configuration import DEFAULTS, GROUP, OWNER as CONFIGURATION_OWNER, target_for, values
from .epub_import import MAX_EPUB_TEXT, parse_epub_upload
from .reading_queue import queue_key, queue_next
from companion_v2.role_work.interests import scope_key, visible_interests
from companion_v2.role_profile.context import ConversationRoleProfileContext


class RoleLifeManagement:
    def __init__(self, app, context):
        self.app, self.context, self.runtime = app, context, app.runtime
        self.life = app.life_runtime.for_context(context)
        self.policy = ObjectAccessPolicy(self.runtime)
        self.operations = OperationRepository(self.runtime)
        self.config_operations = OperationRepository(app.configuration.store)
        self.owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)

    def authorize(self, operator):
        c, now = self.context, self.app.now()
        if c.scope.kind is not ScopeKind.PRIVATE or c.audience.recipients != (c.actor.subject,):
            raise TaskError("life_scope_invalid", "select one private participant")
        current = require(self.app.tasks.identity.resolve_authenticated(c.actor.principal, observed_at=now))
        binding = require(self.app.tasks.identity.resolve_role(c.scope, observed_at=now))
        if (current.subject != c.actor.subject or current.binding_id != c.actor.binding_id
                or current.binding_revision != c.actor.binding_revision or binding != c.role_binding):
            raise TaskError("life_target_changed", "selected participant or role binding changed")
        return require(self.policy.scope_access(operator, scope=c.scope, action="role.life.manage",
                                               observed_at=now, core_only=True))

    def token(self, operator, access, identifier="life", revision=0):
        return digest(["life-management.v1", operator.subject, operator.binding_id, operator.binding_revision,
                       self.context.key, access.policy_revision, identifier, revision])

    def _activity(self, identifier, *, current=True):
        try:
            a = self.app.role_life.activity(identifier, owner=self.owner)
        except KeyError:
            raise TaskError("life_activity_unavailable", "activity is unavailable") from None
        if a.scope != self.context.scope or a.work is None or current and a.work.context_key != self.context.key:
            raise TaskError("life_context_changed", "activity belongs to another authority")
        return a

    def _activities(self):
        cursor = None
        while True:
            page = self.app.role_life.activities_for_role(self.owner, limit=100, after_id=cursor)
            for activity in page:
                if activity.scope == self.context.scope and activity.work:
                    yield activity
            if len(page) < 100:
                return
            cursor = page[-1].activity_id

    def _project_activity(self, a, operator, access, settings):
        unavailable = None
        if a.work.source:
            check = self.app.role_journal.sources.validate_reference_for_audience(a.work.source,
                recipient=self.context.actor.subject, observed_at=self.app.now())
            unavailable = check.failure.code if check.failure else None
        task = None
        title, end_offset = a.description, a.work.end_offset
        if a.work.kind == "reading":
            base, separator, span = a.description.rpartition(" · ")
            _range, divider, total_label = span.partition(" / ")
            total = total_label.removesuffix(" 字符") if total_label.endswith(" 字符") else ""
            if separator and divider and total.isdigit():
                lazy = (self.app.role_life.transition_provider().reading_metadata(context_key=self.context.key,
                            source=json.loads(encode(a.work.source)))["lazy"]
                        if self.app.role_life.activity_owner() == "good_life" else bool(self.runtime.read_one(
                            "SELECT 1 FROM runtime_metadata WHERE key = ?", (queue_key(self.context.key, a.work.source),))))
                if a.state is State.PLANNED and a.work.context_key == self.context.key and lazy:
                    end_offset = min(int(total), a.work.start_offset + settings["reading_chunk_chars"])
                title = f"{base} · {a.work.start_offset + 1}–{end_offset} / {total} 字符"
            task_id = "reading-task-" + digest([a.work.context_key, a.activity_id, a.work.request_id])
            row = self.runtime.read_one("SELECT state, record_json FROM execution_tasks WHERE task_id = ?", (task_id,))
            unknown = self.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id = ? AND state IN ('reserved','dispatched','unknown')", (task_id,))
            if unknown and self.life.approved_interrupted_reading_retry(a, task_id):
                unknown = None
            if row:
                task = {"task_id": task_id, "state": "unknown" if unknown else row["state"],
                        "reason": json.loads(row["record_json"])["reason"]}
        return {"activity_id": a.activity_id, "revision": a.revision, "kind": a.work.kind,
            "current_context": a.work.context_key == self.context.key,
            "state": a.state.value, "title": title, "checkpoint": a.checkpoint,
            "started_at": (None if a.work.kind == "reading" and a.state is State.PLANNED
                           and a.work.due_at is None else a.started_at.isoformat()),
            "updated_at": a.updated_at.isoformat(),
            "due_at": a.work.due_at.isoformat() if a.work.due_at else None,
            "start_offset": a.work.start_offset, "end_offset": end_offset,
            "sleep": ({"kind": a.sleep.kind, "target_minutes": a.sleep.target_minutes,
                "slept_seconds": a.sleep.slept_seconds, "interruptions": a.sleep.interruptions,
                "early_interruptions": a.sleep.early_interruptions,
                "quality_range": [a.sleep.quality_min, a.sleep.quality_max],
                "wake_until": a.sleep.wake_until.isoformat() if a.sleep.wake_until else None,
                "oversleep_minutes": a.sleep.oversleep_minutes} if a.sleep else None),
            "reflection": a.reading.reflection if a.reading and unavailable is None else None,
            "unavailable_reason": unavailable, "task": task,
            "view_token": self.token(operator, access, a.activity_id, a.revision)}

    def _journal(self, key, operator, access):
        journal = self.app.role_journal
        head = journal.head(key)
        if head.state is JournalState.ABSENT:
            return None
        row = self.runtime.read_one("SELECT context_json FROM role_journals WHERE entry_key = ?", (digest(key),))
        target = codec.context(json.loads(row["context_json"]))
        if target.scope != self.context.scope:
            return None
        data = {"kind": key.kind.value, "local_date": key.local_date, "revision": head.revision,
                "state": head.state.value, "body": None, "afterglow": None, "recall": None,
                "summary": None, "dream_fragments": [], "unavailable_reason": None,
                "view_token": self.token(operator, access, digest(key), head.revision)}
        if head.state is JournalState.ACTIVE:
            try:
                entry = journal.get(key)
                for source in entry.draft.sources:
                    require(journal.sources.validate_reference_for_audience(source,
                        recipient=self.context.actor.subject, observed_at=self.app.now()))
                data.update(body=entry.draft.body, afterglow=entry.draft.afterglow,
                            recall=entry.draft.recall.value if entry.draft.recall else None)
                derived = journal.derivatives(key)
                if derived:
                    data.update(summary=derived.material.summary, dream_fragments=derived.material.dream_fragments)
            except TaskError as error:
                data["unavailable_reason"] = error.code
        return data

    def _reading_note(self, activity, timezone, source_cache):
        receipt = activity.reading
        data = {"kind": "reading_note", "activity_id": activity.activity_id,
                "local_date": receipt.completed_at.astimezone(ZoneInfo(timezone)).date().isoformat(),
                "completed_at": receipt.completed_at.isoformat(), "state": "active",
                "book_title": activity.description.rsplit(" · ", 1)[0],
                "start_offset": receipt.start_offset, "end_offset": receipt.end_offset,
                "body": None, "excerpt": None, "unavailable_reason": None}
        check = self.app.role_journal.sources.validate_reference_for_audience(receipt.source,
            recipient=self.context.actor.subject, observed_at=self.app.now(),
            expected_content_hash=receipt.content_hash)
        if check.failure:
            data["unavailable_reason"] = check.failure.code
            return data
        data["body"] = receipt.reflection
        # Original text requires its own live read grant; a provenance check alone
        # is intentionally not used to bypass the source repository's access check.
        cache_key = (receipt.source, receipt.content_hash)
        if cache_key not in source_cache:
            original = self.app.role_journal.sources.read(receipt.source, resolved=self.context.actor,
                capability="role.reading.record", observed_at=self.app.now(), authorizer=self.app.tasks.identity)
            source = original.unwrap() if original.is_success else None
            source_cache[cache_key] = (source.payload if source and source.content_hash == receipt.content_hash
                                       and digest(source.payload) == receipt.content_hash else None)
        payload = source_cache[cache_key]
        if payload is not None:
            data["excerpt"] = payload[receipt.start_offset:min(receipt.end_offset, receipt.start_offset + 300)]
        return data

    def _reading_plan(self, settings, records):
        if not settings["enabled"] or not settings["reading_enabled"]:
            return {"state": "disabled"}
        pace_until = self.life.reading_pace_until(settings)
        if pace_until is not None and self.app.now() < pace_until:
            return {"state": "paced", "resume_at": pace_until.isoformat()}
        day = self.app.now().astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
        slot = self.life._smart_reading.current_choice_slot(records, day)
        reader = self.app.role_reader
        if self.app.role_life.activity_owner() == "good_life":
            choice = self.app.role_life.transition_provider().reading_choice(context_key=self.context.key, slot=slot)
        else:
            row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                        (reader.choice_key(self.context, slot),))
            choice = json.loads(row["value"]) if row else None
        if choice:
            if not choice.get("source_id"):
                return {"state": "declined", "reason": choice.get("reason", "")}
            item = next((a for a in records if a.work.source and
                         a.work.source.source_id == choice["source_id"]), None)
            return {"state": "selected", "title": item.description.rsplit(" · ", 1)[0] if item else "已选书籍",
                    "reason": choice.get("reason", "")}
        if "book-choice:" + slot in self.life._workers:
            return {"state": "choosing"}
        if reader.operations.get(reader.choice_id(self.context, slot)).is_success:
            return {"state": "incomplete"}
        if not any(a.work.kind == "reading" and a.state is State.PLANNED for a in records):
            return {"state": "empty"}
        return {"state": "waiting"}

    def status(self, operator, *, before=None, after_activity=None, archive_kind=None):
        access = self.authorize(operator)
        config = require(self.app.configuration.read(operator, target=target_for(self.context), group_id=GROUP,
                                                      observed_at=self.app.now()))
        # Cursor pages include unavailable/deleted records; a damaged source never hides the rest of the archive.
        if before is not None:
            date.fromisoformat(before)
        if after_activity is not None and (not isinstance(after_activity, str) or len(after_activity) > 512):
            raise ValueError("invalid activity cursor")
        if archive_kind not in (None, "diary", "reading_note", "social_note"):
            raise ValueError("invalid archive kind")
        rows = []
        if archive_kind in (None, "diary"):
            rows = self.runtime.read_all("""SELECT context_json FROM role_journals WHERE owner_key = ?
                AND local_date IN (SELECT DISTINCT local_date FROM role_journals WHERE owner_key = ?
                    AND (? IS NULL OR local_date < ?) ORDER BY local_date DESC LIMIT 50)
                ORDER BY local_date DESC, kind""", (digest(self.owner), digest(self.owner), before, before))
        journals = []
        for row in rows:
            target = codec.context(json.loads(row["context_json"]))
            if target.scope == self.context.scope:
                journals.append(self._journal(target.key, operator, access))
        settings = values(config)
        timezone = settings["timezone"]
        zone = ZoneInfo(timezone)
        all_activities = [a for a in self._activities() if a.work.context_key == self.context.key]
        notes = [a for a in all_activities if archive_kind in (None, "reading_note") and a.reading
                 and (before is None or a.reading.completed_at.astimezone(zone).date().isoformat() < before)]
        social_notes = []
        if archive_kind == "social_note":
            for activity in all_activities:
                if activity.work.kind != "social" or activity.state is not State.COMPLETED:
                    continue
                local_date = activity.updated_at.astimezone(zone).date().isoformat()
                if before is not None and local_date >= before:
                    continue
                try:
                    checkpoint = json.loads(activity.checkpoint)
                except (ValueError, TypeError):
                    continue
                reflection = checkpoint.get("subjective_reflection") if isinstance(checkpoint, dict) else None
                if isinstance(reflection, str) and reflection.strip():
                    social_notes.append({"kind": "social_note", "activity_id": activity.activity_id,
                        "local_date": local_date, "completed_at": activity.updated_at.isoformat(),
                        "state": "active", "body": reflection})
        days = sorted({item["local_date"] for item in journals if item is not None} |
            {a.reading.completed_at.astimezone(zone).date().isoformat() for a in notes} |
            {item["local_date"] for item in social_notes}, reverse=True)[:50]
        included = set(days)
        source_cache = {}
        notes = [self._reading_note(a, timezone, source_cache) for a in sorted(notes,
                 key=lambda a: a.reading.completed_at, reverse=True)
                 if a.reading.completed_at.astimezone(zone).date().isoformat() in included]
        journals = [item for item in journals if item is not None] + notes + sorted(
            social_notes, key=lambda item: item["completed_at"], reverse=True)
        journals = sorted((item for item in journals if item["local_date"] in included),
                          key=lambda item: item["local_date"], reverse=True)
        activities, cursor = [], after_activity
        while len(activities) < 30:
            page = self.app.role_life.activities_for_role(self.owner, limit=30, after_id=cursor)
            activities.extend(a for a in page if a.scope == self.context.scope and a.work)
            if len(page) < 30:
                cursor = None
                break
            cursor = page[-1].activity_id
        receipts = []
        for key, result in self.life.results.items():
            if result is None:
                continue
            if result.failure:
                receipts.append({"key": key, "state": "failed", "code": result.failure.code})
            else:
                value = result.unwrap()
                task = getattr(value, "task", None)
                receipts.append({"key": key, "state": getattr(value, "state", None) or (task.state if task else "complete"),
                    "operation_id": getattr(value, "operation_id", None),
                    "code": getattr(value, "failure_code", None)})
        # Trigger receipts survive restarts and contain no journal body.
        persisted = self.runtime.read_all("""SELECT operation_id, state FROM operations
            WHERE owner = 'role.journal.trigger' AND idempotency_key = operation_id || ':' || ?
            ORDER BY created_at DESC LIMIT 40""", (self.context.key,))
        for row in persisted:
            operation = require(self.operations.get(row["operation_id"]))
            if (operation.operation.scope == self.context.scope
                    and operation.operation.idempotency_key == row["operation_id"] + ":" + self.context.key):
                receipt = self.app.journal_triggers._receipt(self.context, operation)
                receipts.append({"key": row["operation_id"], "state": receipt.state,
                    "code": receipt.failure_code, "operation_id": row["operation_id"],
                    "retryable": receipt.retryable})
        # 只要还有一次书写没有结论（unknown/处理中/已受理），就不该再让人从页面
        # 选“失败的书写”重试：那会把同一天的第二次提交送进同一条链。
        journal_unresolved = any(
            item.get("retryable") is not None and item.get("state") in ("unknown", "uncertain", "accepted")
            for item in receipts)
        daily = self.app.journal_schedule._schedules.get(self.life.schedule_id)
        sleep_schedule = self.app.life_runtime.sleep_schedule_status(self.life, config)
        interests = visible_interests(self.app, self.context, self.app.now())
        profile = ConversationRoleProfileContext(self.runtime).capture(self.context, self.app.now())
        for item in interests:
            item["view_token"] = self.token(operator, access, item["interest_id"], item["revision"])
        return {"role_id": self.owner.persona_id, "participant": self.context.scope.native_scope_id,
            "interests": interests, "disliked_items": self._disliked_items(operator, access),
            "recent_exposures": self._recent_exposures(), "profile_hobbies": profile["fields"].get("hobbies", "") if profile else "",
            "host_state": self.app.state, "config": settings, "edit_token": config.edit_token,
            "view_token": self.token(operator, access), "failure_code": self.life.failure_code,
            "next_diary_at": daily.next_at.isoformat() if daily else None,
            "next_sleep_at": sleep_schedule["next_sleep_at"], "sleep_schedule": sleep_schedule,
            "body_sleep": self.app.role_state.body_clock.read(self.context),
            "reading_plan": self._reading_plan(settings, all_activities),
            "activities": [self._project_activity(a, operator, access, settings) for a in activities],
            "next_activity": cursor, "journals": journals,
            "next_date": days[-1] if len(days) == 50 else None,
            "receipts": receipts[-60:], "running": list(self.life._workers),
            "journal_unresolved": journal_unresolved}

    def sleep_status(self, operator, *, access=None):
        """Small shared-schedule projection for role-state management views."""
        if access is None:
            self.authorize(operator)
        config = require(self.app.configuration.read(operator, target=target_for(self.context), group_id=GROUP,
                                                      observed_at=self.app.now()))
        return {"sleep_schedule": self.app.life_runtime.sleep_schedule_status(self.life, config),
                "body_sleep": self.app.role_state.body_clock.read(self.context)}

    def operation_id(self, prefix, operator, key):
        # Preserve pre-partitioning retries only in their original private scope.
        legacy = prefix + digest([operator.subject, key])
        receipt = self.config_operations.get(legacy)
        if not receipt.is_success:
            receipt = self.operations.get(legacy)
        if prefix != "life-config-" and receipt.is_success and receipt.unwrap().operation.scope == self.context.scope:
            return legacy
        allowed_owners = (CONFIGURATION_OWNER,
                          *getattr(self.app.configuration._owners.get(CONFIGURATION_OWNER),
                                   "legacy_owner_ids", ()))
        if (receipt.is_success and receipt.unwrap().operation.scope == self.context.scope
                and receipt.unwrap().operation.owner in allowed_owners):
            return legacy
        return prefix + digest([operator.subject, self.context.key, key])

    def configuration_operation_owner(self, operation_id):
        """Use a retired owner only to replay its exact persisted receipt."""
        owner = self.app.configuration._owners.get(CONFIGURATION_OWNER)
        legacy_ids = tuple(getattr(owner, "legacy_owner_ids", ()))
        receipt = self.config_operations.get(operation_id)
        if not receipt.is_success:
            receipt = self.operations.get(operation_id)
        if (receipt.is_success and receipt.unwrap().operation.scope == self.context.scope
                and receipt.unwrap().operation.owner in legacy_ids):
            return receipt.unwrap().operation.owner
        return CONFIGURATION_OWNER

    def configure(self, operator, data):
        self.authorize(operator)
        changes = data["changes"]
        if type(changes) is not dict or set(changes) - set(DEFAULTS):
            raise ValueError("unknown routine fields")
        target = target_for(self.context)
        current = require(self.app.configuration.read(operator, target=target, group_id=GROUP, observed_at=self.app.now()))
        normalized = {GROUP + "." + k: v for k, v in changes.items()}
        preview = require(self.app.configuration.preview_changes(operator, target=target, group_id=GROUP,
            changes=normalized, observed_at=self.app.now()))
        values(replace(current, fields=preview.after))
        op_id = self.operation_id("life-config-", operator, data["operation_key"])
        operation = OperationRef(op_id, self.configuration_operation_owner(op_id), self.context.scope,
                                 VersionRef(op_id, 1), op_id)
        with self.runtime.transaction():
            receipt = require(self.app.configuration.patch(operator, target=target, group_id=GROUP,
                operation=operation, expected_edit_token=data["edit_token"], changes=normalized, observed_at=self.app.now()))
            if not receipt.replayed and "enabled" in changes:
                self._grants(changes["enabled"], op_id)
        self.app.notify()
        return {"operation_id": op_id, "replayed": receipt.replayed}

    def _grants(self, enabled, op_id):
        identity = self.app.tasks.identity
        prefix = "life-grant-" + digest(self.context.key) + ":"
        for grant in identity.snapshot().grants:
            if (grant.grant_id.startswith("life-grant-") and grant.scope == self.context.scope
                    and grant.subject == self.context.actor.subject and grant.state is GrantState.ACTIVE):
                identity.revoke_grant(grant.grant_id, revoked_at=self.app.now(), expected_revision=identity.revision)
        if enabled:
            for action in ("role.life.run", "role.life.read", "role.journal.write", "role.reading.record"):
                identity.issue_grant(grant_id=prefix + digest([op_id, action]), subject=self.context.actor.subject,
                    scope=self.context.scope, capability=action, tier=AuthorizationTier.STANDARD,
                    valid_from=self.app.now(), expected_revision=identity.revision)

    def sync_configuration_grant(self, operator, enabled, receipt):
        """Keep the generic settings editor on the same grant path as this manager."""
        now = self.app.now()
        require(self.policy.scope_access(operator, scope=self.context.scope, action="configuration.write",
                                         observed_at=now, core_only=True))
        self.app.tasks._context(self.context, now)
        current = require(self.app.configuration.read(operator, target=target_for(self.context), group_id=GROUP,
                                                      observed_at=now))
        if current.values[GROUP + ".enabled"] is not enabled:
            return

        identity, context = self.app.tasks.identity, self.context
        prefix = "life-grant-" + digest(context.key) + ":"
        actions = ("role.life.run", "role.life.read", "role.journal.write", "role.reading.record")
        if not receipt["replayed"]:
            self._grants(enabled, receipt["operation_id"])
            return

        if not enabled:
            for grant in identity.snapshot().grants:
                if (grant.grant_id.startswith(prefix) and grant.scope == context.scope
                        and grant.subject == context.actor.subject and grant.state is GrantState.ACTIVE):
                    identity.revoke_grant(grant.grant_id, revoked_at=now, expected_revision=identity.revision)
            return

        grants = identity.snapshot().grants
        for action in actions:
            grant_id = prefix + digest([receipt["operation_id"], action])
            if any(grant.grant_id == grant_id for grant in grants):
                continue
            if any(grant.grant_id.startswith(prefix) and grant.scope == context.scope
                   and grant.subject == context.actor.subject and grant.capability == action
                   and grant.state is GrantState.ACTIVE for grant in grants):
                continue
            identity.issue_grant(grant_id=grant_id, subject=context.actor.subject, scope=context.scope,
                capability=action, tier=AuthorizationTier.STANDARD, valid_from=now,
                expected_revision=identity.revision)

    def _add_text(self, data, op_id, *, max_characters=262144):
        text, title = data["text"], data["title"]
        if (type(text) is not str or not text.strip() or len(text) > max_characters
                or type(title) is not str or not title.strip() or len(title) > 160):
            raise ValueError("provide a title and valid original text")
        author = data.get("author", "")
        if not isinstance(author, str) or len(author) > 120:
            raise ValueError("invalid book author")
        display_title = f"{title}（{author}）" if author.strip() else title
        now = self.app.now()
        source = SourceRef("book-" + digest(op_id), GROUP, self.context.scope, VersionRef(op_id, 1), now)
        content_hash = digest(text)
        require(self.app.role_journal.sources.publish(SourceRecord(source, self.context.audience, content_hash, text), observed_at=now))
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)",
                               (queue_key(self.context.key, source), "1"))
        first = queue_next(self.app.role_life, self.context, source, content_hash, text, display_title, 0, now)
        if first is None:
            raise ValueError("original text has no readable span")
        self.app.role_life.add_artifact(RoleArtifact("book-" + digest(op_id), first.activity_id, RoleArtifactKind.TEXT,
            source, now, journal_meaning=RoleArtifactJournalMeaning.READING), owner=self.owner)
        snapshot = require(self.app.configuration.read(self.context.actor, target=target_for(self.context),
            group_id=GROUP, observed_at=now))
        size = values(snapshot)["reading_chunk_chars"]
        count = sum(bool(text[start:start + size].strip()) for start in range(0, len(text), size))
        return {"activity_ids": [first.activity_id], "total_segments": count}

    def _recent_exposures(self):
        if self.app.role_life.activity_owner() == "good_life":
            rows = []
            for item in self.app.role_social.exposure.recent(self.context):
                feedback = self.runtime.read_one("""SELECT 1 FROM role_exposure_feedback WHERE scope_key=? AND activity_id=?
                    AND platform=? AND content_id=? LIMIT 1""", (scope_key(self.context), item["activity_id"], item["platform"], item["content_id"]))
                rows.append({**item, "has_feedback": bool(feedback)})
        else:
            rows = self.runtime.read_all("""SELECT e.*, EXISTS(SELECT 1 FROM role_exposure_feedback f
                WHERE f.scope_key=e.scope_key AND f.activity_id=e.activity_id AND f.platform=e.platform
                    AND f.content_id=e.content_id) AS has_feedback
                FROM role_exposures e WHERE e.scope_key=? ORDER BY e.fetched_at DESC LIMIT 100""", (scope_key(self.context),))
        result, allowed = [], {}
        for row in rows:
            aid = row["activity_id"]
            if aid not in allowed:
                try:
                    activity = self._activity(aid)
                    allowed[aid] = bool(activity.work and activity.work.kind == "social" and activity.work.context_key == self.context.key)
                except (KeyError, ValueError, TaskError):
                    allowed[aid] = False
            if not allowed[aid]:
                continue
            stage = ("反馈" if row["has_feedback"] else "尝试" if row["attempted_at"] else
                     "接触" if row["exposed_at"] else "获取")
            result.append({"platform": row["platform"], "title": row["title"], "source_url": row["source_url"],
                           "fetched_at": row["fetched_at"], "exposed_at": row["exposed_at"],
                           "stage": stage, "coverage": row["coverage"], "random_attempt": bool(row["random_attempt"])})
        return result

    def _disliked_items(self, operator, access):
        if self.app.role_life.activity_owner() == "good_life":
            references = self.runtime.read_all("""SELECT rowid AS revision,target_id,activity_id,platform,content_id,reason,occurred_at
                FROM role_exposure_feedback WHERE scope_key=? AND target_kind='item' AND reaction='negative' AND active=1
                ORDER BY occurred_at DESC,rowid DESC""", (scope_key(self.context),))
            rows = []
            for reference in references:
                item = self.app.role_social.exposure.exposure(self.context, reference["activity_id"],
                                                              reference["platform"], reference["content_id"])
                if item is not None:
                    rows.append({**dict(reference), "title": item["title"], "source_url": item["source_url"]})
        else:
            rows = self.runtime.read_all("""SELECT f.rowid AS revision, f.target_id, f.activity_id, f.platform,
                f.content_id, f.reason, f.occurred_at, e.title, e.source_url
                FROM role_exposure_feedback f JOIN role_exposures e ON e.scope_key=f.scope_key AND e.activity_id=f.activity_id
                    AND e.platform=f.platform AND e.content_id=f.content_id
                WHERE f.scope_key=? AND f.target_kind='item' AND f.reaction='negative' AND f.active=1
                ORDER BY f.occurred_at DESC, f.rowid DESC""", (scope_key(self.context),))
        result, seen = [], set()
        for row in rows:
            if row["target_id"] in seen:
                continue
            try:
                activity = self._activity(row["activity_id"])
                if not activity.work or activity.work.kind != "social" or activity.work.context_key != self.context.key:
                    continue
            except (KeyError, ValueError, TaskError):
                continue
            seen.add(row["target_id"])
            identifier = digest(["social-dislike", row["target_id"]])
            result.append({"dislike_id": identifier, "revision": row["revision"], "title": row["title"],
                "platform": row["platform"], "source_url": row["source_url"], "reason": row["reason"],
                "occurred_at": row["occurred_at"], "view_token": self.token(operator, access, identifier, row["revision"])})
        return result

    async def command(self, operator, data):
        try:
            access = self.authorize(operator)
            action, key = data["action"], data["operation_key"]
            if type(key) is not str or not key.strip() or len(key) > 128:
                raise ValueError("stable operation key required")
            if action == "configure":
                return Outcome.success(self.configure(operator, data))
            identifier, revision = data.get("activity_id", "life"), data.get("expected_revision", 0)
            if action in ("interest_pause", "interest_resume", "interest_edit", "interest_merge", "interest_clear_dislike", "interest_item_clear"):
                identifier = data.get("dislike_id") if action == "interest_item_clear" else data.get("interest_id")
                if not isinstance(identifier, str) or len(identifier) != 64:
                    raise ValueError("invalid interest")
            if type(revision) is not int or revision < 0:
                raise ValueError("original revision required")
            journal_target = None
            if action in ("edit", "delete", "restore", "derive"):
                journal_target = JournalContext(JournalKey(self.owner, JournalKind(data["kind"]), data["local_date"]),
                    self.context.scope, data.get("timezone", "Asia/Shanghai"))
                identifier = digest(journal_target.key)
            if data.get("view_token") != self.token(operator, access, identifier, revision):
                raise TaskError("life_view_stale", "refresh before applying this change")
            epub = None
            if action == "add_epub":
                epub = await asyncio.to_thread(parse_epub_upload, data.get("filename"), data.get("epub_base64"))
                access = self.authorize(operator)
                if data.get("view_token") != self.token(operator, access, identifier, revision):
                    raise TaskError("life_view_stale", "refresh before applying this change")
            op_id = self.operation_id("life-command-", operator, key)
            if action in ("add_text", "add_epub") and self.app.role_life.activity_owner() == "good_life":
                from companion_v2.integration.reading_import_handoff import ReadingImportHandoff
                handoff = ReadingImportHandoff(self, self.app.role_life.creation_provider())
                return Outcome.success(handoff.execute(operator, data, epub, observed_at=self.app.now()))
            if action in ("remove", "withdraw") and self.app.role_life.activity_owner() == "good_life":
                from companion_v2.integration.activity_deletion_handoff import ActivityDeletionHandoff
                handoff = ActivityDeletionHandoff(self, self.app.role_life.deletion_provider())
                at = self.app.now()
                request = handoff.prepare(operator, data, observed_at=at)
                try:
                    decision = handoff.decide(operator, data, request, observed_at=at)
                except TaskError:
                    # Abort only if no commit decision exists; an unavailable
                    # provider leaves the preparation for later recovery.
                    try:
                        handoff.abort(request, observed_at=at)
                    except (TaskError, ValueError):
                        pass
                    raise
                applied = handoff.apply(request, decision)
                handoff.complete(request, decision, observed_at=at)
                handoff.stop_workers(request, decision)
                handoff.acknowledge(request, decision)
                self.app.notify()
                result = {"operation_id": op_id, **({"removed": identifier} if action == "remove" else {"activity_id": identifier})}
                if applied["state"] == "acknowledged":
                    result.update(state="completed", replayed=True)
                return Outcome.success(result)
            if action in ("diary", "dream", "derive"):
                await self.life.preflight()
                if action == "diary":
                    result = await self.life.diary(local_date=data.get("local_date"), retry_of=data.get("retry_of"), request_id=key)
                elif action == "dream":
                    activity = self._activity(identifier)
                    if activity.work.kind != "sleep" or activity.state is not State.COMPLETED or activity.revision != revision:
                        raise TaskError("life_sleep_invalid", "select the unchanged completed sleep")
                    result = await self.life.dream(activity, retry_of=data.get("retry_of"), request_id=key)
                else:
                    entry = self.app.role_journal.get(journal_target.key)
                    if entry.draft.context.scope != self.context.scope or entry.reference.revision != revision:
                        raise TaskError("journal_revision_conflict", "journal changed")
                    result = await self.life.derive(entry.draft.context, self.life.event("derive", "独立提取私人书写索引。"), automatic=False)
                    result = result or Outcome.success({"state": "already_derived"})
                self.life.results[op_id] = result
                return result
            if action in ("sleep", "read", "wake"):
                await self.life.preflight()
                self.authorize(operator)
            if action == "read" and self.app.role_life.activity_owner() == "good_life":
                from companion_v2.integration.manual_reading_queue import enqueue
                return Outcome.success(enqueue(self, operator, data, op_id))
            if action == "sleep" and self.app.role_life.activity_owner() == "good_life":
                from companion_v2.integration.manual_sleep_handoff import ManualSleepHandoff
                handoff = ManualSleepHandoff(self, self.app.role_life.creation_provider())
                at = self.app.now()
                request = handoff.prepare(operator, data, observed_at=at)
                try:
                    decision = handoff.decide(operator, data, request, observed_at=at)
                except (TaskError, ValueError):
                    try:
                        handoff.abort(request, observed_at=at)
                    except (TaskError, ValueError):
                        pass
                    raise
                applied = handoff.apply(data, request, decision)
                handoff.complete(data, request, decision, observed_at=at)
                handoff.acknowledge(data, request, decision)
                self.app.notify()
                result = {"operation_id": op_id, "activity_id": applied["result"]["activity_id"]}
                if applied["state"] == "acknowledged":
                    result.update(state="completed", replayed=True)
                return Outcome.success(result)
            cancel_sleep = cancel_reading = False
            if action == "cancel" and self.app.role_life.activity_owner() == "good_life":
                with self.app.role_life.consistent_snapshot():
                    work_kind = self._activity(identifier, current=False).work.kind
                    cancel_sleep, cancel_reading = work_kind == "sleep", work_kind == "reading"
            if (action == "wake" or cancel_sleep or cancel_reading) and self.app.role_life.activity_owner() == "good_life":
                from companion_v2.integration.manual_wake_handoff import ManualCancelSleepHandoff, ManualWakeHandoff
                if cancel_reading:
                    from companion_v2.integration.manual_cancel_reading_handoff import ManualCancelReadingHandoff
                    handoff_type = ManualCancelReadingHandoff
                else:
                    handoff_type = ManualCancelSleepHandoff if cancel_sleep else ManualWakeHandoff
                handoff = handoff_type(self, self.app.role_life.transition_provider())
                at = self.app.now()
                ended = handoff.already_ended(operator, data, observed_at=at)
                if ended is not None:
                    return Outcome.success(ended)
                request = handoff.prepare(operator, data, observed_at=at)
                try:
                    decision = handoff.decide(operator, data, request, observed_at=at)
                except (TaskError, ValueError):
                    try:
                        handoff.abort(request, observed_at=at)
                    except (TaskError, ValueError):
                        pass
                    raise
                applied = handoff.apply(data, request, decision)
                handoff.complete(data, request, decision, observed_at=at)
                if cancel_reading:
                    handoff.stop_worker(request)
                handoff.acknowledge(data, request, decision)
                self.app.notify()
                result = {"operation_id": op_id, "activity_id": applied["result"]["activity_id"]}
                if applied["state"] == "acknowledged":
                    result.update(state="completed", replayed=True)
                return Outcome.success(result)
            operation = OperationRef(op_id, "role.life.management", self.context.scope, VersionRef(op_id, 1), op_id)
            with self.runtime.transaction() as connection:
                reserved = require(self.operations.reserve(operation, parameter_hash=digest(data), observed_at=self.app.now()))
                if reserved.replayed:
                    return Outcome.success({"operation_id": op_id, "state": reserved.state.value, "replayed": True})
                result = {}
                if action == "interest_item_clear":
                    item = next((entry for entry in self._disliked_items(operator, access)
                                 if entry["dislike_id"] == identifier and entry["revision"] == revision), None)
                    if item is None:
                        raise TaskError("life_view_stale", "dislike changed or is unavailable")
                    rows = connection.execute("""SELECT DISTINCT target_id FROM role_exposure_feedback WHERE scope_key=?
                        AND target_kind='item' AND reaction='negative' AND active=1""", (scope_key(self.context),)).fetchall()
                    target = next((r["target_id"] for r in rows if digest(["social-dislike", r["target_id"]]) == identifier), None)
                    connection.execute("""UPDATE role_exposure_feedback SET active=0 WHERE scope_key=? AND target_kind='item'
                        AND target_id=? AND reaction='negative'""", (scope_key(self.context), target))
                elif action in ("interest_pause", "interest_resume", "interest_edit", "interest_merge", "interest_clear_dislike"):
                    available = visible_interests(self.app, self.context, self.app.now())
                    existing = next((item for item in available
                                     if item["interest_id"] == identifier), None)
                    if not existing or existing["revision"] != revision:
                        raise TaskError("life_view_stale", "interest changed or is unavailable")
                    if action == "interest_edit":
                        name = data.get("name")
                        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 50:
                            raise ValueError("invalid interest name")
                        if any(item["interest_id"] != identifier and item["name"].casefold() == name.strip().casefold()
                               for item in available):
                            raise ValueError("interest name already exists; merge instead")
                        row = connection.execute("SELECT aliases_json FROM role_interests WHERE scope_key = ? AND interest_id = ?", (scope_key(self.context), identifier)).fetchone()
                        aliases = list(dict.fromkeys(json.loads(row["aliases_json"]) + [existing["name"]]))
                        updated = connection.execute("UPDATE role_interests SET name = ?, aliases_json = ?, revision = revision + 1 WHERE scope_key = ? AND interest_id = ? AND revision = ?",
                            (name.strip(), json.dumps(aliases, ensure_ascii=False), scope_key(self.context), identifier, revision))
                    elif action == "interest_merge":
                        target = next((item for item in available if item["interest_id"] == data.get("target_id")), None)
                        if (target is None or target["interest_id"] == identifier or
                                type(data.get("target_revision")) is not int or target["revision"] != data["target_revision"]):
                            raise TaskError("life_view_stale", "merge target changed")
                        key = scope_key(self.context)
                        target_row = connection.execute("SELECT aliases_json FROM role_interests WHERE scope_key = ? AND interest_id = ?", (key, target["interest_id"])).fetchone()
                        source_row = connection.execute("SELECT aliases_json FROM role_interests WHERE scope_key = ? AND interest_id = ?", (key, identifier)).fetchone()
                        connection.execute("""INSERT OR IGNORE INTO role_interest_evidence
                            SELECT scope_key, ?, activity_id, occurred_at, reaction, wants_to_continue,
                                   autonomous_return, reason, next_question FROM role_interest_evidence
                            WHERE scope_key = ? AND interest_id = ?""", (target["interest_id"], key, identifier))
                        connection.execute("DELETE FROM role_interest_evidence WHERE scope_key = ? AND interest_id = ?", (key, identifier))
                        connection.execute("""INSERT OR IGNORE INTO role_exposure_topics
                            SELECT scope_key,activity_id,platform,content_id,? FROM role_exposure_topics
                            WHERE scope_key=? AND interest_id=?""", (target["interest_id"], key, identifier))
                        connection.execute("DELETE FROM role_exposure_topics WHERE scope_key=? AND interest_id=?", (key, identifier))
                        connection.execute("""INSERT OR IGNORE INTO role_interest_coefficients
                            SELECT scope_key,?,coefficient,assessed FROM role_interest_coefficients WHERE scope_key=? AND interest_id=?""",
                            (target["interest_id"], key, identifier))
                        connection.execute("DELETE FROM role_interest_coefficients WHERE scope_key=? AND interest_id=?", (key, identifier))
                        connection.execute("""INSERT OR IGNORE INTO role_exposure_feedback
                            SELECT scope_key,activity_id,platform,content_id,target_kind,?,reaction,wants_continue,reason,occurred_at,autonomous,active
                            FROM role_exposure_feedback WHERE scope_key=? AND target_kind='topic' AND target_id=?""",
                            (target["interest_id"], key, identifier))
                        connection.execute("DELETE FROM role_exposure_feedback WHERE scope_key=? AND target_kind='topic' AND target_id=?", (key, identifier))
                        updated = connection.execute("DELETE FROM role_interests WHERE scope_key = ? AND interest_id = ? AND revision = ?", (key, identifier, revision))
                        aliases = list(dict.fromkeys(json.loads(target_row["aliases_json"]) + json.loads(source_row["aliases_json"]) + [existing["name"]]))
                        connection.execute("UPDATE role_interests SET aliases_json = ?, revision = revision + 1 WHERE scope_key = ? AND interest_id = ?", (json.dumps(aliases, ensure_ascii=False), key, target["interest_id"]))
                    elif action == "interest_clear_dislike":
                        connection.execute("""UPDATE role_exposure_feedback SET active=0 WHERE scope_key=? AND target_kind='topic'
                            AND target_id=? AND reaction='negative'""", (scope_key(self.context), identifier))
                        updated = connection.execute("UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id=? AND revision=?",
                            (scope_key(self.context), identifier, revision))
                    else:
                        updated = connection.execute("UPDATE role_interests SET paused = ?, revision = revision + 1 WHERE scope_key = ? AND interest_id = ? AND revision = ?",
                            (int(action == "interest_pause"), scope_key(self.context), identifier, revision))
                    if updated.rowcount != 1:
                        raise TaskError("life_view_stale", "interest changed")
                elif action == "add_text":
                    result = self._add_text(data, op_id)
                elif action == "add_epub":
                    result = self._add_text(epub, op_id, max_characters=MAX_EPUB_TEXT)
                    result["title"] = epub["title"]
                elif action == "sleep":
                    self.life.refresh()
                    a = self.life.start_sleep(op_id, data["minutes"])
                    result = {"activity_id": a.activity_id}
                elif action == "read":
                    a = self.life.start_reading(identifier, expected_revision=revision, request_id=op_id)
                    result = {"activity_id": a.activity_id}
                elif action == "wake":
                    a = self.life.wake(identifier, expected_revision=revision, operation_id=op_id + ":wake")
                    result = {"activity_id": a.activity_id}
                elif action in ("cancel", "withdraw"):
                    a = self._activity(identifier, current=False)
                    if a.revision != revision:
                        raise TaskError("life_revision_conflict", "activity changed")
                    original, cancelled_work = None, None
                    if (action == "cancel" and a.work.kind == "reading"
                            and a.work.context_key == self.context.key):
                        original = self.app.role_journal.sources.read(a.work.source, resolved=self.context.actor,
                            capability="role.reading.record", observed_at=self.app.now(),
                            authorizer=self.app.tasks.identity)
                        if (original.is_success and a.state is State.PLANNED and self.runtime.read_one(
                                "SELECT 1 FROM runtime_metadata WHERE key = ?", (queue_key(self.context.key, a.work.source),))):
                            self.life.refresh()
                            cancelled_work = replace(a.work, end_offset=min(len(original.unwrap().payload),
                                a.work.start_offset + self.life.settings["reading_chunk_chars"]))
                    if action == "withdraw":
                        if a.work.source is None:
                            raise ValueError("only original reading text can be withdrawn")
                        row = self.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id = ?", (a.work.source.source_id,))
                        require(self.app.role_journal.sources.revoke(a.work.source,
                            expected_governance_revision=row["governance_revision"], observed_at=self.app.now()))
                        for related in self._activities():
                            if (related.activity_id != identifier and related.work.source == a.work.source
                                    and related.state not in (State.COMPLETED, State.CANCELLED)):
                                self.app.role_life.transition_activity(related.activity_id, owner=self.owner,
                                    expected_revision=related.revision, state=State.CANCELLED,
                                    operation_id=op_id + ":" + digest(related.activity_id), observed_at=self.app.now())
                                self.app.life_runtime.cancel_activity(related.activity_id)
                    if a.state not in (State.COMPLETED, State.CANCELLED):
                        if action == "cancel" and a.work.kind == "sleep":
                            self.app.role_state.body_clock.finish(self.context, a, operation_id=op_id + ":cancel",
                                observed_at=self.app.now(), checkpoint="睡眠被手动结束并保留实际睡眠时长",
                                state=State.CANCELLED)
                        else:
                            self.app.role_life.transition_activity(identifier, owner=self.owner, expected_revision=revision,
                                state=State.CANCELLED, operation_id=op_id + ":cancel", observed_at=self.app.now(),
                                work=cancelled_work)
                            if original and original.is_success and original.unwrap().content_hash == a.work.content_hash:
                                queue_next(self.app.role_life, self.context, a.work.source, a.work.content_hash,
                                    original.unwrap().payload, a.description.rsplit(" · ", 1)[0],
                                    (cancelled_work or a.work).end_offset, self.app.now())
                    self.app.life_runtime.cancel_activity(identifier)
                elif action == "remove":
                    a = self._activity(identifier, current=False)
                    if a.revision != revision:
                        raise TaskError("life_revision_conflict", "activity changed")
                    if a.work.kind != "reading" or a.state not in (State.COMPLETED, State.CANCELLED):
                        raise TaskError("life_activity_not_removable",
                                        "only completed or cancelled reading records can be removed")
                    source = a.work.source
                    if source is not None:
                        row = self.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id = ?",
                                                    (source.source_id,))
                        if row is not None:
                            try:
                                self.app.role_journal.sources.revoke(
                                    source, expected_governance_revision=row["governance_revision"],
                                    observed_at=self.app.now())
                            except TaskError:
                                # An already-revoked original still lets the test card go.
                                pass
                        for related in self._activities():
                            if (related.activity_id != identifier and related.work.source == source
                                    and related.state not in (State.COMPLETED, State.CANCELLED)):
                                self.app.role_life.transition_activity(
                                    related.activity_id, owner=self.owner, expected_revision=related.revision,
                                    state=State.CANCELLED,
                                    operation_id=op_id + ":" + digest(related.activity_id),
                                    observed_at=self.app.now())
                                self.app.life_runtime.cancel_activity(related.activity_id)
                    self.app.role_life.delete_activity(identifier, owner=self.owner)
                    result = {"removed": identifier}
                elif action in ("edit", "delete", "restore"):
                    old = self._journal(journal_target.key, operator, access)
                    if old is None or old["revision"] != revision:
                        raise TaskError("journal_revision_conflict", "journal changed or belongs to another scope")
                    if action == "delete":
                        self.app.role_journal.delete(journal_target, expected_revision=revision, operation_id=op_id + ":journal")
                    else:
                        if action == "edit":
                            original = self.app.role_journal.get(journal_target.key).draft
                            draft = replace(original, body=data["body"], afterglow=data.get("afterglow", original.afterglow),
                                            origin=JournalOrigin.MANUAL)
                        else:
                            draft = JournalDraft(journal_target, body=data["body"], afterglow=data.get("afterglow", ""),
                                origin=JournalOrigin.MANUAL,
                                recall=DreamRecall(data.get("recall", "fragments")) if journal_target.key.kind is JournalKind.DREAM else None)
                        method = self.app.role_journal.restore if action == "restore" else self.app.role_journal.save
                        method(draft, expected_revision=revision, operation_id=op_id + ":journal")
                else:
                    raise ValueError("unknown life action")
                require(self.operations.transition(op_id, expected_state=OperationState.ACCEPTED,
                    next_state=OperationState.COMPLETED, observed_at=self.app.now()))
            self.app.notify()
            return Outcome.success({"operation_id": op_id, **result})
        except TaskError as error:
            return Outcome.failed(error.code, str(error))
        except (KeyError, ValueError, TypeError) as error:
            return Outcome.failed(
                "life_command_invalid",
                f"invalid command or stale activity revision; refresh and check the selected record "
                f"({type(error).__name__}: {error})")
