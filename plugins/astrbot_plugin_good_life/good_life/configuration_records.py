"""Read only the life-owned settings staged in this plugin's database."""

from .configuration_import import LIFE_FIELDS, SHARED_FIELDS
from .storage import StorageReader


class ConfigurationTransitionPending(Exception):
    code = "life_configuration_pending"


class StagedConfigurationRecords:
    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)

    def dispatch(self, operation, **request):
        if operation == "snapshot" and not request:
            return self.storage.read_snapshot()
        if operation == "targets" and set(request) == {"domain", "target_keys", "fields"}:
            keys = request["target_keys"]
            if (not isinstance(keys, (tuple, list)) or len(keys) > 16
                    or any(type(key) is not str or not key for key in keys) or len(keys) != len(set(keys))):
                raise ValueError("invalid life configuration targets")
            # Hold the life storage lock across all layers of the effective setting.
            with self.reader.locked():
                return {key: self.dispatch("target", domain=request["domain"], target_key=key,
                                           fields=request["fields"]) for key in keys}
        if operation != "target" or set(request) != {"domain", "target_key", "fields"}:
            raise ValueError("unknown life configuration read")
        domain, key, fields = request["domain"], request["target_key"], request["fields"]
        if (domain not in ("life", "state") or type(key) is not str or not key or len(key) > 512
                or not isinstance(fields, (tuple, list)) or len(fields) > 512
                or any(type(name) is not str or not name for name in fields) or len(fields) != len(set(fields))):
            raise ValueError("invalid life configuration target")
        if domain == "life":
            allowed = {"role.life." + name for name in LIFE_FIELDS | SHARED_FIELDS}
            if not set(fields) <= allowed:
                raise ValueError("configuration field does not belong to life")
            target_table, value_table = "staged_configuration_targets", "staged_configuration_values"
        else:
            if any(not name.startswith("role.state.") for name in fields):
                raise ValueError("configuration field does not belong to state")
            target_table, value_table = "staged_state_configuration_targets", "staged_state_configuration_values"
        with self.reader.locked():
            journal = self.reader.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'", ())
            if journal and self.reader.one("""SELECT 1 FROM life_prepared_commands WHERE kind='configuration.patch'
                AND state='committed' AND json_extract(payload_json,'$.domain')=? LIMIT 1""", (domain,)):
                raise ConfigurationTransitionPending("life configuration awaits host confirmation")
            row = self.reader.one(f"SELECT revision FROM {target_table} WHERE target_key=?", (key,))
            values = () if not fields else self.reader.all(
                f"SELECT field_id,value_json,revision FROM {value_table} WHERE target_key=? AND field_id IN ({','.join('?' for _ in fields)}) ORDER BY field_id",
                (key, *fields))
            return {"revision": row["revision"] if row else 0,
                    "values": [{"field_id": item["field_id"], "value_json": item["value_json"], "revision": item["revision"]}
                               for item in values]}
