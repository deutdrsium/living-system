"""Read migrated configuration through the registered life provider."""

from companion_v2.execution.models import TaskError


OWNER_KEY = "life.configuration.execution_owner.v1"
LIFE_FIELDS = frozenset({
    "enabled", "timezone", "reading_chunk_chars", "reading_daily_segments", "reading_enabled",
    "reading_speed_chars_per_minute", "sleep_enabled", "sleep_minutes", "sleep_time", "sleep_timezone",
    "sleep_variation_minutes", "sleep_window_minutes", "social_existing_percent", "social_interval_minutes",
    "social_new_percent", "social_random_read_percent", "social_recommendation_enabled", "social_related_percent",
})


def transferred(store):
    row = store.read_one("SELECT value FROM runtime_metadata WHERE key=?", (OWNER_KEY,))
    value = row["value"] if row else "legacy"
    if value not in ("legacy", "good_life"):
        raise TaskError("life_configuration_owner_invalid", "unknown configuration owner")
    return value == "good_life"


class LifeConfigurationStore:
    """A fixed provider snapshot for the sparse-owner read algorithms."""

    def __init__(self, store, domain, target, role_target, fields):
        lease = getattr(store, "_life_configuration_lease", None)
        if lease is None:
            raise TaskError("life_provider_unavailable", "life configuration provider is unavailable")
        keys = sorted({layer.key for item in (target, role_target) for layer in item.chain()})
        ids = sorted(field.field_id for field in fields)
        try:
            records = lease.dispatch("configuration_read", "targets", domain=domain, target_keys=keys, fields=ids)
        except Exception as error:
            if getattr(error, "code", None) == "life_configuration_pending":
                raise TaskError("life_configuration_pending", "life configuration awaits host confirmation") from error
            raise
        if type(records) is not dict or set(records) != set(keys):
            raise TaskError("life_provider_data_invalid", "invalid life configuration targets")
        for record in records.values():
            if (type(record) is not dict or set(record) != {"revision", "values"}
                    or type(record["revision"]) is not int or record["revision"] < 0
                    or type(record["values"]) is not list):
                raise TaskError("life_provider_data_invalid", "invalid life configuration record")
            names = set()
            for value in record["values"]:
                if (type(value) is not dict or set(value) != {"field_id", "value_json", "revision"}
                        or value["field_id"] not in ids or value["field_id"] in names
                        or type(value["value_json"]) is not str or type(value["revision"]) is not int
                        or not 0 <= value["revision"] <= record["revision"]):
                    raise TaskError("life_provider_data_invalid", "invalid life configuration value")
                names.add(value["field_id"])
        self._records = records

    def read_one(self, statement, parameters):
        if not statement.startswith("SELECT revision FROM configuration_targets") or len(parameters) != 1:
            raise ValueError("unsupported configuration provider read")
        record = self._records[parameters[0]]
        return {"revision": record["revision"]}

    def read_all(self, statement, parameters):
        if not statement.startswith("SELECT field_id, value_json, revision FROM configuration_values"):
            raise ValueError("unsupported configuration provider read")
        requested = set(parameters[1:])
        return tuple(value for value in self._records[parameters[0]]["values"] if value["field_id"] in requested)
