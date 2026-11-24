"""Evidence-based interest projection; persistence and authorization live with callers."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
import math
import json
import unicodedata

from companion_v2.execution.models import digest


@dataclass(frozen=True)
class InterestEvidence:
    activity_id: str
    occurred_at: datetime
    reaction: str  # positive, negative, neutral, unknown
    wants_to_continue: bool
    autonomous_return: bool

    def __post_init__(self):
        if not self.activity_id or self.occurred_at.utcoffset() is None:
            raise ValueError("evidence needs an activity and an aware timestamp")
        if self.reaction not in {"positive", "negative", "neutral", "unknown"}:
            raise ValueError("invalid reaction")
        if type(self.wants_to_continue) is not bool or type(self.autonomous_return) is not bool:
            raise ValueError("interest flags must be boolean")


def project_interest(evidence, now, *, paused=False):
    """Project one interest from independent activities without inventing feedback."""
    if now.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    unique = {}
    for item in evidence:
        if not isinstance(item, InterestEvidence) or item.occurred_at > now:
            raise ValueError("invalid interest evidence")
        previous = unique.setdefault(item.activity_id, item)
        if previous != item:
            raise ValueError("conflicting evidence for one activity")
    items = sorted(unique.values(), key=lambda item: (item.occurred_at, item.activity_id))
    if not items:
        return {"status": "paused" if paused else "curious", "affinity": None,
                "confidence": 0.0, "salience": 0.0, "last_engaged_at": None}
    affinity = None
    for item in items:
        if item.reaction in {"positive", "negative"}:
            target = 1.0 if item.reaction == "positive" else 0.0
            affinity = target if affinity is None else 0.7 * affinity + 0.3 * target
    positive = [item for item in items if item.reaction == "positive"]
    days = {item.occurred_at.date() for item in positive}
    span = positive[-1].occurred_at - positive[0].occurred_at if positive else timedelta(0)
    if (len(positive) >= 4 and len(days) >= 4 and span >= timedelta(days=14)
            and any(item.autonomous_return for item in positive[1:])):
        status = "established"
    elif len(positive) >= 2 and len(days) >= 2 and any(item.wants_to_continue for item in positive):
        status = "active"
    else:
        status = "curious"
    half_life = 90 if status == "established" else 14 if status == "active" else 7
    age_days = (now - items[-1].occurred_at).total_seconds() / 86400
    salience = math.pow(2, -age_days / half_life)
    # Repeated explicit negative reactions can put an interest aside; silence cannot.
    explicit = [item.reaction for item in items if item.reaction in {"positive", "negative"}]
    if salience < 0.1 or explicit[-2:] == ["negative", "negative"]:
        status = "dormant"
    if paused:
        status = "paused"
    return {"status": status, "affinity": None if affinity is None else round(affinity, 3),
            "confidence": round(min(1.0, len({item.occurred_at.date() for item in items if item.reaction in {"positive", "negative"}}) / 4), 3),
            "salience": round(salience, 3), "last_engaged_at": items[-1].occurred_at.isoformat()}


def scope_key(context):
    return digest([context.scope, context.role_binding.role.subject_id, context.actor.subject])


class InterestStore:
    """Store observations once per completed activity within its private scope."""

    def __init__(self, runtime):
        self.runtime = runtime

    def processed(self, context, activity_id):
        return bool(self.runtime.read_one("SELECT 1 FROM role_interest_processed WHERE scope_key = ? AND activity_id = ?",
                                          (scope_key(context), activity_id)))

    def apply(self, context, activity, observations, now, *, autonomous_names=frozenset()):
        if activity.scope != context.scope or activity.role_id != context.role_binding.role.subject_id:
            raise ValueError("interest evidence does not belong to this role and scope")
        if activity.state.value != "completed" or activity.updated_at > now:
            raise ValueError("interest evidence must be a completed activity")
        if not isinstance(observations, list) or len(observations) > 3:
            raise ValueError("invalid observations")
        parsed = []
        for item in observations:
            if not isinstance(item, dict) or set(item) != {"name", "reaction", "wants_to_continue", "reason", "next_question"}:
                raise ValueError("invalid observation fields")
            name = item["name"]
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 50:
                raise ValueError("invalid interest name")
            if item["reaction"] not in {"positive", "negative", "neutral", "unknown"} or type(item["wants_to_continue"]) is not bool:
                raise ValueError("invalid interest reaction")
            if any(not isinstance(item[k], str) or len(item[k]) > 200 for k in ("reason", "next_question")):
                raise ValueError("invalid interest explanation")
            parsed.append({**item, "name": unicodedata.normalize("NFKC", name).strip()})
        key = scope_key(context)
        with self.runtime.transaction() as connection:
            if connection.execute("SELECT 1 FROM role_interest_processed WHERE scope_key = ? AND activity_id = ?",
                                  (key, activity.activity_id)).fetchone():
                return False
            for item in parsed:
                identifier = digest([key, item["name"].casefold()])
                existing = connection.execute("SELECT interest_id, name, aliases_json FROM role_interests WHERE scope_key = ?", (key,)).fetchall()
                match = next((r for r in existing if item["name"].casefold() in
                    {unicodedata.normalize("NFKC", name).casefold() for name in [r["name"], *json.loads(r["aliases_json"])]}), None)
                if match:
                    identifier = match["interest_id"]
                connection.execute("""INSERT OR IGNORE INTO role_interests
                    (scope_key, interest_id, name, aliases_json, summary, next_question, paused, revision)
                    VALUES (?, ?, ?, '[]', ?, ?, 0, 1)""",
                    (key, identifier, item["name"], item["reason"], item["next_question"]))
                inserted = connection.execute("""INSERT OR IGNORE INTO role_interest_evidence
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (key, identifier, activity.activity_id,
                    activity.updated_at.isoformat(), item["reaction"], int(item["wants_to_continue"]),
                    int(item["name"] in autonomous_names), item["reason"], item["next_question"]))
                if inserted.rowcount:
                    connection.execute("""UPDATE role_interests SET summary = ?, next_question = ?, revision = revision + 1
                        WHERE scope_key = ? AND interest_id = ? AND paused = 0""",
                        (item["reason"], item["next_question"], key, identifier))
            connection.execute("INSERT INTO role_interest_processed VALUES (?, ?, ?)",
                               (key, activity.activity_id, now.isoformat()))
        return True

    def list(self, context, now, *, visible, visible_contact=None, timezone="Asia/Shanghai", exposure=None,
             compact=False, interest_id=None):
        from .exposure import ExposureStore, familiarity
        key = scope_key(context)
        contact_visible = visible_contact or visible
        exposure = exposure or ExposureStore(self.runtime)
        rows = self.runtime.read_all("""SELECT * FROM role_interests WHERE scope_key = ?
            AND (? IS NULL OR interest_id = ?) ORDER BY name""", (key, interest_id, interest_id))
        reader = exposure.life_reader() if rows else None
        contacts_by_topic = {}
        if reader is not None:
            # Host topic links are authoritative, but receipt bodies still come
            # through the live provider and its identity/visibility validation.
            references = self.runtime.read_all("""SELECT t.activity_id,t.platform,t.content_id,t.interest_id
                FROM role_exposure_topics t JOIN role_interests i
                  ON i.scope_key=t.scope_key AND i.interest_id=t.interest_id
                WHERE t.scope_key=? AND (? IS NULL OR t.interest_id=?)""", (key, interest_id, interest_id))
            for reference in references:
                contact = reader.exposure(context, reference["activity_id"], reference["platform"], reference["content_id"])
                if contact and contact["exposed_at"] and exposure.visible(context, contact["activity_id"]):
                    contacts_by_topic.setdefault(reference["interest_id"], []).append(contact)
            for contacts in contacts_by_topic.values():
                contacts.sort(key=lambda contact: contact["exposed_at"])
        result = []
        for row in rows:
            records = self.runtime.read_all("""SELECT * FROM role_interest_evidence
                WHERE scope_key = ? AND interest_id = ? ORDER BY occurred_at, activity_id""", (key, row["interest_id"]))
            records = [r for r in records if datetime.fromisoformat(r["occurred_at"]) <= now and visible(r["activity_id"])]
            topic_contacts = (contacts_by_topic.get(row["interest_id"], []) if reader is not None
                              else exposure.topic_rows(context, row["interest_id"]))
            contacts = [r for r in topic_contacts
                        if datetime.fromisoformat(r["exposed_at"]) <= now and contact_visible(r["activity_id"])]
            if exposure.life_reader() is not None:
                references = self.runtime.read_all("""SELECT * FROM role_exposure_feedback WHERE scope_key=?
                    AND target_kind='topic' AND target_id=? ORDER BY occurred_at""", (key, row["interest_id"]))
                reactions = []
                for reference in references:
                    contact = exposure.exposure(context, reference["activity_id"], reference["platform"], reference["content_id"])
                    if contact is not None and contact["exposed_at"] is not None:
                        reactions.append({**dict(reference), "exposed_at": contact["exposed_at"]})
            else:
                reactions = self.runtime.read_all("""SELECT f.*, e.exposed_at FROM role_exposure_feedback f
                    JOIN role_exposures e ON e.scope_key=f.scope_key AND e.activity_id=f.activity_id
                        AND e.platform=f.platform AND e.content_id=f.content_id
                    WHERE f.scope_key=? AND f.target_kind='topic' AND f.target_id=?
                    ORDER BY f.occurred_at""", (key, row["interest_id"]))
            all_reactions = [r for r in reactions if contact_visible(r["activity_id"]) and
                             datetime.fromisoformat(r["occurred_at"]) <= now]
            reactions = [r for r in all_reactions if r["active"]]
            if not records and not contacts:
                continue
            evidence = [InterestEvidence(r["activity_id"], datetime.fromisoformat(r["occurred_at"]),
                         r["reaction"], bool(r["wants_to_continue"]), bool(r["autonomous_return"])) for r in records]
            known_activities = {r.activity_id for r in evidence}
            for reaction in reactions:
                if reaction["activity_id"] in known_activities:
                    continue
                is_return = bool(reaction["autonomous"]) and any(
                    c["activity_id"] != reaction["activity_id"] and c["exposed_at"] < reaction["exposed_at"] for c in contacts)
                evidence.append(InterestEvidence(reaction["activity_id"], datetime.fromisoformat(reaction["occurred_at"]),
                    reaction["reaction"], bool(reaction["wants_continue"]), is_return))
                known_activities.add(reaction["activity_id"])
            projection = project_interest(evidence, now, paused=bool(row["paused"]))
            negative = [r for r in reactions if r["reaction"] == "negative"]
            if negative and not row["paused"]:
                projection["status"] = "disliked"
            elif not evidence and not row["paused"]:
                projection["status"] = "exposed"
            coefficient = self.runtime.read_one("SELECT coefficient,assessed FROM role_interest_coefficients WHERE scope_key=? AND interest_id=?",
                                                (key, row["interest_id"]))
            score, exposure_count, content_count = familiarity(contacts, now, coefficient["coefficient"] if coefficient else 1.0, timezone)
            summary = (negative[-1]["reason"] if negative else reactions[-1]["reason"] if reactions else
                       records[-1]["reason"] if records else "")
            entries = [] if compact else [{"activity_id": r["activity_id"], "reason": r["reason"], "occurred_at": r["occurred_at"], "source": "旧版活动"} for r in records]
            entries.extend({"activity_id": r["activity_id"], "reason": r["reason"], "occurred_at": r["occurred_at"],
                            "source": r["platform"], "reaction": r["reaction"], "active": bool(r["active"])} for r in ([] if compact else all_reactions))
            result.append({"interest_id": row["interest_id"], "name": row["name"], "summary": summary,
                           "next_question": records[-1]["next_question"] if records else "", "revision": row["revision"],
                           "evidence": sorted(entries, key=lambda item: item["occurred_at"]),
                           "familiarity": score, "exposure_count": exposure_count, "content_count": content_count,
                           "coefficient": coefficient["coefficient"] if coefficient else 1.0,
                           "contacts": [{"activity_id": x["activity_id"], "platform": x["platform"],
                                         "title": x["title"], "source_url": x["source_url"],
                                         "occurred_at": x["exposed_at"], "coverage": x["coverage"]} for x in ([] if compact else contacts[-8:])],
                           **projection})
            if compact:
                result[-1].pop("evidence")
                result[-1].pop("contacts")
                result[-1]["compact"] = True
        return result


def visible_interests(app, context, now, **options):
    return visible_interests_for(app.role_life, app.role_journal, app.runtime, context, now, **options)


def visible_interests_for(life, journal, runtime, context, now, *, activities=None, source_check=None,
                          compact=False, interest_id=None):
    from companion_v2.foundation.refs import RoleOwnerRef
    from companion_v2.execution.models import TaskError
    owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
    context_key = context.key
    def activity_for(activity_id):
        # A management snapshot already scanned this owner's complete ledger.
        return activities[activity_id] if activities is not None else life.activity(activity_id, owner=owner)
    @cache
    def visible(activity_id):
        try:
            activity = activity_for(activity_id)
            if (activity.scope != context.scope or activity.state.value != "completed"
                    or not activity.work or activity.work.context_key != context_key):
                return False
            if activity.reading:
                if source_check is not None:
                    return source_check(activity.reading.source).is_success
                return journal.sources.validate_reference_for_audience(activity.reading.source,
                    recipient=context.actor.subject, observed_at=now).is_success
            return bool(activity.work and activity.work.kind == "social")
        except (KeyError, ValueError, TaskError):
            return False
    @cache
    def visible_contact(activity_id):
        try:
            activity = activity_for(activity_id)
            return bool(activity.scope == context.scope and activity.work and activity.work.kind == "social"
                        and activity.work.context_key == context_key)
        except (KeyError, ValueError, TaskError):
            return False
    try:
        from .life.configuration import LifeConfigurationOwner, configuration_fields, target_for, values
        zone = values(LifeConfigurationOwner(runtime).read(target_for(context), configuration_fields()))["timezone"]
    except (KeyError, ValueError, TypeError):
        zone = "Asia/Shanghai"
    from .exposure import ExposureStore
    reader = getattr(life, "browsing_reader", None)
    exposure = ExposureStore(runtime, visible=lambda _context, activity_id: visible_contact(activity_id),
                             session_reader=reader.for_projection() if reader is not None else None, life=life)
    return InterestStore(runtime).list(context, now, visible=visible, visible_contact=visible_contact, timezone=zone,
                                       exposure=exposure, compact=compact, interest_id=interest_id)


def interest_materials(app, context, now):
    import json
    from companion_v2.conversation.foreground import PromptMaterial
    from companion_v2.foundation.refs import RoleOwnerRef
    owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
    items = [item for item in visible_interests(app, context, now)
             if item["status"] not in ("paused", "dormant", "exposed")]
    items.sort(key=lambda item: item["salience"], reverse=True)
    result = []
    for item in items[:4]:
        current_evidence = [entry for entry in item["evidence"] if entry.get("active", True)]
        if not current_evidence:
            continue
        latest = current_evidence[-1]
        activity = app.role_life.activity(latest["activity_id"], owner=owner)
        reference = activity.reading.source if activity.reading else None
        payload = {key: item[key] for key in ("name", "status", "summary", "next_question")}
        result.append(PromptMaterial("role.interests", "interest_context", json.dumps(payload, ensure_ascii=False), reference))
    return tuple(result)
