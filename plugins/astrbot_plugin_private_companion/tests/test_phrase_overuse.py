from dataclasses import replace
import json
from types import SimpleNamespace

from companion_v2.adapters.astrbot.model_wire import build_wire
from companion_v2.conversation.foreground import (BackgroundPromptTrigger, PromptMaterial, PromptRequest,
                                                   SealedForegroundContext)
from companion_v2.conversation.phrase_overuse import inject_native_hint, overuse_hint
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, RoleBinding
from companion_v2.proactive.staged_prompts import write_request_for
from tests.test_management_access import INSTALLATION, NOW, ROLE, SCOPE


REPLIES = (
    "小团子，今天可以慢悠悠地散步，回来聊咖啡豆和天文馆。",
    "咖啡豆到了。小团子，我们慢悠悠选一款，今天还是去天文馆。",
    "今天翻到《天文馆》。你可以先看，我慢悠悠整理咖啡豆。小团子，晚点聊。",
)
PREFIX = "你最近反复用过："


def test_document_frequency_ignores_stopwords_echoes_names_and_shared_topics():
    hint = overuse_hint(REPLIES, names=("小团子",), current_inputs=("咖啡豆到了吗？",),
                        recent_inputs=("约在天文馆吧。", "我们还去天文馆吗？"))
    assert hint == PREFIX + "慢悠悠。这次换别的说法，别再用它们。"
    assert not overuse_hint(("慢悠悠，慢悠悠，慢悠悠。", "已经回来了。", "晚点见。"))
    assert not overuse_hint(REPLIES, settings={"threshold": 4})
    assert not overuse_hint(("不紧不慢。",) * 3 + ("嗯。",) * 30)


def test_native_prompt_counts_assistant_text_and_already_loaded_proactive_body():
    request = SimpleNamespace(system_prompt="人格提示", contexts=[
        {"role": "assistant", "content": REPLIES[0]},
        {"role": "assistant", "content": [{"type": "text", "text": REPLIES[1]}]},
        *({"role": "user", "content": "上课铃，天文馆。"},) * 3,
        {"role": "tool", "content": "不紧不慢。"},
    ])
    options = {"current_input": "咖啡豆到了吗？", "proactive_outputs": (REPLIES[0], REPLIES[2]),
               "names": ("小团子",), "settings": {"max_phrases": 1}}
    inject_native_hint(request, **options)
    inject_native_hint(request, **options)
    assert request.system_prompt.splitlines() == ["人格提示", PREFIX + "慢悠悠。这次换别的说法，别再用它们。"]
    request.system_prompt = "人格提示"
    inject_native_hint(request, **{**options, "settings": {"enabled": False}})
    assert request.system_prompt == "人格提示"


def _proactive_request():
    principal = AuthenticatedHostPrincipal(INSTALLATION, "host", SCOPE.platform, "user", SCOPE.bot_id, SCOPE.account_id)
    trigger = BackgroundPromptTrigger("contact", principal, SCOPE, NOW)
    materials = tuple(PromptMaterial("conversation.private_history", "recent_private_dialogue", json.dumps({
        "messages": [{"role": "user", "text": "今天可以继续吗？"},
                     {"role": "assistant", "text": "慢吞吞。愣怔怔。亮闪闪。嘀嘀咕咕。不紧不慢。慢悠悠。"}],
    }, ensure_ascii=False)) for _ in range(3))
    return PromptRequest(trigger, "角色提示", SealedForegroundContext(materials, (), ()), "主动表达",
                         RoleBinding(SCOPE, ROLE, 1), output_format="proactive_contact")


def _system(request, settings=None):
    envelope = SimpleNamespace(original=request, tools=(), tool_history=(), task_prompt="",
                               reasoning_rounds=(), record_prompt_snapshot=None, provider_capabilities=("text",))
    return build_wire(envelope, phrase_overuse_settings=settings).messages[0]["content"]


def test_wire_caps_hint_and_honors_switch():
    request = _proactive_request()
    hint, = (line for line in _system(request, {"max_phrases": 2}).splitlines() if line.startswith(PREFIX))
    assert len(hint.removeprefix(PREFIX).split("。", 1)[0].split("、")) == 2
    assert PREFIX not in _system(request, {"enabled": False})
    assert PREFIX not in request.instruction


def test_v2_group_chat_counts_only_attributed_assistant_replies():
    base = _proactive_request()
    materials = []
    for text in REPLIES:
        for kind, body in (("confirmed_group_reply", text), ("group_member_message", "上课铃，天文馆。")):
            materials.append(PromptMaterial("conversation.group_context", "group_conversation_history",
                json.dumps({"type": kind, "text": body, "speaker_name": "小团子"}, ensure_ascii=False)))
    materials.append(PromptMaterial("conversation.input", "current_source", "咖啡豆到了吗？"))
    request = replace(base, output_format="conversation", context=replace(base.context, materials=tuple(materials)))
    assert PREFIX + "慢悠悠。这次换别的说法，别再用它们。" in _system(request)


def test_staged_proactive_counts_before_compacting_history():
    base = _proactive_request()
    job = SimpleNamespace(plan={}, intent=SimpleNamespace(purpose="life"), automatic_origin=None,
                          diagnostic_origin=None, administrator_test=False)
    preferences = SimpleNamespace(values={"proactive_intensity_preset": "off", "proactive_intensity_body_chars": 0})
    options = dict(reactions_available=False, voice_available=False, poke_available=False, extra_instruction="")
    request = write_request_for(base, job, (), preferences, **options)
    assert len([m for m in request.context.materials if m.purpose == "recent_private_dialogue"]) == 2
    assert _system(request).count(PREFIX) == 1
    disabled = write_request_for(base, job, (), preferences, **options, phrase_overuse_settings={"enabled": False})
    assert PREFIX not in _system(disabled, {"enabled": False})
