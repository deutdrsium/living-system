"""Prompt-only guidance derived from recent, attributed companion messages."""

from collections import Counter
from itertools import islice
import json
import re


_HISTORY_LIMIT = 30
_SCAN_LIMIT = 90
_TEXT_LIMIT = 512
_HINT_PREFIX = "你最近反复用过："
_HAN = re.compile(r"[㐀-䶿一-鿿]+")
_NAMED = re.compile(r"@([㐀-鿿]+)|《([^》]+)》")
_START_PARTICLES = frozenset("我你他她它的了着过也就都很又还和与而在从把被给让")
_END_PARTICLES = frozenset("的地得了着过吗呢吧啊呀哦嘛啦呗和与而把被给")
_STOP_WORDS = frozenset("""
我们 你们 他们 她们 自己 大家 对方 什么 怎么 怎样 哪个 哪里 这里 那里
这个 那个 这些 那些 这样 那样 这么 那么 一点 一下 一些 一个 一起 已经
今天 昨天 明天 现在 刚才 时候 以后 之前 之后 可能 可以 不用 不要 不会
没有 不是 就是 还是 也是 只是 如果 因为 所以 但是 而且 不过 然后
其实 觉得 感觉 知道 想想 真的 比较 非常 有点 谢谢 没关系 没问题 不客气
""".split())
_BACKGROUND_PURPOSES = frozenset({"confirmed_background_message", "dedup_candidate",
                                 "last_confirmed_reply", "confirmed_reply_context"})


def _grams(text):
    return {run[start:start + size] for run in _HAN.findall(text[:_TEXT_LIMIT])
            for size in (2, 3, 4) for start in range(len(run) - size + 1)}


_STOP_GRAMS = frozenset(gram for word in _STOP_WORDS for gram in _grams(word))


def _candidate(phrase):
    return (phrase[0] not in _START_PARTICLES and phrase[-1] not in _END_PARTICLES
            and not any(phrase[i:i + 2] in _STOP_GRAMS for i in range(len(phrase) - 1)))


def overuse_hint(outputs, *, current_inputs=(), recent_inputs=(), names=(), settings=None):
    """Rank by document frequency and give one slot to overlapping phrases."""
    settings = settings if isinstance(settings, dict) else {}
    if settings.get("enabled", True) is False:
        return ""
    threshold = settings.get("threshold", 3)
    threshold = max(2, min(_HISTORY_LIMIT, threshold)) if type(threshold) is int else 3
    limit = settings.get("max_phrases", 4)
    limit = max(1, min(5, limit)) if type(limit) is int else 4
    outputs = outputs[-_HISTORY_LIMIT:]
    if len(outputs) < threshold:
        return ""

    frequency, topic_frequency, excluded = Counter(), Counter(), set()
    for text in outputs:
        frequency.update(_grams(text))
        for match in _NAMED.finditer(text[:_TEXT_LIMIT]):
            excluded.update(_grams(match[1] or match[2]))
    for text in recent_inputs[-_HISTORY_LIMIT:]:
        topic_frequency.update(_grams(text))
    excluded.update(phrase for phrase, count in topic_frequency.items() if count >= 2)
    for text in (*current_inputs, *names):
        excluded.update(_grams(text))

    ranked = sorted((phrase for phrase, count in frequency.items()
                     if count >= threshold and phrase not in excluded and _candidate(phrase)),
                    key=lambda phrase: (-frequency[phrase], -len(phrase), phrase))
    selected = []
    for phrase in ranked:
        if any(phrase in chosen or chosen in phrase for chosen in selected):
            continue
        selected.append(phrase)
        if len(selected) == limit:
            break
    return _HINT_PREFIX + "、".join(selected) + "。这次换别的说法，别再用它们。" if selected else ""


def _message_text(content):
    if isinstance(content, str):
        return content[:_TEXT_LIMIT]
    if isinstance(content, list):
        return "\n".join(part["text"][:_TEXT_LIMIT] for part in content[:8]
                         if isinstance(part, dict) and part.get("type") == "text"
                         and isinstance(part.get("text"), str))[:_TEXT_LIMIT]
    return ""


def inject_native_hint(request, *, current_input, proactive_outputs=(), names=(), settings=None):
    """AstrBot supplies the native conversation window only on its request hook."""
    if isinstance(settings, dict) and settings.get("enabled", True) is False:
        return
    contexts = getattr(request, "contexts", None)
    contexts = contexts if isinstance(contexts, (list, tuple)) else ()
    outputs, inputs, history_names = [], [], []
    for item in contexts[-_SCAN_LIMIT:]:
        if not isinstance(item, dict) or item.get("role") not in ("assistant", "user"):
            continue
        text = _message_text(item.get("content"))
        if text:
            (outputs if item["role"] == "assistant" else inputs).append(text)
        if isinstance(item.get("name"), str):
            history_names.append(item["name"])
    native_outputs = set(outputs)
    outputs.extend(text for body in proactive_outputs if (text := _message_text(body)) and text not in native_outputs)
    hint = overuse_hint(outputs, current_inputs=(current_input,), recent_inputs=inputs,
                        names=(*names, *history_names), settings=settings)
    system = getattr(request, "system_prompt", "") or ""
    if hint and _HINT_PREFIX not in system:
        request.system_prompt = "\n".join(part for part in (system, hint) if part)


def with_material_hint(request, *, instruction=None, settings=None):
    instruction = request.instruction if instruction is None else instruction
    if _HINT_PREFIX in instruction:
        return instruction
    role = getattr(request.role_binding, "role", None)
    hint = material_hint(request.context.materials, settings=settings,
                         persona_name=getattr(role, "subject_id", ""))
    return instruction + "\n" + hint if hint else instruction


def material_hint(materials, *, settings=None, persona_name=""):
    """Read only attributed history bodies already in a V2 prompt."""
    if isinstance(settings, dict) and settings.get("enabled", True) is False:
        return ""
    outputs, inputs, current, seen = [], [], [], set()
    names = [persona_name]
    for item in materials:
        if item.purpose == "current_source" or item.purpose.startswith("merged_input:"):
            current.append(item.payload)
        background = item.owner == "delivery.history" and item.purpose in _BACKGROUND_PURPOSES
        dialogue = item.owner == "conversation.private_history" and item.purpose == "recent_private_dialogue"
        group = item.owner == "conversation.group_context" and item.purpose == "group_conversation_history"
        if not (background or dialogue or group) or len(item.payload) > 32768:
            continue
        try:
            history = json.loads(item.payload)
        except ValueError:
            continue
        if not isinstance(history, dict):
            continue
        messages = history.get("messages", [history])
        if not isinstance(messages, list):
            continue
        for message in islice(messages, _HISTORY_LIMIT):
            if not isinstance(message, dict):
                continue
            if background and message.get("owner") not in (
                    "proactive.contact", "conversation.confirmed_reply", "conversation.native_assistant"):
                continue
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            assistant = message.get("type") == "confirmed_group_reply" if group else message.get("role") == "assistant"
            user = message.get("type") == "group_member_message" if group else message.get("role") == "user"
            if background or assistant:
                # The last-reply material can duplicate a turn from dialogue.
                identifier = (message.get("delivered_at"), text)
                if identifier[0] and identifier in seen:
                    continue
                seen.add(identifier)
                outputs.append(text)
            elif user:
                inputs.append(text)
                if isinstance(name := message.get("speaker_name"), str):
                    names.append(name)
    return overuse_hint(outputs, current_inputs=current, recent_inputs=inputs, names=names, settings=settings)
