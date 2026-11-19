"""Role-wide sleep defaults with explicit window overrides for routines."""

from copy import copy
from dataclasses import replace
from datetime import time
from zoneinfo import ZoneInfo

from companion_v2.configuration.models import ApplyMode, ConfigLayer, ConfigTarget, ConfigurationError
from companion_v2.configuration.owners import OwnerSnapshot
from companion_v2.configuration.life_provider import LIFE_FIELDS as TRANSFERRED_FIELDS, LifeConfigurationStore, transferred
from companion_v2.configuration.role_settings import RoleSharedSettingsOwner
from companion_v2.configuration.registry import FieldDefinition, ValueKind
from .rest_gate import DEFAULTS as REST_GATE_DEFAULTS, LIMITS as REST_GATE_LIMITS
from .rest_gate import validate_changes as validate_rest_changes, validate_values as validate_rest_values

GROUP = "role.life"
DEFAULTS = {"enabled": False, "diary_enabled": True, "diary_time": "23:50", "timezone": "Asia/Shanghai",
            "sleep_enabled": False, "sleep_time": "00:30", "sleep_timezone": "Asia/Shanghai",
            "sleep_variation_minutes": 20, "sleep_window_minutes": 120, "sleep_minutes": 480,
            "reading_enabled": False, "reading_chunk_chars": 1500, "reading_daily_segments": 2,
            "reading_speed_chars_per_minute": 800, "social_interval_minutes": 30,
            "social_existing_percent": 60, "social_related_percent": 25, "social_new_percent": 15,
            "social_recommendation_enabled": True, "social_random_read_percent": 25,
            "derive_enabled": True,
            "diary_style_prompt": "",
            "dream_theme_candidates": "", "dream_mixed_themes_enabled": False,
            "dream_intimate_theme_enabled": False, "dream_afterglow_mode": "auto",
            **REST_GATE_DEFAULTS}
OWNER = "role.life.settings"
SLEEP_FIELDS = frozenset(("sleep_enabled", "sleep_time", "sleep_timezone", "sleep_variation_minutes",
                          "sleep_window_minutes", "sleep_minutes"))
AFTERGLOW_MODES = ("auto", "轻", "标准", "明显")
EXPLORATION_FIELDS = ("social_existing_percent", "social_related_percent", "social_new_percent")


def validate_exploration(values):
    weights = [values[name] for name in EXPLORATION_FIELDS]
    if any(type(value) is not int or not 0 <= value <= 100 for value in weights) or sum(weights) != 100:
        raise ValueError("social exploration percentages must total 100")


def configuration_fields():
    fields = []
    for name, value in DEFAULTS.items():
        kind = ValueKind.BOOLEAN if type(value) is bool else ValueKind.INTEGER if type(value) is int else ValueKind.TEXT
        bounds = ({"minimum": 0, "maximum": 120} if name == "sleep_variation_minutes" else
                  {"minimum": 0, "maximum": 360} if name == "sleep_window_minutes" else
                  {"minimum": 200, "maximum": 6000} if name == "reading_chunk_chars" else
                  {"minimum": 1, "maximum": 6} if name == "reading_daily_segments" else
                  {"minimum": 1, "maximum": 100000} if name == "reading_speed_chars_per_minute" else
                  {"minimum": 0, "maximum": 10080} if name == "social_interval_minutes" else
                  {"minimum": 0, "maximum": 100} if name in (*EXPLORATION_FIELDS, "social_random_read_percent") else
                  {"minimum": REST_GATE_LIMITS[name][0], "maximum": REST_GATE_LIMITS[name][1]}
                  if name in REST_GATE_LIMITS else {"minimum": 1, "maximum": 1440})
        fields.append(FieldDefinition(GROUP + "." + name, GROUP, GROUP, kind, value,
            layers=(ConfigLayer.WINDOW, ConfigLayer.ROLE), apply_mode=ApplyMode.IMMEDIATE, owner_id=OWNER,
            **(bounds if kind is ValueKind.INTEGER else {})))
    return tuple(fields)


def target_for(context):
    return ConfigTarget(context.scope.installation_id, ConfigLayer.WINDOW, context.role_binding.role, context.scope)


def values(snapshot):
    # A configuration-service snapshot carries ``values``; an owner snapshot
    # carries the same effective fields under ``fields``.
    source = getattr(snapshot, "values", None)
    if not isinstance(source, dict):
        source = {key: field.value for key, field in snapshot.fields.items()}
    result = {name.rsplit(".", 1)[-1]: value for name, value in source.items()}
    ZoneInfo(result["timezone"])
    ZoneInfo(result["sleep_timezone"])
    for name in ("diary_time", "sleep_time"):
        parsed = time.fromisoformat(result[name])
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond or len(result[name]) != 5:
            raise ValueError("use HH:MM local time")
    if type(result["sleep_variation_minutes"]) is not int or not 0 <= result["sleep_variation_minutes"] <= 120:
        raise ValueError("sleep variation must be 0..120 minutes")
    if type(result["sleep_window_minutes"]) is not int or not 0 <= result["sleep_window_minutes"] <= 360:
        raise ValueError("sleep window must be 0..360 minutes")
    if type(result["reading_chunk_chars"]) is not int or not 200 <= result["reading_chunk_chars"] <= 6000:
        raise ValueError("reading chunk must be 200..6000 characters")
    if type(result["reading_daily_segments"]) is not int or not 1 <= result["reading_daily_segments"] <= 6:
        raise ValueError("daily reading count must be 1..6 segments")
    if type(result["reading_speed_chars_per_minute"]) is not int or not 1 <= result["reading_speed_chars_per_minute"] <= 100000:
        raise ValueError("reading speed must be 1..100000 characters per minute")
    if type(result["social_interval_minutes"]) is not int or not 0 <= result["social_interval_minutes"] <= 10080:
        raise ValueError("social interval must be 0..10080 minutes")
    validate_exploration(result)
    if type(result["social_recommendation_enabled"]) is not bool or type(result["social_random_read_percent"]) is not int or not 0 <= result["social_random_read_percent"] <= 100:
        raise ValueError("invalid recommendation settings")
    if result["dream_afterglow_mode"] not in AFTERGLOW_MODES:
        raise ValueError("unsupported dream afterglow mode")
    # The eight rest-gate fields are closed here too, so a malformed stored value
    # degrades to the defaults instead of reaching the reply gate.
    validate_rest_values(result)
    return result


def dream_settings(runtime, context):
    """Best-effort dream writing preferences; unusable stored values degrade to defaults."""
    try:
        snapshot = LifeConfigurationOwner(runtime).read(target_for(context), configuration_fields())
        return values(snapshot)
    except (ValueError, TypeError):
        return dict(DEFAULTS)


class LifeConfigurationOwner(RoleSharedSettingsOwner):
    owner_id = OWNER
    # Before role-wide sleep settings were introduced, this group used the
    # generic sparse owner.  Keep that identity readable for an already
    # recorded operation, but never accept it for a newly created operation.
    legacy_owner_ids = ("configuration",)

    def is_shared(self, field):
        return field.group_id == GROUP and field.field_id.rsplit(".", 1)[-1] in SLEEP_FIELDS

    # The rest-gate fields are window-scoped and must not reach the reader in a
    # shape it would have to coerce, so a write is checked before it is stored.
    def preview_set(self, target, fields, *, changes):
        result = super().preview_set(target, fields, changes=changes)
        validate_rest_changes(changes)
        self._validate_exploration_patch(result, changes, ())
        return result

    def write(self, target, fields, *, changes, reset, expected_revision, observed_at):
        if transferred(self.store):
            from companion_v2.execution.models import TaskError
            raise TaskError("life_configuration_write_unavailable", "life configuration handoff is unavailable")
        validate_rest_changes(changes, reset)
        self._validate_exploration_patch(self.read(target, fields).fields, changes, reset)
        return super().write(target, fields, changes=changes, reset=reset,
                             expected_revision=expected_revision, observed_at=observed_at)

    @staticmethod
    def _validate_exploration_patch(current, changes, reset):
        names = {GROUP + "." + name for name in EXPLORATION_FIELDS}
        if not names.intersection((*changes, *reset)):
            return
        values = {name: (DEFAULTS[name] if GROUP + "." + name in reset else
                  changes.get(GROUP + "." + name, current[GROUP + "." + name].value)) for name in EXPLORATION_FIELDS}
        try:
            validate_exploration(values)
        except ValueError as error:
            raise ConfigurationError("life_exploration_invalid", str(error)) from error

    def read(self, target, fields):
        if not transferred(self.store):
            snapshot = super().read(target, fields)
        else:
            life_fields = tuple(field for field in fields if field.field_id.rsplit(".", 1)[-1] in TRANSFERRED_FIELDS)
            host_fields = tuple(field for field in fields if field not in life_fields)
            host = super().read(target, host_fields) if host_fields else OwnerSnapshot(0, (), {})
            if life_fields:
                proxy_owner = copy(self)
                proxy_owner.store = LifeConfigurationStore(self.store, "life", target, self.role_target(target), life_fields)
                life = RoleSharedSettingsOwner.read(proxy_owner, target, life_fields)
            else:
                life = OwnerSnapshot(0, (), {})
            snapshot = OwnerSnapshot(host.revision + life.revision,
                tuple(("host:" + key, revision) for key, revision in host.dependencies)
                + tuple(("life:" + key, revision) for key, revision in life.dependencies),
                {**host.fields, **life.fields})
        timezone_id, sleep_timezone_id = GROUP + ".timezone", GROUP + ".sleep_timezone"
        if (sleep_timezone_id in snapshot.fields and snapshot.fields[sleep_timezone_id].source is None
                and timezone_id in snapshot.fields):
            fallback = dict(snapshot.fields)
            fallback[sleep_timezone_id] = replace(fallback[sleep_timezone_id], value=fallback[timezone_id].value,
                source=fallback[timezone_id].source, explicit=fallback[timezone_id].explicit,
                source_revision=fallback[timezone_id].source_revision)
            snapshot = OwnerSnapshot(snapshot.revision, snapshot.dependencies, fallback, snapshot.lifecycle_token)
        return snapshot
