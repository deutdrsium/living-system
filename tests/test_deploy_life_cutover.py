from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import deploy_life_cutover as deployment


class DeploymentTests(unittest.TestCase):
    def test_stopped_install_and_preinstall_failure_restore(self):
        for fail in (False, True):
            with self.subTest(fail=fail), TemporaryDirectory() as temporary:
                root = Path(temporary)
                data, release = root / "data", root / "release"
                code = data / "plugins" / deployment.HOST_ID
                runtime = data / "plugin_data" / deployment.HOST_ID / "installations" / "main" / "runtime"
                code.mkdir(parents=True)
                (runtime / "configuration").mkdir(parents=True)
                release.mkdir()
                (code / "main.py").write_text("original", encoding="utf-8")
                for path, version in ((runtime / "companion-v2.sqlite3", deployment.RUNTIME_VERSION),
                                      (runtime / "configuration" / "companion-v2.sqlite3", deployment.CONFIGURATION_VERSION)):
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT)")
                        db.execute("INSERT INTO runtime_metadata VALUES('schema_version',?)", (version,))
                        db.commit()
                baseline_commit = "b" * 40
                (release / "baseline-manifest.json").write_text(json.dumps({"source_commit": baseline_commit,
                    "files": {"main.py": hashlib.sha256(b"original").hexdigest()}}), encoding="utf-8")
                items = []
                for identity in (deployment.HOST_ID, deployment.LIFE_ID):
                    archive = release / (identity + ".zip")
                    with zipfile.ZipFile(archive, "w") as zipped:
                        zipped.writestr(identity + "/main.py", "new-" + identity)
                    items.append({"id": identity, "archive": archive.name, "sha256": deployment.sha256(archive)})
                (release / "release-manifest.json").write_text(json.dumps({"source_commit": "a" * 40, "plugins": items}), encoding="utf-8")
                tools = release / "tools.zip"
                with zipfile.ZipFile(tools, "w") as zipped:
                    zipped.writestr("tools/prepare_life_cutover.py", "# isolated fake")
                calls = []
                def run(command, **kwargs):
                    calls.append(command[:2])
                    if len(command) > 1 and str(command[1]).endswith("prepare_life_cutover.py"):
                        if fail:
                            raise subprocess.CalledProcessError(1, command)
                        target = Path(command[command.index("--destination") + 1])
                        (target / "configuration").mkdir(parents=True)
                        (target / "life").mkdir()
                        (target / "companion-v2.sqlite3").write_bytes(b"new runtime")
                        (target / "configuration" / "companion-v2.sqlite3").write_bytes(b"new configuration")
                        (target / "life" / "good-life.sqlite3").write_bytes(b"new life")
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(deployment.subprocess, "check_output", return_value="true\n"), \
                        patch.object(deployment.subprocess, "run", side_effect=run):
                    if fail:
                        with self.assertRaises(subprocess.CalledProcessError):
                            deployment.deploy(data, release, tools_sha256=deployment.sha256(tools),
                                              container="fake", baseline_commit=baseline_commit)
                        self.assertEqual((code / "main.py").read_text(), "original")
                        self.assertFalse((data / "plugins" / deployment.LIFE_ID).exists())
                    else:
                        result = deployment.deploy(data, release, tools_sha256=deployment.sha256(tools),
                                              container="fake", baseline_commit=baseline_commit)
                        self.assertEqual(result["state"], "started")
                        self.assertTrue((Path(result["backup"]) / "backup" / "plugin-data").is_dir())
                        self.assertEqual((code / "main.py").read_text(), "new-" + deployment.HOST_ID)
                        self.assertEqual((data / "plugin_data" / deployment.LIFE_ID / "good-life.sqlite3").read_bytes(), b"new life")
                    self.assertEqual(calls[0], ["docker", "stop"])
                    self.assertEqual(calls[-1], ["docker", "start"])


if __name__ == "__main__":
    unittest.main()
