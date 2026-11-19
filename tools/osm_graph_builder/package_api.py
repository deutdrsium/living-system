"""Reuse the plugin's pure package boundary without loading AstrBot/runtime code."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins" / "astrbot_plugin_good_life"))
from good_life.graph_packages import (  # noqa: E402
    ATTRIBUTION, SCHEMA, diff_packages, package_digest, seal_package, validate_package,
)
