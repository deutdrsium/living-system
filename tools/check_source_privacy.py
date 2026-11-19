"""Check Git's proposed source inventory without printing sensitive content."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "astrbot_plugin_private_companion"
sys.path.insert(0, str(PLUGIN))

from tools.build_v2_release import SECRET_PATTERNS  # noqa: E402
from tools.check_tracked_privacy import is_private_path  # noqa: E402


def main():
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout
    names = sorted({name.decode("utf-8") for name in output.split(b"\0") if name})
    failures = []
    for name in names:
        path = ROOT / name
        relative = path.relative_to(PLUGIN).as_posix() if path.is_relative_to(PLUGIN) else name
        if is_private_path(relative) or path.is_symlink() or not path.is_file():
            failures.append(name)
        elif any(pattern.search(path.read_bytes().replace(b"\\n", b"\n")) for pattern in SECRET_PATTERNS):
            failures.append(name)
    for name in failures:
        print(f"private or credential-like source: {name}")
    print(f"source privacy: {'FAILED' if failures else 'ok'} ({len(names)} files)")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
