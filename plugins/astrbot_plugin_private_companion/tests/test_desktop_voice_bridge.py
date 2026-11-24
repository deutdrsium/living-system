"""B04/B05 exercise the host WebChat turn and native Live response hooks."""

import asyncio
from types import SimpleNamespace

import pytest

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse
from astrbot_host.desktop_api import DESKTOP_SESSION_FLAG, DesktopAPI, _WebChatTurn
from astrbot_host.native_extensions.host_events import observe_native_host_response, observe_native_host_result
from astrbot_host.native_extensions.live.vts_parameter_scheduler import VTSParameterScheduler
from astrbot_host.native_extensions.live.wardrobe import WardrobeController
from astrbot_host.native_extensions.request_context import set_turn_context


class Event:
    def __init__(self, origin, turn_id):
        self.unified_msg_origin = origin
        self.extra = {"llm_checkpoint_id": turn_id}

    def get_extra(self, name):
        return self.extra.get(name)

    def set_extra(self, name, value):
        self.extra[name] = value


@pytest.mark.parametrize("enabled", [True, False])
def test_webchat_legacy_tags_are_consumed_by_one_owner(enabled):
    pytest.importorskip("astrbot_plugin_desktop_access.session")
    pytest.importorskip("companion_desktop.contracts.lines")
    from astrbot_plugin_desktop_access.session import Binding, SessionHost
    from companion_desktop.contracts.wire import PROTOCOL, message

    async def exercise():
        gestures, wardrobe = [], []

        async def old_gesture(tags):
            gestures.extend(tags)

        async def select(choice):
            wardrobe.append(choice)

        live = SimpleNamespace(config={}, _wardrobe=SimpleNamespace(select=select),
                               _is_soullink_enabled=lambda: False, _is_subtitle_enabled=lambda: False,
                               _is_mouth_sync_enabled=lambda: False, _safe_parse_int=lambda _, default: default,
                               _parse_l2d_tags=lambda text: (["nod"], text.replace("<l2d:nod>", "")),
                               _trigger_l2d_tags=old_gesture, _l2d_tasks=set())
        controller = SimpleNamespace(config={DESKTOP_SESSION_FLAG: enabled},
                                     native_extensions=SimpleNamespace(active_service=lambda name: live if name == "live" else None))
        api = controller.desktop_api = DesktopAPI(controller)
        host = api._session_state.host = SessionHost()
        binding = Binding("desktop", "binding-demo", frozenset({"speak"}))
        ref = host.handle(binding, "register", {"protocols": [PROTOCOL], "permissions": ["speak"]})["session"]
        target = SimpleNamespace(participant_id="webchat!user-demo!window-demo")
        origin = f"webchat:FriendMessage:{target.participant_id}"

        async def prepare(_target):
            pass

        async def get_platform_session_by_id(_window):
            return None

        async def build_chat_stream(_username, data):
            turn_id = data["_llm_checkpoint_id"]
            assert not data["enable_streaming"]
            run = SimpleNamespace(llm_checkpoint_id=turn_id, session_id="window-demo", status="running", message_parts=[])
            chat.chat_runs["run-demo"] = run

            async def stream():
                event = Event(origin, turn_id)
                response = LLMResponse(role="assistant", result_chain=MessageChain(chain=[
                    Plain("你看🐟[点"), Plain("头]这条鱼[示例]！<l2d:nod>"), Plain('<wardrobe>{"top":"demo"}</wardrobe>'),
                ]))
                observe_native_host_response(controller, event, response)
                observe_native_host_result(controller, event)
                run.message_parts = [{"type": "plain", "text": part.text} for part in response.result_chain.chain]
                run.status = "completed"
                yield 'data: {"type":"end"}\n\n'

            return stream()

        api._prepare_target = prepare
        chat = SimpleNamespace(chat_runs={}, build_chat_stream=build_chat_stream,
                               db=SimpleNamespace(get_platform_session_by_id=get_platform_session_by_id))
        backend = _WebChatTurn(api, target, chat, lambda: None, ref if enabled else None)
        sent = message("turn.send", ref, {"client_msg_id": "message-demo", "text": "Hello"})
        host.handle(binding, "exchange", {"session": ref, "messages": [sent], "turns": []}, turns=backend)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        frames = host.handle(binding, "exchange", {"session": ref, "messages": [], "turns": ["message-demo"]})["messages"]
        assert not api._session_state.line_turns
        if enabled:
            completed, utterance = frames
            assert completed["body"]["text"] == "你看🐟这条鱼[示例]！"
            assert utterance["type"] == "utterance" and utterance["body"]["text"] == completed["body"]["text"]
            cue, = utterance["body"]["cues"]
            assert (cue["action"], cue["text_offset"]) == ("nod", 3)
            assert gestures == []
        else:
            completed, = frames
            assert completed["body"]["text"] == "你看🐟[点 头]这条鱼[示例]！"
            assert gestures == ["nod"]
        assert wardrobe == [{"top": "demo"}]
        host.handle(binding, "disconnect", {"session": ref, "messages": [], "turns": []})

    asyncio.run(exercise())


@pytest.mark.parametrize("grant_avatar", [False, True])
def test_desktop_whole_look_uses_wardrobe_snapshot_not_native_vts(tmp_path, grant_avatar):
    pytest.importorskip("astrbot_plugin_desktop_access.session")
    pytest.importorskip("companion_desktop.contracts.lines")
    from astrbot_plugin_desktop_access.session import Binding, SessionHost
    from companion_desktop.contracts.wire import PROTOCOL, message, now_utc

    choices = []

    async def select(choice):
        choices.append(choice)

    live = SimpleNamespace(_wardrobe=SimpleNamespace(select=select))
    controller = SimpleNamespace(config={DESKTOP_SESSION_FLAG: True}, data_root=tmp_path,
                                 native_extensions=SimpleNamespace(active_service=lambda name: live if name == "live" else None))
    api = controller.desktop_api = DesktopAPI(controller)
    host = api._session_state.host = SessionHost()
    permissions = ["speak", "avatar"] if grant_avatar else ["speak"]
    binding = Binding("desktop", "binding-demo", frozenset(permissions))
    ref = host.handle(binding, "register", {"protocols": [PROTOCOL], "permissions": permissions})["session"]
    caps = {"revision": 1, "model_id": "model-demo", "asset_version": "assets-demo", "observations": [],
            "avatar": {"actions": [{"name": "outfit.jacket", "binding": "demo-jacket.exp3.json"}],
                       "inputs": [], "hotkeys": []}, "games": []}
    host.handle(binding, "exchange", {"session": ref, "messages": [message("capabilities", ref, caps)], "turns": []})
    assert api.owns_wardrobe() is grant_avatar
    target = SimpleNamespace(participant_id="webchat!user-demo!window-demo")
    turn = _WebChatTurn(api, target, None, lambda: None, ref)
    api._session_state.line_turns["turn-demo"] = turn
    origin = f"webchat:FriendMessage:{target.participant_id}"
    event = Event(origin, "turn-demo")
    req = SimpleNamespace()
    set_turn_context(req, "wardrobe", "legacy inventory")
    api.inject_wardrobe(event, req)
    context, = req.extra_user_content_parts
    assert ("jacket" in context.text) is grant_avatar
    assert ("legacy inventory" in context.text) is not grant_avatar

    # A control-only completion still selects the look. An unadvertised name
    # never replaces it, and neither should go through the native selector.
    for outfit in ("jacket", "unmapped-demo"):
        response = LLMResponse(role="assistant", completion_text=f'<wardrobe>{{"outfit":"{outfit}"}}</wardrobe>')
        observe_native_host_response(controller, event, response)
        assert not response.completion_text
    assert not choices
    path = api._desktop_wardrobe_path(target, caps)
    assert path.exists() is grant_avatar
    if not grant_avatar:
        return

    def snapshots(current):
        wardrobe = api._wardrobe_snapshot(binding.key, target, current)
        return [("wardrobe", wardrobe)] if wardrobe is not None else []

    frame, = host.handle(binding, "exchange", {"session": ref, "messages": [], "turns": []},
                         snapshots=snapshots)["messages"]
    assert frame["type"] == "wardrobe"
    assert frame["body"]["selection"] == {"outfit": "jacket"}
    receipt = {key: frame["body"][key] for key in ("revision", "model_id", "asset_version")}
    receipt.update(status="completed", evidence={"source": "vts_model_readback", "ref": "read-demo"},
                   reason_code=None, occurred_at=now_utc())
    host.handle(binding, "exchange", {"session": ref, "messages": [message("wardrobe_applied", ref, receipt)],
                                     "turns": []}, snapshots=snapshots)
    assert host.wardrobe_applied(ref) == receipt

    # The native wardrobe stops before talking to VTS at all while leased.
    wardrobe = WardrobeController(SimpleNamespace(controller=controller, data_dir=tmp_path))
    assert asyncio.run(wardrobe._apply({})) is False
    controller.config[DESKTOP_SESSION_FLAG] = False
    assert not api.owns_wardrobe()


def test_leased_desktop_input_is_filtered_from_ordinary_host_writes():
    async def exercise():
        owned = {"MouthOpen"}
        writes = []

        async def inject(**kwargs):
            writes.append(kwargs["parameters"])

        async def ensure():
            return True

        vts = SimpleNamespace(is_connected=True, inject_parameters=inject)
        scheduler = VTSParameterScheduler(vts, ensure)
        scheduler.set_layer("mouth", [{"id": "MouthOpen", "value": 1}])
        scheduler.desktop_owned_inputs = lambda: owned
        scheduler.set_layer("soullink", [{"id": "MouthOpen", "value": 0.5}, {"id": "other", "value": 0.5}])
        assert await scheduler.flush()
        assert writes[-1] == [{"id": "other", "value": 0.5}]
        owned.clear()
        scheduler.set_layer("mouth", [{"id": "MouthOpen", "value": 0.8}])
        assert await scheduler.flush()
        assert {item["id"] for item in writes[-1]} == {"other", "MouthOpen"}

    asyncio.run(exercise())
