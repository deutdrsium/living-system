"""Keep one pending span per imported original, creating the next after completion."""

from companion_v2.execution.models import digest
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec


def queue_key(context_key, source):
    return "role.reading.lazy:" + digest([context_key, source])


def queue_next(ledger, context, source, content_hash, text, title, start, at):
    if not ledger.runtime.read_one("SELECT 1 FROM runtime_metadata WHERE key = ?",
                                   (queue_key(context.key, source),)):
        return None
    while start < len(text):
        end = min(start + 6000, len(text))
        if text[start:end].strip():
            identifier = "book-part-" + digest([source.version.lineage_id, start])
            work = RoleWorkSpec("reading", context.key, identifier, source, content_hash, start, end)
            return ledger.add_activity(RoleActivity(identifier, context.role_binding.role.subject_id,
                context.scope, at, RoleActivityState.PLANNED,
                description=f"{title} · {start + 1}–{end} / {len(text)} 字符", work=work))
        start = end
    return None
