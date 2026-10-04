#!/usr/bin/env python3
"""Private Playwright adapter for the guarded Google Doc comment writer."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


FIND_INPUT = 'input[aria-label="Find"]:visible'
FIND_DIALOG = '[role="dialog"][aria-labelledby]'
COMPOSER = ".docos-input-textarea:visible"
CANVAS = ".kix-appview-editor"


def _json_from_gws(*args):
    binary = os.environ.get("GWS_BIN") or "gws"
    completed = subprocess.run([binary, *args], text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "gws failed")
    start = completed.stdout.find("{")
    if start < 0:
        raise RuntimeError("gws returned no JSON object")
    return json.loads(completed.stdout[start:])


def _comments(doc_id):
    rows = {}
    page_token = None
    while True:
        params = {
            "fileId": doc_id,
            "fields": "nextPageToken,comments(id,content,quotedFileContent)",
            "includeDeleted": False,
            "pageSize": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        value = _json_from_gws(
            "drive", "comments", "list", "--params", json.dumps(params))
        rows.update({
            str(row.get("id")): row for row in value.get("comments", []) if row.get("id")
        })
        page_token = value.get("nextPageToken")
        if not page_token:
            return rows


def _assert_capabilities(doc_id):
    value = _json_from_gws(
        "drive", "files", "get", "--params",
        json.dumps({"fileId": doc_id, "fields": "capabilities(canComment,canEdit)"}),
    )
    capabilities = value.get("capabilities") or {}
    if not capabilities.get("canComment"):
        raise RuntimeError("the authenticated Google account cannot comment on this document")
    if not capabilities.get("canEdit"):
        raise RuntimeError(
            "comment-only documents are not supported by unattended mode; use a foreground run")


def _cdp_ready(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _ensure_chrome(cdp_url):
    parsed = urllib.parse.urlparse(cdp_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise RuntimeError("SIDEQUESTOR_GDOC_CDP_URL must use loopback HTTP")
    try:
        port = parsed.port or 9222
    except ValueError as exc:
        raise RuntimeError("SIDEQUESTOR_GDOC_CDP_URL has an invalid port") from exc
    if _cdp_ready(cdp_url):
        return
    chrome = Path(os.environ.get(
        "SIDEQUESTOR_GDOC_CHROME",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )).expanduser()
    if not chrome.is_file():
        raise RuntimeError(f"Google Chrome was not found at {chrome}")
    workspace = Path(os.environ.get("SIDEQUESTOR_WORKSPACE") or os.getcwd())
    profile = Path(os.environ.get(
        "SIDEQUESTOR_GDOC_CHROME_PROFILE",
        str(workspace / "state/browser-profiles/gdoc-comments"),
    )).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(chrome), "--headless=new", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--window-size=1400,1800", "--no-first-run",
         "--no-default-browser-check", "about:blank"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(20):
        if _cdp_ready(cdp_url):
            return
        time.sleep(0.5)
    raise RuntimeError(
        "Chrome CDP did not start; run launch-chrome.sh once headful to authenticate")


def _find_dialog(page):
    return page.locator(FIND_DIALOG).filter(has_text="Find and replace").last


def _close_find(page):
    dialog = _find_dialog(page)
    if dialog.is_visible():
        dialog.get_by_role("button", name="Close").click()
        page.wait_for_timeout(200)


def _open_find(page):
    _close_find(page)
    page.get_by_text("Edit", exact=True).first.click()
    page.get_by_text("Find and replace", exact=True).last.click()
    find = page.locator(FIND_INPUT)
    find.wait_for(state="visible", timeout=5000)
    return _find_dialog(page), find


def _select_unique_match(page, dialog, find, text):
    match_case = dialog.get_by_role("checkbox", name="Match case")
    if match_case.count() != 1:
        raise RuntimeError("Find and replace did not expose the Match case control")
    match_case.check()
    if not match_case.is_checked():
        raise RuntimeError("Find and replace did not enable exact-case matching")
    find.fill(text)
    page.wait_for_timeout(600)
    count = re.search(r"(\d+)\s+of\s+(\d+)", dialog.inner_text())
    if not count or int(count.group(2)) == 0:
        raise RuntimeError(f"anchor text was not found: {text!r}")
    if int(count.group(2)) != 1:
        raise RuntimeError(f"anchor text is ambiguous ({count.group(2)} matches): {text!r}")
    find.press("Enter")


def _post_and_verify(page, doc_id, known_ids, requested):
    composer = page.locator(COMPOSER).first
    try:
        composer.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        page.keyboard.press("Escape")
        raise RuntimeError(
            "comment composer did not open; refused to type into the document body") from exc
    composer.fill(requested["comment"])
    page.get_by_role("button", name="Post Comment").click()
    return _new_verified_comment(doc_id, known_ids, requested)


def _new_verified_comment(doc_id, before_ids, requested, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _comments(doc_id)
        for comment_id, row in current.items():
            quoted = html.unescape(str((row.get("quotedFileContent") or {}).get("value") or ""))
            if (comment_id not in before_ids and row.get("content") == requested["comment"]
                    and quoted == requested["text"]):
                return {
                    "anchor": requested["text"], "comment": requested["comment"],
                    "comment_id": comment_id, "quoted_text": quoted, "status": "anchored",
                }
        time.sleep(1)
    raise RuntimeError(f"posted comment was not verified as anchored on {requested['text']!r}")


def run(payload):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed; reinstall Sidequestor with its declared dependencies") from exc

    doc_id = payload["doc_id"]
    cdp_url = os.environ.get("SIDEQUESTOR_GDOC_CDP_URL", "http://127.0.0.1:9222")
    _assert_capabilities(doc_id)
    _ensure_chrome(cdp_url)
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("Chrome CDP has no browser context")
        context = contexts[0]
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        matches = [page for page in context.pages if f"/document/d/{doc_id}" in page.url]
        page = matches[0] if matches else context.new_page()
        for duplicate in matches[1:]:
            duplicate.close()
        if f"/document/d/{doc_id}" not in page.url:
            page.goto(doc_url, wait_until="domcontentloaded", timeout=30000)
        page.bring_to_front()
        page.locator(CANVAS).wait_for(state="visible", timeout=20000)
        results = []
        known_ids = set(_comments(doc_id))
        for requested in payload["anchors"]:
            page.locator(CANVAS).click(timeout=5000)
            dialog, find = _open_find(page)
            _select_unique_match(page, dialog, find, requested["text"])
            page.wait_for_timeout(400)
            dialog.get_by_role("button", name="Close").click()
            page.wait_for_timeout(500)
            page.get_by_role("button", name=re.compile(r"^Add comment")).last.click()
            row = _post_and_verify(page, doc_id, known_ids, requested)
            page.keyboard.press("Escape")
            known_ids.add(row["comment_id"])
            results.append(row)
        return {"ok": True, "doc_id": doc_id, "results": results}
    finally:
        playwright.stop()


def main():
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(run(payload), ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
