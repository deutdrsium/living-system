"""Academics JSON boundary, paired dispatch and proxy routes (SDK or unittest fixtures)."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "plugins/astrbot_plugin_private_companion"
LIFE = ROOT / "plugins/astrbot_plugin_good_life"
sys.path[:0] = [str(HOST), str(LIFE)]

from companion_v2.execution.models import TaskError, digest

HAS_SDK = importlib.util.find_spec("astrbot") is not None
if HAS_SDK:
    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.requests import Request
    from astrbot_host import academics_routes as routes
    from astrbot_host import life_dashboard as dashboard
    from good_life.web_api import LifePageAPI, PREFIX
else:
    # Isolate the fallback modules so collecting this test cannot install a fake
    # SDK or fake host implementation into other tests' import namespaces.
    _current = ContextVar("academics_test_request")

    class RequestFixture:
        def __getattr__(self, name):
            return getattr(_current.get(), name)

    class QueryFixture:
        def __init__(self, value):
            self.pairs = parse_qsl(value, keep_blank_values=True)

        def keys(self):
            return dict(self.pairs).keys()

        def getlist(self, key):
            return [value for name, value in self.pairs if name == key]

        def __getitem__(self, key):
            return self.getlist(key)[-1]

        def __bool__(self):
            return bool(self.pairs)

    def _module(name, **values):
        result = ModuleType(name)
        result.__dict__.update(values)
        return result

    def _response(result):
        if result.failure:
            failure = result.failure
            return {"status": "error", "message": failure.code, "reason": failure.reason, "data": {"code": failure.code}}
        return {"status": "ok", "message": "", "data": result.unwrap()}

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        result = importlib.util.module_from_spec(spec)
        sys.modules[name] = result
        spec.loader.exec_module(result)
        return result

    class ManagerFixture:
        def __init__(self, controller):
            self.controller = controller

        def manager(self):
            raise AssertionError("provide a trusted manager in each test")

    package = "_academics_route_fixture"
    dependencies = {
        "astrbot": _module("astrbot", __path__=[]),
        "astrbot.api": _module("astrbot.api", __path__=[]),
        "astrbot.api.web": _module("astrbot.api.web", request=RequestFixture()),
        package: _module(package, __path__=[]),
        package + ".life_api": _module(package + ".life_api", LifeWebAPI=ManagerFixture),
        package + ".web_api": _module(package + ".web_api", outcome_response=_response),
        package + ".life_page_reads": _module(package + ".life_page_reads", LifePageReads=object, life_page_data=lambda data: data),
        package + ".multi_api": _module(package + ".multi_api", TargetControllerView=object),
        package + ".page_api": _module(package + ".page_api", PageWebAPI=object),
        package + ".state_api": _module(package + ".state_api", StateWebAPI=object),
    }
    with patch.dict(sys.modules, dependencies):
        routes = _load(package + ".academics_routes", HOST / "astrbot_host/academics_routes.py")
        dashboard = _load(package + ".life_dashboard", HOST / "astrbot_host/life_dashboard.py")
        proxy = _load(package + ".proxy", LIFE / "good_life/web_api.py")
    LifePageAPI, PREFIX = proxy.LifePageAPI, proxy.PREFIX


NOW = datetime(2026, 11, 17, 8, tzinfo=timezone.utc)
TERM = {"term_id": "fall", "academic_year": "2026-2027", "timezone": "Asia/Shanghai",
    "start_date": "2026-09-01", "end_date": "2027-01-31", "week_starts": ["2026-09-07"],
    "holidays": ["2026-10-01"], "makeup_days": [{"date": "2026-10-10", "teaching_date": "2026-10-08"}], "source": "manual"}
ENTRY = {"slot_id": "ethics", "term_id": "fall", "course": {"course_id": "ethics", "name": "伦理学",
    "kind": "required", "credits": 2.5, "teacher_ref": None}, "weekday": 1, "start_time": "09:00", "end_time": "10:00",
    "weeks": [1, 2, 3], "week_rule": "all", "place_id": "classroom", "exceptions": [{"date": "2026-09-14",
    "cancelled": False, "start_time": "10:00", "end_time": "11:00", "place_id": None}], "source": "manual"}
OCCURRENCE = {"session_id": "session", "slot_id": "ethics", "term_id": "fall", "course_id": "ethics",
    "course_name": "伦理学", "start_at": "2026-09-07T09:00:00+08:00", "end_at": "2026-09-07T10:00:00+08:00",
    "local_date": "2026-09-07", "place_id": "classroom", "revision": 1, "cancelled": False, "source": "manual"}
SYLLABUS = {"unit_id": "unit-1", "term_id": "fall", "course_id": "ethics", "week": 1,
    "topic": "功利主义", "concepts": ["理由与反例"], "materials": [{"source_ref": {
        "source_id": "textbook", "version": "1", "content_digest": "sha256:chapter-2",
        "fragment_start": 0, "fragment_end": 1200, "retrieved_at": "2026-11-17T08:00:00Z",
        "collected_at": "2026-11-17T08:00:00Z", "permission_ref": "permission-1"},
        "audience_bound": {"permission_ref": "permission-1", "context_keys": ["trusted-context"]}}],
    "questions": ["如何评价反例？"], "assignment_ids": ["assignment-1"], "source": "manual"}
ASSIGNMENT = {"assignment_id": "assignment-1", "term_id": "fall", "course_id": "ethics", "unit_id": "unit-1",
    "title": "讨论反例", "due_at": "2026-11-20T08:00:00Z", "chunk_work_s": 1500,
    "estimated_work_s": 3000, "source": "manual"}
PROGRESS = {"assignment_id": "assignment-1", "chunk_id": "chunk-1", "chunk_index": 0, "due_at": ASSIGNMENT["due_at"],
    "required_work_s": 1500, "completed_work_s": 900, "remaining_work_s": 600, "status": "in_progress",
    "revision": 3, "evaluation": "simulated", "provenance": "simulated"}
STUDY_KINDS = ("syllabus", "assignments", "study_progress")
STUDY_QUERY = "term_id=fall&course_id=ethics"


def command(kind="term", action="create", revision=0):
    records = {"term": (TERM, "term_id"), "timetable": (ENTRY, "slot_id"),
               "syllabus": (SYLLABUS, "unit_id"), "assignment": (ASSIGNMENT, "assignment_id"),
               "course": ({"term_id": "fall", **ENTRY["course"]}, "course_id")}
    record, id_field = records[kind]
    result = {"command_id": "manual-1", "expected_revision": revision, "action": action,
              "kind": kind, "id": record[id_field]}
    if action != "delete":
        result["record"] = deepcopy(record)
    return result


@contextmanager
def request_context(*, query="", raw=b"", username="admin", plugin_name="astrbot_plugin_good_life"):
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    if HAS_SDK:
        native = Request({"type": "http", "method": "POST" if raw else "GET", "path": "/",
            "headers": [(b"content-type", b"application/json")], "query_string": query.encode()}, receive=receive)
        with bind_request_context(PluginRequest(native, username=username, plugin_name=plugin_name)):
            yield
    else:
        async def body():
            return raw
        token = _current.set(SimpleNamespace(query=QueryFixture(query), body=body,
                                             username=username, plugin_name=plugin_name))
        try:
            yield
        finally:
            _current.reset(token)


class ServiceFixture:
    def __init__(self, case):
        self.case, self.calls, self.error = case, [], None
        self.responses = {
            "term": {"schema_version": 1, "revision": 0, "terms": []},
            "timetable": {"schema_version": 1, "revision": 0, "term_id": "fall", "entries": []},
            "occurrences": {"schema_version": 1, "revision": 0, "term_id": "fall", "timezone": "Asia/Shanghai",
                            "start": "2026-09-01", "end": "2026-10-01", "items": []},
            "syllabus": {"schema_version": 1, "revision": 0, "term_id": "fall", "course_id": "ethics", "units": []},
            "assignments": {"schema_version": 1, "revision": 0, "term_id": "fall", "course_id": "ethics", "assignments": []},
            "study_progress": {"schema_version": 1, "revision": 0, "term_id": "fall", "course_id": "ethics", "items": []},
        }

    def _check(self):
        self.case.assertFalse(self.case.controller._control_lock.locked(), "service called under the host lock")
        self.case.assertTrue(self.case.authorizations)
        if self.error:
            raise self.error

    def read(self, owner_key, context_key, kind, query):
        self._check()
        self.calls.append(("read", owner_key, context_key, kind, query))
        return self.responses[kind]

    def command(self, owner_key, context_key, body, *, now):
        self._check()
        self.calls.append(("command", owner_key, context_key, body, now))
        return {"schema_version": 1, "revision": 1, "kind": body["kind"], "id": body["id"],
                "deleted": body["action"] == "delete", "owner_key": "private-marker"}


class AcademicsRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = SimpleNamespace(_control_lock=asyncio.Lock())
        self.authorizations = []
        self.owner = {"installation_id": "installation", "bot_id": "bot", "role_id": "role-a"}
        self.manager = SimpleNamespace(owner=self.owner, context=SimpleNamespace(key="trusted-context"),
                                       app=SimpleNamespace(now=lambda: NOW), authorize=self.authorize)
        self.service = ServiceFixture(self)
        self.api = routes.AcademicsRoutes(self.controller, self.service)
        self.manager_patch = patch.object(routes.LifeWebAPI, "manager", lambda _api: (self.manager, "trusted-operator"))
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)

    def authorize(self, operator):
        self.assertTrue(self.controller._control_lock.locked())
        self.authorizations.append(operator)

    async def call(self, handler=None, *, kind="term", body=None, raw=None, query="", **identity):
        handler = handler or (self.api.command if body is not None or raw is not None else lambda: self.api.read(kind))
        if raw is None:
            raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
        with request_context(query=query, raw=raw, **identity):
            return await handler()

    def ok(self, result):
        self.assertEqual(result["status"], "ok", result)
        return result["data"]

    async def test_reads_use_trusted_scope_after_authorization_and_release_host_lock(self):
        for kind, query, expected in (("term", "", {}), ("timetable", "term_id=fall", {"term_id": "fall"}),
            ("occurrences", "term_id=fall&start=2026-09-01&end=2026-10-02",
             {"term_id": "fall", "start": "2026-09-01", "end": "2026-10-02"})):
            self.ok(await self.call(kind=kind, query=query))
            self.assertEqual(self.service.calls[-1], ("read", digest(self.owner), "trusted-context", kind, expected))
        self.assertEqual(self.authorizations, ["trusted-operator"] * 3)

    async def test_create_update_delete_accept_only_records_and_return_receipt_whitelist(self):
        for kind in ("term", "timetable", "syllabus", "assignment"):
            for action in ("create", "update", "delete"):
                body = command(kind, action)
                result = self.ok(await self.call(body=body))
                self.assertEqual(result, {"schema_version": 1, "revision": 1, "kind": kind,
                                          "id": body["id"], "deleted": action == "delete"})
                self.assertEqual(self.service.calls[-1], ("command", digest(self.owner), "trusted-context", body, NOW))

    async def test_read_responses_recursively_trim_private_fields_without_mutation(self):
        term, entry = deepcopy(TERM), deepcopy(ENTRY)
        term.update(schema_version=1, revision=2, owner_key="private-marker")
        term["makeup_days"][0]["internal"] = "private-marker"
        entry.update(schema_version=1, revision=2, context_key="private-marker")
        entry["course"]["internal"] = "private-marker"
        entry["exceptions"][0]["internal"] = "private-marker"
        self.service.responses["term"].update(terms=[term], internal="private-marker")
        self.service.responses["timetable"].update(entries=[entry], internal="private-marker")
        self.service.responses["occurrences"].update(items=[OCCURRENCE | {"internal": "private-marker"}], internal="private-marker")
        for kind, query in (("term", ""), ("timetable", "term_id=fall"),
            ("occurrences", "term_id=fall&start=2026-09-01&end=2026-10-01")):
            result = self.ok(await self.call(kind=kind, query=query))
            self.assertNotIn("private-marker", json.dumps(result))
            self.assertIn("private-marker", json.dumps(self.service.responses[kind]))
        self.assertEqual(result["items"], [OCCURRENCE])
        self.assertEqual(self.ok(await self.call())["terms"], [TERM | {"schema_version": 1, "revision": 2}])

    async def test_study_reads_authorize_trusted_scope_and_trim_nested_private_fields(self):
        unit, assignment, progress = deepcopy(SYLLABUS), deepcopy(ASSIGNMENT), deepcopy(PROGRESS)
        unit.update(schema_version=1, revision=2)
        assignment.update(schema_version=1, revision=3)
        expected = {"syllabus": ("units", deepcopy(unit)), "assignments": ("assignments", deepcopy(assignment)),
                    "study_progress": ("items", deepcopy(progress))}
        unit["materials"][0]["source_ref"]["internal"] = "private-marker"
        unit["materials"][0]["audience_bound"]["owner_key"] = "private-marker"
        unit["materials"][0]["internal"] = "private-marker"
        for kind, field, record in (("syllabus", "units", unit), ("assignments", "assignments", assignment),
                                    ("study_progress", "items", progress)):
            record["internal"] = "private-marker"
            self.service.responses[kind].update({field: [record], "internal": "private-marker"})
            result = self.ok(await self.call(kind=kind, query=STUDY_QUERY))
            self.assertEqual(result[field], [expected[kind][1]])
            self.assertNotIn("private-marker", json.dumps(result))
            self.assertIn("private-marker", json.dumps(self.service.responses[kind]))
            self.assertEqual(self.service.calls[-1], ("read", digest(self.owner), "trusted-context", kind,
                                                      {"term_id": "fall", "course_id": "ethics"}))
        self.assertEqual(self.authorizations, ["trusted-operator"] * 3)

    async def test_study_queries_require_exact_single_nonblank_term_and_course(self):
        for kind in STUDY_KINDS:
            for query in ("", "term_id=fall", "course_id=ethics", "term_id=&course_id=ethics",
                          "term_id=fall&course_id=%20", STUDY_QUERY + "&course_id=other",
                          STUDY_QUERY + "&term_id=spring", STUDY_QUERY + "&owner_key=forged",
                          STUDY_QUERY + "&start=2026-11-17"):
                with self.subTest(kind=kind, query=query):
                    self.assertEqual((await self.call(kind=kind, query=query))["message"], "invalid_config")
        self.assertEqual(self.service.calls, [])

    async def test_study_commands_reject_unknown_missing_nested_fields_and_mismatched_ids(self):
        paths = ((), ("materials", 0), ("materials", 0, "source_ref"), ("materials", 0, "audience_bound"))
        bodies = []
        for kind in ("syllabus", "assignment"):
            for key in command(kind)["record"]:
                body = command(kind)
                del body["record"][key]
                bodies.append(body)
            bodies.extend((command(kind) | {"id": "other"}, command(kind) | {"owner_key": "forged"},
                           command(kind) | {"context_key": "forged"}, command(kind) | {"now": NOW.isoformat()},
                           command(kind) | {"expected_revision": True},
                           command(kind, "delete") | {"record": command(kind)["record"]}))
        for kind, path in (("syllabus", path) for path in paths):
            for mutation in ("extra", "missing"):
                body = command(kind)
                record = body["record"]
                for key in path:
                    record = record[key]
                if mutation == "extra":
                    record["internal"] = "forged"
                else:
                    del record[next(iter(record))]
                bodies.append(body)
        body = command("assignment")
        body["record"]["internal"] = "forged"
        bodies.append(body)
        for field in ("concepts", "questions", "assignment_ids"):
            body = command("syllabus")
            body["record"][field] = [{"internal": "forged"}]
            bodies.append(body)
        body = command("syllabus")
        body["record"]["materials"][0]["audience_bound"]["context_keys"] = [42]
        bodies.append(body)
        for kind in ("assignments", "study_progress"):
            bodies.append(command("assignment") | {"kind": kind})
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual((await self.call(body=body))["message"], "invalid_config")
        self.assertEqual(self.service.calls, [])

    async def test_study_progress_evaluation_must_be_a_string(self):
        for evaluation in ({"private": "private-marker"}, ["private-marker"], 1, True, None):
            self.service.responses["study_progress"]["items"] = [PROGRESS | {"evaluation": evaluation}]
            with self.subTest(evaluation=evaluation):
                self.assertEqual((await self.call(kind="study_progress", query=STUDY_QUERY))["message"], "invalid_config")
        self.service.responses["study_progress"]["items"] = [deepcopy(PROGRESS)]
        self.assertEqual(self.ok(await self.call(kind="study_progress", query=STUDY_QUERY))["items"], [PROGRESS])

    async def test_study_disabled_service_authorization_and_revision_errors(self):
        for kind in STUDY_KINDS:
            self.api.service = None
            self.assertEqual((await self.call(kind=kind, query=STUDY_QUERY))["message"], "missing_dependency")
        for kind in ("syllabus", "assignment"):
            self.assertEqual((await self.call(body=command(kind)))["message"], "missing_dependency")
        self.api.service = self.service
        error = ValueError("academic revision changed")
        error.code = "stale_version"
        self.service.error = error
        for kind in ("syllabus", "assignment"):
            self.assertEqual((await self.call(body=command(kind)))["message"], "stale_version")
        self.service.error = None
        def denied(_operator):
            raise TaskError("authorization_denied", "access denied")
        self.manager.authorize = denied
        for kind in STUDY_KINDS:
            self.assertEqual((await self.call(kind=kind, query=STUDY_QUERY))["message"], "authorization_denied")
        for kind in ("syllabus", "assignment"):
            self.assertEqual((await self.call(body=command(kind)))["message"], "authorization_denied")
        self.assertEqual(self.service.calls, [])

    async def test_response_scalar_fields_cannot_smuggle_objects(self):
        self.service.responses["term"]["revision"] = {"private": "private-marker"}
        self.assertEqual((await self.call())["message"], "invalid_config")

    async def test_queries_reject_unknown_repeated_missing_and_noncanonical_ranges(self):
        cases = {"term": ("term_id=fall", "owner_key=forged"),
            "timetable": ("", "term_id=", "term_id=fall&term_id=spring", "term_id=fall&owner_key=forged"),
            "occurrences": ("term_id=fall", "term_id=fall&start=20260901&end=2026-09-02",
                "term_id=fall&start=2026-09-31&end=2026-10-01", "term_id=fall&start=2026-09-01&end=2026-09-01",
                "term_id=fall&start=2026-09-02&end=2026-09-01", "term_id=fall&start=2026-09-01&end=2026-10-03",
                "term_id=fall&start=2026-09-01&start=2026-09-02&end=2026-09-03",
                "term_id=fall&start=2026-09-01&end=2026-09-03&timezone=UTC")}
        for kind, queries in cases.items():
            for query in queries:
                with self.subTest(kind=kind, query=query):
                    self.assertEqual((await self.call(kind=kind, query=query))["message"], "invalid_config")
        self.assertEqual(self.service.calls, [])

    async def test_command_body_whitelist_includes_nested_records_and_no_scope_or_clock(self):
        bodies = [[], {}, command() | {"owner_key": "forged"}, command() | {"context_key": "forged"},
            command() | {"now": NOW.isoformat()}, command() | {"expected_revision": True},
            command() | {"expected_revision": -1}, command() | {"command_id": ""}, command() | {"id": "other"},
            command() | {"kind": "occurrences"}, command() | {"action": "configure"},
            command(action="delete") | {"record": TERM}]
        for kind, path in (("term", ()), ("term", ("makeup_days", 0)), ("timetable", ()),
                           ("timetable", ("course",)), ("timetable", ("exceptions", 0))):
            body = command(kind)
            record = body["record"]
            for key in path:
                record = record[key]
            record["internal"] = "forged"
            bodies.append(body)
        for key in TERM:
            body = command()
            del body["record"][key]
            bodies.append(body)
        for kind in ("term", "timetable", "syllabus", "assignment"):
            for key in ("revision", "schema_version"):
                body = command(kind)
                body["record"][key] = 1
                bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual((await self.call(body=body))["message"], "invalid_config")
        self.assertEqual(self.service.calls, [])

    async def test_course_command_is_update_only_with_exact_metadata_fields(self):
        body = command("course", "update")
        self.assertEqual(self.ok(await self.call(body=body))["kind"], "course")
        self.assertEqual(self.service.calls[-1], ("command", digest(self.owner), "trusted-context", body, NOW))
        rejected = [command("course", "create"), command("course", "delete"), command("course", "update") | {"id": "other"}]
        for change in ({"slot_id": "ethics"}, {"revision": 1}, {"internal": "forged"}):
            body = command("course", "update")
            body["record"] |= change
            rejected.append(body)
        body = command("course", "update")
        del body["record"]["term_id"]
        rejected.append(body)
        for body in rejected:
            with self.subTest(body=body):
                self.assertEqual((await self.call(body=body))["message"], "invalid_config")
        self.assertEqual(len(self.service.calls), 1)

    async def test_command_json_limits_duplicate_fields_and_query_rejection(self):
        valid = json.dumps(command()).encode()
        for raw in (b"", b"null", b"{", b"\xff", b"[" * 2000, b" " * (routes.MAX_BODY_BYTES + 1),
                    valid.replace(b'"command_id":', b'"command_id":"duplicate","command_id":'),
                    valid.replace(b'"term_id":', b'"term_id":"duplicate","term_id":'),
                    valid.replace(b'"expected_revision": 0', b'"expected_revision": NaN')):
            with self.subTest(raw=raw[:100]):
                self.assertEqual((await self.call(raw=raw))["message"], "invalid_config")
        self.ok(await self.call(raw=valid + b" " * (routes.MAX_BODY_BYTES - len(valid))))
        self.assertEqual((await self.call(body=command(), query="term_id=fall"))["message"], "invalid_config")
        self.assertEqual(len(self.service.calls), 1)

    async def test_disabled_service_and_authorization_errors(self):
        self.api.service = None
        self.assertEqual((await self.call())["message"], "missing_dependency")
        self.assertEqual((await self.call(body=command()))["message"], "missing_dependency")
        for code in ("authorization_denied", "core_access_required", "life_target_changed"):
            def denied(_operator):
                raise TaskError(code, "access denied")
            self.manager.authorize = denied
            self.assertEqual((await self.call())["message"], code)
            self.assertEqual((await self.call(body=command()))["message"], code)
        self.assertEqual(self.service.calls, [])

    async def test_service_value_errors_preserve_only_frozen_codes(self):
        for code in routes.ERROR_CODES | {"unrecognized-private-code", None}:
            error = ValueError("service refusal")
            error.code = code
            self.service.error = error
            expected = code if code in routes.ERROR_CODES else "invalid_config"
            self.assertEqual((await self.call())["message"], expected)
            self.assertEqual((await self.call(body=command()))["message"], expected)

    async def test_storage_command_does_not_block_event_loop(self):
        import threading
        heartbeat = threading.Event()
        original = self.api.service.command

        def command_with_io(*args, **kwargs):
            if not heartbeat.wait(2):
                raise RuntimeError("event loop blocked by synchronous storage command")
            return original(*args, **kwargs)

        async def tick():
            await asyncio.sleep(0)
            heartbeat.set()

        self.api.service.command = command_with_io
        for kind in ("term", "syllabus", "assignment"):
            heartbeat.clear()
            task = asyncio.create_task(tick())
            self.ok(await self.call(body=command(kind)))
            await task

    async def test_study_reads_do_not_block_event_loop(self):
        import threading
        original = self.api.service.read
        for kind in STUDY_KINDS:
            heartbeat = threading.Event()
            def read_with_io(*args, **kwargs):
                if not heartbeat.wait(2):
                    raise RuntimeError("event loop blocked by synchronous storage read")
                return original(*args, **kwargs)
            async def tick():
                await asyncio.sleep(0)
                heartbeat.set()
            self.api.service.read = read_with_io
            task = asyncio.create_task(tick())
            self.ok(await self.call(kind=kind, query=STUDY_QUERY))
            await task

    async def test_real_service_revision_fence_replay_reads_and_context_isolation(self):
        from tempfile import TemporaryDirectory
        from good_life.academics import Academics, AcademicsConfig
        from good_life.storage import LifeStorage

        temporary = TemporaryDirectory(prefix="academics-routes-")
        self.addCleanup(temporary.cleanup)
        storage = LifeStorage.open(temporary.name)
        self.addCleanup(storage.close)
        self.api.service = Academics(config=AcademicsConfig(enabled=True))
        self.api.service.install(storage)
        self.assertEqual(self.ok(await self.call()), {"schema_version": 1, "revision": 0, "terms": []})
        term = command()
        term["record"].update(week_starts=["2026-09-07", "2026-09-14", "2026-09-21"], makeup_days=[])
        first = self.ok(await self.call(body=term))
        self.assertEqual(first["revision"], 1)
        slot = command("timetable", revision=1)
        slot["command_id"] = "manual-slot-1"
        self.assertEqual(self.ok(await self.call(body=slot))["revision"], 2)
        self.assertEqual(self.ok(await self.call(body=term)), first, "replay keeps the original receipt after later writes")
        result = self.ok(await self.call())
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["terms"], [term["record"] | {"schema_version": 1, "revision": 1}])
        result = self.ok(await self.call(kind="timetable", query="term_id=fall"))
        self.assertEqual(result["entries"], [slot["record"] | {"schema_version": 1, "revision": 2}])
        result = self.ok(await self.call(kind="occurrences", query="term_id=fall&start=2026-09-07&end=2026-09-28"))
        self.assertEqual(result["items"], [], "past manual timetable entry does not invent historical occurrences")
        stale = command(action="update", revision=1)
        stale["command_id"] = "stale-update"
        self.assertEqual((await self.call(body=stale))["message"], "stale_version")
        self.manager.context.key = "another-private-context"
        self.assertEqual((await self.call())["message"], "unauthorized")
        self.assertEqual((await self.call(body=term))["message"], "unauthorized")
        self.manager.context.key = "trusted-context"
        self.assertEqual(self.ok(await self.call())["revision"], 2)

    async def test_real_study_service_records_reads_revision_conflict_and_replay(self):
        from tempfile import TemporaryDirectory
        from good_life.academics import Academics, AcademicsConfig
        from good_life.storage import LifeStorage
        from test_academics_domain import term, entry

        temporary = TemporaryDirectory(prefix="academics-study-routes-")
        self.addCleanup(temporary.cleanup)
        storage = LifeStorage.open(temporary.name)
        self.addCleanup(storage.close)
        self.api.service = Academics(config=AcademicsConfig(enabled=True))
        self.api.service.install(storage)
        unit = deepcopy(SYLLABUS)
        unit["assignment_ids"] = []  # Link only after the assignment exists.
        requests, receipts = {}, {}
        for revision, (kind, record, id_field) in enumerate((("term", term(), "term_id"),
                ("timetable", entry(), "slot_id"), ("syllabus", unit, "unit_id"),
                ("assignment", deepcopy(ASSIGNMENT), "assignment_id"))):
            body = command(kind, revision=revision)
            body.update(command_id="study-route-" + kind, id=record[id_field], record=record)
            requests[kind] = body
            receipts[kind] = self.ok(await self.call(body=body))
            self.assertEqual(receipts[kind]["revision"], revision + 1)

        self.assertEqual(self.ok(await self.call(kind="syllabus", query=STUDY_QUERY)),
            {"schema_version": 1, "revision": 4, "term_id": "fall", "course_id": "ethics",
             "units": [unit | {"schema_version": 1, "revision": 3}]})
        self.assertEqual(self.ok(await self.call(kind="assignments", query=STUDY_QUERY)),
            {"schema_version": 1, "revision": 4, "term_id": "fall", "course_id": "ethics",
             "assignments": [ASSIGNMENT | {"schema_version": 1, "revision": 4}]})
        progress = self.ok(await self.call(kind="study_progress", query=STUDY_QUERY))
        self.assertEqual(progress, {"schema_version": 1, "revision": 4, "term_id": "fall", "course_id": "ethics",
            "items": [{"assignment_id": "assignment-1", "chunk_id": "assignment-1:" + str(index),
                "chunk_index": index, "due_at": ASSIGNMENT["due_at"], "required_work_s": 1500,
                "completed_work_s": 0, "remaining_work_s": 1500, "status": "pending", "revision": 0,
                "evaluation": "not_started", "provenance": "simulated"} for index in range(2)]})

        for kind in ("syllabus", "assignment"):
            self.assertEqual(self.ok(await self.call(body=requests[kind])), receipts[kind])
            stale = deepcopy(requests[kind])
            stale.update(command_id="stale-study-" + kind, action="update", expected_revision=3)
            self.assertEqual((await self.call(body=stale))["message"], "stale_version")
        self.assertEqual(self.ok(await self.call(kind="study_progress", query=STUDY_QUERY)), progress)
        self.manager.context.key = "another-private-context"
        for kind in STUDY_KINDS:
            self.assertEqual((await self.call(kind=kind, query=STUDY_QUERY))["message"], "unauthorized")

    def paired_page(self):
        self.controller.web_api = SimpleNamespace(target_inventory=lambda **_kwargs: [
            {"target_id": "first", "scope_kind": "private", "roles": [{"role_id": "role-a", "life_api": "/life", "state_api": "/state"}]}])
        self.controller.role_source_target_id = lambda role, fallback: "primary"
        plugin = SimpleNamespace(_runtime=None)
        registry = SimpleNamespace(owns_registration=lambda token, caller: token == "lease" and caller is plugin)

        async def paired(**kwargs):
            return await dashboard.dispatch(self.controller, registry, **kwargs)

        plugin._runtime = SimpleNamespace(api=SimpleNamespace(life_dashboard=paired), academics=self.service,
                                          ready=True, token="lease")
        page = LifePageAPI(plugin)
        return page, plugin

    async def test_paired_proxy_dispatches_all_academics_operations_before_generic_commands(self):
        page, _plugin = self.paired_page()
        selected = []
        def view(controller, source, role):
            selected.append((source, role))
            return controller
        with patch.object(dashboard, "TargetControllerView", view):
            for name, query in (("term", ""), ("timetable", "term_id=fall"),
                               ("occurrences", "term_id=fall&start=2026-09-01&end=2026-09-02"),
                               *((kind, STUDY_QUERY) for kind in STUDY_KINDS)):
                handler = getattr(page, "academics_" + name)
                self.ok(await self.call(lambda: handler("first", digest("role-a")), query=query))
                self.assertEqual(self.service.calls[-1][3], name)
            # 'create' is outside STATE_COMMANDS, so success proves early dispatch.
            for kind in ("term", "syllabus", "assignment"):
                self.ok(await self.call(lambda: page.academics_command("first", digest("role-a")), body=command(kind)))
                self.assertEqual(self.service.calls[-1][3]["kind"], kind)
        self.assertEqual(selected, [("primary", "role-a")] * 9)

    async def test_paired_artifact_pagination_details_and_workspace_denial(self):
        from tempfile import TemporaryDirectory
        from good_life.academics import Academics, AcademicsConfig
        from good_life.storage import LifeStorage
        from test_academics_domain import term, entry
        from test_study_artifacts import Arrived

        temporary = TemporaryDirectory(prefix="study-artifact-routes-")
        self.addCleanup(temporary.cleanup)
        storage = LifeStorage.open(temporary.name)
        self.addCleanup(storage.close)
        academics = Academics(config=AcademicsConfig(enabled=True, study_enabled=True))
        academics.install(storage)
        page, plugin = self.paired_page()
        plugin._runtime.academics = academics
        self.api.service = academics
        unit = SYLLABUS | {"materials": [], "assignment_ids": []}
        records = (("term", term(), "term_id"), ("timetable", entry(), "slot_id"),
                   ("syllabus", unit, "unit_id"), ("assignment", ASSIGNMENT, "assignment_id"))
        for revision, (kind, record, id_field) in enumerate(records):
            body = command(kind, revision=revision)
            body.update(command_id="artifact-route-" + kind, id=record[id_field], record=record)
            self.ok(await self.call(body=body))
        owner, context = digest(self.owner), self.manager.context.key
        port = academics.study_artifacts
        await port.advance(owner, context, "assignment-1", 0, expected_revision=0, now=NOW, walk=Arrived())
        await port.advance(owner, context, "assignment-1", 0, expected_revision=1, now=NOW, work_s=1500, allocated_s=1500)

        list_handler = lambda: page.academics_study_artifacts("first", digest("role-a"))
        def detail_handler(artifact_id):
            return lambda: page.academics_study_artifact("first", digest("role-a"), artifact_id)
        query = "assignment_id=assignment-1"
        with patch.object(dashboard, "TargetControllerView", lambda *_args: self.controller):
            for bad in ("", query + "&limit=0", query + "&limit=51", query + "&before=", query + "&limit=3&limit=1",
                        query + "&context_key=forged"):
                self.assertEqual((await self.call(list_handler, query=bad))["message"], "invalid_config")
            first = self.ok(await self.call(list_handler, query=query + "&limit=2"))
            self.assertEqual(set(first), {"schema_version", "items", "next_before"})
            self.assertEqual(first["next_before"], first["items"][-1]["artifact_id"])
            second = self.ok(await self.call(list_handler, query=query + "&before=" + first["next_before"]))
            self.assertIsNone(second["next_before"])
            items = first["items"] + second["items"]
            self.assertEqual({item["kind"] for item in items}, {"draft", "note", "review"})
            for item in items:
                self.assertNotIn("text", item)
                detail = self.ok(await self.call(detail_handler(item["artifact_id"])))
                self.assertEqual((detail["artifact_id"], detail["kind"], detail["source"], detail["availability"]),
                                 (item["artifact_id"], item["kind"], "simulated", "available"))
                self.assertIn("虚构材料", detail["text"])
                self.assertTrue(detail["next_step"])
                self.assertNotIn("materials", detail)
            known = detail_handler(items[0]["artifact_id"])
            unknown = detail_handler("no-such-artifact")
            self.assertEqual((await self.call(known, query="owner_key=forged"))["message"], "invalid_config")
            self.assertEqual((await self.call(known, username="api_key:42"))["message"], "dashboard_identity_required")
            absent = await self.call(unknown)
            self.manager.context.key = "another-private-context"
            self.assertEqual(await self.call(known), absent)
            self.assertEqual(await self.call(unknown), absent)
            self.assertEqual((await self.call(list_handler, query=query))["message"], "unauthorized")
            self.manager.context.key = context
            self.manager.owner = {**self.owner, "role_id": "another-role"}
            self.assertEqual(await self.call(known), absent)
        self.manager.owner = self.owner
        self.assertEqual(self.ok(await self.call(kind="study_progress", query=STUDY_QUERY))["items"][0]["completed_work_s"], 1500)

    async def test_paired_auth_identity_registration_target_role_and_disabled_guards(self):
        page, plugin = self.paired_page()
        cases = [(page.academics_term, None, ""), (page.academics_command, command(), "")]
        cases.extend((getattr(page, "academics_" + kind), None, STUDY_QUERY) for kind in STUDY_KINDS)
        cases.extend((page.academics_command, command(kind), "") for kind in ("syllabus", "assignment"))
        for handler, body, query in cases:
            invoke = lambda: handler("first", digest("role-a"))
            for username in (None, "", " ", "api_key:42"):
                self.assertEqual((await self.call(invoke, username=username, body=body, query=query))["message"], "dashboard_identity_required")
            self.assertEqual((await self.call(invoke, plugin_name="wrong", body=body, query=query))["message"], "dashboard_identity_required")
            plugin._runtime.token = "expired"
            self.assertEqual((await self.call(invoke, body=body, query=query))["message"], "life_provider_unavailable")
            plugin._runtime.token = "lease"
            self.assertEqual((await self.call(lambda: handler("missing", digest("role-a")), body=body, query=query))["message"], "life_dashboard_invalid")
            self.assertEqual((await self.call(lambda: handler("first", digest("unknown-role")), body=body, query=query))["message"], "life_dashboard_invalid")
            with patch.object(dashboard, "TargetControllerView", lambda *_args: self.controller):
                plugin._runtime.academics = None
                self.assertEqual((await self.call(invoke, body=body, query=query))["message"], "missing_dependency")
            plugin._runtime.academics = self.service
        self.assertEqual(self.service.calls, [])

    async def test_dashboard_rechecks_identity_and_operation_allowlist(self):
        page, plugin = self.paired_page()
        registry = SimpleNamespace(owns_registration=lambda *_args: True)
        async def invoke(operation="academics.export"):
            return await dashboard.dispatch(self.controller, registry, plugin=plugin, registration_id="lease",
                                            operation=operation, target_id="first", role_token=digest("role-a"))
        self.assertEqual((await self.call(invoke))["message"], "life_dashboard_invalid")
        self.assertEqual((await self.call(lambda: invoke("academics.term"), username="api_key:42"))["message"],
                         "dashboard_identity_required")

    async def test_places_authorize_then_read_outside_the_host_lock_and_trim_fields(self):
        reads = []
        def reader():
            self.assertFalse(self.controller._control_lock.locked(), "place reader called under the host lock")
            self.assertEqual(self.authorizations, ["trusted-operator"])
            reads.append(True)
            return {"classes_enabled": True, "graph": {"graph_id": "campus", "version": 3, "digest": "private-marker"},
                    "places": [{"place_id": "osm:way/1", "name": "第三教学楼", "type": "university", "lat": 31.2,
                                "tags": {"internal": "private-marker"}}], "owner_key": "private-marker"}
        result = self.ok(await self.call(lambda: self.api.places(reader)))
        self.assertEqual(result, {"academics_enabled": True, "classes_enabled": True, "graph": {"graph_id": "campus", "version": 3},
                                  "places": [{"place_id": "osm:way/1", "name": "第三教学楼", "type": "university"}]})
        self.assertEqual(reads, [True])
        self.assertEqual(self.service.calls, [], "place options never touch academics storage")

    async def test_places_answer_while_academics_is_disabled_and_without_a_reader(self):
        self.api.service = None
        reader = lambda: {"classes_enabled": True, "graph": None, "places": []}
        self.assertEqual(self.ok(await self.call(lambda: self.api.places(reader))),
                         {"academics_enabled": False, "classes_enabled": False, "graph": None, "places": []})
        self.api.service = self.service
        self.assertEqual(self.ok(await self.call(lambda: self.api.places(None))),
                         {"academics_enabled": True, "classes_enabled": False, "graph": None, "places": []})

    async def test_places_reject_queries_bad_reader_shapes_and_denied_scopes(self):
        good = lambda: {"classes_enabled": False, "graph": None, "places": []}
        self.assertEqual((await self.call(lambda: self.api.places(good), query="graph_id=other"))["message"], "invalid_config")
        for bad in ({"classes_enabled": "yes", "graph": None, "places": []}, {"classes_enabled": False, "graph": "x", "places": []},
                    {"classes_enabled": False, "graph": None, "places": {}}, {"classes_enabled": False, "graph": None,
                    "places": [{"place_id": {"private": "private-marker"}, "name": "x", "type": "y"}]}, [], None):
            with self.subTest(bad=bad):
                self.assertEqual((await self.call(lambda: self.api.places(lambda: bad)))["message"], "invalid_config")
        def denied(_operator):
            raise TaskError("authorization_denied", "access denied")
        self.manager.authorize = denied
        called = []
        self.assertEqual((await self.call(lambda: self.api.places(lambda: called.append(1))))["message"], "authorization_denied")
        self.assertEqual(called, [])

    async def test_paired_places_route_uses_runtime_reader_and_dashboard_guards(self):
        page, plugin = self.paired_page()
        plugin._runtime.read_academic_places = lambda: {"classes_enabled": False, "graph": None,
            "places": [{"place_id": "p", "name": "图书馆", "type": "library"}]}
        invoke = lambda: page.academics_places("first", digest("role-a"))
        with patch.object(dashboard, "TargetControllerView", lambda *_args: self.controller):
            self.assertEqual(self.ok(await self.call(invoke)), {"academics_enabled": True, "classes_enabled": False,
                "graph": None, "places": [{"place_id": "p", "name": "图书馆", "type": "library"}]})
            plugin._runtime.academics = None
            self.assertFalse(self.ok(await self.call(invoke))["academics_enabled"])
            plugin._runtime.academics = self.service
            self.assertEqual((await self.call(invoke, username="api_key:42"))["message"], "dashboard_identity_required")
            self.assertEqual((await self.call(lambda: page.academics_places("first", digest("unknown-role"))))["message"],
                             "life_dashboard_invalid")

    async def test_proxy_route_registration_and_cleanup_preserves_other_routes(self):
        page, plugin = self.paired_page()
        registered = [("/unrelated", object(), ["GET"], "Other plugin")]
        plugin.context = SimpleNamespace(registered_web_apis=registered, register_web_api=lambda *args: registered.append(args))
        page.register()
        prefix = PREFIX + "/targets/<target>/<role>/academics/"
        actual = {path: methods for path, _handler, methods, _description in registered if path.startswith(prefix)}
        self.assertEqual(actual, {prefix + "term": ["GET"], prefix + "timetable": ["GET"],
                                  prefix + "occurrences": ["GET"], prefix + "syllabus": ["GET"],
                                  prefix + "assignments": ["GET"], prefix + "study_progress": ["GET"],
                                  prefix + "command": ["POST"], prefix + "places": ["GET"]})
        page.unregister()
        self.assertEqual([item[0] for item in registered], ["/unrelated"])
        plugin._study_enabled = True
        page.register()
        artifacts = {path: methods for path, _handler, methods, _description in registered
                     if path.startswith(prefix + "study_artifacts")}
        self.assertEqual(artifacts, {prefix + "study_artifacts": ["GET"], prefix + "study_artifacts/<artifact_id>": ["GET"]})
        page.unregister()
        self.assertEqual([item[0] for item in registered], ["/unrelated"])


if __name__ == "__main__":
    unittest.main()
