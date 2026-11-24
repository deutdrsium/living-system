"""Read role and conversation calendar records from the life provider.

The host still resolves historical role ownership and rechecks source and
audience access. This proxy never falls back to the sealed calendar records.
"""

from companion_v2.agenda.records import calendar_role_owner
from companion_v2.execution.models import TaskError, digest
from companion_v2.storage.versioned_records import owner_key as conversation_owner_key


class LifeCalendarRecords:
    def __init__(self, provider, *, legacy_owners):
        if not callable(legacy_owners):
            raise ValueError("host-verified legacy calendar ownership is required")
        self.provider, self.legacy_owners = provider, legacy_owners

    def role_key(self, context):
        return digest(["calendar-role.v1", calendar_role_owner(context)])

    def conversation_key(self, context):
        return conversation_owner_key(context)

    def operation_key(self, context):
        return self.conversation_key(context)

    def _call(self, operation, **request):
        try:
            return self.provider.dispatch(operation, **request)
        except TaskError:
            raise
        except KeyError:
            raise
        except Exception as error:
            if getattr(error, "code", None) == "life_calendar_pending":
                raise TaskError("life_calendar_pending", "calendar change awaits host confirmation") from error
            raise TaskError("life_provider_unavailable", "calendar life provider is unavailable") from error

    def planned_spending(self, **request):
        return self._call("planned_spending", **request)

    def class_sessions(self, **request):
        return self._call("class_sessions", **request)

    @staticmethod
    def _record(record, *, identifier=None):
        if (type(record) is not dict or type(record.get("id")) is not str or not record["id"]
                or type(record.get("revision")) is not int or record["revision"] < 1
                or type(record.get("updated_at")) is not str
                or (identifier is not None and record["id"] != identifier)):
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid calendar record")
        return record

    def _get(self, owner, identifier):
        record = self._call("get", owner_key=owner, identifier=identifier)
        return self._record(record, identifier=identifier) if record is not None else None

    def _legacy(self, context):
        result, seen = [], set()
        for item in self.legacy_owners(context):
            if type(item) is not dict or set(item) != {"owner_key", "record_id"}:
                raise TaskError("life_provider_data_invalid", "legacy calendar ownership is invalid")
            owner, identifier = item["owner_key"], item["record_id"]
            if (type(owner) is not str or not owner or type(identifier) is not str or not identifier
                    or (owner, identifier) in seen):
                raise TaskError("life_provider_data_invalid", "legacy calendar ownership is invalid")
            seen.add((owner, identifier))
            record = self._get(owner, identifier)
            if record is not None:
                if record.get("kind") != "role_plan":
                    raise TaskError("life_provider_data_invalid", "legacy role plan changed its ownership kind")
                result.append({"owner_key": owner, "record": record})
        return tuple(result)

    def _page(self, owner, *, after_id, limit, kinds):
        page = self._call("list", owner_key=owner, after_id=after_id, limit=limit, kinds=kinds)
        if not isinstance(page, (tuple, list)) or len(page) > limit:
            raise TaskError("life_provider_data_invalid", "life provider returned an invalid calendar page")
        records, cursor = [], after_id
        for item in page:
            item = self._record(item)
            if item["id"] <= cursor or (kinds is not None and item.get("kind") not in kinds):
                raise TaskError("life_provider_data_invalid", "life provider returned unordered calendar records")
            records.append(item)
            cursor = item["id"]
        return tuple(records)

    def get(self, context, identifier, *, storage_key=None):
        if storage_key is not None:
            return self._get(storage_key, identifier)
        shared = self._get(self.role_key(context), identifier)
        if shared is not None:
            return shared
        legacy = next((item["record"] for item in self._legacy(context) if item["record"]["id"] == identifier), None)
        return legacy or self._get(self.conversation_key(context), identifier)

    def list(self, context, *, after_id="", limit=100, kinds=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(after_id) is not str:
            raise TaskError("record_page_invalid", "invalid record page")
        if kinds is not None:
            kinds = tuple(kinds)
            if not kinds or any(type(kind) is not str or not kind for kind in kinds):
                raise TaskError("record_page_invalid", "invalid record kind filter")
        shared = self._page(self.role_key(context), after_id=after_id, limit=limit, kinds=kinds)
        local = self._page(self.conversation_key(context), after_id=after_id, limit=limit, kinds=kinds)
        records = {item["id"]: item for item in shared}
        for item in self._legacy(context):
            record = item["record"]
            if record["id"] > after_id and (kinds is None or record.get("kind") in kinds):
                records.setdefault(record["id"], record)
        for record in local:
            records.setdefault(record["id"], record)
        return tuple(records[key] for key in sorted(records)[:limit])

    def shared_role_plans(self, context, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TaskError("record_page_invalid", "invalid shared role-plan page")
        records = {item["id"]: item for item in self._page(self.role_key(context), after_id="", limit=limit, kinds=("role_plan",))}
        for item in self._legacy(context):
            record = item["record"]
            if record["id"]:
                records.setdefault(record["id"], record)
        ordered = tuple(records[key] for key in sorted(records))
        return ordered[:limit], len(ordered) > limit

    def version(self, context, identifier, revision, *, storage_key=None):
        if storage_key is not None:
            keys = (storage_key,)
        else:
            role_key = self.role_key(context)
            legacy = tuple(item["owner_key"] for item in self._legacy(context) if item["record"]["id"] == identifier)
            keys = (role_key, *legacy, self.conversation_key(context))
            if self._get(role_key, identifier) is not None:
                keys = (role_key,)
        for key in dict.fromkeys(keys):
            try:
                result = self._call("version", owner_key=key, identifier=identifier, revision=revision)
            except KeyError:
                continue
            result = self._record(result, identifier=identifier)
            if result["revision"] != revision:
                raise TaskError("life_provider_data_invalid", "life provider returned the wrong calendar revision")
            return result
        raise TaskError("record_version_unavailable", "original record version is unavailable")

    def receipt(self, context, operation_id, *, storage_key=None):
        keys = ((storage_key,) if storage_key is not None else
                (self.role_key(context), *(item["owner_key"] for item in self._legacy(context)), self.conversation_key(context)))
        for key in dict.fromkeys(keys):
            try:
                result = self._call("receipt", owner_key=key, operation_id=operation_id)
            except KeyError:
                continue
            if type(result) is not dict or result.get("operation_id") != operation_id:
                raise TaskError("life_provider_data_invalid", "life provider returned an invalid calendar receipt")
            return result
        raise TaskError("operation_unavailable", "original domain operation is unavailable")

    def by_reminder(self, context, reminder_id):
        records = self._call("by_reminder", owner_keys=(self.role_key(context), self.conversation_key(context)),
                             reminder_id=reminder_id)
        if not isinstance(records, (tuple, list)):
            raise TaskError("life_provider_data_invalid", "life provider returned invalid calendar reminders")
        result = tuple(self._record(item) for item in records)
        if any(item.get("reminder_id") != reminder_id for item in result):
            raise TaskError("life_provider_data_invalid", "life provider returned the wrong reminder")
        return result
