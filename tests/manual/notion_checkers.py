"""Opt-in live tree-watcher test; edits/trashes only its own disposable fixtures.

python tests/manual/notion_checkers.py --allow-test-writes --page-id UUID --database-id UUID
Needs Read/Insert/Update content and Read/Insert comments. Tokens stay in memory.
Scanner state is isolated in a temporary directory, never in a production workspace.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
T = ROOT / "src/sidequestor/runtime/yaas-triage"
sys.path.insert(0, str(T / "checkers"))
import notion
from notion_store import Store, watch_key
from notion_tree import options

spec = importlib.util.spec_from_file_location("notion_live_bridge", T / "surfaces/notion-call.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--page-id", required=True)
parser.add_argument("--database-id", required=True)
parser.add_argument("--allow-test-writes", action="store_true")
args = parser.parse_args()
if not args.allow_test_writes:
    parser.error("--allow-test-writes is required; use a disposable test parent/database")
parent = notion.resource_id(args.page_id)
database = notion.resource_id(args.database_id)
secret = bridge.token()
state_root = Path(tempfile.mkdtemp(prefix="sidequestor-notion-tree-live-"))
print("Isolated scanner state:", state_root, flush=True)
created = []


def rich(value):
    return [{"type": "text", "text": {"content": value}}]


def api(method, path, body=None):
    req = urllib.request.Request("https://api.notion.com/v1/" + path, method=method,
        data=None if body is None else json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + secret, "Notion-Version": bridge.VERSION,
            "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise RuntimeError(f"Notion HTTP {code}") from None
    time.sleep(0.4)
    return value


def create_page(parent_spec, title, title_property="title", children=None):
    payload = {"parent": parent_spec, "properties": {title_property: {"title": rich(title)}}}
    if children:
        payload["children"] = children
    page = api("POST", "pages", payload)
    created.append(page["id"])
    return page["id"]


def paragraph(text):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}


def inspect(entry):
    with contextlib.closing(Store(state_root)) as store:
        state = store.load(watch_key(entry, entry["type"], options(entry, entry["type"])))
    return state


def check(entry):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        notion.run(entry, entry["type"], store_dir=state_root)
    value = json.loads(output.getvalue())
    if value["outcome"] in ("error", "misconfig", "ratelimited"):
        raise RuntimeError(str(value))
    state = inspect(entry)
    if state["coverage"]["status"] in ("error", "limited", "retrying"):
        raise RuntimeError(str(state["coverage"]))
    return value, state


def settle(entry, expected=(), baseline=False):
    deadline = time.time() + 150
    found = set()
    ticks = 0
    while time.time() < deadline:
        value, state = check(entry)
        ticks += 1
        for event in state["events"]:
            if event["cursor"] > float(entry["last_checked_ts"]):
                found.add(event["id"])
        # This is an isolated test dictionary emulating successful worker ack,
        # not an edit of any workspace watch or production watermark.
        if value["outcome"] == "dirty":
            entry["last_checked_ts"] = value["advance_to"]
        if state["coverage"]["status"] == "complete" and set(expected) <= found:
            print(json.dumps({"watch": entry["watch_id"], "status": "complete", "ticks": ticks,
                              "found_expected": sorted(set(expected) & found)}), flush=True)
            return found
        time.sleep(1)
    raise AssertionError("Live scan did not cover expected changes within 150 seconds")


try:
    source = api("GET", "databases/" + database)["data_sources"][0]["id"]
    props = api("GET", "data_sources/" + source)["properties"]
    title_property = next(k for k, v in props.items() if v["type"] == "title")
    root = create_page({"page_id": parent}, "Disposable recursive Notion checker test", children=[
        paragraph("Root block"), {"object": "block", "type": "toggle", "toggle": {
            "rich_text": rich("Nested blocks"), "children": [paragraph("Nested paragraph")]}}])
    blocks = api("GET", "blocks/" + root + "/children")["results"]
    nested = api("GET", "blocks/" + blocks[1]["id"] + "/children")["results"][0]["id"]
    child = create_page({"page_id": root}, "Child page boundary", children=[paragraph("Child body")])
    child_block = api("GET", "blocks/" + child + "/children")["results"][0]["id"]
    grandchild = create_page({"page_id": child}, "Grandchild boundary", children=[paragraph("Grandchild body")])
    grand_block = api("GET", "blocks/" + grandchild + "/children")["results"][0]["id"]
    since = str(time.time())
    base = {"last_checked_ts": since, "max_requests": 30, "scan_seconds": 30}
    entries = [
        {**base, "type": "notion_page", "page_id": root, "watch_id": "watch-root"},
        {**base, "type": "notion_page", "page_id": root, "watch_id": "watch-depth-one",
         "subpage_depth": 1, "include_subpage_comments": True},
        {**base, "type": "notion_page", "page_id": root, "watch_id": "watch-depth-two",
         "subpage_depth": 2, "include_subpage_comments": True},
        {**base, "type": "notion_comments", "block_id": root, "watch_id": "watch-comments"},
        {**base, "type": "notion_data_source", "data_source_id": source, "watch_id": "watch-source"},
    ]
    for entry in entries:
        settle(entry, baseline=True)
    print("Baselines and existing database-item enrollment complete.", flush=True)

    before = api("GET", "pages/" + root)["last_edited_time"]
    comment = api("POST", "comments", {"parent": {"block_id": nested}, "rich_text": rich("Anchored nested comment")})
    reply = api("POST", "comments", {"discussion_id": comment["discussion_id"], "rich_text": rich("Reply on nested block")})
    after = api("GET", "pages/" + root)["last_edited_time"]
    assert before == after, "Comment test unexpectedly changed the page edit timestamp"
    settle(entries[0], [comment["id"], reply["id"]])
    settle(entries[3], [comment["id"], reply["id"]])
    api("PATCH", "comments/" + comment["id"], {"rich_text": rich("Anchored comment edited without changing page content")})
    settle(entries[0], [comment["id"]])
    settle(entries[3], [comment["id"]])
    assert api("GET", "pages/" + root)["last_edited_time"] == before
    print("Anchored comment, reply and later edit detected with unchanged page timestamp.", flush=True)

    new_block = api("PATCH", "blocks/" + root + "/children", {"children": [paragraph("Discovered after baseline")]})["results"][0]["id"]
    new_comment = api("POST", "comments", {"parent": {"block_id": new_block}, "rich_text": rich("Comment before block enrollment")})
    settle(entries[0], [new_block, new_comment["id"]])
    print("New block and its pre-enrollment comment discovered.", flush=True)

    child_comment = api("POST", "comments", {"parent": {"block_id": child_block}, "rich_text": rich("Depth-one comment")})
    grand_comment = api("POST", "comments", {"parent": {"block_id": grand_block}, "rich_text": rich("Depth-two comment")})
    one = settle(entries[1], [child_comment["id"]])
    assert grand_comment["id"] not in one
    settle(entries[2], [child_comment["id"], grand_comment["id"]])
    default = settle(entries[0])
    assert child_comment["id"] not in default and grand_comment["id"] not in default
    print("Default, depth-one and depth-two boundaries verified.", flush=True)

    item = create_page({"data_source_id": source}, "New automatic enrollment test", title_property, [paragraph("New row body")])
    item_block = api("GET", "blocks/" + item + "/children")["results"][0]["id"]
    item_comment = api("POST", "comments", {"parent": {"block_id": item_block}, "rich_text": rich("Comment created before row enrollment")})
    settle(entries[4], [item, item_comment["id"]])
    print("New database row and its pre-enrollment anchored comment detected.", flush=True)
    # Once acknowledged and fully rescanned, unchanged data must stay clean.
    for entry in [entries[0], entries[3], entries[4]]:
        settle(entry)
        value, state = check(entry)
        assert value["outcome"] in ("clean", "hold") and not state["events"], value
    print("LIVE TREE TEST PASSED", flush=True)
finally:
    for ident in reversed(created):
        try:
            if not api("GET", "pages/" + ident).get("in_trash"):
                api("PATCH", "pages/" + ident, {"in_trash": True})
        except Exception as exc:
            print("Fixture cleanup needs inspection:", ident, type(exc).__name__, flush=True)
