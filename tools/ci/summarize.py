"""Summarize incremental shard reports by test file and list failures."""
from collections import defaultdict
import json
import os
from pathlib import Path
import sys

folder = Path(sys.argv[1])
folder.mkdir(parents=True, exist_ok=True)
shard = os.environ.get("CI_SHARD_INDEX")
if shard is not None:
    (folder / f"shard-{shard}-status.txt").write_text(
        os.environ.get("TEST_OUTCOME", "unknown") + "\n", encoding="utf-8", newline="\n")

failures = defaultdict(set)
durations = defaultdict(float)
corrupt = []
for source in sorted(folder.glob("shard-*.jsonl")) + sorted(folder.glob("python-shard-*/shard-*.jsonl")):
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        try:
            entry = json.loads(line)
            nodeid = entry["nodeid"]
            file = nodeid.split("::", 1)[0]
            if entry["outcome"] == "failed":
                failures[file].add(nodeid)
            if entry["phase"] == "call":
                durations[file] += float(entry["duration"])
        except (ValueError, KeyError, TypeError):
            corrupt.append(f"{source.name}:{number}")

lines = ["## Companion Python report", ""]
statuses = sorted(folder.glob("shard-*-status.txt")) + sorted(folder.glob("python-shard-*/shard-*-status.txt"))
lines += [f"- {p.stem}: {p.read_text(encoding='utf-8').strip()}" for p in statuses]
if not statuses:
    lines.append("- No shard status was available.")
lines += ["", f"### Failures by file ({sum(map(len, failures.values()))} tests)", ""]
if not failures:
    lines.append("No failures recorded; check shard status for incomplete runs.")
for file, nodes in sorted(failures.items()):
    lines += [f"- `{file}`: {len(nodes)}"]
    lines += [f"  - `{node}`" for node in sorted(nodes)]
lines += ["", "### Slowest files by completed test call time", ""]
for file, seconds in sorted(durations.items(), key=lambda item: item[1], reverse=True)[:20]:
    lines.append(f"- `{file}`: {seconds:.1f}s")
if not durations:
    lines.append("No completed test timings recorded.")
if corrupt:
    lines += ["", "Incomplete report records: " + ", ".join(corrupt)]
text = "\n".join(lines) + "\n"
name = f"shard-{shard}-report.md" if shard is not None else "report.md"
(folder / name).write_text(text, encoding="utf-8", newline="\n")
print(text)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write(text)
