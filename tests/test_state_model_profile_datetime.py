from datetime import timedelta, timezone

from test_state_model_support import NOW, StateModelCase


class ProfileDatetimeTests(StateModelCase):
    def test_aware_datetime_profile_is_normalized_and_equal_offset_is_accepted(self):
        with self.storage.transaction() as db:
            self.add_role(db, "datetime-profile")
        profile = {"sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": NOW}
        record = self.model.initialize("datetime-profile", at=NOW, sleep_profile=profile)
        self.assertEqual(record["seed"]["sleep_profile"]["observed_at"], NOW.isoformat(timespec="microseconds"))
        with self.storage.transaction() as db:
            self.add_role(db, "other-zone-profile")
        other_zone = NOW.astimezone(timezone(timedelta(hours=8)))
        record = self.model.initialize("other-zone-profile", at=NOW, sleep_profile={
            "sleep_pressure": 42, "sleep_debt_minutes": 90, "observed_at": other_zone})
        self.assertEqual(record["seed"]["sleep_profile"]["observed_at"], NOW.isoformat(timespec="microseconds"))

    def test_profile_datetime_with_different_instant_is_rejected(self):
        with self.storage.transaction() as db:
            self.add_role(db, "wrong-profile-time")
        with self.assertRaises(ValueError):
            self.model.initialize("wrong-profile-time", at=NOW, sleep_profile={
                "sleep_pressure": 42, "observed_at": NOW + timedelta(seconds=1)})
