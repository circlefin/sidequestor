---
name: yaas-reactions
description: The Sidequestor Reactions Fast Path — handle emoji-triggered actions (process/draft/save/adopt/expand) applied to Slack messages. Load on an autonomous Mode A dispatch whose target is `reactions`. Self-contained; logs to Inbox and touches other quest folders only for adoption or explicitly creating ongoing work.
---

# yaas-reactions

Load this when dispatched with `dirty target: reactions`. It is self-contained — do NOT read any quest folder except where the configured `adopt` reaction explicitly requires it. The runtime owns lifecycle emojis; workers report outcomes through the ack ledger, never direct emoji changes. All shared-state and draft-first rules in `.yaas/engine/current/OPERATING.md` still apply.

Before interpreting any pending emoji, run `python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/reaction_config.py"` and use its JSON roles (`process`, `draft`, `save`, `adopt`, `expand`, `loading`, `done`, `blocked`). `.env` may customize every one. Never infer the action from a hardcoded default when the resolved mapping differs.

## Reactions Fast Path

Reactions are their own dispatch target, **handled independently from quests**. When the dispatch target list contains `reactions`, run this path — do NOT read any quest folder files. Runtime send/lifecycle/ack helpers record reaction history in Inbox without changing send authorization. For saves, adoptions, and new quests, also use `log-event.py` with `quest_id: quest-inbox`, the source channel/message timestamp or permalink, outcome, and `linked_quest_id` when applicable. Include source/result links and any created/adopted quest ID in the ack note so blocked and skipped outcomes remain understandable in Inbox.

### Behavior table

| Reaction | Behavior | State file |
|---|---|---|
| `process` | Research the thread, then reply through `surfaces/slack-send.py` without a `quest_id` (send-only mode), with the parent `thread_ts`. On `mcp_externally_shared_channel_restricted`, retry through the same helper with `"draft": true` in the actual target thread, then DM the user only the draft permalink. Ack `handled` only after success; if both fail, ack `blocked`. | `state/claude_intensifies_replied.json` |
| `draft` | Research, then create a draft through `surfaces/slack-send.py` with `"draft": true` and no `quest_id`. If it fails with `mcp_externally_shared_channel_restricted`, save it to the actual target thread through the same helper, then DM the user only the permalink to that thread — not the draft text. They'll open the thread and send the draft themselves. Ack the outcome; the runtime updates the lifecycle. | `state/writing_hand_replied.json` |
| `save` | Save context silently to `state/context-memory/` (see § Context memory). Do not reply. No lifecycle (one-shot, no emoji swaps). | `state/floppy_disk_saved.json` |
| `adopt` | Find the active quest watching this channel/thread, add a `slack_thread` watch with `"refresh_activity": true`, and log to that quest with `log-event.py`. The helper re-arms an existing duplicate watch. Do not reply. Ack `blocked` if no owning quest can be found; otherwise ack the verified adoption. | `state/incoming_envelope_adopted.json` |
| `expand` | Expand your own message in place through the guarded `slack-expand.py` helper, then complete explicit requests or a verified quest handoff. See below; never fall back to posting a new message. | `state/magic_wand_expanded.json` |

### Expand in place (`expand`, default 🪄)

This is the fifth independent reaction action. Its first successful scan establishes
a baseline; reactions already present when the feature is enabled are not replayed. It works like `\expand`, without
requiring that marker in the message. Expand only the authenticated user's own
message. Preserve its intent, voice, audience, and level of formality, use local
skills and verified context, and remove `\expand` if present. Never invent facts
or commitments. Other trigger reactions on the same message remain independent.

Use the guarded helper, never a native edit tool or a new reply as a fallback:
`python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-expand.py" '<json>'`.
First locate missing coordinates through `ack-watch.py locate`. Call the helper
with `action: "inspect"`, the current `run_id`, `item_id`, `channel_id`,
`message_ts`, and `thread_ts` for a reply. It verifies ownership and returns the
exact `text` and `revision`. After preparing the expansion, call `action: "apply"`
with the same coordinates, those values as `expected_text` and
`expected_revision`, and the expanded text as `message`. The helper checks for
intervening edits and records completion separately from the business ack.

If `already_edited` is true, do not expand again. Use the returned text and
verified action receipts to continue only unfinished work. If `changed_after_edit`
is true, do not overwrite the user’s revision or assume it authorizes new work;
verify any outstanding actions or route them to review. After editing, carry
out explicit requested actions in the same dispatch under existing authorization
rules. For ongoing work, create a quest with the confirmed message/thread as
context and an explicit bootstrap trigger. Record verified completions; uncertain
external outcomes must be reconciled before retrying. Editing alone is not a
completed execution request. Acknowledge `handled` only after all requested work
is verified complete or has a verified quest handoff; otherwise acknowledge
`blocked`. If no edit or action is needed, `nothing_to_do` is appropriate.

For an uncertain edit, use the existing Inbox reaction-recovery review. The
claimed Inbox worker may call the same helper with the review's `approval_id`,
`run_id`, and original expand coordinates. It accepts only a live `executing`
review bound to that exact reaction and channel. A reviewed retry is allowed only
when the original text and edit revision are unchanged and at least 60 seconds
have elapsed since the uncertain attempt. For normalized text or a later user
revision, a claimed recovery can use `action: "resolve"` with
`resolution: "accept_current"` or `"abandon"` according to the reviewed instruction;
this records the resolution without sending another edit. Otherwise clarify the
review without replaying the edit. Completing the reviewed recovery closes the original lifecycle.

If ownership, current text, permissions, or an earlier edit outcome cannot be
verified, acknowledge `blocked`. Never post the expansion somewhere else. The
runtime owns lifecycle emojis and durable completion receipts; the legacy state
file for this role is `state/magic_wand_expanded.json` (`expanded_timestamps`).

### Reaction lifecycle (action reactions)

The runtime queues `loading` before launching the harness. `handled` and a conscious
`nothing_to_do` acknowledgement queue `done`; `blocked` queues the configured blocked
marker (default `warning`). Exhausted retries create a real Inbox review. A durable
outbox retries emoji delivery independently of business actions, including after a
restart. Never rerun a completed send just because an emoji update failed.

If the manifest lacks a channel ID, immediately after locating the message register it:
`python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/ledger/ack-watch.py" locate <run_id> <emoji:msg_ts> <channel_id>`.
The runtime then delivers pickup. Do this before attachments, research, or other work.
Do not call `react-lifecycle.py` or hand-compose lifecycle add/remove pairs. Slack
cannot swap atomically; the runtime adds the new status first and retries cleanup.
The `save` role remains silent with no lifecycle swap.

### Minimum run loop

For the `reactions` target, execute exactly these steps:

1. **Read `state/triage/pending_reactions.json`** — shape `{ "<emoji>": ["<msg_ts>", ...] }`.
2. **For each `(emoji, msg_ts)` pair:**
   a. `slack_read_thread` to get the message + thread context. If the reacted message (or the thread
      context you rely on) lists `Files:` with an image or PDF, fetch and open it before acting:
      `slack-file.py fetch '{"channel_id":"C...","ts":"<msg_ts>"}'` (read-only). `save` records what the
      attachment shows, not just its filename.
   b. **Register missing coordinates** with `ack-watch.py locate` immediately after locating the message, before fetching attachments or doing other work (§ Reaction lifecycle).
   c. Act per the table above. Before composing a `process` reply or `draft`, load
      `.yaas/engine/current/skills/yaas-answering-quality/SKILL.md` and
      `.yaas/engine/current/skills/yaas-draft-readiness/SKILL.md`.
   d. **Commitment check (§ 3b applies here).** Before sending, scan your draft reply for commitment phrases ("I'll", "will rerun", "will confirm", "let me"). If the thread asks you to DO something, do the work first and reply once with the result — never reply with a promise and exit. Verify capability (available tools, credentials in `.env`) before deciding you can't. If the action genuinely cannot complete in-tick, spawn a quest (§ New quests from reactions) before exiting so the promise has a trigger — a commitment tracked nowhere never resurfaces.
   e. Choose an honest outcome: completed action or verified tracked handoff → `handled`; consciously no action needed → `nothing_to_do`; unresolved capability/scope or uncertain external outcome → `blocked`. An unresolved task is not a conscious no-op.
   f. **Append `msg_ts`** to the emoji's state file only for completed or consciously declined work. Never mark unresolved work processed. The ack receipt also prevents re-processing even if status delivery fails.
3. **Ack each `(emoji, msg_ts)` pair** in the ledger (§ 4a) using item_id `<emoji>:<msg_ts>` — `handled` when you replied / drafted / saved, `nothing_to_do` for a conscious skip, `blocked` if you couldn't. Triage keeps every unacked pair in `pending_reactions.json` for the next tick and deletes the file only once all of them are closed, so a reaction you silently skip is no longer lost.
4. **Exit 0** when all entries are processed and acked.

For `process` and `draft`, never call `slack_send_message` or
`slack_send_message_draft` directly. Use the sanctioned surface even though reaction work has no
quest timeline:

```bash
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-send.py" '{"channel_id":"C...","thread_ts":"...","message":"..."}'
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-send.py" '{"channel_id":"C...","thread_ts":"...","message":"...","draft":true}'
```

Omitting `quest_id` deliberately selects send-only mode. The helper still enforces the stale
reply guard and keeps every backend on the same Slack identity.

When a `process` reply needs to include a file (a chart or screenshot you produced), post it with
`slack-file.py send`, also without `quest_id`, in the same thread:

```bash
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-file.py" send '{"channel_id":"C...","thread_ts":"...","file":"<local path>","initial_comment":"..."}'
```

Slack has no file drafts, so a `draft` reaction cannot attach a file: draft the text, and name the
file and its local path to the user in the DM instead of uploading it. Exit 3 from `send` means the
file may already be in the thread; read it before retrying, and ack `blocked`
if you cannot tell. Do not repeat the upload blindly.

### State file schemas

```json
// claude_intensifies_replied.json, floppy_disk_saved.json
{ "replied_timestamps": ["1774393839.892619", ...] }    // or "saved_timestamps"

// writing_hand_replied.json (adds skipped_notes for consciously-declined)
{ "replied_timestamps": [...], "skipped_notes": { "1768811362.124979": "why I chose not to reply" } }

// incoming_envelope_adopted.json (adds skipped_notes for no-quest-match / declined)
{ "adopted_timestamps": [...], "skipped_notes": { "1768811362.124979": "no active quest watches this channel" } }
```

### Skips and failures

- **`draft`** and you consciously chose NOT to respond (too old, already resolved, not relevant) → add `msg_ts` as a key in `skipped_notes` with a one-sentence reason. Treated as "processed".
- **Worker exited non-zero** → leave state files untouched; `pending_reactions.json` persists; next tick re-surfaces.

### Thread tracking

The § 3a "Track what you touched" rule applies only to quest work. The `adopt` role is the
sole exception: it adds or re-arms the owning quest's thread watch. Other reaction roles do
not append to any `watch.json`; their state file (`*_replied.json`) is the sole handling record.

### Context memory structure (`save`)

```
state/context-memory/
├── index.json            (master index: {slack_ts, channel, permalink, saved_at, files:[...]})
├── people/<name-slug>.md (per-person: Expertise & Role, Key Context with dated bullets)
└── topics/<topic-slug>.md
```

Rules: one message may feed multiple files; append, never overwrite; kebab-case names; cite the Slack permalink on every bullet.

### New quests from reactions

Only create a new quest if the thread genuinely needs ongoing tracking beyond the single reply/draft. Most `process` / `draft` uses are one-shot (answer the question, done) and should NOT spawn a quest.

Spawn a quest only when:
- The reply itself opens a task (e.g., "I'll research X and come back") — quest tracks the follow-up thread for the return reply.
- The thread will clearly have multi-step follow-on (you need to track a response, a review cycle, etc.).

Otherwise: reply, append to state file, exit. No quest. `save` alone never creates a quest.

---
