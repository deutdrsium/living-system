import asyncio
import json
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from astrbot_host import desktop_api


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    target = SimpleNamespace(target_id="target-demo", platform_id="webchat", bot_id="bot-demo",
                             participant_id="webchat!user-demo!window-demo")
    link = SimpleNamespace(mode="role", alias_target_ids=(target.target_id,), primary_target_id="primary-demo")
    settings = SimpleNamespace(enabled=True, target=lambda _: target, window_link_for_target=lambda _: link,
                               personas=[SimpleNamespace(bot_id="bot-demo", persona_id="persona-demo", role_id="role-demo")],
                               canonical_role_id=lambda value: value,
                               policy=SimpleNamespace(turn_timeout_seconds=420))
    controller = SimpleNamespace(data_root=tmp_path, config={}, settings=settings, session=object(),
                                 context=SimpleNamespace(get_config=lambda **_: {"admins_id": ["user-demo"]}),
                                 _terminated=False)
    api = desktop_api.DesktopAPI(controller)
    api._device_path().parent.mkdir()
    api._device_path().write_text(json.dumps({"target_id": "target-demo", "api_key_id": "key-demo"}), encoding="utf-8")
    monkeypatch.setattr(desktop_api, "request", SimpleNamespace(username="api_key:key-demo", query={}))
    return api, target


def test_disabled_hides_session_and_keeps_legacy_device_auth(desktop):
    api, target = desktop
    assert api._authorize() is target
    assert asyncio.run(api.device_session()) == {"status": "error", "reason": "disabled"}
    assert api.device_sessions is None
    legacy_routes = [
        (f"{desktop_api.PREFIX}/{name}", [method])
        for name, method in (("setup", "POST"), ("prepare", "POST"), ("history", "GET"),
                             ("speech", "POST"), ("playback", "POST"), ("screen", "POST"))
    ]
    assert [(route, methods) for route, _, methods, _ in api.routes()] == legacy_routes
    api.controller.config[desktop_api.DESKTOP_SESSION_FLAG] = True
    assert [(route, methods) for route, _, methods, _ in api.routes()] == [
        *legacy_routes, (f"{desktop_api.PREFIX}/session", ["POST"]),
    ]


@pytest.mark.parametrize("enabled", [False, True])
def test_route_refresh_keeps_session_state_and_discards_legacy_caches(desktop, monkeypatch, enabled):
    from astrbot_host.multi_api import MultiHostWebAPI

    api, _ = desktop
    config = api.controller.config
    config["performance"] = {"enabled": True}
    config[desktop_api.DESKTOP_SESSION_FLAG] = enabled
    routes = api.controller.context.registered_web_apis = []
    api.controller.context.register_web_api = lambda *route: routes.append(route)
    web = MultiHostWebAPI(api.controller)
    web.register()
    old = api.controller.desktop_api
    old.audio["audio-demo"] = ([], 0, float("inf"))
    old.screen_session = "screen-demo"
    old.observation = {"subject": "demo"}
    if enabled:
        host = old._session_state.host = object()
        revision = old._next_snapshot_revision()
        state = old._session_state.role_snapshot = ("binding-demo", 1, {"revision": revision})

    web.unregister()
    web.register()
    new = api.controller.desktop_api
    assert new is not old
    assert any(path == f"{desktop_api.PREFIX}/session" for path, *_ in routes) is enabled
    if enabled:
        assert new.device_sessions is host
        assert new._session_state.role_snapshot is state
        assert new._next_snapshot_revision() == revision + 1
    else:
        assert new.device_sessions is None
    assert new.screen_session == "" and new.observation is None

    async def body():
        return b'{"id": "audio-demo", "action": "start"}'

    monkeypatch.setattr(desktop_api.request, "body", body, raising=False)
    playback = next(handler for path, handler, _, _ in routes if path == f"{desktop_api.PREFIX}/playback")
    assert asyncio.run(playback()) == {"status": "error", "message": "播放状态不可用"}


def test_other_key_is_denied_before_transport_load(desktop, monkeypatch):
    api, _ = desktop
    api.controller.config[desktop_api.DESKTOP_SESSION_FLAG] = True
    monkeypatch.setattr(desktop_api.request, "username", "api_key:other-demo")
    assert asyncio.run(api.device_session()) == {"status": "error", "reason": "unauthorized"}
    assert api.device_sessions is None


def test_route_registers_real_public_session_with_bound_permissions(desktop, monkeypatch):
    pytest.importorskip("astrbot_plugin_desktop_access.session")
    from companion_desktop.contracts.types import PROTOCOL
    from companion_desktop.contracts.wire import message

    api, target = desktop
    api.controller.config[desktop_api.DESKTOP_SESSION_FLAG] = True
    api.controller.native_extensions = None
    api._device_path().write_text(json.dumps({"target_id": "target-demo", "api_key_id": "key-demo",
                                             "session_permissions": ["avatar"]}), encoding="utf-8")

    async def run():
        payload = {"operation": "register", "payload": {"protocols": [PROTOCOL], "permissions": ["avatar", "game"]}}

        async def body():
            return json.dumps(payload).encode()

        monkeypatch.setattr(desktop_api.request, "body", body, raising=False)
        reply = await api.device_session()
        ref = reply["data"]["session"]
        assert reply["data"]["permissions"] == ["avatar"]
        scope = SimpleNamespace(installation_id="installation-demo", bot_id="bot-demo")
        context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=SimpleNamespace(subject_id="role-demo")))
        api.controller.authority = SimpleNamespace(context=lambda target_id, role: context)
        api.controller.session = SimpleNamespace(application=SimpleNamespace(
            role_state=SimpleNamespace(read=lambda *_args, **_kwargs: {"values": {"energy": 50}}),
            role_life=SimpleNamespace(context_activities=lambda *_args, **_kwargs: ()),
        ))
        caps = {"revision": 1, "model_id": None, "asset_version": None,
                "avatar": {"actions": [], "inputs": [], "hotkeys": []}, "observations": [], "games": []}
        payload.update(operation="exchange", payload={"session": ref, "turns": [],
                                                       "messages": [message("capabilities", ref, caps)]})
        reply = await api.device_session()
        state = reply["data"]["messages"][0]
        assert state["type"] == "role_state"
        assert state["body"]["energy"] == 0.5
        assert state["body"]["sleepiness"] is None
        payload.update(operation="disconnect", payload={"session": ref, "turns": [], "messages": []})
        assert (await api.device_session())["status"] == "ok"

    asyncio.run(run())


@pytest.mark.parametrize(("ack_delivered", "outcome"), [
    (True, "completed"), (False, "completed"), (True, "missing"), (True, "partial"), (True, "aborted"),
])
def test_reconnect_recovers_only_saved_reply_for_exact_checkpoint(desktop, tmp_path, monkeypatch,
                                                                  ack_delivered, outcome):
    pytest.importorskip("astrbot_plugin_desktop_access.session")
    from astrbot.core.agent.message import CheckpointData, CheckpointMessageSegment, Message, TextPart
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.db.sqlite import SQLiteDatabase
    from astrbot.dashboard.api import auth
    from companion_desktop.contracts.wire import encode_request
    from companion_desktop.session.client import DesktopSession

    api, _ = desktop
    api.controller.config[desktop_api.DESKTOP_SESSION_FLAG] = True
    api._device_path().write_text(json.dumps({"target_id": "target-demo", "api_key_id": "key-demo",
                                             "session_permissions": ["speak"]}), encoding="utf-8")
    events, checkpoints = [], []
    lost_ack = not ack_delivered
    current = api

    async def require_scope(request, scope):
        assert scope == "chat"
        return SimpleNamespace(username="api_key:key-demo", scopes={"*"})

    async def transport(operation, payload):
        nonlocal lost_ack

        async def body():
            return encode_request(operation, payload)

        monkeypatch.setattr(desktop_api.request, "body", body, raising=False)
        response = await current.device_session()
        assert response["status"] == "ok", response
        if lost_ack and any(frame["type"] == "turn.send" for frame in payload.get("messages", ())):
            lost_ack = False
            raise ConnectionError("connection_lost")
        return response["data"]

    async def exercise():
        nonlocal current
        db = SQLiteDatabase(str(tmp_path / "webchat.sqlite3"))
        saved = asyncio.Event()

        async def store(window, checkpoint, role, text):
            await db.insert_platform_message_history("webchat", window, {
                "type": role, "message": [{"type": "plain", "text": text}],
            }, llm_checkpoint_id=checkpoint)

        async def build_chat_stream(username, payload):
            assert (username, payload["session_id"]) == ("user-demo", "window-demo")
            turn_id = payload["_llm_checkpoint_id"]
            checkpoints.append(turn_id)
            await store("window-demo", turn_id, "user", "Hello")
            await store("window-demo", "unrelated-demo", "bot", "Different checkpoint")
            await store("other-window-demo", turn_id, "bot", "Different window")
            run = SimpleNamespace(llm_checkpoint_id=turn_id, session_id="window-demo", status="running",
                                  message_parts=[], task=None)
            chat.chat_runs["run-demo"] = run

            async def stream():
                if outcome != "missing":
                    await store("window-demo", turn_id, "bot", "<l2d:nod>Saved ")
                    await store("window-demo", turn_id, "bot", "reply")
                    text = (ToolLoopAgentRunner.USER_INTERRUPTION_MESSAGE if outcome == "aborted" else "Saved reply")
                    messages = [Message(role="assistant", content=[TextPart(text=text)]).model_dump()]
                    checkpoint = CheckpointMessageSegment(content=CheckpointData(id=turn_id)).model_dump()
                    if outcome == "partial":
                        await db.create_conversation("webchat:FriendMessage:webchat!user-demo!other-window-demo",
                                                     "webchat", content=[*messages, checkpoint])
                    else:
                        messages.append(checkpoint)
                    await db.create_conversation("webchat:FriendMessage:webchat!user-demo!window-demo",
                                                 "webchat", content=messages)
                if outcome == "completed":
                    run.status = "completed"
                saved.set()
                yield 'data: {"type":"end"}\n\n'

            return stream()

        async def prepare(_target):
            pass

        chat = SimpleNamespace(db=db, build_chat_stream=build_chat_stream, chat_runs={})
        host_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(chat=chat))))
        monkeypatch.setattr(auth, "require_scope", require_scope)
        monkeypatch.setattr(desktop_api.request, "_get_current", lambda: SimpleNamespace(_request=host_request), raising=False)
        monkeypatch.setattr(api, "_prepare_target", prepare)
        monkeypatch.setattr(desktop_api.DesktopAPI, "_device_snapshots", lambda *_args: ())
        await db.create_platform_session("user-demo", session_id="window-demo")
        client = DesktopSession(transport, permissions={"speak"})
        client.subscribe(events.append)
        try:
            await client._register()
            await client._exchange()
            client.send_message("msg-demo", "Hello")
            if ack_delivered:
                await client._exchange()
            else:
                with pytest.raises(ConnectionError):
                    await client._exchange()
            await asyncio.wait_for(saved.wait(), 2)
            assert not any(event["type"] in {"turn.completed", "turn.failed"} for event in events)
            client._drop("connection_lost")
            chat.chat_runs.clear()
            current = desktop_api.DesktopAPI(api.controller)
            await client._register()
            await client._exchange()
            client.send_message("msg-demo", "Hello")
            await client._exchange()
            assert len(checkpoints) == 1
            terminal = [event for event in events if event["type"] in {"turn.completed", "turn.failed"}]
            expected = {"type": "turn.completed", "text": "Saved reply"} if outcome == "completed" else {
                "type": "turn.failed", "reason": "result_unknown",
            }
            assert terminal == [{**expected, "turn_id": checkpoints[0], "client_msg_id": "msg-demo"}]
        finally:
            await client._exchange("disconnect")
            await db.engine.dispose()

    asyncio.run(exercise())


class Event:
    def __init__(self, checkpoint):
        self.extra = {"llm_checkpoint_id": checkpoint}

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value


@pytest.fixture
def turn_result_stub(monkeypatch):
    @dataclass
    class TurnResult:
        status: str
        text: str = ""

    module = ModuleType("astrbot_plugin_desktop_access.session")
    module.TurnResult = TurnResult
    monkeypatch.setitem(sys.modules, module.__name__, module)


def test_webchat_stop_checks_exact_checkpoint_and_abort_evidence(desktop, monkeypatch, turn_result_stub):
    from astrbot.core.utils.active_event_registry import active_event_registry

    api, target = desktop

    async def check(aborted):
        started, stop_signal = asyncio.Event(), asyncio.Event()
        selected, other = Event("turn-demo"), Event("unrelated-demo")
        umo = f"webchat:FriendMessage:{target.participant_id}"
        monkeypatch.setitem(active_event_registry._events, umo, {other})
        monkeypatch.setitem(active_event_registry._agent_stop_callbacks, selected, stop_signal.set)
        record = SimpleNamespace(creator="user-demo", platform_id="webchat", is_group=False)
        run = SimpleNamespace(llm_checkpoint_id="turn-demo", session_id="window-demo", status="running", message_parts=[])

        async def get_session(window):
            assert window == "window-demo"
            return record

        async def prepare(active_target):
            assert active_target is target

        async def build_chat_stream(username, payload):
            assert username == "user-demo"
            assert payload["_llm_checkpoint_id"] == "turn-demo"

            async def stream():
                started.set()
                await stop_signal.wait()
                selected.set_extra("agent_user_aborted", aborted)
                run.status = "completed"
                yield 'data: {"type":"end"}\n\n'

            return stream()

        monkeypatch.setattr(api, "_prepare_target", prepare)
        chat = SimpleNamespace(db=SimpleNamespace(get_platform_session_by_id=get_session),
                               build_chat_stream=build_chat_stream, chat_runs={"run-demo": run})
        turn = desktop_api._WebChatTurn(api, target, chat, lambda: None)
        task = asyncio.create_task(turn.run("turn-demo", "Hello"))
        await asyncio.wait_for(started.wait(), 1)
        async def yield_once(_delay):
            await asyncio.sleep(0)

        with monkeypatch.context() as clock:
            ticks = iter((0, 0, 4))
            clock.setattr(desktop_api, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
            clock.setattr(desktop_api, "asyncio", SimpleNamespace(sleep=yield_once))
            await turn.cancel("turn-demo")
        assert not stop_signal.is_set()
        active_event_registry._events[umo].add(selected)
        await turn.cancel("turn-demo")
        result = await asyncio.wait_for(task, 1)
        assert other.get_extra("agent_stop_requested") is None
        return result.status

    assert asyncio.run(check(True)) == "cancelled"
    assert asyncio.run(check(False)) == "unknown"


@pytest.mark.parametrize("agent_aborted", [True, False])
def test_webchat_releases_consumer_when_the_end_marker_never_arrives(
    desktop, monkeypatch, turn_result_stub, agent_aborted,
):
    from astrbot.core.utils.active_event_registry import active_event_registry
    from astrbot.dashboard.services.chat_service import ChatRunState, ChatService, webchat_queue_mgr

    api, target = desktop
    umo = f"webchat:FriendMessage:{target.participant_id}"
    selected = Event("turn-demo")
    monkeypatch.setitem(active_event_registry._events, umo, {selected})

    async def prepare(_target):
        pass

    async def get_session(_window):
        return None

    monkeypatch.setattr(api, "_prepare_target", prepare)

    async def exercise():
        opened = asyncio.Event()
        chat = ChatService.__new__(ChatService)
        chat.db = SimpleNamespace(get_platform_session_by_id=get_session)
        queue = webchat_queue_mgr.get_or_create_back_queue("run-demo", "window-demo")
        run = ChatRunState("run-demo", "user-demo", "window-demo", "turn-demo", "webchat", queue)
        chat.chat_runs = {run.run_id: run}
        chat.chat_runs_by_session = {run.session_id: {run.run_id}}
        chat.running_convs = {}

        async def build_chat_stream(_username, _payload):
            run.task = asyncio.create_task(chat._consume_chat_run(run))
            opened.set()
            return chat._subscribe_chat_run(run, include_snapshot=False)

        chat.build_chat_stream = build_chat_stream
        turn = desktop_api._WebChatTurn(api, target, chat, lambda: None)
        task = asyncio.create_task(turn.run("turn-demo", "Hello"))
        try:
            await asyncio.wait_for(opened.wait(), 1)
            assert run.subscribers and run.status == "running"
            if agent_aborted:
                selected.set_extra("agent_user_aborted", True)
                active_event_registry._events[umo].remove(selected)
                assert (await asyncio.wait_for(asyncio.shield(task), 2)).status == "cancelled"
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert selected.get_extra("agent_stop_requested") is True
            await asyncio.wait_for(run.task, 1)
            assert turn.finished and run.status == "stopped"
            assert not run.subscribers and not chat.chat_runs and not chat.running_convs
            assert run.run_id not in webchat_queue_mgr.back_queues
        finally:
            for pending in (task, run.task):
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            webchat_queue_mgr.remove_back_queue(run.run_id)

    asyncio.run(exercise())
