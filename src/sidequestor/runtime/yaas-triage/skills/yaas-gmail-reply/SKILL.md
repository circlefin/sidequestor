---
name: yaas-gmail-reply
description: Send an explicit sender-only or reply-all Gmail response through the native gws reply helpers, with threading and quoted history handled automatically.
type: worker-tool
---

# yaas-gmail-reply

Sends a Gmail reply through `gws gmail +reply` or `gws gmail +reply-all`.
The native helper preserves the thread and quotes the original message.

## When to use

Use this helper any time a quest instructs you to reply to a Gmail message.
Read the complete thread first, decide who should receive the response, and
state that decision explicitly in the command. Do not infer a default mode.

## Required reply mode

Exactly one reply mode is required:

- `--reply-sender` replies only to the author or `Reply-To` address of the selected message.
- `--reply-all` replies to the author and all original `To` and `Cc` recipients.

The command fails before calling `gws` when neither or both modes are supplied.
`--cc` and `--remove` are valid only with `--reply-all`; sender-only mode cannot
add or remove recipients.

## Authorization

Before composing the body, load
`.yaas/engine/current/skills/yaas-draft-readiness/SKILL.md`. Draft first unless the quest has
`allow_send: true` or an exact claimed approval authorizes this email. Exactly one delivery mode,
`--draft` or `--send`, is required. Use `--draft` when direct sending is not authorized. A
reply-all review must identify the full intended audience, including added or removed recipients.

`--send` is enforced, not advisory: it requires `--quest-id`, the quest must be active and match
the dispatch target, and it needs either `allow_send: true` or `--approval-id` naming a claimed
approval for this exact email. To request one, rerun the same command with `--approval-spec`
instead of `--send` and merge the printed `{action_type, target, message_text}` into
`sq approval write`. The target pins the message, added and removed recipients, HTML mode and
each attachment's SHA-256, and `message_text` is the body, so a changed audience, attachment or
body needs a new review. `send_fresh.py` follows the same rule with `--to` and `--subject` pinned.

## Usage

```bash
GWS_BIN=$(command -v gws || echo /opt/homebrew/bin/gws) \
  python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/skills/yaas-gmail-reply/gmail-reply.py" \
  <gmail_message_id> --reply-sender --draft --body "<reply text>"
```

```bash
GWS_BIN=$(command -v gws || echo /opt/homebrew/bin/gws) \
  python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/skills/yaas-gmail-reply/gmail-reply.py" \
  <gmail_message_id> --reply-all --draft --body "<reply text>"
```

- `<gmail_message_id>` is the specific Gmail message being answered.
- `--body "<text>"` supplies the reply; alternatively pipe the body via stdin.
- `--cc <emails>` adds comma-separated CC recipients to a reply-all. The flag may be repeated.
- `--remove <emails>` removes recipients from a reply-all. The flag may be repeated.
- `--html` treats the body as HTML and preserves Gmail-style HTML quoting.
- `--attach <path>` adds an attachment. The flag may be repeated.
- `--draft` creates a draft; `--send` delivers immediately; `--approval-spec` prints the approval
  this exact reply needs and sends nothing. Exactly one is required.
- `--quest-id <id>` and, for a draft-only quest, `--approval-id <id>` authorize `--send`.

The native `gws` helpers set `threadId`, `In-Reply-To`, and `References` and
quote the original message. Sidequestor does not construct MIME or append history itself.

### Temporary reply-all CC workaround

GWS 0.22.5 can omit original CC recipients when Gmail returns a noncanonical
`CC` or `cc` header (`googleworkspace/cli#911` and `#642`). The wrapper temporarily
reads message metadata case-insensitively and supplies affected addresses through
native `--cc`. Remove this workaround after the minimum supported GWS version
contains the upstream header fix.

Prints the sent message or draft ID on success and exits non-zero on failure.

## Environment

- `GWS_BIN` is the path to the `gws` CLI and defaults to `gws` on `PATH`.
- `SIDEQUESTOR_FROM_EMAIL` is the sender or send-as alias. `YAAS_FROM_EMAIL`
  remains a compatibility fallback. If neither is set, `gws` uses the account default.
