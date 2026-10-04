---
name: yaas-gdoc-anchored-comments
description: Add real text-anchored comments to a Google Doc from a Sidequestor quest through the guarded sq writer. Use when a quest must leave inline Google Doc comments; do not use for ordinary document edits or unanchored Drive comments.
---

# Anchored Google Doc comments

Use the guarded writer. Do not invoke the Playwright driver directly: `sq gdoc-comment` enforces
quest scope, approval, idempotency, serialization, Drive verification, and timeline logging.

Each anchor text must occur exactly once in the document. The writer fails closed on zero or
multiple exact-case matches and never types user text through the global keyboard. Each invocation
accepts exactly one anchor so the idempotency record and external write have the same boundary.
Use a distinct idempotency key for each additional comment. The writer supports edit-capable
documents in unattended mode; comment-only documents require an attended foreground workflow.

## Prerequisites

The `gws` CLI must be installed and authenticated as the same Google account, with Drive access.
The writer uses it to check commenting capabilities and to verify the new comment ID, content, and
quoted anchor after the browser action. Verify access before the first write:

```bash
gws drive files get --params '{"fileId":"<DOC_ID>","fields":"capabilities(canComment,canEdit)"}'
```

## One-time Google authentication

The helper uses a dedicated Chrome profile. Authenticate it once in a visible window:

```bash
bash "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/skills/yaas-gdoc-anchored-comments/launch-chrome.sh" "https://docs.google.com/document/d/<DOC_ID>/edit"
```

After login, the writer can reuse the profile and starts Chrome headlessly when CDP is not already
running. The capability is enabled by default. `SIDEQUESTOR_GDOC_COMMENTS_ENABLED=0` disables it.

## Write

Pass one JSON payload. Every write requires a unique `idempotency_key` so a completed browser
action can be replayed safely if timeline logging fails.

```bash
sq gdoc-comment '{
  "quest_id":"<quest-id>",
  "approval_id":"<claimed approval id, when required>",
  "idempotency_key":"<quest/doc/revision unique key>",
  "doc_id":"<Google Doc id>",
  "anchors":[
    {"text":"Exact unique text","comment":"Comment to post"}
  ],
  "note":"Why these comments were added"
}'
```

The command succeeds only when Drive returns a new comment ID whose content and
`quotedFileContent.value` exactly match the request. A browser interruption after a possible post
is recorded as indeterminate and is never retried automatically. The Post Comment control is
clicked only once; if verification times out, inspect the document instead of replaying the write.

## Approval when `allow_send` is false

Generate the exact approval coordinates and message:

```bash
sq gdoc-comment approval-spec '<same payload without approval_id>'
```

Merge that output into the ordinary `sq approval write` object with the quest title, context, and
risk reason. After review, claim the approval with `sq approval start <id>`, add its ID to the
original payload, and run the writer. The approval is valid only for that document and exact set
of anchor/comment pairs.
