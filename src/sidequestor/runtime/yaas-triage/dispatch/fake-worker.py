#!/usr/bin/env python3
"""Stage 3 test worker; acknowledges a real manifest without invoking an agent."""

import json
import os
import subprocess
import sys
from pathlib import Path


run_id = ""
label = ""
arguments = sys.argv[1:]
for index, value in enumerate(arguments):
    if index + 1 >= len(arguments):
        continue
    option_value = arguments[index + 1]
    if value == "--label":
        label = option_value
    elif value == "--header":
        header = option_value
        if header.startswith("Run ID: "):
            run_id = header[len("Run ID: "):]

workspace = Path(os.environ["YAAS_WORKSPACE"])
runtime = Path(os.environ["YAAS_RUNTIME_ROOT"])
manifest = workspace / "state" / "triage" / f"dispatch-{run_id}.json"
data = json.loads(manifest.read_text())
ack = runtime / "yaas-triage" / "ledger" / "ack-watch.py"
for item in data["items"]:
    subprocess.run([
        sys.executable, str(ack), "ack", run_id, item["item_id"],
        "handled", "stage3 fake worker",
    ], capture_output=True, text=True, check=True)

called = {
    "run_id": run_id,
    "target": label,
    "items": [item["item_id"] for item in data["items"]],
}
(workspace / "state" / "fake-worker-called.json").write_text(json.dumps(called, indent=2) + "\n")
ndjson = workspace / "logs" / "fake-worker.ndjson"
ndjson.write_text(json.dumps({"type": "result", "result": "fake worker completed"}) + "\n")
print(json.dumps({"exit": 0, "wall_sec": 0, "log": "fake-worker.log", "ndjson": str(ndjson)}))
