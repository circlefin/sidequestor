#!/usr/bin/env python3
"""Read-only pending changes/coverage. Usage: notion-state.py '<watch_json>'.

Use the exact watch entry, including watch_id and last_checked_ts, from the
quest's watch.json. No network requests or state creation are performed here.
"""
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checkers"))
from notion_store import state_root, watch_key
from notion_tree import options
sys.path.pop(0)


def inspect(entry, root=None, strict=True):
    mode = entry["type"]
    opts = options(entry, mode)
    since = float(entry.get("last_checked_ts") or 0)
    if not math.isfinite(since) or since < 0:
        raise ValueError("invalid last_checked_ts")
    path = (Path(root) if root is not None else state_root()) / "collector.sqlite3"
    if not path.exists():
        if strict:
            raise ValueError("Notion collector state is missing; do not acknowledge the dispatch")
        return {"coverage": {"status": "not_started"}, "changes": []}
    db = sqlite3.connect("file:" + quote(str(path.resolve())) + "?mode=ro", uri=True, timeout=1)
    try:
        if db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() != ("1",):
            raise ValueError("unsupported Notion state schema")
        row = db.execute("SELECT value FROM watches WHERE key=?", (watch_key(entry, mode, opts),)).fetchone()
        if not row:
            if strict:
                raise ValueError("Notion watch/credential scope does not match collector state")
            return {"coverage": {"status": "not_started"}, "changes": []}
        state = json.loads(row[0])
        return {"coverage": state.get("coverage", {}),
                "max_cursor": state.get("last_cursor"),
                "changes": [e for e in state["events"] if e["cursor"] > since]}
    finally:
        db.close()


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(json.loads(sys.argv[1]))))
    except Exception:
        print(json.dumps({"error": "Notion state is unavailable; do not infer a clean scan"}))
        sys.exit(2)
