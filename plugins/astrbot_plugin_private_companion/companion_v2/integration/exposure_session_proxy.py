"""Host adapter for scoped browsing-session reads, without a host DB handle."""

from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.refs import ReferenceValidationError, RoleOwnerRef
from companion_v2.role_work.interests import scope_key


class ExposureSessionReadProxy:
    def __init__(self, runtime, registry, *, life=None, visible=None, writable=None):
        self.runtime, self.registry, self.life, self.visible, self.writable = runtime, registry, life, visible, writable

    def active(self):
        return self.life is None or self.life.activity_owner() == "good_life"

    def _key(self, context):
        if context.scope.installation_id != self.runtime.installation_id:
            raise ReferenceValidationError("browsing context belongs to another installation")
        return scope_key(context)

    def _read(self, context, activity_id, *, previous):
        key = self._key(context)
        operation = "previous_session" if previous else "session"
        row = self.registry.dispatch("browsing_read", operation, scope_key=key, activity_id=activity_id)
        if row is None:
            return None
        if (type(row) is not dict or set(row) != {"scan_id", "scope_key", "activity_id", "platform"}
                or type(row["scan_id"]) is not int or row["scan_id"] < 1 or row["scope_key"] != key
                or type(row["activity_id"]) is not str or type(row["platform"]) is not str
                or (previous and (row["activity_id"] == activity_id or row["platform"] == "unavailable"))
                or (not previous and row["activity_id"] != activity_id)):
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid browsing session")
        if self.visible is not None and not self.visible(context, row["activity_id"]):
            return None
        return row["platform"]

    def _item(self, context, value):
        fields = {"scope_key", "activity_id", "platform", "content_id", "title", "summary", "source_url", "fetched_at",
                  "exposed_at", "attempted_at", "coverage", "random_attempt"}
        if (type(value) is not dict or set(value) != fields or value["scope_key"] != self._key(context)
                or any(type(value[name]) is not str for name in ("activity_id", "platform", "content_id", "title", "summary",
                                                                 "source_url", "fetched_at", "coverage"))
                or any(value[name] is not None and type(value[name]) is not str for name in ("exposed_at", "attempted_at"))
                or type(value["random_attempt"]) is not int or value["random_attempt"] not in (0, 1)):
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid browsing receipt")
        if self.visible is not None and not self.visible(context, value["activity_id"]):
            return None
        return value

    def exposure(self, context, activity_id, platform, content_id):
        row = self.registry.dispatch("browsing_read", "exposure", scope_key=self._key(context),
            activity_id=activity_id, platform=platform, content_id=content_id)
        if row is None:
            return None
        row = self._item(context, row)
        if row is not None and (row["activity_id"], row["platform"], row["content_id"]) != (activity_id, platform, content_id):
            raise TaskError("life_provider_data_invalid", "browsing receipt identity changed")
        return row

    def recent(self, context, *, limit=100):
        rows = self.registry.dispatch("browsing_read", "recent", scope_key=self._key(context), limit=limit)
        if not isinstance(rows, (list, tuple)) or len(rows) > limit:
            raise TaskError("life_provider_data_invalid", "browsing history is invalid")
        return tuple(item for row in rows if (item := self._item(context, row)) is not None)

    def attempted(self, context, platform, content_id):
        after = None
        while True:
            rows = self.registry.dispatch("browsing_read", "attempted", scope_key=self._key(context), platform=platform,
                                          content_id=content_id, after_activity=after, limit=100)
            if (not isinstance(rows, (list, tuple)) or len(rows) > 100
                    or any(type(item) is not str or not item for item in rows)
                    or tuple(rows) != tuple(sorted(set(rows))) or after is not None and rows and rows[0] <= after):
                raise TaskError("life_provider_data_invalid", "browsing attempt history is invalid")
            if any(self.visible is None or self.visible(context, item) for item in rows):
                return True
            if len(rows) < 100:
                return False
            after = rows[-1]

    def authorize_write(self, context, activity_id):
        self._key(context)
        if self.writable is None or not self.writable(context, activity_id):
            raise TaskError("life_browsing_changed", "browsing activity is no longer active or authorized")

    def command(self, context, activity_id, operation, **request):
        self.authorize_write(context, activity_id)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        return self.registry.dispatch("browsing_command", operation, owner_key=digest(owner), context_key=context.key,
                                      scope_key=self._key(context), activity_id=activity_id, **request)

    def session_platform(self, context, activity_id):
        return self._read(context, activity_id, previous=False)

    def previous_platform(self, context, activity_id):
        return self._read(context, activity_id, previous=True)
