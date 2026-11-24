"""Private, workspace-local Notion scan state and short-lived shared read cache."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


SCHEMA = "1"


def state_root():
    override = os.environ.get("NOTION_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    workspace = os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ.get("YAAS_WORKSPACE")
    if not workspace:
        raise ValueError("set SIDEQUESTOR_WORKSPACE for durable Notion polling state")
    return Path(workspace).expanduser().resolve() / "state" / "notion"


def credential_key():
    # Changing selectors or an environment token isolates caches and baselines. No
    # credentials themselves are written to the store, filenames, or logs.
    values = [os.environ.get(k, "") for k in (
        "NOTION_TOKEN", "NOTION_KEYCHAIN_SERVICE", "NOTION_KEYCHAIN_ACCOUNT", "NOTION_KEYCHAIN_PATH")]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def watch_key(entry, mode, options):
    identity = {"watch_id": entry.get("watch_id"), "mode": mode,
                "credential": credential_key(), **options.scope()}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else state_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "collector.sqlite3"
        # Create privately before sqlite opens it (rather than chmod after data
        # could already be visible under a permissive process umask).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, timeout=1)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        version = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if version and version[0] != SCHEMA:
            self.db.close()
            raise ValueError("unsupported Notion collector schema; preserve state for migration")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (SCHEMA,))
        self.db.execute("CREATE TABLE IF NOT EXISTS watches (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, value TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS cache_expiry ON cache(expires)")
        self.db.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
        self.db.commit()

    def close(self):
        try:
            self.db.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
            self.db.commit()
        finally:
            self.db.close()

    def load(self, key):
        row = self.db.execute("SELECT value FROM watches WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, state):
        self.db.execute("INSERT OR REPLACE INTO watches VALUES (?, ?)",
                        (key, json.dumps(state, separators=(",", ":"))))
        self.db.commit()

    def cached(self, key, now):
        row = self.db.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, now)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key, value, now):
        encoded = json.dumps(value, separators=(",", ":"))
        self.db.execute("DELETE FROM cache WHERE expires<=?", (now,))
        # Cache is an optimization, never a prerequisite for scan progress.
        if len(encoded) <= 2_000_000:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, now + 20, encoded))
        self.db.commit()

    def not_before(self, credential):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", ("retry:" + credential,)).fetchone()
        return float(row[0]) if row else 0.0

    def defer(self, credential, until):
        until = max(until, self.not_before(credential))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("retry:" + credential, str(until)))
        self.db.commit()

    def read_report(self, key, since):
        state = self.load(key)
        if state is None:
            return {"coverage": {"status": "not_started"}, "changes": []}
        return {"coverage": state.get("coverage", {}),
                "changes": [event for event in state["events"] if event["cursor"] > since]}
