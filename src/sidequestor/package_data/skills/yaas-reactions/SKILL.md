---
name: yaas-reactions
description: The Sidequestor Reactions Fast Path — handle emoji-triggered actions (process/draft/save/adopt) applied to Slack messages. Load on an autonomous Mode A dispatch whose target is `reactions`. Self-contained; touches no quest folder except the adopt case.
---

# yaas-reactions

Load this when dispatched with `dirty target: reactions`. It is self-contained — do NOT read any quest folder except where the configured `adopt` reaction explicitly requires it. Drive the reaction lifecycle through `surfaces/react-lifecycle.py advance` only (never hand-composed add/remove). All shared-state and draft-first rules in `.yaas/engine/current/OPERATING.md` still apply.

Before interpreting any pending emoji, run `python3 yaas-triage/reaction_config.py` and use its JSON roles (`process`, `draft`, `save`, `adopt`, `loading`, `done`). `.env` may customize every one. Never infer the action from a hardcoded default when the resolved mapping differs.

## Reactions Fast Path

Reactions are their own dispatch target, **handled independently from quests**. When the dispatch target list contains `reactions`, run this path — do NOT read any quest folder files.

### Behavior table

| Reaction | Behavior | State file |
|---|---|---|
| `process` | The user's "process this" trigger. Research the thread, then reply through `surfaces/slack-send.py` without a `quest_id` (send-only mode) and with the parent `thread_ts`. Drive the reaction lifecycle as you go (see § Reaction lifecycle). | `state/claude_intensifies_replied.json` |
| `draft` | Research, then create a draft through `surfaces/slack-send.py` with `"draft": true` and no `quest_id`. Drive the reaction lifecycle as you go (see § Reaction lifecycle). If the draft fails with `mcp_externally_shared_channel_restricted`, save it to the actual target thread through the same helper, then DM the user only the permalink to that thread — not the draft text. They'll open the thread, find the draft in the compose box, and send it themselves. | `state/writing_hand_replied.json` |
| `save` | Save context silently to `state/context-memory/` (see § Context memory). Do not reply. No lifecycle (one-shot, no emoji swaps). | `state/floppy_disk_saved.json` |
| `adopt` | Adopt the message into its owning quest: find the active quest watching this channel/thread, append a `slack_thread` watch on it, log to that quest's timeline with `log-event.py` (never hand-write the line: it stamps the real UTC time, which you cannot know). Do not reply. Drive the reaction lifecycle as you go (see § Reaction lifecycle). | `state/incoming_envelope_adopted.json` |

### Reaction lifecycle (action reactions)

Every reaction where you **take action** carries a visible three-state lifecycle on the message. The action triggers are the resolved `process`, `draft`, and `adopt` role names. **Each transition unchecks the previous reaction and adds the next one** — never leave two lifecycle emojis on the message at once. The resolved `save` role is the sole exception: it saves silently and has no lifecycle.

1. **Marked for processing** — the user applies the trigger reaction. This is what the checker detects; it is the only lifecycle reaction the user adds.
2. **Processing** — the moment you pick the message up (before doing the work), advance it to the configured `loading` emoji with the ONE lifecycle verb:
   ```bash
   ./yaas-triage/surfaces/react-lifecycle.py advance <channel_id> <msg_ts> loading
   ```
3. **Done actioning** — once the action is complete (reply sent / draft posted / message adopted) and every in-tick commitment is done, advance to the configured `done` emoji:
   ```bash
   ./yaas-triage/surfaces/react-lifecycle.py advance <channel_id> <msg_ts> done
   ```

**Always use `react-lifecycle.py advance`, never hand-composed `slack-react.sh remove`/`add` pairs.** `advance` removes every OTHER lifecycle emoji and adds the target in one step, so the message can never show two lifecycle emojis at once, a half-applied previous run self-heals, and every transition is logged. It exits non-zero if the add fails, in which case the emoji did NOT advance and the step is not done. If the action genuinely can't complete this tick (blocked, needs a quest; or `adopt` found no owning quest and got skipped), leave it at the configured `loading` emoji — do NOT advance to `done`, since that signals completion. `slack-react.sh` still exists for one-off manual reactions, but the lifecycle goes through `advance`.

### Minimum run loop

For the `reactions` target, execute exactly these steps:

1. **Read `state/triage/pending_reactions.json`** — shape `{ "<emoji>": ["<msg_ts>", ...] }`.
2. **For each `(emoji, msg_ts)` pair:**
   a. `slack_read_thread` to get the message + thread context.
   b. **For action reactions, mark processing first:** swap the trigger reaction to the configured `loading` emoji (§ Reaction lifecycle) before doing the work. The configured `save` emoji has no lifecycle swap.
   c. Act per the table above.
   d. **Commitment check (§ 3b applies here).** Before sending, scan your draft reply for commitment phrases ("I'll", "will rerun", "will confirm", "let me"). If the thread asks you to DO something, do the work first and reply once with the result — never reply with a promise and exit. Verify capability (available tools, credentials in `.env`) before deciding you can't. If the action genuinely cannot complete in-tick, spawn a quest (§ New quests from reactions) before exiting so the promise has a trigger — a commitment tracked nowhere never resurfaces.
   e. **For action reactions, mark done:** once the action is complete and every in-tick commitment is done, swap configured `loading` → configured `done` (§ Reaction lifecycle). If blocked / spun into a quest / skipped, leave it at the configured `loading` emoji.
   f. **Append `msg_ts`** to the emoji's state file. This is how we avoid re-processing.
3. **Ack each `(emoji, msg_ts)` pair** in the ledger (§ 4a) using item_id `<emoji>:<msg_ts>` — `handled` when you replied / drafted / saved, `nothing_to_do` for a conscious skip, `blocked` if you couldn't. Triage keeps every unacked pair in `pending_reactions.json` for the next tick and deletes the file only once all of them are closed, so a reaction you silently skip is no longer lost.
4. **Exit 0** when all entries are processed and acked.

For `process` and `draft`, never call `slack_send_message` or
`slack_send_message_draft` directly. Use the sanctioned surface even though reaction work has no
quest timeline:

```bash
python3 yaas-triage/surfaces/slack-send.py '{"channel_id":"C...","thread_ts":"...","message":"..."}'
python3 yaas-triage/surfaces/slack-send.py '{"channel_id":"C...","thread_ts":"...","message":"...","draft":true}'
```

Omitting `quest_id` deliberately selects send-only mode. The helper still enforces the stale
reply guard and keeps every backend on the same Slack identity.

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

The § 3a "Track what you touched" rule applies only to quest work. Reactions do NOT append to any `watch.json`. The state file (`*_replied.json`) is the sole record that the reaction was handled.

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
