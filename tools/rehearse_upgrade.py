"""Check a synthetic phase-one database upgrade; never accepts production DB paths.

--baseline-commit optionally builds the fixture using historical initializers
from the local Git object database. CI uses the pinned, unchanged v71 schema.
All data is generated inside a new TemporaryDirectory and deleted on exit.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
import hashlib
from importlib.util import find_spec
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins/astrbot_plugin_private_companion"),
                str(ROOT / "plugins/astrbot_plugin_good_life")]
from companion_v2.storage.runtime import StorageRuntime
from companion_v2.storage.schema import V71_SCHEMA_STATEMENTS
from good_life.storage import LifeStorage
from good_life.runtime import LifeRuntime
from good_life.academics import AcademicsConfig
from good_life.kernel import KernelConfig
from good_life.state_model import ShadowConfig

NO_CONSOLE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
V71_SHA = "80982fba99feda03d57805f50b9558f4f65b911c7a1ee02a6a2f84be6289df72"
FORBIDDEN = ("life_kernel_", "life_wallet_", "life_academic_", "life_study_", "life_campus_", "life_city_", "life_state_model_")


def historical_module(commit, path, name, package):
    source = subprocess.check_output(["git", "show", commit + ":" + path], cwd=ROOT,
                                     creationflags=NO_CONSOLE)
    module = ModuleType(name)
    module.__package__ = package
    module.__file__ = str(ROOT / path)
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def rows(path):
    with closing(sqlite3.connect(path)) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall() for name in tables}


@contextmanager
def offline_astrbot():
    if "astrbot" in sys.modules or find_spec("astrbot") is not None:
        yield
        return
    # Only platform imports are stubbed. The host facade, provider registry,
    # storage and scheduler implementations come from the upgraded plugins.
    names = ("astrbot", "astrbot.api", "astrbot.api.message_components", "astrbot.core",
             "astrbot.core.message", "astrbot.core.message.message_event_result")
    modules = {name: ModuleType(name) for name in names}
    modules["astrbot.api.message_components"].Plain = object
    modules["astrbot.core.message.message_event_result"].MessageChain = object
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name in modules:
            sys.modules.pop(name, None)


async def rehearse_host_binding(runtime, upgraded):
    with offline_astrbot():
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import PROVIDER_ID
        from companion_v2.integration.life_ledger_router import LifeLedgerRouter
        from companion_v2.role_work.ledger import PersistentRoleLifeLedger

        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=runtime.plugin, activated=True)
        app = SimpleNamespace(runtime=upgraded, role_life=LifeLedgerRouter(PersistentRoleLifeLedger(upgraded)))
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]),
                                     session=SimpleNamespace(application=app), _terminated=False)
        api = UnifiedCompanionExtensionAPI(controller)
        try:
            await runtime.bind(api)
            assert runtime.ready and runtime.api is api, "life runtime failed to bind to the upgraded host"
            schedulers = ("state", "calendar", "reading", "social")
            for name in schedulers:
                assert getattr(runtime.scheduler, name + "_scheduler") is not None, f"missing {name} scheduler"
            return list(schedulers)
        finally:
            await runtime.close()
            api.close_life_providers()
            api.close_memory_providers()


def rehearse(*, baseline_commit=None):
    if hashlib.sha256(json.dumps(V71_SCHEMA_STATEMENTS).encode()).hexdigest() != V71_SHA:
        raise ValueError("v71 schema differs from the recorded 7.1.17 production schema")
    old_host, old_life = StorageRuntime, LifeRuntime
    if baseline_commit:
        if not re.fullmatch(r"[0-9a-f]{40}", baseline_commit):
            raise ValueError("baseline commit must be a full Git commit SHA")
        # Historical runtime imports current sibling schemas; verify those files
        # and LifeStorage are identical to the selected baseline commit.
        paths = ["plugins/astrbot_plugin_private_companion/companion_v2/storage",
                 "plugins/astrbot_plugin_good_life/good_life/storage.py"]
        changed = subprocess.check_output(["git", "diff", "--name-only", baseline_commit, "--", *paths],
                                          cwd=ROOT, text=True, creationflags=NO_CONSOLE).splitlines()
        if any(path != paths[0] + "/runtime.py" for path in changed):
            raise ValueError("historical initializer dependencies changed")
        old_host = historical_module(baseline_commit, paths[0] + "/runtime.py",
            "companion_v2.storage.upgrade_baseline_runtime", "companion_v2.storage").StorageRuntime
        old_life = historical_module(baseline_commit, "plugins/astrbot_plugin_good_life/good_life/runtime.py",
            "good_life.upgrade_baseline_runtime", "good_life").LifeRuntime
    with tempfile.TemporaryDirectory(prefix="lsr-synthetic-upgrade-") as temporary:
        root = Path(temporary)
        host_root, life_root = root / "host", root / "life"
        original_host = old_host.open(data_root=host_root, installation_id="synthetic-upgrade")
        host_db = original_host.database_path
        original_host.close()
        with closing(sqlite3.connect(host_db)) as db:
            db.execute("INSERT INTO runtime_metadata VALUES('synthetic.preserved', 'never-reset')")
            for domain in ("calendar", "activity", "state", "configuration"):
                db.execute("INSERT INTO runtime_metadata VALUES(?, 'good_life')", ("life." + domain + ".execution_owner.v1",))
            db.execute("INSERT INTO inbox(consumer,event_id,payload_hash,created_at) VALUES('synthetic','already-consumed','hash','2026-11-16T00:00:00Z')")
            db.commit()
        configuration = old_host.open_configuration(data_root=root / "configuration", installation_id="synthetic-upgrade")
        configuration_db = configuration.database_path
        configuration.close()
        sys.path.insert(0, str(ROOT / "tools"))
        from stage_life_migration import stage_all
        stage_all(host_db, configuration_db, runtime_version="companion-v2.storage.v71",
                  configuration_version="configuration-store.v1", destination=life_root)
        storage = LifeStorage.open(life_root)
        runtime = old_life(storage, object())
        asyncio.run(runtime.close())
        storage.close()
        life_db = life_root / "good-life.sqlite3"
        with closing(sqlite3.connect(life_db)) as db:
            db.execute("INSERT INTO life_metadata VALUES('life.phase1.cutover_ready.v1', 'ready')")
            db.execute("INSERT INTO life_events(event_id,owner_key,context_key,kind,payload_json,content_hash,occurred_at) VALUES('synthetic-event','owner','context','state.change','{}','hash','2026-11-16T00:00:00Z')")
            db.execute("INSERT INTO life_event_deliveries VALUES(1, 'synthetic-consumer', '2026-11-16T00:00:01Z')")
            db.commit()
        before_host, before_life = rows(host_db), rows(life_db)
        before_configuration = rows(configuration_db)
        with closing(StorageRuntime.open(data_root=host_root, installation_id="synthetic-upgrade")) as upgraded:
            upgraded_configuration = StorageRuntime.open_configuration(data_root=root / "configuration", installation_id="synthetic-upgrade")
            upgraded_configuration.close()
            assert before_configuration == rows(configuration_db), "configuration rows changed on upgrade"
            with closing(LifeStorage.open(life_root)) as storage:
                runtime = LifeRuntime(storage, object(), reading_executor_enabled=False, reading_abort_own_enabled=False,
                    academics_config=AcademicsConfig(enabled=False), kernel_config=KernelConfig(enabled=False),
                    state_model_config=ShadowConfig(enabled=False))
                assert runtime.kernel is None and runtime.academics is None and runtime.state_model is None
                assert runtime.reading_executor_enabled is False and runtime.reading_abort_own_enabled is False
                schedulers = asyncio.run(rehearse_host_binding(runtime, upgraded))
        after_host, after_life = rows(host_db), rows(life_db)
        assert before_host == after_host, "host schema/rows changed on default-off upgrade"
        assert all(after_life.get(table) == saved for table, saved in before_life.items()), "existing life rows changed"
        # The already-active phase-one state scheduler can initialize its empty
        # bookkeeping tables on first bind. Optional domains remain uninstalled.
        added = after_life.keys() - before_life.keys()
        assert added <= {"life_state_schedule_claims", "life_state_manual_requests"}, added
        assert all(not after_life[table] for table in added), "scheduler created unexpected rows"
        assert not any(name.startswith(FORBIDDEN) for name in after_life)
        with closing(sqlite3.connect(life_db)) as db:
            assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'a3_reading_%'").fetchall()
        return {"state": "passed", "fixture": "historical initializers" if baseline_commit else "synthetic pinned-v71 schema",
            "baseline_commit": baseline_commit, "host_schema": "companion-v2.storage.v71",
            "life_schema": "good-life.storage.v1", "host_tables_preserved": len(after_host),
            "life_tables_preserved": len(after_life), "all_existing_rows_preserved": True,
            "optional_domain_schemas_installed": [], "new_features_enabled": [],
            "life_host_binding_ready": True, "initialized_schedulers": schedulers,
            "limits": ["Synthetic state, not production data or a full production restore",
                       "HTTP authorization and life page behavior are checked by the separate SDK route/page regressions",
                       "main adds academics/wallet routes and page assets even while domain flags are off; byte-for-byte UI equivalence is not claimed",
                       "Pocket ID browser verification remains an approved post-deploy user step"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--baseline-commit")
    args = parser.parse_args()
    print(json.dumps(rehearse(baseline_commit=args.baseline_commit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
