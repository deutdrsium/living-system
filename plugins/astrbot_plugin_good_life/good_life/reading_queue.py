"""Plan the next unread span, migrated from the companion reading queue.

The original text and authority are supplied by the host; this module does not
store a second book or make a claim that the planned span was read.
"""

import hashlib
import json


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def queue_key(context_key, source):
    return "role.reading.lazy:" + _digest([context_key, source])


def next_span(*, enabled, lineage_id, text, title, start):
    if not enabled:
        return None
    while start < len(text):
        end = min(start + 6000, len(text))
        if text[start:end].strip():
            return {"activity_id": "book-part-" + _digest([lineage_id, start]),
                    "start_offset": start, "end_offset": end,
                    "description": f"{title} · {start + 1}–{end} / {len(text)} 字符"}
        start = end
    return None
