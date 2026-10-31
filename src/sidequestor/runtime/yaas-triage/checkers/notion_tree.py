"""Bounded Notion traversal and acknowledgement-safe detected-change delivery.

Each invocation does a small amount of a persistent breadth-first scan. The
external advance_to is a local delivery cursor, NOT a Notion edit timestamp.
Snapshots and pending events are committed together. Pending events are pruned
only after triage supplies an acknowledged last_checked_ts covering them.
"""
import collections
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import result
from notion_store import Store, credential_key, watch_key


class ConfigError(ValueError):
    pass


class Budget(Exception):
    pass


class Limited(Exception):
    pass


class Incomplete(Exception):
    pass


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError("Notion target must be a UUID, not a URL") from exc


def integer(entry, field, default, low, high):
    value = entry.get(field, default)
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{field} must be an integer between {low} and {high}")
    return value


def boolean(entry, field, default):
    value = entry.get(field, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{field} must be a boolean")
    return value


@dataclass
class Options:
    target: str
    database: bool
    depth: int
    sub_content: bool
    sub_comments: bool
    archived: bool
    requests: int
    seconds: int
    entities: int

    def scope(self):
        return {k: getattr(self, k) for k in (
            "target", "database", "depth", "sub_content", "sub_comments", "archived")}


def options(entry, mode):
    if mode not in ("notion_page", "notion_comments", "notion_data_source"):
        raise ConfigError("unknown Notion watch type")
    field = {"notion_page": "page_id", "notion_comments": "block_id", "notion_data_source": "data_source_id"}[mode]
    database = mode == "notion_data_source" and bool(entry.get("database_id"))
    if database and entry.get("data_source_id"):
        raise ConfigError("provide data_source_id OR database_id")
    target = identifier(entry.get("database_id") if database else entry.get(field))
    max_pages = integer(entry, "max_pages", 8, 1, 50)  # compatibility: per-tick read budget
    return Options(target, database,
        integer(entry, "subpage_depth", 0, 0, 100),
        boolean(entry, "include_subpage_content", False),
        boolean(entry, "include_subpage_comments", False),
        boolean(entry, "include_archived", True),
        integer(entry, "max_requests", max_pages, 1, 50),
        integer(entry, "scan_seconds", 8, 3, 30),
        integer(entry, "max_entities", 10000, 1, 100000))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stable(value):
    """Exclude volatile metadata and temporary signed file URLs from fingerprints."""
    if isinstance(value, list):
        return [stable(v) for v in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") in ("last_edited_time", "last_edited_by"):
        return {"type": value["type"]}
    output = {}
    for key, item in value.items():
        if key in ("request_id", "last_edited_time", "last_edited_by", "expiry_time"):
            continue
        if key == "url" and "expiry_time" in value and isinstance(item, str):
            # Keep the stable object path so replacing an uploaded image/file
            # is detected; discard only the expiring signature query.
            parts = urlsplit(item)
            output[key] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        else:
            output[key] = stable(item)
    return output


def edited(row):
    from datetime import datetime
    value = row.get("last_edited_time") or row.get("created_time")
    if not isinstance(value, str):
        raise RuntimeError("Notion resource omitted its edit timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError("Notion timestamp omitted timezone")
    return parsed.timestamp()


class Collector:
    def __init__(self, store, key, entry, mode, opts, api, now):
        self.store, self.key, self.entry, self.mode, self.o, self.api = store, key, entry, mode, opts, api
        self.now = now
        self.deadline = time.monotonic() + opts.seconds
        self.reads = self.cache_hits = self.steps = 0
        self.credential = credential_key()
        try:
            self.since = float(entry.get("last_checked_ts") or 0)
            if not math.isfinite(self.since) or self.since < 0:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ConfigError("last_checked_ts must be finite and nonnegative") from exc
        self.state = store.load(key) or {
            "initial_since": max(0, math.floor((self.since + 0.0011) / 60) * 60 - 0.001), "bootstrapped": False, "cycle": 0,
            "queue": [], "scheduled": [], "entities": [], "seen": [], "visits": {},
            "snapshots": {}, "events": [], "last_cursor": 0, "coverage": {}, "gaps": []}
        self.state["events"] = [e for e in self.state["events"] if e["cursor"] > self.since]
        self.queue = collections.deque(self.state["queue"])
        self.scheduled = set(self.state["scheduled"])
        self.entities = set(self.state["entities"])
        self.seen = set(self.state["seen"])
        self.cursor = max(int(now), int(self.since) + 1, self.state["last_cursor"] + 1)
        self.note = ""

    def save(self):
        s = self.state
        s.update(queue=list(self.queue), scheduled=sorted(self.scheduled),
                 entities=sorted(self.entities), seen=sorted(self.seen))
        self.store.save(self.key, s)

    def enqueue(self, task):
        key = digest(task)
        if key in self.scheduled:
            return
        self.scheduled.add(key)
        self.queue.append(task)

    def claim_entity(self, kind, ident):
        key = kind + ":" + ident
        if key not in self.entities:
            if len(self.entities) >= self.o.entities:
                raise Limited("max_entities reached; increase the limit or narrow the scope")
            self.entities.add(key)

    def fetch(self, method, path, body=None):
        if time.monotonic() >= self.deadline or self.steps >= self.o.requests * 4:
            raise Budget()
        key = digest([self.credential, method, path, body])
        cached = self.store.cached(key, time.time())
        if cached is not None:
            self.cache_hits += 1
            self.steps += 1
            return cached
        if self.reads >= self.o.requests:
            raise Budget()
        # One bounded request at a time per workspace/credential, shared by all
        # watches. Never hold sqlite's write transaction over a network request.
        lock_path = self.store.root / ("request-" + self.credential + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() + 2 >= self.deadline:
                        self.note = "shared request queue busy; scan will resume"
                        raise Budget() from None
                    time.sleep(0.05)
            cached = self.store.cached(key, time.time())
            if cached is not None:
                self.cache_hits += 1
                self.steps += 1
                return cached
            wait = self.store.not_before(self.credential) - time.time()
            if wait > 0:
                if wait > 0.5 or time.monotonic() + wait + 2 >= self.deadline:
                    self.note = "shared request budget cooling down"
                    raise Budget()
                time.sleep(wait)
            remaining = self.deadline - time.monotonic()
            if remaining < 2:
                raise Budget()
            self.reads += 1
            self.steps += 1
            self.store.defer(self.credential, time.time() + 0.35)
            timeout = min(30, remaining)
            try:
                value = self.api(method, path, body, timeout=timeout)
            except Exception as exc:
                if getattr(exc, "local_timeout", False) and timeout < 30:
                    self.note = "local scan deadline exhausted; request will retry next tick"
                    raise Budget() from None
                raise
            if not isinstance(value, dict) or value.get("object") == "error":
                raise RuntimeError("invalid Notion response")
            self.store.cache(key, value, time.time())
            return value

    def emit_change(self, snapshot_key, row, payload, kind, page_id, force=False, container_id=None):
        ident = identifier(row["id"])
        self.claim_entity("comment" if kind == "comment" else "node", ident)
        self.seen.add(snapshot_key)
        value = digest(stable(payload))
        previous = self.state["snapshots"].get(snapshot_key)
        if previous == value:
            return
        if previous is None and not (self.state["bootstrapped"] or force
                or edited(row) > self.state["initial_since"]):
            self.state["snapshots"][snapshot_key] = value
            return
        pending = self.state["events"]
        same = lambda e: e["kind"] == kind and e["id"] == ident
        if len(pending) >= 10000 and not any(same(e) for e in pending):
            raise Limited("pending change limit reached; acknowledge existing changes before scanning further")
        event = {"kind": kind, "id": ident, "page_id": page_id,
                 "version": value, "cursor": self.cursor, "container_id": container_id}
        # Coalesce repeated edits, but assign a newer delivery token so an ack
        # from an already-running worker cannot erase a subsequent edit.
        self.state["events"] = [e for e in pending if not same(e)] + [event]
        self.state["last_cursor"] = self.cursor
        self.state["snapshots"][snapshot_key] = value

    def schedule_node(self, row, depth, content, comments, page_id, page=False):
        ident = identifier(row["id"])
        self.claim_entity("node", ident)
        if not page and row.get("type") == "child_page":
            self.block(row, {"id": ident, "depth": depth, "content": content,
                             "comments": comments, "page_id": page_id})
            return
        marker = ("page:" if page else "node:") + ident
        previous = self.state["visits"].get(marker)
        if previous and depth >= previous[0] and (not content or previous[1]) and (not comments or previous[2]):
            return
        if previous:
            depth, content, comments = min(depth, previous[0]), content or previous[1], comments or previous[2]
        self.state["visits"][marker] = [depth, content, comments]
        task = {"id": ident, "depth": depth, "content": content,
                "comments": comments, "page_id": ident if page else page_id}
        if page:
            self.enqueue({"kind": "page", **task})
        else:
            try:
                self.block(row, task)
            except Exception:
                # A bounded inbox can interrupt block enrollment before its
                # comments/children are queued. Retry that node when resuming;
                # a visited marker must describe completed enrollment only.
                if previous is None:
                    self.state["visits"].pop(marker, None)
                else:
                    self.state["visits"][marker] = previous
                raise

    def block(self, row, task):
        ident = identifier(row["id"])
        kind = row.get("type")
        if not isinstance(kind, str):
            raise RuntimeError("Notion block omitted its type")
        if kind == "child_page":
            # Treat page references as boundaries; title changes alone must not
            # sneak through when subpage content monitoring is disabled.
            if task["depth"] < self.o.depth and ((self.o.sub_content and self.mode != "notion_comments") or self.o.sub_comments):
                self.schedule_node(row, task["depth"] + 1,
                    self.o.sub_content and self.mode != "notion_comments",
                    self.o.sub_comments, ident, page=True)
            return
        if kind in ("child_database", "link_to_page"):
            return  # databases get their own explicit data-source watch
        if task["content"]:
            self.emit_change("block:" + ident, row,
                {"type": kind, "body": row.get(kind), "in_trash": row.get("in_trash", False)},
                "block", task["page_id"])
        if task["comments"]:
            self.enqueue({"kind": "comments", **task})
        if row.get("has_children"):
            self.enqueue({"kind": "children", **task})

    def paginate(self, task, data):
        if (data.get("object") != "list" or not isinstance(data.get("results"), list)
                or not isinstance(data.get("has_more"), bool)):
            raise RuntimeError("invalid Notion pagination response")
        status = data.get("request_status")
        if status is not None:
            if not isinstance(status, dict) or status.get("type") not in ("complete", "incomplete"):
                raise RuntimeError("invalid Notion request_status")
            if status["type"] == "incomplete":
                raise Incomplete("Notion query is incomplete; narrow scope before claiming full coverage")
        if data["has_more"]:
            cursor = data.get("next_cursor")
            history = task.get("cursors", [])
            if len(history) >= self.o.entities:
                raise Limited("pagination reached max_entities; narrow scope or increase the limit")
            if not isinstance(cursor, str) or not cursor or cursor in history:
                raise RuntimeError("Notion pagination cursor missing or repeated")
            next_task = {**task, "cursor": cursor, "cursors": [*history, cursor]}
            return next_task
        return None

    def process(self, task):
        kind, ident = task["kind"], task["id"]
        if kind == "database":
            data = self.fetch("GET", f"/v1/databases/{ident}")
            sources = data.get("data_sources")
            if not isinstance(sources, list) or len(sources) != 1:
                raise ConfigError("database must have one data source; specify data_source_id")
            source = identifier(sources[0]["id"])
            self.source_tasks(source)
            return
        if kind == "page":
            data = self.fetch("GET", f"/v1/pages/{ident}")
            if data.get("object") != "page":
                raise RuntimeError("Notion did not return a page")
            if task["content"]:
                payload = {k: data.get(k) for k in ("properties", "icon", "cover", "in_trash", "is_archived")}
                # Preserve page-edit detection even when Notion projects only
                # part of a large relation/rollup property. Comment traversal
                # never depends on this hint and still catches same-minute edits.
                payload["edit_revision"] = edited(data)
                self.emit_change("page:" + ident, data, payload, "page", ident)
            if task["comments"]:
                self.enqueue({**task, "kind": "comments"})
            if not data.get("in_trash"):
                self.enqueue({**task, "kind": "children"})
            return
        if kind == "root_block":
            # Pages are accepted by the block API as child_page blocks. Unlike
            # child-page references encountered during traversal, this is the
            # selected root and must always be traversed.
            data = self.fetch("GET", f"/v1/blocks/{ident}")
            if data.get("object") != "block":
                raise RuntimeError("Notion did not return a block")
            parent = data.get("parent", {})
            task = {**task, "page_id": ident if data.get("type") == "child_page" else parent.get("page_id")}
            self.enqueue({**task, "kind": "comments"})
            if data.get("has_children") or data.get("type") == "child_page":
                self.enqueue({**task, "kind": "children"})
            return
        params = {"page_size": 100}
        if task.get("cursor"):
            params["start_cursor"] = task["cursor"]
        if kind == "source":
            params.update(is_archived=task["archived"], sorts=[{"timestamp": "created_time", "direction": "ascending"}])
            data = self.fetch("POST", f"/v1/data_sources/{ident}/query", params)
        elif kind == "comments":
            data = self.fetch("GET", "/v1/comments?" + urlencode({"block_id": ident, **params}))
        elif kind == "children":
            data = self.fetch("GET", f"/v1/blocks/{ident}/children?" + urlencode(params))
        else:
            raise RuntimeError("unknown Notion scan task")
        next_task = self.paginate(task, data)
        if kind == "source":
            for row in data["results"]:
                if row.get("object") != "page":
                    raise ConfigError("wiki data sources require explicit page watches")
                self.schedule_node(row, 0, True, True, row["id"], page=True)
        elif kind == "comments":
            for row in data["results"]:
                if row.get("object") != "comment":
                    raise RuntimeError("Notion did not return a comment")
                self.emit_change("comment:" + identifier(row["id"]), row,
                    {k: row.get(k) for k in ("rich_text", "discussion_id", "parent", "attachments")},
                    "comment", task["page_id"], container_id=ident)
        else:
            members = [*task.get("members", []), *[identifier(row["id"]) for row in data["results"]]]
            for row in data["results"]:
                self.schedule_node(row, task["depth"], task["content"], task["comments"], task["page_id"])
            if next_task is not None:
                next_task["members"] = members
            elif task["content"]:
                # Detect removals/reorders without guessing whether a missing
                # child was deleted, moved, or became inaccessible.
                row = {"id": ident, "last_edited_time": "1970-01-01T00:00:00+00:00"}
                self.emit_change("children:" + ident, row, members, "children", task["page_id"])
        if next_task:
            self.enqueue(next_task)

    def source_tasks(self, ident):
        for archived in ([False, True] if self.o.archived else [False]):
            self.enqueue({"kind": "source", "id": ident, "archived": archived})

    def start_cycle(self):
        self.scheduled.clear()
        self.state["visits"] = {}
        self.state["gaps"] = []
        self.entities.clear()
        self.seen.clear()
        self.state["cycle"] += 1
        self.state["cycle_started_at"] = self.now
        if self.mode == "notion_data_source":
            if self.o.database:
                self.enqueue({"kind": "database", "id": self.o.target})
            else:
                self.source_tasks(self.o.target)
        else:
            self.state["visits"][("page:" if self.mode == "notion_page" else "node:") + self.o.target] = [0, self.mode == "notion_page", True]
            self.claim_entity("node", self.o.target)
            self.enqueue({"kind": "page" if self.mode == "notion_page" else "root_block",
                          "id": self.o.target, "depth": 0,
                          "content": self.mode == "notion_page", "comments": True,
                          "page_id": self.o.target})

    def coverage(self, status):
        old = self.state.get("coverage", {})
        value = {"status": status, "cycle": self.state["cycle"],
                 "entities_scanned": len(self.entities), "pending_tasks": len(self.queue),
                 "requests_this_tick": self.reads, "cache_hits_this_tick": self.cache_hits,
                 "last_complete_scan": old.get("last_complete_scan"),
                 "subpage_depth": self.o.depth, "max_entities": self.o.entities,
                 "include_subpage_content": self.o.sub_content, "include_subpage_comments": self.o.sub_comments,
                 "request_limit": self.o.requests, "time_limit_seconds": self.o.seconds,
                 "pending_changes": len(self.state["events"]), "gaps": self.state.get("gaps", []),
                 "reason": self.note}
        if status == "complete":
            value["last_complete_scan"] = self.now
        self.state["coverage"] = value
        return value

    def run(self):
        if not self.queue:
            self.start_cycle()
        status = "scanning"
        try:
            while self.queue:
                if self.steps >= self.o.requests * 4 or time.monotonic() >= self.deadline:
                    raise Budget()
                task = self.queue[0]
                try:
                    self.process(task)
                except Exception as exc:
                    selected = task["id"] == self.o.target and self.mode != "notion_data_source"
                    root_task = selected or task["kind"] in ("source", "database")
                    if not getattr(exc, "unavailable_resource", False) or root_task:
                        raise
                    # A descendant may vanish/move or expose an unreadable
                    # synced original between discovery and a queued read.
                    # Continue other branches, retry discovery next cycle and
                    # report availability without claiming it was deleted.
                    gap = {"id": task["id"], "operation": task["kind"], "reason": str(exc)[:200]}
                    if gap not in self.state["gaps"]:
                        self.state["gaps"].append(gap)
                    row = {"id": task["id"], "last_edited_time": "1970-01-01T00:00:00+00:00"}
                    self.emit_change("gap:" + task["kind"] + ":" + task["id"], row, gap,
                        "coverage_gap", task.get("page_id"), force=True, container_id=task["id"])
                self.queue.popleft()
                self.save()  # cursor, snapshots, pending changes and BFS progress together
            status = "partial" if self.state.get("gaps") else "complete"
            if status == "partial":
                self.note = "Some descendants are unavailable; other branches were scanned and gaps will be retried"
            self.state["bootstrapped"] = True
            self.state["snapshots"] = {k: v for k, v in self.state["snapshots"].items() if k in self.seen}
        except Budget:
            pass
        except (Limited, Incomplete) as exc:
            status = "limited"
            self.note = str(exc)
        except Exception as exc:
            retry = getattr(exc, "retry_after", None)
            if retry is not None:
                self.store.defer(self.credential, time.time() + max(1, retry))
            self.note = str(exc)[:250]
            self.coverage("retrying" if retry is not None else "error")
            self.save()
            raise
        coverage = self.coverage(status)
        self.save()
        pending = self.state["events"]
        if pending:
            result.emit("dirty", count=len(pending), preview=f"Notion: {len(pending)} pending changes ({status})",
                        advance_to=max(e["cursor"] for e in pending), complete=True,
                        reason=self.note)
        elif status == "limited":
            result.emit("hold", complete=False, reason=self.note)
        elif status != "complete":
            # Normal incremental scanning is progress, not a stalled upstream
            # window. HOLD increments triage's unacked/backoff counter. CLEAN
            # with the exact old cursor avoids backoff without skipping events;
            # durable coverage remains scanning/partial, never "complete".
            result.counted(0, advance_to=self.since)
        else:
            result.counted(0, advance_to=self.since)
        return coverage


def run(entry, mode, api, now=None, root=None):
    opts = options(entry, mode)
    try:
        since = float(entry.get("last_checked_ts") or 0)
        if not math.isfinite(since) or since < 0:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ConfigError("last_checked_ts must be finite and nonnegative") from exc
    store = Store(root)
    key = watch_key(entry, mode, opts)
    fd = os.open(store.root / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result.counted(0, advance_to=since)
                return
            collector = Collector(store, key, entry, mode, opts, api, time.time() if now is None else now)
            return collector.run()
    finally:
        store.close()
