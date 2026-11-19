"""A6 shadow-only body models; explicit opt-in, separate data and no live writes.

Load a versioned Parameters package and explicitly enable the required models
in ShadowConfig. Supply a trusted synchronous access adapter with
``authorize(owner_key, action)`` and ``storage_key(owner_key)``; the latter maps
the host-verified envelope owner to its existing role-body storage key. Identity
is never recomputed here. The bundled example is an uncalibrated simulation
fixture and is never loaded automatically. Initialize from migrated observations;
a supplied sleep profile must carry its UTC-aware ``observed_at`` calculation
time matching the initial observation, so elapsed wake time is never counted
again from an already-evolved profile.

Register ongoing actual phases with ``observe_interval(..., end=None)`` before
advancing. They keep their asserted sleep/rest/load mode until ``close_interval``
records the actual end (including early wakes). This allows live sleep pressure
and energy diagnostics during sleep without predicting a planned wake time.
Close each phase before advancing beyond its actual wake/pause/cancel boundary;
late corrections still require explicit historical revision. Completed finite
observations remain immutable. Schema v2 upgrades only the shadow interval table
from v1, preserving its rows/hashes and all other records transactionally.

Legacy imported timestamps are adapted without changing the original records:
current VersionedRecords/stamp producers write aware UTC and state_import copies
values unchanged; older offset-free ISO values are explicitly assumed UTC and
that assumption is retained. New commands and profile times remain strictly
aware. Register consumed portions, then call advance with one explicit time.
No background tasks, activity selection or host state output use these values.

Rollback: close the embedding, then remove only life_state_model_* objects and
state_model_schema_version after saving diagnostics if needed. No legacy table,
event cursor, receipt or execution-ownership marker is changed. Parameter and
switch changes on existing trajectories require a future explicit migration;
opening a different package never silently replaces in-flight model parameters.
"""
from .config import Parameters, ShadowConfig
from .engine import ShadowState

__all__ = ("Parameters", "ShadowConfig", "ShadowState")
