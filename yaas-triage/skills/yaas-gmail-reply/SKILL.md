---
name: yaas-gmail-reply
description: Send a threaded Gmail reply from the worker. Use when a quest requires replying to a Gmail message and the reply must thread correctly (In-Reply-To / References headers). The gws CLI's messages.send does not set threading headers natively — this script handles that.
type: worker-tool
---

# yaas-gmail-reply

Sends a Gmail reply with correct RFC 2822 threading headers so it appears
as a reply in the recipient's thread, not a new message.

## When to use

Any time a quest instructs you to reply to a Gmail message. The `gws gmail
users messages send` command alone does not set `In-Reply-To` or `References`
headers, so replies show up as new conversations. This script fetches the
original message metadata, builds the headers, and sends via `gws`.

## Usage

```bash
GWS_BIN=$(command -v gws || echo /opt/homebrew/bin/gws) \
  python3 yaas-triage/skills/yaas-gmail-reply/gmail-reply.py <gmail_message_id> --body "<reply text>"
```

- `<gmail_message_id>` — the Gmail message ID to reply to (from `messages.list` or timeline)
- `--body "<text>"` — reply body; alternatively pipe via stdin

Prints the sent Gmail message ID on success. Exits 1 on failure.

## Environment

- `GWS_BIN` — path to gws CLI (defaults to `gws` on PATH)
- `YAAS_FROM_EMAIL` — sender display name + address, e.g. `Jane Smith <jane@example.com>`
  Set in `.env` and sourced by triage.sh.

## Example (from a quest context)

```bash
GWS_BIN=$(command -v gws || echo /opt/homebrew/bin/gws) \
  python3 yaas-triage/skills/yaas-gmail-reply/gmail-reply.py 19e12bf3e4bb4694 \
  --body "Why did the webhook retry queue go to therapy? It kept showing up uninvited."
```
