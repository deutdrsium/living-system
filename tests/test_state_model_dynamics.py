from datetime import timedelta
import math
import unittest

from test_state_model_support import NOW, StateModelCase, parameters
from good_life.state_model import models


class BodyTrajectoryTests(StateModelCase):
    def test_continuous_awake_and_overnight_use_two_process_exact_solutions(self):
        self.interval("awake", 0, 16 * 3600)
        self.interval("night", 16 * 3600, 24 * 3600, sleeping=True)
        awake = self.model.advance("role", to=NOW + timedelta(hours=16))["values"]
        expected = 1 - (1 - .42) * math.exp(-16 / 18)
        self.assertAlmostEqual(awake["sleep.pressure"], expected, places=14)
        asleep = self.model.advance("role", to=NOW + timedelta(hours=24))["values"]
        self.assertAlmostEqual(asleep["sleep.pressure"], expected * math.exp(-8 / 4), places=14)
        self.assertLess(asleep["sleep.pressure"], awake["sleep.pressure"])
        self.assertGreater(asleep["energy"], awake["energy"])
        self.assertGreater(asleep["hunger"], awake["hunger"] if awake["hunger"] < 1 else .99)

    def test_nap_and_message_wake_grace_are_actual_separate_sleep_segments(self):
        self.interval("nap-before-wake", 0, 1200, sleeping=True)
        self.interval("message-grace", 1200, 2400)
        self.interval("nap-resumed", 2400, 3600, sleeping=True)
        actual = self.model.advance("role", to=NOW + timedelta(hours=1))["values"]
        s = .42 * math.exp(-1200 / 14400)
        s = 1 - (1 - s) * math.exp(-1200 / 64800)
        s *= math.exp(-1200 / 14400)
        self.assertAlmostEqual(actual["sleep.pressure"], s, places=14)
        uninterrupted = .42 * math.exp(-3600 / 14400)
        self.assertGreater(actual["sleep.pressure"], uninterrupted)
        # Credit only 2400 seconds, never the interrupted 1200 second gap.
        debt = 201600 - (201600 - 5400) * (1 - 3600 / 604800) - 2400
        self.assertAlmostEqual(actual["sleep.debt_s"], max(0, debt), places=9)

    def test_circadian_sign_phase_and_midnight_continuity(self):
        p = self.parameters
        at_peak = models.project(self.initial["values"], NOW, p)
        at_trough = models.project(self.initial["values"], NOW + timedelta(hours=12), p)
        self.assertAlmostEqual(at_peak["sleep.circadian"], .15)
        self.assertAlmostEqual(at_trough["sleep.circadian"], -.15)
        self.assertLess(at_peak["sleep.sleepiness"], at_trough["sleep.sleepiness"])
        for hour in (16, 24):  # UTC midnight and configured phase wrap.
            left = models.project(self.initial["values"], NOW + timedelta(hours=hour, microseconds=-1), p)
            right = models.project(self.initial["values"], NOW + timedelta(hours=hour, microseconds=1), p)
            self.assertLess(abs(left["sleep.circadian"] - right["sleep.circadian"]), 1e-9)

    def test_half_meal_and_cancelled_half_work_only_apply_actual_parts(self):
        self.interval("exercise-completed-half", 0, 600, load=1)
        self.interval("after-cancel", 600, 1200)
        self.meal("eaten-half", 600, .5)
        final = self.model.advance("role", to=NOW + timedelta(seconds=1200))["values"]
        self.assertAlmostEqual(final["hunger"], .65 + .00003 * 600 - .6 * .5 + .00002 * 600)
        self.assertAlmostEqual(final["energy"], .37 - .00002 * 600 - .000005 * 600)
        self.assertEqual(self.count("life_state_model_effects"), 1)
        self.assertEqual(self.count("life_events"), 0)

    def test_drink_does_not_create_permanent_energy(self):
        self.meal("drink", 0, kind="drink.consumed")
        values = self.model.repo.read("role")["values"]
        self.assertAlmostEqual(values["hunger"], .60)
        self.assertEqual(values["energy"], .37)

    def test_rest_and_sleep_restore_energy_but_rest_keeps_awake_pressure(self):
        self.interval("rest", 0, 3600, resting=True)
        final = self.model.advance("role", to=NOW + timedelta(hours=1))["values"]
        self.assertAlmostEqual(final["energy"], 1 - (1 - .37) * math.exp(-3600 / 28800))
        self.assertGreater(final["sleep.pressure"], .42)
        self.assertGreater(final["hunger"], .65)

    def test_large_duration_remains_bounded_and_old_debt_seed_ages_out(self):
        end = NOW + timedelta(days=8)
        self.interval("known-awake", 0, 8 * 86400)
        while models.moment(self.model.repo.read("role")["at"]) < end:
            final = self.model.advance("role", to=end)["values"]
        self.assertEqual(final["sleep.debt_s"], 201600)
        for key in ("energy", "hunger", "sleep.pressure", "sleep.sleepiness"):
            self.assertGreaterEqual(final[key], 0)
            self.assertLessEqual(final[key], 1)


class StepInvarianceTests(unittest.TestCase):
    def test_48_hour_trace_across_awake_night_nap_interruption_meals_load_and_rest(self):
        p = parameters()
        switches = {"sleep": True, "hunger": True, "energy": True}
        origin, seed = models.initialize({"values": {"energy": 37, "hunger": "有点饿", "sleep": "有些困"}},
                                        {"sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": NOW.isoformat()}, NOW, p)
        # Each segment is a verified boundary. Tick size never changes the inputs.
        segments = [(14400, False, False, .7), (3600, False, True, 0), (32400, False, False, .2),
                    (14400, True, False, 0), (1200, False, False, 0), (13200, True, False, 0),
                    (18000, False, False, .5), (1800, True, False, 0), (19800, False, False, .1),
                    (7200, False, True, 0), (18000, False, False, .8), (28800, True, False, 0)]
        meals = {14400, 50400, 108000, 144000}
        # Insert all effect boundaries as well as physical mode boundaries.
        boundaries, cursor = [], 0
        for seconds, sleeping, resting, load in segments:
            end = cursor + seconds
            cuts = sorted({cursor, end} | {at for at in meals if cursor < at < end})
            for left, right in zip(cuts, cuts[1:]):
                boundaries.append((left, right, {"sleeping": sleeping, "resting": resting, "load": load}))
            cursor = end
        self.assertEqual(cursor, 48 * 3600)
        results = {}
        intervals = [{"start": models.stamp(NOW + timedelta(seconds=left)),
                      "end": models.stamp(NOW + timedelta(seconds=right)), "sleeping": mode["sleeping"]}
                     for left, right, mode in boundaries]
        for step in (1, 60, 900, 3600, 172800):
            values = dict(origin)
            for left, right, mode in boundaries:
                remaining = right - left
                while remaining:
                    seconds = min(step, remaining)
                    values = models.advance(values, seconds, mode, p, switches)
                    remaining -= seconds
                if right in meals:
                    values = models.apply_effect(values, {"kind": "meal.consumed", "fraction": .5}, p, switches)
            values = models.project(values, NOW + timedelta(hours=48), p)
            values["sleep.debt_s"] = models.debt_at(seed, intervals, NOW + timedelta(hours=48), p)
            results[step] = values
        maximum = max(abs(result[key] - results[172800][key]) for result in results.values() for key in origin)
        print(f"A6 48h step invariance (1/60/900/3600/event seconds): max_abs_error={maximum:.12g}; tolerance=1e-10")
        self.assertLess(maximum, 1e-10)  # Roundoff only: no numerical ODE integration here.
