"""Pure constant-input domain updates, all evaluated at one coordinator time.

C > 0 means circadian alerting; sleepiness = clamp(normalized S - weight*C).
Energy only couples to confirmed sleep/rest mode and aggregate activity load.
Sleep debt has no second energy penalty in this first stage.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .config import number, require

MODEL_VERSION = "a6.body.shadow.v1"


def moment(value):
    if type(value) is str:
        value = datetime.fromisoformat(value)
    require(isinstance(value, datetime) and value.utcoffset() is not None, "time must be timezone-aware")
    return value.astimezone(timezone.utc)


def stamp(value):
    return moment(value).isoformat(timespec="microseconds")


def identifier(value):
    require(type(value) is str and value == value.strip() and 0 < len(value) <= 256, "invalid identifier")
    return value


def clamp(value):
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class Domain:
    name: str
    owns: tuple[str, ...]
    reads: tuple[str, ...]


DOMAINS = (
    Domain("sleep", ("sleep.pressure", "sleep.phase_s", "sleep.circadian", "sleep.debt_s", "sleep.sleepiness"),
           ("input.sleep_intervals",)),
    Domain("hunger", ("hunger",), ("input.sleep_mode", "input.activity_load", "meal.consumed", "drink.consumed")),
    Domain("energy", ("energy",), ("input.sleep_mode", "input.rest_mode", "input.activity_load")),
)


def owners(domains=DOMAINS):
    result = {}
    names = set()
    for domain in domains:
        require(domain.name not in names, "duplicate state module")
        names.add(domain.name)
        for key in domain.owns:
            require(key not in result, "duplicate state owner: " + key)
            result[key] = domain.name
    return result


def describe(parameters):
    return {"model_version": MODEL_VERSION, "parameter_version": parameters.version,
            "parameter_hash": parameters.content_hash, "owns": owners(),
            "reads": {domain.name: domain.reads for domain in DOMAINS},
            "updates": ("advance", "apply_effect", "project"), "expose_to_llm": False,
            "circadian_sign": "positive C reduces sleepiness",
            "energy_coupling": "actual sleep/rest mode and aggregate load; no debt penalty"}


def initialize(legacy, profile, at, parameters):
    """Preserve the complete source; unknown descriptions never receive a midpoint."""
    p, mapping = parameters.data, parameters.data["mapping"]
    source = legacy.get("values", {})
    energy = source.get("energy")
    energy = energy / mapping["energy_scale"] if type(energy) in (int, float) else None
    if energy is not None:
        number(energy, maximum=1)
    hunger = mapping["hunger_text"].get(source.get("hunger")) if type(source.get("hunger")) is str else None
    pressure = mapping["sleep_text"].get(source.get("sleep")) if type(source.get("sleep")) is str else None
    if profile is not None:
        require(type(profile) is dict and "observed_at" in profile,
                "sleep profile requires its calculation time in observed_at")
        require(moment(profile["observed_at"]) == moment(at),
                "sleep profile calculation time must equal initial observation")
    profile = profile or {}
    if profile.get("sleep_pressure") is not None:
        pressure = number(profile["sleep_pressure"], maximum=mapping["pressure_scale"]) / mapping["pressure_scale"]
        pressure = p["sleep"]["lower"] + pressure * (p["sleep"]["upper"] - p["sleep"]["lower"])
    debt = profile.get("sleep_debt_minutes")
    if debt is not None:
        debt = number(debt) * 60
        require(debt <= p["sleep"]["target_sleep_s"], "initial debt exceeds configured window target")
    state = {"sleep.pressure": pressure, "sleep.debt_s": debt, "hunger": hunger, "energy": energy}
    return project(state, at, parameters), {
        "at": stamp(at), "sleep_debt_s": debt,
        "mapping_version": parameters.version, "legacy": legacy, "sleep_profile": profile,
        "assumptions": ["configured text mappings are estimates", "unmapped values remain unavailable",
                        "legacy debt is a window seed, not fabricated sleep episodes"],
    }


def project(values, at, parameters):
    result = dict(values)
    p = parameters.data["sleep"]
    phase = (moment(at) - moment(p["phase_at"])).total_seconds() % p["period_s"]
    c = p["amplitude"] * math.cos(2 * math.pi * phase / p["period_s"])
    result.update({"sleep.phase_s": phase, "sleep.circadian": c})
    s = result["sleep.pressure"]
    result["sleep.sleepiness"] = None if s is None else clamp(
        (s - p["lower"]) / (p["upper"] - p["lower"]) - p["sleepiness_c_weight"] * c)
    return result


def advance(old, seconds, mode, parameters, switches):
    """Only read old; do not let domain iteration order create hidden coupling."""
    require(seconds >= 0, "negative elapsed time")
    result = dict(old)
    p = parameters.data
    if switches["sleep"] and old["sleep.pressure"] is not None:
        target = p["sleep"]["lower" if mode["sleeping"] else "upper"]
        tau = p["sleep"]["tau_sleep_s" if mode["sleeping"] else "tau_awake_s"]
        result["sleep.pressure"] = target + (old["sleep.pressure"] - target) * math.exp(-seconds / tau)
    if switches["hunger"] and old["hunger"] is not None:
        rate = p["hunger"]["sleep_per_s" if mode["sleeping"] else "awake_per_s"]
        rate += p["hunger"]["load_per_s"] * mode["load"]
        result["hunger"] = clamp(old["hunger"] + rate * seconds)
    if switches["energy"] and old["energy"] is not None:
        if mode["sleeping"] or mode["resting"]:
            tau = p["energy"]["sleep_tau_s" if mode["sleeping"] else "rest_tau_s"]
            ceiling = p["energy"]["recovery_ceiling"]
            # A constant load shifts the exponential equilibrium. Only the owner
            # integrates load; template cost estimates are deliberately absent.
            equilibrium = ceiling - tau * p["energy"]["load_per_s"] * mode["load"]
            result["energy"] = clamp(equilibrium + (old["energy"] - equilibrium) * math.exp(-seconds / tau))
        else:
            rate = p["energy"]["awake_per_s"] + p["energy"]["load_per_s"] * mode["load"]
            result["energy"] = clamp(old["energy"] - rate * seconds)
    return result


def debt_at(seed, intervals, at, parameters):
    """A rolling target minus actual sleep and an explicitly estimated old credit.

    The imported debt determines old sleep credit, aged out linearly over one
    window. This preserves the imported initial debt without inventing episodes.
    After one window only recorded sleep segments contribute. Unknown gaps have
    no actual sleep credit and remain estimated in snapshot metadata.
    """
    initial = seed["sleep_debt_s"]
    p = parameters.data["sleep"]
    end, origin = moment(at), moment(seed["at"])
    age = (end - origin).total_seconds()
    window, target = p["debt_window_s"], p["target_sleep_s"]
    if initial is None and age < window:
        return None
    old_credit = (target - initial) * max(0, 1 - age / window) if initial is not None else 0
    from datetime import timedelta
    start = max(origin, end - timedelta(seconds=window))
    slept = sum(max(0, (min(end, moment(item["end"]) if item["end"] is not None else end)
                        - max(start, moment(item["start"]))).total_seconds())
                for item in intervals if item["sleeping"])
    return max(0, target - old_credit - slept)


def apply_effect(values, effect, parameters, switches):
    require(effect["kind"] in ("meal.consumed", "drink.consumed"), "unsupported state effect")
    fraction = number(effect["fraction"], maximum=1)
    result = dict(values)
    if switches["hunger"] and result["hunger"] is not None:
        key = "meal_reduction" if effect["kind"] == "meal.consumed" else "drink_reduction"
        result["hunger"] = clamp(result["hunger"] - parameters.data["hunger"][key] * fraction)
    return result
