---
name: yaas-gdoc-anchored-comments
description: Add real text-anchored comments to a Google Doc from a Sidequestor quest through the guarded sq writer. Use when a quest must leave inline Google Doc comments; do not use for ordinary document edits or unanchored Drive comments.
---

# Anchored Google Doc comments

Use the guarded writer rather than calling `gws` directly: `sq gdoc-comment` enforces quest scope,
approval, idempotency, serialization, Docs API verification, and timeline logging.

Each anchor text must occur exactly once in the document. The writer fails closed on zero or
multiple exact-case matches across all tabs, including headers, footers, and footnotes. Each invocation
accepts exactly one anchor so the idempotency record and external write have the same boundary.
Use a distinct idempotency key for each additional comment. The writer resolves text in the Docs
API's `SUGGESTIONS_INLINE` view and uses UTF-16 indexes, the tab ID, and any segment ID to anchor
the comment. Comments must not exceed 2048 UTF-8 bytes.

## Prerequisites

The `gws` CLI must be installed and authenticated with Docs and Drive access as the intended
comment author. The writer checks `canComment` before writing; the Docs API determines whether the
account can insert the comment. Comment-only access is supported. If the API returns a revision ID,
the writer binds the write to it to prevent concurrent edits from shifting the anchor. Google only
returns revision IDs to editors, so comment-only writes rely on the response's exact quoted anchor
for verification. Verify access before the first write:

```bash
gws drive files get --params '{"fileId":"<DOC_ID>","fields":"capabilities(canComment,canEdit)"}'
```

The capability is enabled by default. `SIDEQUESTOR_GDOC_COMMENTS_ENABLED=0` disables it.

## Write

Pass one JSON payload. Every write requires a unique `idempotency_key` so a completed API write
can be replayed safely if timeline logging fails.

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

The command succeeds only when `documents.batchUpdate` reports `commentUpdateState: ALL_SAVED` and
returns a new comment ID, anchor ID, exact comment content, and matching `plainTextQuote`. If the
API call fails or its result cannot be verified, the idempotency key is marked indeterminate and
is never retried automatically. Inspect the document before attempting another write.

## Approval when `allow_send` is false

Generate the exact approval coordinates and message:

```bash
sq gdoc-comment approval-spec '<same payload without approval_id>'
```

Merge that output into the ordinary `sq approval write` object with the quest title, context, and
risk reason. After review, claim the approval with `sq approval start <id>`, add its ID to the
original payload, and run the writer. The approval is valid only for that document and exact set
of anchor/comment pairs.
