"""Real provider snapshots, scaled provenance and actual Dashboard requests."""
import asyncio
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from profile_life_read import ProductionFixture
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.integration.exposure_session_proxy import ExposureSessionReadProxy
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.sources.repositories import SourceRepository
from tests.test_management_access import NOW


class ProductionLifeReadTests(ProductionFixture):
    async def test_views_reuse_metadata_without_reading_bodies_and_recheck_revocation(self):
        app = await self.populate()
        self.assertEqual({key: self.shape[key] for key in (
            "sources", "source_dependencies", "role_exposures", "role_activity_revisions")},
            {"sources": 2400, "source_dependencies": 3500, "role_exposures": 3200, "role_activity_revisions": 1000})
        self.assertGreater(self.shape["source_payload_bytes"], 140_000_000)
        original = SourceRepository.validate_reference_for_audience
        original_key = ExposureSessionReadProxy._key
        for view in ("reading", "interests", "overview"):
            checks, keys = [], []
            def checked(repository, reference, **kwargs):
                checks.append(reference)
                return original(repository, reference, **kwargs)
            def scoped_key(reader, context):
                keys.append(context)
                return original_key(reader, context)
            with patch.object(SourceRepository, "validate_reference_for_audience", checked), \
                    patch.object(ExposureSessionReadProxy, "_key", scoped_key), \
                    patch("companion_v2.sources.repositories._source_from_row", side_effect=AssertionError("metadata view loaded a source body")):
                data = await self.page_read(view)
            self.assertEqual(len(checks), 10, "one live governance check per original, not per activity")
            self.assertLess(len(keys), 120, "projection scope identity must not be digested for each receipt")
            self.assertFalse(data["journals"])
            self.assertEqual(len(data["interests"]), 170 if view == "interests" else 0)
            if view == "interests":
                self.assertTrue(all(item["compact"] for item in data["interests"]))
                self.assertGreater(sum(item["exposure_count"] for item in data["interests"]), 0)
        # Revoke a shared original input through the real producer API. A new
        # snapshot cannot retain the preceding request's rows or positive checks.
        parent = SourceRef("fixture-input-0200", "fixture.source", self.context().scope,
                           VersionRef("fixture-input-0200", 1), NOW - timedelta(days=60))
        app.role_journal.sources.revoke(parent, expected_governance_revision=1, observed_at=NOW).unwrap()
        data = await self.page_read("reading")
        activities = {item["activity_id"]: item for item in data["activities"]}
        self.assertEqual(activities["fixture-note-0000"]["unavailable_reason"], "source_revoked")
        self.assertIsNone(activities["fixture-note-0002"]["unavailable_reason"])

    async def test_wallet_waits_for_provider_snapshot_without_blocking_the_loop(self):
        await self.populate()
        entered, release = Event(), Event()
        original = RoleLifeManagement._status
        def gated(manager, operator, **kwargs):
            # Unlike the old gate before status(), this holds the real life
            # provider snapshot lock and does no GIL-bound work while waiting.
            entered.set()
            if not release.wait(5):
                raise AssertionError("snapshot gate was not released")
            return original(manager, operator, **kwargs)
        life = wallet = None
        try:
            with patch.object(RoleLifeManagement, "_status", gated):
                life = asyncio.create_task(self.page_read("reading"))
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3))
                wallet = asyncio.create_task(self.page_read("reading", wallet=True))
                self.assertTrue(await asyncio.wait_for(asyncio.sleep(.02, result=True), .5))
                self.assertFalse(wallet.done(), "wallet shares the provider storage lock, not the event loop")
                release.set()
                await life
                data = await wallet
                self.assertTrue(data["enabled"])
        finally:
            release.set()
            if life is not None:
                await life
            if wallet is not None:
                await wallet
