"""AstrBot entry point for the good-life plugin."""

import asyncio
from pathlib import Path
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

from .good_life.runtime import LifeRuntime
from .good_life.storage import LifeStorage, StorageReader
from .good_life.web_api import LifePageAPI


PLUGIN_ID = "astrbot_plugin_good_life"
# Reserved for a verified, paired cutover candidate; its preparation is gated.
READY_KEY = "life.phase1.cutover_ready.v1"


def _data_root():
    code = Path(__file__).resolve().parent
    root = Path(StarTools.get_data_dir(PLUGIN_ID)).resolve()
    if root == code:
        parent = code.parent
        base = parent.parent if parent.name.casefold() in ("plugins", "plugin") else parent
        root = (base / "plugin_data" / PLUGIN_ID).resolve()
    if root.is_relative_to(code) or code.is_relative_to(root):
        raise ValueError("life data directory overlaps plugin code")
    return root


@register("astrbot_plugin_good_life", "menglimi", "我会去好好生活", "0.1.1")
class GoodLifePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)
        self._stopped = False
        self._storage = self._runtime = self._watcher = None
        self._failure = None
        self._reading_executor_enabled = bool(config and config.get("reading_executor_enabled") is True)
        self._reading_abort_own_enabled = bool(config and config.get("reading_abort_own_enabled") is True)
        self._kernel_enabled = bool(config and config.get("kernel_enabled") is True)
        self._academics_enabled = bool(config and config.get("academics_enabled") is True)
        self._class_enabled = bool(config and config.get("class_enabled") is True)
        self._attendance_enabled = bool(config and config.get("attendance_enabled") is True)
        self._study_enabled = bool(config and config.get("study_enabled") is True)
        self._campus_settings = {key: config[key] for key in (
            "campus_enabled", "academics_graph_id", "home_place_id", "graph_access_credentials", "graph_private_credentials",
            "treat_unparsed_access_as", "treat_unknown_opening_as",
        ) if config is not None and key in config}
        self._city_settings = {key: config[key] for key in (
            "wallet_enabled", "city_enabled", "amap_navigation_enabled", "amap_poi_search_enabled",
            "amap_navigation_key", "amap_navigation_city_code",
        ) if config is not None and key in config}
        self._page_api = LifePageAPI(self)
        self.extension_api = self

    async def initialize(self):
        self._storage = LifeStorage.open(_data_root())
        from .good_life.academics import AcademicsConfig, register_class_group
        academics = AcademicsConfig(enabled=self._academics_enabled, study_enabled=self._study_enabled)
        classes = self._academics_enabled and self._class_enabled
        self._runtime = LifeRuntime(self._storage, self, reading_executor_enabled=self._reading_executor_enabled,
                                    reading_abort_own_enabled=self._reading_abort_own_enabled,
                                    kernel_enabled=self._kernel_enabled,
                                    academics_config=None if classes and self._kernel_enabled else academics)
        from .good_life.city.domain import register_city_group
        from .good_life.economy import register_wallet_group
        from .good_life.travel import register_shared_travel_group
        register_shared_travel_group(self._runtime, self._campus_settings)
        register_class_group(self._runtime, enabled=classes, attendance_enabled=self._attendance_enabled,
                             academics_config=academics)
        wallet = self._city_settings.get("wallet_enabled") is True
        register_wallet_group(self._runtime, enabled=wallet)
        register_city_group(self._runtime, enabled=wallet and self._city_settings.get("city_enabled") is True,
                            config=self._city_settings)
        self._page_api.register()
        if not self._cutover_ready():
            logger.info("[GoodLife] 等待生活数据迁移；尚未启用调度")
            return
        self._watcher = asyncio.create_task(self._watch_dependency(), name="good-life:dependency")

    def _cutover_ready(self):
        if self._storage is None:
            return False
        row = StorageReader(self._storage).one("SELECT value FROM life_metadata WHERE key=?", (READY_KEY,))
        return row is not None and row["value"] == "ready"

    def _host_api(self):
        if self._dependency_state() != "available":
            return None
        stars = [star for star in self.context.get_all_stars()
                 if getattr(star, "root_dir_name", None) == "astrbot_plugin_private_companion"]
        return getattr(stars[0].star_cls, "extension_api", None)

    async def _watch_dependency(self):
        while not self._stopped:
            try:
                api = self._host_api() if self._cutover_ready() else None
                if api is None:
                    await self._runtime.unbind()
                else:
                    if self._runtime.api is api and api.life_provider_status().get("ready") is not True:
                        await self._runtime.unbind()
                    was_ready = self._runtime.api is api and self._runtime.ready
                    await self._runtime.bind(api)
                    if not was_ready and self._runtime.ready and api.life_provider_status().get("ready") is True:
                        logger.info("[GoodLife] host binding ready")
                self._failure = None
            except Exception:
                await self._runtime.unbind()
                self._failure = "dependency_binding_failed"
            await asyncio.sleep(3)

    async def terminate(self):
        self._stopped = True
        self._page_api.unregister()
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        if self._runtime is not None:
            await self._runtime.close()
        if self._storage is not None:
            self._storage.close()
            self._storage = None

    def _dependency_state(self):
        try:
            stars = [star for star in self.context.get_all_stars()
                     if getattr(star, "root_dir_name", None) == "astrbot_plugin_private_companion"]
            if not stars:
                return "missing"
            if len(stars) != 1:
                return "conflict"
            if getattr(stars[0], "activated", False) is not True:
                return "disabled"
            api = getattr(getattr(stars[0], "star_cls", None), "extension_api", None)
            status = getattr(api, "life_provider_status", None)
            if getattr(api, "api_version", None) != "companion.product.v1" or not callable(status):
                return "incompatible"
            response = status()
            if type(response) is not dict or response.get("api_version") != "companion.life.v1":
                return "incompatible"
            if response.get("host_ready") is not True:
                return "unavailable"
            return "available"
        except Exception:
            return "unavailable"

    def companion_v2_status(self):
        return {"schema": "companion.provider-status.v1", "plugin_id": "astrbot_plugin_good_life",
                "enabled": not self._stopped, "runtime_ready": bool(self._runtime and self._runtime.ready),
                "dependency_state": "stopped" if self._stopped else self._dependency_state()}
