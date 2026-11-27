"""Apply the companion's phrase filter to AstrBot reply and history objects."""

from astrbot.core.message.components import Plain


FILTER_EXTRA = "_companion_v2_banned_phrases"
_HISTORY_EXTRA = "_companion_v2_banned_phrase_history"


def filter_chain(owner, phrases, *, surface):
    chain = getattr(owner, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return
    parts = tuple(part.text for part in chain if isinstance(part, Plain))
    cleaned = phrases.segments(parts, surface=surface)
    if cleaned == parts:
        return
    remaining = iter(cleaned)
    output = []
    for part in chain:
        if not isinstance(part, Plain):
            output.append(part)
        elif text := next(remaining):
            part.text = text
            output.append(part)
    owner.chain = output
    return True


def filter_response(event, response, *, history_original=None):
    getter = getattr(event, "get_extra", None)
    phrases = getter(FILTER_EXTRA) if callable(getter) else None
    if not phrases or getattr(response, "role", None) != "assistant" or getattr(response, "is_chunk", False):
        return
    original = getattr(response, "completion_text", "") or ""
    chain = getattr(response, "result_chain", None)
    if any(isinstance(part, Plain) for part in getattr(chain, "chain", ()) or ()):
        if not filter_chain(chain, phrases, surface="astrbot_reply"):
            return
        # AstrBot derives completion_text from its chain. The independent text
        # attribute on older response objects still needs the same update.
        cleaned = " ".join(part.text for part in chain.chain if isinstance(part, Plain))
    else:
        cleaned = phrases.text(original, surface="astrbot_reply")
    if cleaned != getattr(response, "completion_text", ""):
        response.completion_text = cleaned
    if history_original is not None and cleaned != original:
        event.set_extra(_HISTORY_EXTRA, (history_original, cleaned))


def sync_final_history(event, run_context):
    """AstrBot can append the final assistant text before its response hook."""
    texts = event.get_extra(_HISTORY_EXTRA)
    if texts is None:
        return
    event.set_extra(_HISTORY_EXTRA, None)
    messages = getattr(run_context, "messages", None)
    if not messages or getattr(messages[-1], "role", None) != "assistant":
        return
    original, cleaned = texts
    for part in getattr(messages[-1], "content", ()) or ():
        if getattr(part, "type", None) == "text" and getattr(part, "text", None) == original:
            part.text = cleaned
