#!/usr/bin/env python3
"""Assign persistent IDs to watch entries that predate watch_id support."""

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path


WATCH_ID_RE = re.compile(r"^watch-[0-9a-f]{16}(?:-[0-9]+)?$")


def make_watch_id(quest_id: str, index: int, watch: dict) -> str:
    identity = {k: v for k, v in watch.items() if k not in ("last_checked_ts", "watch_id")}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(f"{quest_id}\0{index}\0{canonical}".encode()).hexdigest()[:16]
    return f"watch-{digest}"


def ensure_watch_ids(quest_id: str, path: Path) -> bool:
    data = json.loads(path.read_text())
    watches = data.setdefault("watches", [])
    changed = False
    seen = set()

    for index, watch in enumerate(watches):
        watch_id = watch.get("watch_id")
        if not isinstance(watch_id, str) or not WATCH_ID_RE.fullmatch(watch_id) or watch_id in seen:
            watch_id = make_watch_id(quest_id, index, watch)
            suffix = 0
            candidate = watch_id
            while candidate in seen:
                suffix += 1
                candidate = f"{watch_id}-{suffix}"
            watch["watch_id"] = candidate
            watch_id = candidate
            changed = True
        seen.add(watch_id)

    if not changed:
        return False

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as temp:
            json.dump(data, temp, indent=2)
            temp.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return True


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: ensure-watch-ids.py <quest_id> <watch.json>", file=sys.stderr)
        return 2
    try:
        ensure_watch_ids(sys.argv[1], Path(sys.argv[2]))
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        print(f"ensure-watch-ids.py: {sys.argv[2]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
