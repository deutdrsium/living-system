"""Authorized manual academics routes for the paired life page.

Keep the life service behind its JSON boundary: the host supplies identity and
clock values, and releases its controller lock before entering life storage.
"""

from datetime import date
import asyncio
import json
import math

from astrbot.api.web import request
from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome

from .life_api import LifeWebAPI
from .web_api import outcome_response


MAX_BODY_BYTES = 256 * 1024
# Frozen error vocabulary, without importing the paired plugin's implementation.
ERROR_CODES = frozenset({"invalid_config", "missing_dependency", "unauthorized", "stale_version",
    "insufficient_resource", "no_time_window", "source_unavailable", "provider_unavailable",
    "result_unknown", "cancelled"})
COURSE = dict.fromkeys(("course_id", "name", "kind", "credits", "teacher_ref"))
EXCEPTION = dict.fromkeys(("date", "cancelled", "start_time", "end_time", "place_id"))
TERM = {**dict.fromkeys(("term_id", "academic_year", "timezone", "start_date", "end_date", "source")),
    "week_starts": [None], "holidays": [None], "makeup_days": [dict.fromkeys(("date", "teaching_date"))]}
ENTRY = {**dict.fromkeys(("slot_id", "term_id", "weekday", "start_time", "end_time", "week_rule", "place_id", "source")),
    "course": COURSE, "weeks": [None], "exceptions": [EXCEPTION]}
SOURCE_REF = dict.fromkeys(("source_id", "version", "content_digest", "fragment_start", "fragment_end",
    "retrieved_at", "collected_at", "permission_ref"))
MATERIAL = {"source_ref": SOURCE_REF, "audience_bound": {"permission_ref": None, "context_keys": [str]}}
SYLLABUS = {**dict.fromkeys(("unit_id", "term_id", "course_id", "week", "topic", "source")),
    "concepts": [str], "materials": [MATERIAL], "questions": [str], "assignment_ids": [str]}
ASSIGNMENT = dict.fromkeys(("assignment_id", "term_id", "course_id", "unit_id", "title", "due_at",
    "chunk_work_s", "estimated_work_s", "source"))
RECORDS = {"term": (TERM, "term_id"), "timetable": (ENTRY, "slot_id"),
    "syllabus": (SYLLABUS, "unit_id"), "assignment": (ASSIGNMENT, "assignment_id"),
    # Update-only: rewrites the shared course metadata of every slot of one term course at once.
    "course": ({"term_id": None, **COURSE}, "course_id")}
METADATA = dict.fromkeys(("schema_version", "revision"))
RECEIPT = {**METADATA, **dict.fromkeys(("kind", "id", "deleted"))}
ARTIFACT = dict.fromkeys(("schema_version", "generator_version", "artifact_id", "assignment_id", "chunk_id",
    "chunk_index", "kind", "title", "revision", "updated_at", "source", "availability"))
ARTIFACT_READS = frozenset({"study_artifacts", "study_artifact"})
READS = {
    "study_artifacts": {"schema_version": None, "items": [ARTIFACT], "next_before": None},
    "study_artifact": {**ARTIFACT, "text": None, "next_step": None},
    "term": {**METADATA, "terms": [{**TERM, **METADATA}]},
    "timetable": {**METADATA, "term_id": None, "entries": [{**ENTRY, **METADATA}]},
    "occurrences": {**METADATA, **dict.fromkeys(("term_id", "timezone", "start", "end")),
        "items": [dict.fromkeys(("session_id", "slot_id", "term_id", "course_id", "course_name",
            "start_at", "end_at", "local_date", "place_id", "revision", "cancelled", "source"))]},
    "syllabus": {**METADATA, "term_id": None, "course_id": None, "units": [{**SYLLABUS, **METADATA}]},
    "assignments": {**METADATA, "term_id": None, "course_id": None, "assignments": [{**ASSIGNMENT, **METADATA}]},
    "study_progress": {**METADATA, "term_id": None, "course_id": None,
        "items": [{**dict.fromkeys(("assignment_id", "chunk_id", "chunk_index", "due_at", "required_work_s",
            "completed_work_s", "remaining_work_s", "status", "revision", "provenance")), "evaluation": str}]},
}
PLACES = {"classes_enabled": None, "graph": dict.fromkeys(("graph_id", "version")),
    "places": [dict.fromkeys(("place_id", "name", "type"))]}


def _shape(value, schema, *, strict):
    """Reject unknown input keys; recursively trim output to the same field shapes."""
    if type(schema) is dict:
        if type(value) is not dict or (strict and value.keys() != schema.keys()):
            raise ValueError("invalid object fields")
        return {key: _shape(value[key], child, strict=strict) for key, child in schema.items() if key in value}
    if type(schema) is list:
        if type(value) is not list:
            raise ValueError("expected an array")
        return [_shape(item, schema[0], strict=strict) for item in value]
    if schema is str and type(value) is not str:
        raise ValueError("expected a JSON string")
    if type(value) not in (str, int, float, bool, type(None)) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("expected a JSON scalar")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("expected finite JSON numbers")


def _query(kind, artifact_id=None):
    fields = {"term": set(), "timetable": {"term_id"}, "occurrences": {"term_id", "start", "end"},
        "syllabus": {"term_id", "course_id"}, "assignments": {"term_id", "course_id"},
        "study_progress": {"term_id", "course_id"}, "study_artifacts": {"assignment_id"},
        "study_artifact": set()}[kind]
    query = request.query
    optional = {"limit", "before"} if kind == "study_artifacts" else set()
    if not fields <= set(query.keys()) <= fields | optional or any(len(query.getlist(key)) != 1 for key in query.keys()):
        raise ValueError("unknown, missing or repeated query fields")
    result = dict(query)
    if kind == "study_artifact":
        result["artifact_id"] = artifact_id
        if type(artifact_id) is not str:
            raise ValueError("provide an artifact id")
    for key in ("assignment_id", "before", "artifact_id"):
        if key in result and (not 1 <= len(result[key]) <= 256 or result[key] != result[key].strip()):
            raise ValueError("invalid study artifact identifier")
    if "limit" in result:
        raw = result["limit"]
        if not (1 <= len(raw) <= 2 and raw.isascii() and raw.isdecimal() and 1 <= int(raw) <= 50):
            raise ValueError("invalid study artifact limit")
        result["limit"] = int(raw)
    for key in ("term_id", "course_id"):
        if key in result and not result[key].strip():
            raise ValueError("provide a " + key)
    if kind == "occurrences":
        first, last = (_day(result[key]) for key in ("start", "end"))
        if not 0 < (last - first).days <= 31:
            raise ValueError("query one to 31 local dates; end is exclusive")
    return result


def _day(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError("provide YYYY-MM-DD dates")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("provide YYYY-MM-DD dates")
    return result


def _command(raw):
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("command too large")
    body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant)
    if type(body) is not dict:
        raise ValueError("expected a command object")
    action, kind = body.get("action"), body.get("kind")
    if action not in ("create", "update", "delete") or kind not in RECORDS or (kind == "course" and action != "update"):
        raise ValueError("invalid academics command")
    fields = {"command_id", "expected_revision", "action", "kind", "id"}
    if action != "delete":
        fields.add("record")
    if set(body) != fields:
        raise ValueError("invalid command fields")
    if (type(body["expected_revision"]) is not int or body["expected_revision"] < 0
            or any(type(body[key]) is not str or not body[key].strip() for key in ("command_id", "id"))):
        raise ValueError("invalid command identity or revision")
    if action != "delete":
        schema, id_field = RECORDS[kind]
        _shape(body["record"], schema, strict=True)
        if body["record"][id_field] != body["id"]:
            raise ValueError("record id must match command id")
    return body


def _failure(error):
    code = getattr(error, "code", None)
    if type(code) is not str or code not in ERROR_CODES:
        return outcome_response(Outcome.failed("invalid_config", "invalid academics request"))
    return outcome_response(Outcome.failed(code, str(error)))


class AcademicsRoutes:
    def __init__(self, controller, service, *, artifacts=None):
        self.controller, self.service = controller, service
        self.artifacts = artifacts

    async def _run(self, kind, *, query=None, body=None, ports=None):
        async with self.controller._control_lock:
            manager, operator = LifeWebAPI(self.controller).manager()
            manager.authorize(operator)
            owner_key, context_key = digest(manager.owner), manager.context.key
            now = manager.app.now() if body is not None else None
        # Do not hold a host lock while the paired service takes its storage lock.
        if body is None and kind in ARTIFACT_READS:
            artifacts = self.artifacts or getattr(self.service, "study_artifacts", None)
            if artifacts is None:
                return outcome_response(Outcome.failed("missing_dependency", "study artifacts are disabled"))
            result = await artifacts.read(owner_key, context_key, kind, query, ports=ports)
        elif self.service is None:
            return outcome_response(Outcome.failed("missing_dependency", "academics is disabled"))
        elif body is not None:
            result = await asyncio.to_thread(self.service.command, owner_key, context_key, body, now=now)
        else:
            result = await asyncio.to_thread(self.service.read, owner_key, context_key, kind, query)
        return outcome_response(Outcome.success(_shape(result, RECEIPT if body is not None else READS[kind], strict=False)))

    async def read(self, kind, *, artifact_id=None, ports=None):
        try:
            return await self._run(kind, query=_query(kind, artifact_id), ports=ports)
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
            return _failure(error)

    async def places(self, reader):
        """Course page setup: whether academics/classes run, and campus place options.

        Answers even while academics is disabled so the page can say so plainly.
        """
        try:
            if request.query:
                raise ValueError("place options do not accept query fields")
            async with self.controller._control_lock:
                manager, operator = LifeWebAPI(self.controller).manager()
                manager.authorize(operator)
            data = {"classes_enabled": False, "graph": None, "places": []}
            if callable(reader):
                data = await asyncio.to_thread(reader)
            if (type(data.get("classes_enabled")) is not bool or not (data.get("graph") is None or type(data["graph"]) is dict)
                    or type(data.get("places")) is not list):
                raise ValueError("invalid place options")
            graph = data["graph"]
            result = _shape({**data, "graph": {}}, PLACES, strict=False)
            result["graph"] = None if graph is None else _shape(graph, PLACES["graph"], strict=False)
            result["academics_enabled"] = self.service is not None
            result["classes_enabled"] = result["classes_enabled"] and self.service is not None
            return outcome_response(Outcome.success(result))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, AttributeError) as error:
            return _failure(error)

    async def command(self):
        try:
            if request.query:
                raise ValueError("commands do not accept query fields")
            body = _command(await request.body())
            return await self._run(body["kind"], body=body)
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
            return _failure(error)
