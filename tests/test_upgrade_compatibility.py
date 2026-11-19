"""Upgrade the pinned phase-one synthetic state with every new feature off."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rehearse_upgrade", ROOT / "tools/rehearse_upgrade.py")
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def test_phase_one_database_upgrade_default_off():
    result = upgrade.rehearse()
    assert result["state"] == "passed"
    assert result["host_tables_preserved"] >= 140
    assert result["life_tables_preserved"] >= 20
    assert result["new_features_enabled"] == []
    assert result["optional_domain_schemas_installed"] == []
    assert result["life_host_binding_ready"] is True
    assert set(result["initialized_schedulers"]) == {"state", "calendar", "reading", "social"}
