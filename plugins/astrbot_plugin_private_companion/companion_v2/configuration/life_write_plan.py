"""Plan a configuration patch with the existing sparse-owner rules."""

from contextlib import contextmanager
from copy import copy
import sqlite3

from .life_provider import LIFE_FIELDS, LifeConfigurationStore
from .models import encode
from companion_v2.role_work.state.settings_scope import PhysicalSettingsOwner


class _ShadowStore:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.configuration_store = self
        self.db.executescript("""
            CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE configuration_targets(target_key TEXT PRIMARY KEY, target_json TEXT, revision INTEGER, updated_at TEXT);
            CREATE TABLE configuration_values(target_key TEXT, field_id TEXT, value_json TEXT, revision INTEGER,
                PRIMARY KEY(target_key, field_id));
        """)

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        if not nested:
            self.db.execute("BEGIN")
        try:
            yield self.db
            if not nested:
                self.db.commit()
        except BaseException:
            if not nested:
                self.db.rollback()
            raise

    def read_one(self, sql, parameters=()):
        return self.db.execute(sql, parameters).fetchone()

    def read_all(self, sql, parameters=()):
        return self.db.execute(sql, parameters).fetchall()


def build_plan(owner, *, domain, target, fields, changes, reset, observed_at):
    """Run the original scope/inheritance logic on a temporary snapshot.

    The resulting rows are only a proposal. Authorization and revision checks
    still happen on the host before its decision is recorded.
    """
    if domain not in ("life", "state"):
        raise ValueError("unknown configuration domain")
    life_fields = tuple(field for field in fields if domain == "state" or field.field_id.rsplit(".", 1)[-1] in LIFE_FIELDS)
    host_fields = tuple(field for field in fields if field not in life_fields)
    role = owner.role_target(target)
    targets = {layer.key: layer for item in (target, role) for layer in item.chain()}
    provider = LifeConfigurationStore(owner.store, domain, target, role, life_fields)
    host_ids = tuple(field.field_id for field in host_fields)
    originals = {}
    host_revisions = {}
    shadow = _ShadowStore()
    try:
        for key, layer in targets.items():
            life = provider._records[key]
            row = owner.store.read_one("SELECT revision FROM configuration_targets WHERE target_key = ?", (key,)) if host_ids else None
            host_revision = row["revision"] if row else 0
            host_revisions[key] = host_revision
            shadow.db.execute("INSERT INTO configuration_targets VALUES (?,?,?,?)",
                              (key, encode(layer.document()), max(host_revision, life["revision"]), ""))
            items = list(life["values"])
            if host_ids:
                items.extend(dict(row) for row in owner.store.read_all(
                    f"SELECT field_id, value_json, revision FROM configuration_values WHERE target_key = ? AND field_id IN ({','.join('?' for _ in host_ids)})",
                    (key, *host_ids)))
            originals[key] = {item["field_id"]: item["value_json"] for item in items}
            for item in items:
                shadow.db.execute("INSERT INTO configuration_values VALUES (?,?,?,?)",
                    (key, item["field_id"], item["value_json"], item["revision"]))
        clone = copy(owner)
        clone.store = clone.runtime = shadow
        current = clone.read(target, fields)
        if domain == "life":
            result = clone.write(target, fields, changes=changes, reset=reset,
                                 expected_revision=current.revision, observed_at=observed_at)
        else:
            result = PhysicalSettingsOwner.write(clone, target, fields, changes=changes, reset=reset,
                                                 expected_revision=current.revision, observed_at=observed_at)
        life_ids = {field.field_id for field in life_fields}
        plans = {"life": [], "host": []}
        for key in sorted(targets):
            previous = originals[key]
            current_values = {row["field_id"]: row["value_json"] for row in shadow.read_all(
                "SELECT field_id,value_json FROM configuration_values WHERE target_key=?", (key,))}
            changed = [name for name in sorted(set(previous) | set(current_values))
                       if previous.get(name) != current_values.get(name)]
            for destination in ("life", "host"):
                names = [name for name in changed if (name in life_ids) == (destination == "life")]
                if not names:
                    continue
                revision = provider._records[key]["revision"] if destination == "life" else host_revisions[key]
                plans[destination].append({"key": key, "document": targets[key].document(), "revision": revision,
                    "changes": [{"field_id": name, "value_json": current_values.get(name),
                                 **({"previous_json": previous.get(name)} if destination == "host" else {})}
                                for name in names]})
        if not plans["life"]:
            plans["life"].append({"key": target.key, "document": target.document(),
                                  "revision": provider._records[target.key]["revision"], "changes": []})
        relevant = sorted({target.key, role.key})
        return {"life_targets": plans["life"], "host_targets": plans["host"],
                "changed_fields": list(result.changed_fields),
                "life_revisions": {key: provider._records[key]["revision"] for key in relevant},
                "host_revision_keys": relevant if host_fields else []}
    finally:
        shadow.db.close()
