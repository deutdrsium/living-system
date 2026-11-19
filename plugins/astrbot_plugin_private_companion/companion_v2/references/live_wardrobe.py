"""Read the explicitly bound Live2D outfit as role-owned image material."""

from __future__ import annotations

import json
from datetime import datetime

from .models import ReferenceRole, ReferenceState
from .repositories import ReferenceAssetRepository


def binding(runtime, owner):
    path = runtime.data_root / "extensions" / "live" / "wardrobe-image-sync.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        activated = datetime.fromisoformat(value["activated_at"])
        if (value.get("version") != 1 or value.get("owner_key") != owner
                or activated.tzinfo is None or not isinstance(value.get("items"), dict)):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None


def current_outfit(runtime, owner):
    synced = binding(runtime, owner)
    if synced is None:
        return None
    path = runtime.data_root / "extensions" / "live" / "wardrobe-current.json"
    try:
        selected = json.loads(path.read_text(encoding="utf-8"))
        choices = selected["selected"]
        if not isinstance(choices, dict):
            return None
        repository = ReferenceAssetRepository(runtime)
        items = []
        for layer, source_id in choices.items():
            if source_id is None:
                continue
            linked = synced["items"][source_id]
            item = repository.get_item(owner, linked["item_id"])
            if item is None or item.state is not ReferenceState.ACTIVE:
                continue
            asset_id = item.asset_id
            if asset_id:
                asset = repository.get(owner, asset_id)
                if (asset is None or asset.state is not ReferenceState.ACTIVE
                        or ReferenceRole.OUTFIT.value not in repository.roles_of(owner, asset_id)):
                    asset_id = None
            items.append({"item_id": item.item_id, "name": item.name, "description": item.description,
                          "tags": item.tags, "asset_id": asset_id, "layer": layer})
        return {"updated_at": selected.get("updated_at"), "items": tuple(items)} if items else None
    except (OSError, ValueError, TypeError, KeyError):
        return None
