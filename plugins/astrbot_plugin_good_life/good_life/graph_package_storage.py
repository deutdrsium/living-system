"""Default-off graph archive. No registration, routes, migration or travel writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from .graph_packages import assert_stable_places, decode_bounded_json, diff_packages, validate_package
from .storage import LifeStorage


class GraphPackageStore:
    def __init__(self, storage, *, enabled=False):
        if not isinstance(storage, LifeStorage):
            raise TypeError("graph archive requires LifeStorage")
        self.storage = storage
        self.enabled = enabled is True

    @staticmethod
    def _exists(connection):
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='geo_graph_versions'").fetchone() is not None

    @classmethod
    def _latest(cls, connection, graph_id):
        if not cls._exists(connection):
            return None
        row = connection.execute("SELECT package_json FROM geo_graph_versions WHERE graph_id=? ORDER BY version DESC LIMIT 1",
                                 (graph_id,)).fetchone()
        return validate_package(decode_bounded_json(row[0])) if row else None

    def latest(self, graph_id):
        with self.storage._lock:
            return self._latest(self.storage._connection, graph_id)

    def current_ref(self, graph_id):
        """Lightweight confirmed head identity; full reads still validate the package."""
        with self.storage._lock:
            connection = self.storage._connection
            if not self._exists(connection):
                return None
            row = connection.execute('SELECT version,digest FROM geo_graph_versions WHERE graph_id=? ORDER BY version DESC LIMIT 1',
                                     (graph_id,)).fetchone()
            return {'graph_id': graph_id, 'version': row[0], 'digest': row[1]} if row else None

    def version(self, graph_id, version, *, digest=None):
        """Read one confirmed immutable revision; never substitute the latest."""
        with self.storage._lock:
            connection = self.storage._connection
            if not self._exists(connection):
                return None
            row = connection.execute("SELECT package_json FROM geo_graph_versions WHERE graph_id=? AND version=?",
                                     (graph_id, version)).fetchone()
            package = validate_package(decode_bounded_json(row[0])) if row else None
            if package is not None and digest is not None and package["digest"] != digest:
                raise ValueError("confirmed graph digest differs from pinned revision")
            return package

    def preview(self, package):
        candidate = validate_package(package)
        with self.storage._lock:
            return diff_packages(self._latest(self.storage._connection, candidate["graph_id"]), candidate)

    def confirm(self, package, *, expected_current_digest, expected_proposed_digest, confirmed=False):
        """Archive precisely the reviewed content; a stale review must be repeated."""
        if not self.enabled:
            raise ValueError("graph import is disabled")
        if confirmed is not True:
            raise ValueError("explicit confirmation is required")
        candidate = validate_package(package)
        if candidate["digest"] != expected_proposed_digest:
            raise ValueError("proposed package changed since review")
        with self.storage.transaction() as connection:
            old = self._latest(connection, candidate["graph_id"])
            if (old["digest"] if old else None) != expected_current_digest:
                raise ValueError("current graph changed since review")
            summary = diff_packages(old, candidate)
            if not summary["can_import"]:
                raise ValueError("graph version must increase")
            if self._exists(connection):
                for row in connection.execute("SELECT package_json FROM geo_graph_versions WHERE graph_id=?", (candidate["graph_id"],)):
                    assert_stable_places(validate_package(decode_bounded_json(row[0])), candidate)
            connection.execute("""CREATE TABLE IF NOT EXISTS geo_graph_versions (
                graph_id TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL,
                package_json TEXT NOT NULL, diff_json TEXT NOT NULL, imported_at TEXT NOT NULL,
                PRIMARY KEY(graph_id, version))""")
            connection.execute("INSERT INTO geo_graph_versions VALUES (?, ?, ?, ?, ?, ?)",
                (candidate["graph_id"], candidate["version"], candidate["digest"],
                 json.dumps(candidate, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                 json.dumps(summary, ensure_ascii=False, allow_nan=False), datetime.now(timezone.utc).isoformat()))
        return summary

    def history(self, graph_id):
        with self.storage._lock:
            connection = self.storage._connection
            if not self._exists(connection):
                return []
            return [{"version": row[0], "digest": row[1], "diff": decode_bounded_json(row[2]), "imported_at": row[3]}
                    for row in connection.execute("SELECT version, digest, diff_json, imported_at FROM geo_graph_versions WHERE graph_id=? ORDER BY version", (graph_id,))]
