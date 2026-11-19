"""Explicit, versioned simulation parameters. No calibrated physiology defaults."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType

from ..state_primitives import digest


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, *, minimum=0, maximum=None, positive=False):
    require(type(value) in (int, float) and math.isfinite(value), "finite numeric parameter required")
    require(value > minimum if positive else value >= minimum, "parameter below minimum")
    require(maximum is None or value <= maximum, "parameter above maximum")
    return float(value)


def frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({key: frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    return value


@dataclass(frozen=True)
class Parameters:
    version: str
    content_hash: str
    data: object

    @classmethod
    def from_dict(cls, data):
        # A round trip detaches caller-owned containers and rejects non-JSON values.
        data = json.loads(json.dumps(data, allow_nan=False))
        require(set(data) == {"version", "units", "sleep", "hunger", "energy", "mapping", "offline"},
                "parameter package fields differ")
        require(type(data["version"]) is str and bool(data["version"].strip()), "parameter version required")
        require(data["units"] == "seconds", "all time parameters must use seconds")
        fields = {
            "sleep": {"lower", "upper", "tau_awake_s", "tau_sleep_s", "amplitude", "period_s",
                      "phase_at", "sleepiness_c_weight", "debt_window_s", "target_sleep_s"},
            "hunger": {"awake_per_s", "sleep_per_s", "load_per_s", "meal_reduction", "drink_reduction"},
            "energy": {"sleep_tau_s", "rest_tau_s", "recovery_ceiling", "awake_per_s", "load_per_s"},
            "mapping": {"energy_scale", "pressure_scale", "hunger_text", "sleep_text"},
            "offline": {"max_advance_s", "unknown_policy"},
        }
        for section, expected in fields.items():
            require(type(data[section]) is dict and set(data[section]) == expected,
                    "invalid parameter section: " + section)
        sleep, hunger, energy, mapping, offline = (data[key] for key in fields)
        for key in ("lower", "upper", "amplitude"):
            number(sleep[key], maximum=1)
        require(sleep["lower"] < sleep["upper"], "sleep lower must be below upper")
        for key in ("tau_awake_s", "tau_sleep_s", "period_s", "debt_window_s", "target_sleep_s"):
            number(sleep[key], positive=True)
        require(sleep["target_sleep_s"] <= sleep["debt_window_s"], "sleep target exceeds debt window")
        number(sleep["sleepiness_c_weight"])
        from .models import moment
        moment(sleep["phase_at"])
        for value in hunger.values():
            number(value)
        require(hunger["sleep_per_s"] > 0, "sleep hunger rate must be positive")
        number(energy["sleep_tau_s"], positive=True)
        number(energy["rest_tau_s"], positive=True)
        number(energy["recovery_ceiling"], maximum=1)
        number(energy["awake_per_s"])
        number(energy["load_per_s"])
        for key in ("energy_scale", "pressure_scale"):
            number(mapping[key], positive=True)
        for key in ("hunger_text", "sleep_text"):
            require(type(mapping[key]) is dict, "text mapping must be a dictionary")
            for text, value in mapping[key].items():
                require(type(text) is str and bool(text.strip()), "text mapping key is empty")
                number(value, minimum=sleep["lower"] if key == "sleep_text" else 0,
                       maximum=sleep["upper"] if key == "sleep_text" else 1)
        require(type(offline["max_advance_s"]) is int and offline["max_advance_s"] > 0,
                "offline cap must be positive integer seconds")
        require(offline["unknown_policy"] == "estimated_awake", "unsupported offline prediction policy")
        return cls(data["version"], digest(data), frozen(data))

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class ShadowConfig:
    enabled: bool = False
    sleep: bool = False
    hunger: bool = False
    energy: bool = False
    parameters: Parameters | None = None

    def __post_init__(self):
        require(all(type(value) is bool for value in (self.enabled, self.sleep, self.hunger, self.energy)),
                "model switches must be booleans")
        if self.enabled:
            require(isinstance(self.parameters, Parameters), "explicit parameter package required")

    @property
    def switches(self):
        return {key: getattr(self, key) for key in ("sleep", "hunger", "energy")}
