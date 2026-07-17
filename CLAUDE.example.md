# YaaS Worker Instructions (Template)

This is the **generic** worker-instruction template that ships at the repo
root alongside `yaas-triage/`. Copy it to `CLAUDE.md` (same directory) and
customize:

- Add your bot identity / tone rules
- Add references to your personal skills
- Add any project-specific behaviors

`CLAUDE.md` is what `claude -p` loads on every worker dispatch. Everything
below is required for the triage worker to operate correctly — do not delete
the section headers or the protocols; they're referenced by `yaas-triage/triage.sh`
via the dispatch prompt.

---

## When this file is loaded into a session

You're in one of two modes. Infer from context:

### Mode A — Triage dispatch (automated)

- Triggered by `triage.sh` calling `claude --model opus -p "YaaS worker dispatch: dirty targets: ..."` (headless mode — no REPL)
- The prompt includes a comma-separated list of dirty **targets**. A target is either a quest ID (e.g., `quest-my-quest-2026-04-28`) or the literal string `reactions`.
- Target-specific protocol:
  - `reactions` → **Reactions Fast Path** (§ Reactions). Self-contained. Do NOT read any quest folder.
  - quest ID → **Quest Activation Protocol** (below). Read only what you need.
- After processing all targets, emit the **Output Contract** summary and exit.
- Do NOT scan for activity anywhere else — triage.sh already did that. Act only on the specified targets.

**Token discipline.** Every tool call and every extra file read re-sends the conversation through the model, multiplying cost. Do the minimum. Read `context.md` before anything else; only read other files when you're about to act and actually need them.

### Mode B — Manual / interactive

- The user is talking to you directly in a Claude Code REPL.
- Do what the user asked.
- If the user wants to create a new quest, read `yaas-triage/skills/yaas-quest-creation/SKILL.md` and follow it (it wraps `yaas-triage/skills/yaas-quest-creation/new-quest.py`).
- Only emit the Output Contract if you did something that looks like a full Mode A run.

---

## Quest Activation Protocol (Mode A)

### ⚠️ Invariant: you do NOT modify existing `watch.json` entries

The triage.sh script is the **sole owner** of watermark state. It advances `last_checked_ts` for every watched target in every quest — clean ones immediately, dirty ones only after you exit successfully. **Never edit an existing entry in `watch.json`** (never change a `last_checked_ts`, never remove an entry). If you do, you'll corrupt the termination-safety guarantee.

You **may** append new entries to `watch.json` — see the "Track what you touched" rule below. New entries start with `last_checked_ts` set to the response_ts of your reply so triage looks forward from there.

You may:
- **Read** `watch.json` (to know what to re-query)
- **Append new entries** to `watch.json` `watches[]` — never modify existing ones
- **Write** `meta.json` (to change quest status / priority)
- **Append** to `timeline.ndjson` (to log actions)
- **Write or edit** `context.md` (to update narrative)
- **Move** the quest folder (`active/` → `completed/` or `archived/`)

---

For each dirty quest ID passed to you:

### 1. Read only what you need

Quest folder:
```
state/quests/active/<quest_id>/
├── context.md      ← why this quest exists + state      (you may write)
├── meta.json       ← status, priority, allow_send       (you may write)
├── watch.json      ← watermarks — READ ONLY for you     (triage.sh owns)
└── timeline.ndjson ← append-only log of prior actions   (you append)
```

**Default: read only `context.md`.** It tells you the objective and the per-quest decision rules. That is usually enough to know what to do with the new activity.

Read the other files **only when a specific decision requires them**:

- **`watch.json`** — only if you need to know which watermarks were in effect (e.g., to compute the cutoff for re-querying Slack or Gmail). For email watches: look in `watches[]` for entries with `"type": "email"` to get each entry's `query` and `last_checked_ts`, then re-run `gws gmail users messages list` + `get` to fetch full content. For Slack watches, triage already handed you the sneak-peek preview — query Slack directly and trust the results.
- **`meta.json`** — only when you're about to send a message (check `allow_send`) or transition status.
- **`timeline.ndjson`** — only when you need to check whether you already acted on this thread/message in a prior tick.

Never read all four as a reflex. Each file read costs a model round-trip.

### 2. Figure out what's actually new

**Slack watch types** (`slack_thread`, `slack_channel`, `slack_dm`): query with the appropriate MCP tool (`slack_read_thread`, `slack_read_channel`, `slack_search_public_and_private`).

**Email watch type** (`email`): read `watch.json` to get each entry's `query` and `last_checked_ts`. Then:
1. `gws gmail users messages list --params '{"userId":"me","q":"<query> after:<YYYY/MM/DD>","maxResults":10}'`
2. For each message ID, `gws gmail users messages get --params '{"userId":"me","id":"<id>","format":"full"}'`. Post-filter by `internalDate/1000 > last_checked_ts`.

**Schedule watch type** (`watches[]` with `"type": "schedule"`): the cron fired. No content to fetch — act based on what the quest says to do at that scheduled time.

### 3. Act

Based on the quest's `context.md`, the watch type that fired, and the new content, decide:

- **Someone replied to a tracked thread** → evaluate whether the quest's objective is met. If yes, update `meta.json` status to `completed`. If not, decide if you need to reply, escalate, or keep waiting. Log to `timeline.ndjson`.
- **A DM arrived from a watched partner** → read the thread context, compose a response (draft first unless quest explicitly authorizes `allow_send`), log action.
- **A new top-level message in a watched channel** → apply the quest's `context.md` decision rules. If the common fast-path is "log and ignore," just exit without any file edits.
- **A new email matching a watched query** → read the full message, apply the quest's `context.md` decision rules. **Always acknowledge the email** with a reply (via `yaas-triage/skills/yaas-gmail-reply/gmail-reply.py`) before or immediately after taking action — even if the action is just "request submitted, will follow up." Exception: bulk, automated, or notification emails where a human reply would be inappropriate. Log `info_received` or `message_sent` to `timeline.ndjson`.

Reactions are never handled here — they are their own dispatch target, see § Reactions Fast Path.

### 3a. Track what you touched (general rule — all quests)

Any time you send a message, post a draft, or otherwise act on a specific Slack message, **append a new entry to `watch.json` `watches[]`** with `type: "slack_thread"`, so the next triage tick picks up replies independently. Skip this if the thread is already in `watches[]`.

```json
{
  "type": "slack_thread",
  "channel_id": "<channel_id>",
  "thread_ts": "<parent_message_ts>",
  "last_checked_ts": "<response_ts from your reply>",
  "watch_mode": "read_only",
  "reason": "escalation in #channel-name — watching for resolution to relay to customer"
}
```

`watch_mode` is optional. Omit it for customer-facing threads (the default is to engage). Set it to `"read_only"` for internal escalation threads — see § 3c below.

**Critical:** set `last_checked_ts` to the `response_ts` of your own reply, NOT the current epoch. Using "now" would silently swallow any replies posted between your send and your watch.json write. Your reply's TS is the correct boundary: anything strictly newer is unseen.

If you don't have a reply TS (e.g., draft save), use the parent `thread_ts` as `last_checked_ts`. Triage will re-surface your own draft on the next tick.

**DMs need a second watch.** When you initiate a top-level DM (not a reply inside an existing thread), append BOTH a `slack_thread` watch on your outbound `message_ts` AND a `slack_channel` watch on the DM channel itself. In a 1-1 DM, the recipient's natural reply is a new top-level message in the channel, not a threaded reply, so a `slack_thread` watch alone will miss it. The `slack_channel` watch covers the top-level case. Set `last_checked_ts` to your outbound `response_ts` on both. This dual-watch rule applies only to DM channels (IM type); for public/private channels and existing threads, a single `slack_thread` watch is enough because the threading convention holds.

**Filter DM channel watches by quest relevance.** When a DM `slack_channel` watch fires with a new top-level message, read the message and decide whether it is a response on the original topic that established the watch. If it is on-topic, act per the quest objective. If it is unrelated (the person DMed you about something else entirely), log it to `timeline.ndjson` as an `info_received` event with `relevant: false` and a one-line note on what the message was about, and exit without composing a reply. A DM watch set up to track a specific outbound question is NOT an open licence to auto-reply on every future DM from that person. Scope is the specific outbound that established the watch.

**Exception — manual review queue (§3d):** when writing an action to `state/pending-approvals.json` instead of executing it immediately, do NOT append a `slack_thread` watch. Append an `approval` watch instead (see §3d). The `slack_thread` watch is added only after the approved action is actually executed, using the real `response_ts` as `last_checked_ts`.

### 3b. Execute your own commitments before exiting (general rule — all quests AND reactions)

This rule binds every outbound reply you compose, including Reactions Fast Path replies (§ Reactions) — "general rule" means the dispatch target doesn't matter. A `:claudeloading:` reply that says "will rerun X and confirm" owes the rerun in that same tick.

If your reply contains a forward-looking commitment ("I'll raise it in X", "let me ping Y", "I'm going to loop in Z"), execute it in the same tick before you exit. `watch.json` only watches inbound signals: messages, reactions, scheduled cron, email. A commitment that lives only as text inside a Slack message has no trigger for triage to fire on, so the next tick will not resurrect it. The user should not have to ping you to do something you just said you would do.

Trigger phrases to treat as TODOs you owe in-tick: "I'll", "let me", "I will", "I'm going to", "happy to", "will raise", "will loop in", "will check with", "let me figure out who", "let me tee it up". When one of these appears in a reply you are about to send, treat it as a task to complete this tick, not flavor text. Either execute it before exit, or rewrite the reply so it does not promise the action.

When you execute the committed action (e.g., posting in another channel, pinging another teammate), follow the § 3a "Track what you touched" rule for the new thread or message you create, so replies to your downstream action surface on the next tick.

If the commitment genuinely needs to wait (e.g., "after my call with X tomorrow"), append a `schedule` watch entry to `watch.json` with `next_fire_ts` set to the awaited time and a `reason` describing the commitment, so triage re-dispatches at that time and the next worker resumes the work.

### 3c. Internal escalation threads are read-only (general rule — all quests)

When you post into an internal routing or expert channel to request help on behalf of a user, the resulting thread is for **outcome monitoring only**. Those channels are staffed by humans who want a single crisp ask — continued bot presence is noise.

**Always set `"watch_mode": "read_only"`** when adding a `slack_thread` watch on a thread you posted into any internal help or routing channel (e.g. `#help-*`, `#oncall-*`, `#eng-*`, or any internal escalation channel in your org).

**When triage fires on a `read_only` watch:**
1. Read the new replies and update `context.md` with the latest state.
2. Log to `timeline.ndjson`.
3. Do NOT post back into the thread. No follow-ups, no re-summaries, no "thanks."
4. **Exception:** a human in the thread asks you a direct question by name. One reply is appropriate.
5. **If a resolution arrives:** draft the relay message to the customer, but do NOT post it in the escalation thread. Log `draft_posted` to `timeline.ndjson` and surface under Attention needed in the Output Contract.

### 3d. Manual review queue (general rule — all quests)

Use this queue when you cannot or should not act immediately and the action needs the user's review first. The live dashboard (`dashboard.html` / `yaas-triage/dashboard-server.py`) surfaces pending items; once reviewed, triage re-dispatches you to execute with the user's instructions applied.

**When to use it:**
1. `allow_send: false` in `meta.json` — any outbound action, regardless of channel.
2. A watch entry's `reason` contains `DRAFT ONLY` (case-insensitive) — any message to that target.
3. Your judgment: first message to an external party, file edits requested by a third party, messages in external shared channels, anything you are not confident enough to send unilaterally.

**Writing a review item:**

Use `yaas-triage/approval-helper.py write <json>` — it handles dedup, flock, and ID generation atomically. Pass a JSON object with: `quest_id`, `quest_title`, `action_type` (`slack_message` / `file_edit` / `remote_request`), `target` (`{channel_id, thread_ts}`), `message_text`, `context` (2-3 sentences: what triggered this, who is involved, why review is needed), `risk_reason`. The script prints the new approval ID on success, or nothing if an identical pending entry already exists (dedup by `quest_id` + target).

```bash
APPR_ID=$(python3 yaas-triage/approval-helper.py write \
  '{"quest_id":"...","quest_title":"...","action_type":"slack_message",
    "target":{"channel_id":"C...","thread_ts":null},
    "message_text":"...","context":"...","risk_reason":"..."}')
```

If `APPR_ID` is non-empty, append an `approval` watch to `watch.json` (additive only) and log `draft_posted` with `"approval_id": "$APPR_ID"` to `timeline.ndjson`. Do NOT add a `slack_thread` watch — see §3a exception.

```json
{"type":"approval","approval_id":"<APPR_ID>","last_checked_ts":"<current epoch as string>"}
```

**Executing a reviewed item — when dispatched for a quest and you find `status: "reviewed"`:**

1. Claim it: `python3 yaas-triage/approval-helper.py start <id>`. If it prints `skip:<status>`, another worker beat you or it was cancelled — log a `note` and exit 0.
2. Read `message_text` from the item (the user may have edited it in the dashboard). Read `review_note` if present — apply it as an instruction: rewrite tone, change format, adjust recipients, whatever it says. Use your full LLM judgment.
3. Execute the action. **If the send fails because the channel is restricted (e.g., `mcp_externally_shared_channel_restricted`):** save the draft to the actual target thread via `slack_send_message_draft` (with `channel_id` + `thread_ts`), then DM the user only the permalink to that thread. Do not paste the draft text in the DM — they can open the thread, find the draft in the compose box, and send it themselves.
4. Mark done: `python3 yaas-triage/approval-helper.py done <id> <response_ts>`.
5. Append a `slack_thread` watch to `watch.json` with `last_checked_ts = response_ts` (per §3a).
6. Log `executed` to `timeline.ndjson` with `approval_id`, `response_ts`, and a note on any changes applied from `review_note`.

**Cancellation edge case:** `start` returns `skip:cancelled` if the user cancelled between triage's check and your dispatch — log a `note`, exit 0.

### 4. Log everything to timeline.ndjson

Append one line per action:

```
{"ts":"<utc_iso>", "event":"<draft_posted|message_sent|executed|info_received|status_change|note|blocked>", "...":"..."}
```

If you **couldn't** complete an action (error, ambiguous situation, needed user input), log it as a `blocked` event in `timeline.ndjson` with details, and **stop without finishing the rest of the work**. Surface the blocker in the Output Contract under "Errors". Do NOT silently skip blocked work and exit clean — triage.sh interprets a clean exit as "everything handled" and will advance watermarks, burying the blocked activity.

(Mechanism note: `claude -p` exits 0 whenever Claude completes its output normally, so you can't force a non-zero exit through prose. The best you can do is surface the failure clearly in the Output Contract — and avoid taking further actions that would suggest the dispatch was a success.)

### 5. Move completed quests

- `meta.status = "completed"` → move the folder to `state/quests/completed/`.
- `meta.status = "cancelled"` → move to `state/quests/archived/`.
- Idle 7+ days with `awaiting_reply` → leave in `active/` but consider marking `blocked`.

---

## Reactions Fast Path

Reactions are their own dispatch target, **handled independently from quests**. When the dispatch target list contains `reactions`, run this path — do NOT read any quest folder files.

### Behavior table

| Reaction | Behavior | State file |
|---|---|---|
| `:claudeloading:` | Research the thread, reply in-thread with `slack_send_message` + `thread_ts`. | `state/claudeloading_replied.json` |
| `:writing_hand:` | Research, create a draft via `slack_send_message_draft` (never send). If draft fails with `mcp_externally_shared_channel_restricted`, save the draft to the actual target thread (with `channel_id` + `thread_ts`), then DM the user only the permalink to that thread — not the draft text. They'll open the thread, edit the draft in the compose box, and send it themselves. | `state/writing_hand_replied.json` |
| `:floppy_disk:` | Save context silently to `state/context-memory/` (see § Context memory). Do not reply. | `state/floppy_disk_saved.json` |

### Minimum run loop

For the `reactions` target, execute exactly these steps:

1. **Read `state/triage/pending_reactions.json`** — shape `{ "<emoji>": ["<msg_ts>", ...] }`.
2. **For each `(emoji, msg_ts)` pair:**
   a. `slack_read_thread` to get the message + thread context.
   b. Act per the table above.
   c. **Commitment check (§ 3b applies here).** Before sending, scan your draft reply for commitment phrases ("I'll", "will rerun", "will confirm", "let me"). If the thread asks you to DO something, do the work first and reply once with the result — never reply with a promise and exit. Verify capability (available tools, credentials in `.env`) before deciding you can't. If the action genuinely cannot complete in-tick, spawn a quest (§ New quests from reactions) before exiting so the promise has a trigger — a commitment tracked nowhere never resurfaces.
   d. **Append `msg_ts`** to the emoji's state file. This is how we avoid re-processing.
3. **Exit 0** when all entries are processed. Triage deletes `pending_reactions.json`.

### State file schemas

```json
// claudeloading_replied.json, floppy_disk_saved.json
{ "replied_timestamps": ["1774393839.892619", ...] }    // or "saved_timestamps"

// writing_hand_replied.json (adds skipped_notes for consciously-declined)
{ "replied_timestamps": [...], "skipped_notes": { "1768811362.124979": "why I chose not to reply" } }
```

### Skips and failures

- **`:writing_hand:`** and you consciously chose NOT to respond (too old, already resolved, not relevant) → add `msg_ts` as a key in `skipped_notes` with a one-sentence reason. Treated as "processed".
- **Worker exited non-zero** → leave state files untouched; `pending_reactions.json` persists; next tick re-surfaces.

### Thread tracking

The § 3a "Track what you touched" rule applies only to quest work. Reactions do NOT append to any `watch.json`. The state file (`*_replied.json`) is the sole record that the reaction was handled.

### Context memory structure (`:floppy_disk:`)

```
state/context-memory/
├── index.json            (master index: {slack_ts, channel, permalink, saved_at, files:[...]})
├── people/<name-slug>.md (per-person: Expertise & Role, Key Context with dated bullets)
└── topics/<topic-slug>.md
```

Rules: one message may feed multiple files; append, never overwrite; kebab-case names; cite the Slack permalink on every bullet.

### New quests from reactions

Only create a new quest if the thread genuinely needs ongoing tracking beyond the single reply/draft. Most `:claudeloading:` / `:writing_hand:` uses are one-shot (answer the question, done) and should NOT spawn a quest.

Spawn a quest only when:
- The reply itself opens a task (e.g., "I'll research X and come back") — quest tracks the follow-up thread for the return reply.
- The thread will clearly have multi-step follow-on (you need to track a response, a review cycle, etc.).

Otherwise: reply, append to state file, exit. No quest. `:floppy_disk:` alone never creates a quest.

---

## Slack access — native MCP tool, or `mcp-call.sh` fallback

Everywhere this file names a Slack tool (`slack_read_thread`, `slack_read_channel`, `slack_search_public_and_private`, `slack_send_message`, `slack_send_message_draft`, `slack_add_reaction`, …), that is the tool to use.

**If you have that tool natively, call it natively.** This is the default and the cheapest path. It is what happens on the Claude harness, where `worker.mcp.json` supplies the Slack MCP server.

**If you do NOT have a native Slack tool exposed** (e.g. you are running on a harness where the Slack MCP server isn't configured, or its status is not connected), do NOT flail — do NOT list servers, grep the repo, or try to discover a path. Go straight to the repo's shell bridge, which calls the exact same Slack tools over HTTP with the same arguments:

```bash
./yaas-triage/mcp-call.sh <tool_name> '<arguments_json>'
```

Examples (the `<tool_name>` and JSON keys are identical to the native tool's):
```bash
./yaas-triage/mcp-call.sh slack_read_thread '{"channel_id":"C0…","thread_ts":"1700…"}'
./yaas-triage/mcp-call.sh slack_search_public_and_private '{"query":"to:me","limit":5}'
./yaas-triage/mcp-call.sh slack_send_message '{"channel_id":"D0…","message":"…"}'
```

Rules for the shell path:
- **Two args only:** `<tool_name>` then a single-quoted JSON string. It prints the tool result to stdout (pipe through `jq` to extract fields); exit 0 = success, non-zero = auth/MCP error.
- **JSON discipline.** You are hand-writing the JSON, so nothing validates it before send. Keep the whole JSON in single quotes. If the `message` text contains a single quote, a double quote, or a newline, build the JSON safely rather than inlining it — e.g. `jq -nc --arg c "<channel>" --arg m "$MSG" '{channel_id:$c,message:$m}'` and pass that. A malformed blob is the one real failure mode here.
- **Same semantics as native.** `channel_id` is a channel/DM ID (`C…`/`D…`/`G…`), not a user ID; resolve a user's DM channel first. All tone, draft-vs-send, and behavioral rules apply unchanged — the transport does not change what you are allowed to send.

(This is why YaaS runs identically across the Claude, Codex, and Cursor harnesses: whichever one lacks a native Slack tool falls back to this single shell path. See the README "Choosing a harness" section.)

---

## Output contract (Mode A only)

When the run finishes, emit exactly this shape and stop:

```
Quests processed: <n dispatched>
  completed:  <ids or "none">
  in-progress: <ids or "none">
  errored:    <ids or "none">
Messages sent: <count>
Drafts created: <count>
Watermarks advanced: <count>
Attention needed:
  - <bullet or "none">
Errors:
  - <bullet or "none">
```

≤10 lines. No raw tool output, no full message bodies, no tokens. Anything the user needs to act on goes under **Attention needed**.

---

## Behavioral rules

1. **Only act on dispatched quests in Mode A.** Don't proactively scan channels.
2. **Never modify existing `watch.json` entries.** Triage.sh owns watermark advancement. You MAY append new entries per § 3a; additive only.
3. **Never send without authorization.** Draft first unless the quest explicitly says `allow_send: true` in `meta.json`.
4. **Log every outbound action** to `timeline.ndjson` with a permalink/message ID.
5. **Fail loud.** If you couldn't complete the dispatch, log a `blocked` event in the relevant `timeline.ndjson` and surface the failure in the Output Contract under "Errors". Don't continue as if it succeeded.
6. **Respect privacy absolutely.** Never share info about one person with another unless directly relevant or explicitly asked. When in doubt, share less.
7. **Nothing persists in memory after you terminate.** Write to `state/` if it matters.
8. **Honor in-tick commitments — in quest work AND reaction replies.** If your reply contains "I'll do X", do X in the same tick before exiting (see § 3b). `watch.json` does not track outbound promises, and reaction state files track nothing at all, so an unkept commitment never resurfaces on its own.
9. **Use the manual review queue for uncertain or high-risk actions.** When `allow_send: false`, when a watch reason says `DRAFT ONLY`, or when your judgment flags an action as risky — write to `state/pending-approvals.json` via `yaas-triage/approval-helper.py` rather than acting directly. See §3d.
10. **Acknowledge emails when acting on them.** Any time a quest takes action triggered by an email, send a reply to the sender acknowledging receipt before or immediately after acting (see § 3 email rule). Never leave a human email unanswered while acting on it silently.

---

## YOUR CUSTOMIZATION GOES BELOW THIS LINE

Add your personal sections here:
- Bot identity / voice / tone rules
- Daily/weekly scheduled routines specific to you
- References to your domain-specific skills (e.g., `skills/<your-area>/SKILL.md`)
- Personal channel/contact lookup tables
- Tone-of-voice norms (e.g., for `#your-team` channel, and for email replies — see below)

**Email reply tone.** Write as if composing an actual email — proper greeting ("Hi [name],"), full prose sentences, and a sign-off when the context warrants it. Never reply to an email in bullet-point Slack style. Match the register of the incoming message: casual for casual, formal for formal.

These additions don't change the protocol above — they extend it.
