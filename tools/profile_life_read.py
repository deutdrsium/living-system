"""Synthetic life.read benchmark; uses real host storage/authorization, no model calls."""
import asyncio
import cProfile
from datetime import timedelta
import io
import json
from pathlib import Path
import pstats
import sys
from time import perf_counter
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins/astrbot_plugin_private_companion"),
                str(ROOT / "plugins/astrbot_plugin_good_life")]
from tests.multi_life_support import MultiLifeFixture
from tests.test_management_access import NOW
from astrbot_host.life_page_reads import life_page_data
from astrbot_host.management_identity import dashboard_operator
from companion_v2.execution.models import digest
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.role_work.life import management as management_module
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State, RoleReadingReceipt
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord


class Fixture(MultiLifeFixture, unittest.IsolatedAsyncioTestCase):
    async def populate(self):
        app = await self.initialize()
        await self.enable()
        context = self.context()
        sources = []
        text = "这是一段合成的原文，用来测量阅读记录与权限检查。" * 10000
        for book in range(10):
            source = SourceRef(f"fixture-book-{book}", "role.life", context.scope,
                               VersionRef(f"fixture-book-{book}", 1), NOW - timedelta(days=60))
            app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
            sources.append(source)
        for n in range(500):
            completed = NOW - timedelta(days=n // 10, minutes=n % 10)
            source = sources[n % 10]
            receipt = RoleReadingReceipt(source, digest(text), len(text), n * 100, n * 100 + 100,
                                         completed, "合成读后感，记录本次阅读的想法。" * 30)
            activity = app.role_life.add_activity(RoleActivity(f"fixture-note-{n:04}", "role-a", context.scope,
                completed, State.COMPLETED, description=f"合成书 {n % 10} · {n * 100 + 1}–{n * 100 + 100} / {len(text)} 字符",
                reading=receipt, work=RoleWorkSpec("reading", context.key, f"fixture-{n}",
                    source=source, content_hash=receipt.content_hash, start_offset=receipt.start_offset,
                    end_offset=receipt.end_offset)))
            for i in range(n % 170, n % 170 + 1):
                app.role_interests.store.apply(context, activity, [{"name": f"合成兴趣 {i:03}", "reaction": "positive",
                    "wants_to_continue": True, "reason": "合成感受与兴趣证据。" * 10, "next_question": "接下来想了解什么？"}], NOW)
        return app


class ProductionFixture(Fixture):
    """Scale-only synthetic data, including the deployed life-provider boundary.

    No production content is used. The 3,500 dependency edges are an acyclic
    fan-in from ten reading originals to shared inputs; production edge topology
    is unknown. The small Fixture remains available to existing cursor tests.
    """
    async def populate(self):
        from dataclasses import replace
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from companion_v2.execution.models import encode
        from companion_v2.role_work.interests import scope_key
        from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
        from companion_v2.integration.life_activity_control_proxy import LifeActivityTransitionProxy
        from companion_v2.integration.exposure_session_proxy import ExposureSessionReadProxy
        from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
        from good_life.activity_import import stage_activities
        from good_life.exposure_import import stage_exposures
        from good_life.metadata_import import stage_metadata
        from good_life.storage import LifeStorage
        from good_life.activity_control import ActivityControl
        from good_life.activity_records import StagedActivityRecords
        from good_life.exposure_records import StagedExposureRecords
        from good_life.event_delivery import LifeEventDelivery
        from good_life.prepared_commands import PreparedCommands

        app = await super().populate()
        await self.command("configure", operation_key="fixture-enable-reading", changes={"reading_enabled": True})
        context = self.context()
        body = "合成来源正文。" * 2857  # about 60 KB UTF-8, not 60 KB ASCII characters
        with app.runtime.transaction():
            for n in range(2390):
                identifier = f"fixture-input-{n:04}"
                source = SourceRef(identifier, "fixture.source", context.scope, VersionRef(identifier, 1), NOW - timedelta(days=60))
                app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(body), body), observed_at=NOW).unwrap()
        with app.runtime.transaction() as connection:
            for book in range(10):
                for n in range(350):
                    source = f"fixture-book-{book}"
                    parent = f"fixture-input-{(book * 175 + n) % 2390:04}"
                    connection.execute("INSERT INTO source_dependencies VALUES (?, ?, 1, ?, ?, 1)", (source, source, parent, parent))
            # Retain valid head/revision JSON identities; only historical volume
            # and exposure volume are added, not scheduler or model work.
            owner = management_module.RoleLifeManagement(app, context).owner
            for n in range(475):
                activity = app.role_life.activity(f"fixture-note-{n:04}", owner=owner)
                updated = replace(activity, revision=2)
                connection.execute("INSERT INTO role_activity_revisions VALUES (?, ?, 2, ?, 'recorded', NULL, ?)",
                    (digest(updated.owner), updated.activity_id, updated.updated_at.isoformat(), encode(updated)))
                connection.execute("UPDATE role_activities SET revision=2, record_json=? WHERE owner_key=? AND activity_id=?",
                                   (encode(updated), digest(updated.owner), updated.activity_id))
            for n in range(25):
                activity = RoleActivity(f"fixture-social-{n:02}", "role-a", context.scope, NOW - timedelta(days=1),
                    State.COMPLETED, work=RoleWorkSpec("social", context.key, f"fixture-social-{n}"))
                app.role_life.add_activity(activity)
                connection.execute("INSERT INTO role_exposure_sessions VALUES (?, ?, 'fixture', '', 0)",
                                   (scope_key(context), activity.activity_id))
            interests = connection.execute("SELECT interest_id FROM role_interests ORDER BY interest_id").fetchall()
            for n in range(3200):
                aid, cid = f"fixture-social-{n % 25:02}", f"fixture-item-{n:04}"
                connection.execute("INSERT INTO role_exposures VALUES (?, ?, 'fixture', ?, ?, ?, '', ?, ?, NULL, '', 0)",
                    (scope_key(context), aid, cid, "合成接触", "合成简介" * 30, NOW.isoformat(), NOW.isoformat()))
                connection.execute("INSERT INTO role_exposure_topics VALUES (?, ?, 'fixture', ?, ?)",
                    (scope_key(context), aid, cid, interests[n % len(interests)][0]))
        self.shape = {table: app.runtime.read_one(f"SELECT count(*) AS n FROM {table}", ())["n"]
                      for table in ("sources", "source_dependencies", "role_exposures", "role_activity_revisions")}
        self.shape["source_payload_bytes"] = app.runtime.read_one(
            "SELECT sum(length(CAST(payload_json AS BLOB))) AS n FROM sources", ())["n"]

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / "life"
        version = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(app.runtime.database_path, expected_version=version, destination=destination)
        stage_exposures(app.runtime.database_path, expected_version=version, destination=destination,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(app.runtime.database_path, expected_version=version, destination=destination)
        self.life_storage = LifeStorage.open(destination)
        self.addCleanup(self.life_storage.close)
        PreparedCommands(self.life_storage).install()
        records, exposure, events = StagedActivityRecords(self.life_storage), StagedExposureRecords(self.life_storage), LifeEventDelivery(self.life_storage)
        self.plugin = SimpleNamespace()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=self.plugin, activated=True)
        registry = LifeProviderRegistry(SimpleNamespace(get_all_stars=lambda: [star]), owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers.update(activity_read=records.dispatch, activity_history=events.dispatch,
                        activity_control=ActivityControl(self.life_storage).dispatch, browsing_read=exposure.dispatch)
        self.registry, self.registration = registry, registry.register(plugin=self.plugin, api_version=API_VERSION, handlers=handlers)
        lease = registry.lease()
        reader = LifeLedgerReadProxy(app.runtime, lease)
        app.role_life.bind_reader(reader, transition_provider=LifeActivityTransitionProxy(lease))
        self.addCleanup(app.role_life.unbind_reader, reader)
        browsing = ExposureSessionReadProxy(app.runtime, lease, life=app.role_life,
            visible=app.role_social._visible_contact, writable=app.role_social._active_contact)
        app.role_social.exposure.session_reader = app.role_life.browsing_reader = browsing
        from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
        with app.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        from functools import partial
        from good_life.economy import Wallet
        from good_life.economy.access import wallet_page
        from good_life.web_api import LifePageAPI
        from astrbot_host.life_dashboard import dispatch
        wallet = Wallet("CNY")
        wallet.install(self.life_storage)
        async def dashboard(**kwargs):
            return await dispatch(self.controller, registry, **kwargs)
        self.plugin._runtime = SimpleNamespace(api=SimpleNamespace(life_dashboard=dashboard), token=self.registration,
            ready=True, read_wallet_page=partial(wallet_page, wallet))
        self.page = LifePageAPI(self.plugin)
        self.route = {"target": "first", "role": digest("role-a")}
        return app

    async def page_read(self, view, *, wallet=False):
        if str(ROOT / "tests") not in sys.path:
            sys.path.insert(0, str(ROOT / "tests"))
        from test_life_page_reads import LifePageReadTests
        query = "" if wallet else "compact=1&limit=20&view=" + view
        return self.ok(await LifePageReadTests.call(self, self.page.wallet if wallet else self.page.life, query=query))

    async def loop_probe(self, view):
        gaps = []
        async def heartbeat():
            previous = perf_counter()
            while True:
                await asyncio.sleep(.005)
                now = perf_counter()
                gaps.append(now - previous)
                previous = now
        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(.01)
        start = perf_counter()
        pending = asyncio.create_task(self.page_read(view))
        await asyncio.sleep(.01)
        wallet_start = perf_counter()
        await self.page_read(view, wallet=True)
        wallet_seconds = perf_counter() - wallet_start
        await pending
        elapsed = perf_counter() - start
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        return {"life_seconds": elapsed, "wallet_seconds": wallet_seconds,
                "heartbeat_max_gap_seconds": max(gaps), "heartbeat_ticks": len(gaps)}


async def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--view", choices=("archive", "reading", "interests", "overview"))
    parser.add_argument("--baseline", help="load the read/projection modules directly from this git revision")
    parser.add_argument("--production-shape", action="store_true", help="profile the actual paired routes with scaled SQLite and provider data")
    args = parser.parse_args()
    fixture = ProductionFixture() if args.production_shape else Fixture()
    fixture.setUp()
    try:
        app = await fixture.populate()
        if args.baseline:
            import subprocess
            from companion_v2.role_work import interests
            from companion_v2.integration import exposure_session_proxy
            from companion_v2.sources import dependencies
            for module in (dependencies, interests, management_module, exposure_session_proxy):
                relative = Path(module.__file__).relative_to(ROOT).as_posix()
                source = subprocess.check_output(["git", "show", args.baseline + ":" + relative], cwd=ROOT)
                if module is dependencies and b"sources_governance_idx" not in source:
                    # Match the prior physical read path, not only Python code.
                    with app.runtime.transaction() as connection:
                        connection.execute("DROP INDEX sources_governance_idx")
                exec(compile(source, "baseline/" + relative, "exec"), module.__dict__)
            if args.production_shape:
                from astrbot_host import life_api
                life_api.RoleLifeManagement = management_module.RoleLifeManagement
                app.role_life.browsing_reader.__class__ = exposure_session_proxy.ExposureSessionReadProxy
        manager = management_module.RoleLifeManagement(app, fixture.context())
        operator = dashboard_operator(app.runtime, "admin", at=NOW)
        if args.production_shape:
            print(json.dumps({"shape": fixture.shape}), flush=True)
            for view in (args.view,) if args.view else ("reading", "interests", "overview"):
                timings = []
                for _ in range(3):
                    start = perf_counter()
                    data = await fixture.page_read(view)
                    timings.append(perf_counter() - start)
                print(json.dumps({"view": view, "seconds": timings,
                    "bytes": len(json.dumps(data, ensure_ascii=False).encode("utf-8")),
                    "loop_probe": await fixture.loop_probe(view)}), flush=True)
                profile = cProfile.Profile()
                profile.runcall(manager.status, operator, archive_kind="reading_note", page_size=20, compact=True, view=view)
                output = io.StringIO()
                pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(35)
                print(output.getvalue(), flush=True)
            return
        options = {"archive_kind": "reading_note"}
        if args.compact:
            options.update(page_size=20, compact=True, view=args.view)
        def read():
            return life_page_data(manager.status(operator, **options))
        timings = []
        for _ in range(3):
            start = perf_counter()
            data = read()
            timings.append(perf_counter() - start)
        start = perf_counter()
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        serialization = perf_counter() - start
        print(json.dumps({"seconds": timings, "bytes": len(payload), "journals": len(data["journals"]),
            "interests": len(data["interests"]), "serialization_seconds": serialization}))
        profile = cProfile.Profile()
        profile.runcall(read)
        output = io.StringIO()
        pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(35)
        print(output.getvalue())
    finally:
        # This standalone benchmark has no unittest Runner; execute the fixture's
        # registered async cleanups on the loop already driving the benchmark.
        import inspect
        while fixture._cleanups:
            cleanup, args, kwargs = fixture._cleanups.pop()
            result = cleanup(*args, **kwargs)
            if inspect.isawaitable(result):
                await result


if __name__ == "__main__":
    asyncio.run(main())
