from types import SimpleNamespace

import pytest

pytest.importorskip("astrbot")

from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID


def test_facades_share_provider_and_stale_shutdown_keeps_replacement():
    plugin = object()
    star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
    context = SimpleNamespace(get_all_stars=lambda: [star])
    controller = SimpleNamespace(context=context, _terminated=False)
    first = UnifiedCompanionExtensionAPI(controller)
    second = UnifiedCompanionExtensionAPI(controller)
    handlers = {name: (lambda: None) for name in HANDLERS}
    token = first.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=handlers)
    assert second.life_provider_status()["ready"] is True
    assert second.unregister_life_provider(token) is True

    controller._terminated = True
    replacement = UnifiedCompanionExtensionAPI(SimpleNamespace(context=context, _terminated=False))
    first.close_life_providers()
    token = replacement.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=handlers)
    assert replacement.life_provider_status()["ready"] is True
    assert replacement.unregister_life_provider(token) is True
    replacement.close_life_providers()
    replacement.close_memory_providers()
