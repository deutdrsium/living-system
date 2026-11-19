"""Build both plugin archives from the tracked blobs of one clean Git commit."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "plugins" / "astrbot_plugin_private_companion"
LIFE = ROOT / "plugins" / "astrbot_plugin_good_life"
NO_CONSOLE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_sources(commit, destination):
    """Export Git objects, never ignored/local files or smudge-filter output."""
    archive = destination / "source.tar"
    with archive.open("xb") as output:
        subprocess.run(["git", "archive", "--format=tar", commit, "--",
            "plugins/astrbot_plugin_private_companion", "plugins/astrbot_plugin_good_life"],
            cwd=ROOT, stdout=output, check=True, creationflags=NO_CONSOLE)
    snapshot = destination / "source"
    snapshot.mkdir(mode=0o700)
    with tarfile.open(archive) as source:
        for entry in source:
            name = PurePosixPath(entry.name)
            if (name.is_absolute() or "\\" in entry.name or ":" in entry.name
                    or ".." in name.parts or not (entry.isdir() or entry.isfile())):
                raise ValueError("Git release sources contain a symlink or unsafe path")
            target = snapshot.joinpath(*name.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(entry) as payload, target.open("xb") as output:
                    for block in iter(lambda: payload.read(1024 * 1024), b""):
                        output.write(block)
    return snapshot


def build(destination):
    target = Path(destination).resolve()
    if target.exists() or target.is_relative_to(ROOT / "plugins"):
        raise ValueError("release directory must be new and outside plugin directories")
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "plugins/astrbot_plugin_private_companion",
                    "plugins/astrbot_plugin_good_life"], cwd=ROOT, check=True, creationflags=NO_CONSOLE)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--",
        "plugins/astrbot_plugin_private_companion", "plugins/astrbot_plugin_good_life"], cwd=ROOT,
        creationflags=NO_CONSOLE)
    if untracked:
        raise ValueError("commit plugin source files before building the paired release")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                     creationflags=NO_CONSOLE).strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".paired-release-", dir=target.parent) as temporary:
        snapshot = export_sources(commit, Path(temporary))
        host_root = snapshot / "plugins" / HOST.name
        life_root = snapshot / "plugins" / LIFE.name
        spec = importlib.util.spec_from_file_location("companion_release_builder", host_root / "tools/build_v2_release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metadata = (life_root / "metadata.yaml").read_text(encoding="utf-8")
        version = re.search(r"^version:\s*([^\s]+)\s*$", metadata, re.MULTILINE)
        name = re.search(r"^name:\s*([^\s]+)\s*$", metadata, re.MULTILINE)
        if not version or not name or name.group(1) != "astrbot_plugin_good_life" or version.group(1) != "0.1.1":
            raise ValueError("life plugin identity or version changed")
        stage = Path(temporary) / "release"
        stage.mkdir(mode=0o700)
        host = module.build_plugin(host_root, module.CORE_DIRS, stage)
        files = {}
        candidates = [life_root / item for item in ("main.py", "metadata.yaml", "README.md", "LICENSE")]
        for folder in ("good_life", "pages", "docs"):
            candidates.extend(path for path in (life_root / folder).rglob("*") if path.is_file())
        for path in candidates:
            relative = path.relative_to(life_root)
            if path.is_symlink() or any(part in {"__pycache__", ".git", "data", "logs"} for part in relative.parts):
                continue
            if path.suffix.lower() in {".pyc", ".db", ".sqlite", ".sqlite3", ".bak", ".log"}:
                continue
            files[relative.as_posix()] = module.checked_payload(path, life_root)
        required = {"main.py", "metadata.yaml", "README.md", "LICENSE", "good_life/runtime.py",
                    "pages/我会去好好生活/index.html", "docs/architecture.md"}
        if not required <= set(files):
            raise ValueError("life release source is incomplete")
        file_manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
        entries = {"astrbot_plugin_good_life/" + name: data for name, data in files.items()}
        entries["astrbot_plugin_good_life/RELEASE_FILES.json"] = (json.dumps(
            {"plugin_id": name.group(1), "version": version.group(1), "source_commit": commit, "files": file_manifest},
            ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        life_name = "astrbot_plugin_good_life-" + version.group(1) + ".zip"
        module.zip_entries(stage / life_name, entries)
        manifest = {"schema": "good-life.paired-release.v1", "source_commit": commit,
            "plugins": [{"id": host["plugin_id"], "version": host["version"], "archive": host["filename"],
                         "sha256": host["sha256"]},
                        {"id": name.group(1), "version": version.group(1), "archive": life_name,
                         "sha256": _sha(stage / life_name)}]}
        (stage / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                                                      encoding="utf-8", newline="\n")
        (stage / "SHA256SUMS.txt").write_text("".join(f"{_sha(path)}  {path.name}\n" for path in sorted(stage.iterdir())),
                                                encoding="utf-8", newline="\n")
        os.rename(stage, target)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("destination")
    args = parser.parse_args()
    try:
        result = build(args.destination)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"paired release build failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
