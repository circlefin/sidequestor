---
name: yaas-quest-dispatch
description: How to handle a Sidequestor quest dispatch — read the fired watches, act (draft-first), track threads, log, and ack. Load on an autonomous Mode A quest dispatch, or when interactively (Mode B) steering a quest. Uses only the locking helpers so it is safe while the loop ticks.
---

# yaas-quest-dispatch

**Mode A (autonomous):** you were dispatched with `dirty target: <quest_id>` and an item list. Work only that quest, follow §1-5, and ack every listed item before exiting.

**Mode B (interactive):** you may use §1-5 to inspect or steer a quest for a human — but with three constraints, because the loop may be handling the same quest right now (see the Shared-state rules in `.yaas/engine/current/OPERATING.md`): (a) use the SAME helpers below, never hand-edit state files; (b) do NOT open or ack the ack ledger — that is Mode A's; (c) before sending or editing `context.md`/`meta.json`, confirm no in-flight `state/triage/dispatch-*.json` manifest and no just-written timeline entry for this quest, else route through the review queue.

The loop owns watermark advancement in both modes: never edit an existing `watch.json` entry.

## Quest Activation Protocol (Mode A)

### ⚠️ Invariant: you do NOT modify existing `watch.json` entries

The triage orchestrator is the **sole owner** of watermark state. It advances `last_checked_ts` for clean watches immediately, and for a dispatched watch only when you closed it in the ack ledger (§ 4a) AND the checker proved it drained its window. Your exit code alone advances nothing. **Never edit an existing entry in `watch.json`** (never change a `last_checked_ts`, never remove an entry by hand). If you do, you'll corrupt the termination-safety guarantee. In Mode B, retire an obsolete watch only through `sq watch retire <quest_id> <watch_id> "<reason>"`; it refuses to run during an active dispatch.

You **may** append new entries to `watch.json` — see the "Track what you touched" rule below. New entries start with `last_checked_ts` set to the response_ts of your reply so triage looks forward from there.

You may:
- **Read** `watch.json` (to know what to re-query)
- **Append new entries** to `watch.json` `watches[]` — never modify existing ones
- **Write** `meta.json` (to change quest status / priority)
- **Append** to `timeline.ndjson` via `log-event.py` (to log actions)
- **Write or edit** `context.md` (to update the latest summary of things)
- **Move** the quest folder (`active/` → `completed/` or `archived/`)

---

For each dirty quest ID passed to you:

### 1. Read only what you need

Quest folder:
```
state/quests/active/<quest_id>/
├── context.md      ← objective, rules, latest summary   (you may write)
├── meta.json       ← status, priority, allow_send       (you may write)
├── watch.json      ← watermarks — READ ONLY for you     (the orchestrator owns)
└── timeline.ndjson ← append-only log of prior actions   (append via log-event.py)
```

**Default: read only `context.md`.** It tells you the objective and the per-quest decision rules. That is usually enough to know what to do with the new activity.

`context.md` is the compact working summary, not the history. Keep the objective, durable
decision rules, important links, and the latest summary of things there. Rewrite that summary
in place when the situation changes. `timeline.ndjson` is the chronological record of facts,
actions, messages, and milestones. Do not append dated updates, copied conversations, tool
output, or timeline-style log dumps to `context.md`.

Read the other files **only when a specific decision requires them**:

- **`watch.json`** — select every exact `watch_id` named in the dispatch prompt to get its source coordinates and watermark. For email watches, use that entry's `query` and `last_checked_ts`, then re-run `gws gmail users messages list` + `get` to fetch full content. For Slack watches, query the source named by the selected entry directly.
- **`meta.json`** — only when you're about to send a message (check `allow_send`) or transition status.
- **`timeline.ndjson`** — only when you need to check whether you already acted on this thread/message in a prior tick.

Never read all four as a reflex. Each file read costs a model round-trip. After `context.md`, select and process every exact dirty watch named in the dispatch prompt.

### 2. Figure out what's actually new

**Slack watch types** (`slack_thread`, `slack_channel`, `slack_dm`): query with the appropriate MCP tool (`slack_read_thread`, `slack_read_channel`, `slack_search_public_and_private`). For a `slack_thread` watch, read the complete thread without an `oldest` boundary, then post-filter messages newer than `last_checked_ts` to identify what triggered the dispatch. The whole thread is the conversational context; the watermark identifies what is new. Never decide whether to reply from the latest message or a watermark-truncated excerpt alone.

> **Truncate `last_checked_ts` to 6 decimals before using it as `oldest`/`latest`.** Slack returns
> ZERO messages for a timestamp with more precision than that, and returns them normally with
> exactly 6, so an over-precise watermark makes you blind to the very activity you were dispatched
> for. `1786939623.4141629` → `1786939623.414162`. Truncate, never round up. The orchestrator now
> stores watermarks already normalized, but entries written before 2026-08-17 can still carry the
> old precision, so check the value you read rather than trusting it.
>
> This is not hypothetical: on 2026-08-17 a worker passed a 7-decimal watermark to
> `slack_read_channel`, got an empty result, acked `nothing_to_do`, and the orchestrator advanced
> the watermark past a real unanswered request. **If a channel reads as empty but the dispatch says
> it is dirty, suspect this before concluding there is nothing to do** — and if the read still comes
> back empty, ack `blocked`, not `nothing_to_do`, so the watermark is not burned.

**Attached files.** Slack reads list attachments as `Files: <name> (ID: F…, <type>, <size>)` under the message. When a message you are acting on carries an image or PDF (a screenshot of an error, a diagram, a statement), fetch it and open the downloaded path before you answer: `python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-file.py" fetch '{"channel_id":"C...","ts":"<message ts>"}'` prints `{"files":[{"path":...}],"skipped":[...]}`. Read-only, so it needs no approval. Never infer what an image shows from its filename or the surrounding text. A `skipped` entry (wrong type, too large, not Slack-hosted) means you have not seen it; say so rather than guess.

**Slack mention watch type** (`slack_mention`): fires on any new message that @mentions the entry's `user_id`, anywhere Slack search can see (global, not channel-scoped). The entry has no channel, so read `watch.json` for the entry's `last_checked_ts`, re-run `slack_search_public_and_private` with query `<@USER_ID> after:<date>`, keep only results newer than the watermark (skipping `[BOT]` authors and the watched user's own posts), then `slack_read_thread` on each hit before acting.

**Telegram watch types** (`telegram_chat`, `telegram_search`): re-run the packaged
`surfaces/telegram-call.py` with the watch's `credential_id`, `peer`, a current `before_ts`, and a
bounded `limit`; include `query`/`from_user` for `telegram_search`. Post-filter returned messages
to `ts > last_checked_ts` and apply the entry's filters. The surface acts as the authorized user
and returns structured message IDs, sender IDs, timestamps, kinds, and text. Never claim an edit,
deletion, reaction, or Secret Chat event: these checkers only establish that a new cloud message
entered the selected history window.

If a connector is absent from `SIDEQUESTOR_CHECKER_CONNECTORS`, its watch was intentionally not
dispatched. Do not work around that operator opt-in with exploratory API calls.

**X watch types** (`x_search`, `x_mentions`, `x_user_posts`, `x_home`, `x_dm`): use the packaged
`surfaces/x-call.py GET ... user:<credential_id>` with the exact endpoint and source filter encoded
by the watch. Page only the dispatched interval and retain items newer than `last_checked_ts`.
For `x_dm`, ignore events sent by the authorized account and apply its conversation or participant
selector. A broad watch may fire on irrelevant posts; apply `context.md` before acting. Reads consume
X API credits, so do not make exploratory calls outside the dispatched watch's exact window.

**Email watch type** (`email`): read `watch.json` to get each entry's `query` and `last_checked_ts`. Then:
1. `gws gmail users messages list --params '{"userId":"me","q":"<query> after:<YYYY/MM/DD>","maxResults":10}'`
2. For each message ID, `gws gmail users messages get --params '{"userId":"me","id":"<id>","format":"full"}'`. Post-filter by `internalDate/1000 > last_checked_ts`.

**Schedule watch type** (`watches[]` with `"type": "schedule"`): the cron fired. Normally there
is no content to fetch; act based on what the quest says to do at that scheduled time.

One explicit exception supports installations with `SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0`. If the quest
context defines this schedule as a Slack sweep, read the same quest's `watch.json` and treat its
`slack_*` entries as MCP query targets. Use the fired schedule entry's `last_checked_ts` as the
shared lower bound, not the dormant Slack entries' frozen watermarks. Ack the schedule
`handled|nothing_to_do` only after every required Slack target was read successfully. If any read
fails, ack the schedule `blocked`, which holds the sweep window for retry. The Slack entries are
coordinates in this interim mode; never edit or ack them because they were not dispatched.

**Jira watch type** (`jira`): fires when an issue in the entry's `jql` set changed (status transition, new comment, any field edit — Jira bumps `updated` on all of them). An interactive Atlassian MCP is typically NOT exposed in headless dispatch, so do not reach for `searchJiraIssues`; it returns `tool_not_found` there. Use the REST bridge:
1. `bash "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/jira-call.sh" GET '/rest/api/3/search/jql?jql=<url-encoded>&fields=status,summary,updated&maxResults=100'` — re-read the set and diff it against what the quest last recorded.
2. `bash "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/jira-call.sh" GET '/rest/api/3/issue/<KEY>/comment'` — read new comments when the change was a reply rather than a transition. A reviewer question needs an answer (draft to the approval queue unless the quest sets `allow_send`).

Post a comment with `POST /rest/api/3/issue/<KEY>/comment` only when the quest authorizes it. The checker already confirmed something moved; your job is to identify what and act.

**GitHub PR watch type** (`github_pr`): fires when a PR in the entry's `repo` changed (new PR, new commit, review, comment, or merge). Note that a PR review comment does NOT bump the linked Jira issue, which is why this watch exists alongside `jira`. Use `gh`:
- `gh pr view <n> --repo <repo> --json number,title,state,updatedAt,comments,reviews` — activity on one PR.
- `gh pr diff <n> --repo <repo>` — what actually changed, when reviewing a fix.
- `gh search prs --repo <repo> --sort updated --order desc --limit 20 --json number,title,state,updatedAt` — re-locate what moved.

The watch is usually repo-wide, so it fires on PRs unrelated to the quest. **If the changed PR is out of scope, log nothing and exit** — do not investigate it, comment on it, or add a watch for it.

Before replying to an in-scope PR, check `timeline.ndjson` and the PR activity timestamps for the
same PR number. If the only update after the recorded action is the quest's own GitHub comment or
review, ack `nothing_to_do` and do not write again. Act only when a later human comment, review,
state change, or head commit adds new information. This guard is mandatory for `involves:<self>`
searches because the quest's own comment keeps the PR in the result set and bumps `updatedAt`.

**GitHub issue watch type** (`github_issue`): fires when an issue in the entry's `repo` changed (opened, commented, relabelled, closed). Pull requests are excluded, so it never double-reports with a `github_pr` watch on the same repo; pair the two when you want both halves. Use `gh`:
- `gh issue view <n> --repo <repo> --json number,title,body,state,author,labels,comments` — the issue and its discussion.
- `gh search issues --repo <repo> --sort updated --order desc --limit 20 --json number,title,state,updatedAt` — re-locate what moved.

Repo-wide, and the same out-of-scope rule applies. Two extra traps specific to this type: a fire may be a **comment or label change on an issue already handled**, which is not an instruction to act again, and it may be **the quest's own write** coming back around, which must never trigger a second round. Check the quest's `timeline.ndjson` for the issue number before acting.

If the entry carries `gh_account`, every `gh` call for that repo needs the same identity: prefix `GH_TOKEN=$(gh auth token -u <account>)`. Never run `gh auth switch` — it mutates global state and breaks every other repo for the rest of the run.

**`gh` write access is per-action, not all-or-nothing. Probe before declaring a block.** Run `gh api repos/<owner>/<repo> -q .permissions` and read the actual grant. With `pull: true, push: false`, all of these still work: PR comments (`gh api repos/<r>/issues/<n>/comments -X POST`), replies to inline review comments (`.../pulls/<n>/comments/<id>/replies`), new inline review comments including a ` ```suggestion ` block (`.../pulls/<n>/comments` with `commit_id`, `path`, `line`, `side`), and submitting a review (`.../pulls/<n>/reviews`). Only pushing a commit or branch returns `403`.

So a reviewer question, a correction, or a one-line fix is **never** blocked by `push: false` — answer it in-tick per §3b, using a suggestion block when the fix is a line or two so the author can commit it. Only a genuine multi-line or multi-file code change needs the approval queue or a DM. Never write "no gh write access" as a blanket reason without having run the permissions probe; a wrong capability assumption strands a reviewer for days. The same applies to any bridged service: try the REST bridge before concluding the service is unreachable because its MCP server is absent.

### 3. Act

Based on the quest's `context.md`, the watch type that fired, and the new content, decide:

Load `.yaas/engine/current/skills/yaas-action-closure/SKILL.md` when the quest may act on its
findings. Before composing or revising any outbound message, load both
`.yaas/engine/current/skills/yaas-answering-quality/SKILL.md` and
`.yaas/engine/current/skills/yaas-draft-readiness/SKILL.md`.

**Wait for your turn.** New activity is a reason to read the complete conversation, not an
obligation to speak. Before composing anything, read the full thread or conversation to
identify who is talking to whom, what remains unresolved, and whether the agent now has a clear
conversational turn.

Speak when a human directly asks the user or agent a question, supplies information that requires
their response, or the conversation has reached a conclusion that the quest must acknowledge or
act on. When several humans are talking to each other, let the exchange continue and wait for its
conclusion. Routing-only messages (`cc`, `for visibility`, adding someone), acknowledgements,
reactions, partial answers, and intermediate hand-offs normally mean **wait**. A person being
mentioned by someone else does not give the agent a turn to address or re-tag that person.

When it is not the agent's turn, take no outbound action, ack the watch `nothing_to_do`, and keep
watching the conversation. Silence is a successful outcome. Do not post a courtesy acknowledgement,
repeat the open questions, narrate that you are waiting, or manufacture a next step merely because
a human message triggered the watch. `allow_send: true` permits a send when one is warranted; it
does not create a conversational turn.

When it is the agent's turn, respond on the same surface, then complete any stated action in the
same dispatch under §3b. This turn-taking rule applies to Slack threads, channels, and DMs; Jira
comments; Telegram chats; X conversations; email; GitHub review, issue, and PR comments; and any
future reply-capable connector. Send through the surface's supported helper and normal
authorization path. If a warranted response requires review, queue it. If the connector cannot
write, ack `blocked` so the reply is retried instead of burning the watermark, except for a safely
transferred complete `slack_mention` fan-out under the explicit exception below. Self-authored
messages, bots, automated or bulk notifications, and non-conversational status or field changes
also require no reply.

Treat every legacy `watch_mode` field as inert metadata. It never overrides the turn-taking
decision above.

- **Someone added activity to a tracked thread** → read the complete thread and decide whether it is the agent's turn. If yes, respond and then evaluate whether the quest's objective is met. If no, ack `nothing_to_do` and keep waiting. If the objective is met, update `meta.json` status to `completed`; otherwise continue as the objective requires. Log only material new information or action.
- **A DM arrived from a watched partner** → read the conversation context and decide whether it calls for a response. If yes, compose one (draft first unless the quest explicitly authorizes `allow_send`) and log the action. If it is an acknowledgement or the conversation is still between other humans, wait.
- **A new top-level message in a watched channel** → apply the quest's `context.md` decision rules. If the common fast-path is "log and ignore," just exit without any file edits.
- **A new email matching a watched query** → read the full thread and decide whether it is the agent's turn. If a response is warranted, use `python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/skills/yaas-gmail-reply/gmail-reply.py"` and explicitly select `--reply-sender` or `--reply-all`. Draft first with `--draft`; omit `--draft` only when `allow_send: true` or an exact claimed approval authorizes that email and audience. Bulk, automated, notification, acknowledgement-only, and human-to-human messages require no reply. Log `info_received` or `message_sent` only when material.

Reactions are never handled here — they are their own dispatch target, see § Reactions Fast Path.

### 3a. Track what you touched (general rule — all quests)

Any time you send a message or post a draft, watch the thread so the next tick picks up replies:

```bash
sq watch <quest_id> '{"type":"slack_thread","channel_id":"C...","thread_ts":"<parent_ts>","last_checked_ts":"<response_ts>","reason":"why"}'
```

`add-watch.py` is the only way to add a watch: Edit/Write on `watch.json` is blocked by a hook. It appends, validates the type's fields, assigns the `watch_id`, and prints `skip:duplicate` if the thread is already watched, so you can call it without checking first.

**Pass `last_checked_ts` explicitly, as the `response_ts` of your own reply.** This is the one thing the script cannot decide for you, and the default is wrong for your case: your reply's ts is the correct boundary, whereas "now" silently swallows any reply posted between your send and this call. With no send ts (a draft), omit it and the script falls back to the parent `thread_ts`; triage then re-surfaces your own draft next tick, which you ignore as self-authored.

**DMs need a second watch, and it MUST be marked `ephemeral: true`.** When you initiate a top-level DM (not a reply inside an existing thread), append BOTH a `slack_thread` watch on your outbound `message_ts` AND a `slack_channel` watch on the DM channel itself, the latter with `"ephemeral": true`. In a 1-1 DM, the recipient's natural reply is a new top-level message in the channel, not a threaded reply, so a `slack_thread` watch alone will miss it. The `slack_channel` watch covers the top-level case. Set `last_checked_ts` to your outbound `response_ts` on both. This dual-watch rule applies only to DM channels (IM type); for public/private channels and existing threads, a single `slack_thread` watch is enough because the threading convention holds.

The `ephemeral` flag is not optional and is not decoration: it is what lets `housekeep.py` retire the watch after `SIDEQUESTOR_RETIRE_EPHEMERAL_HOURS` (default 168, one week). You are opening an *unbounded* watch to catch a *bounded* reply, and without the flag nothing ever closes it. On 2026-08-08 two such watches were still firing 3 and 12 days after their question was answered, one of them acted on a completely unrelated message, and the same content was posted to Slack twice. Mark it, or you are creating that bug again. Conversely, do NOT mark a watch that is meant to persist (a quest whose job is monitoring a bot's DMs or a channel) — unmarked is permanent, and that is the safe default.

**Filter DM channel watches by quest relevance.** When a DM `slack_channel` watch fires with a new top-level message, read the message and decide whether it is a response on the original topic that established the watch. If it is on-topic, act per the quest objective. If it is unrelated (the person DMed you about something else entirely), log it with `log-event.py` as an `info_received` event with `relevant: false` and a one-line note on what the message was about, and exit without composing a reply. A DM watch set up to track a specific outbound question is NOT an open licence to auto-reply on every future DM from that person. Scope is the specific outbound that established the watch.

**Exception — manual review queue (§3c):** when writing an action to `state/pending-approvals.json` instead of executing it immediately, do NOT append a `slack_thread` watch. Append an `approval` watch instead (see §3c). The `slack_thread` watch is added only after the approved action is actually executed, using the real `response_ts` as `last_checked_ts`.

### 3b. Execute your own commitments before exiting (general rule — all quests AND reactions)

This rule binds every outbound reply you compose, including Reactions Fast Path replies (§ Reactions) — "general rule" means the dispatch target doesn't matter. A reaction-workflow `process` reply that says "will rerun X and confirm" owes the rerun in that same tick.

If your reply contains a forward-looking commitment ("I'll raise it in X", "let me ping Y", "I'm going to loop in Z"), execute it in the same tick before you exit. `watch.json` only watches inbound signals: messages, reactions, scheduled cron, email. A commitment that lives only as text inside a Slack message has no trigger for triage to fire on, so the next tick will not resurrect it. The user should not have to ping you to do something you just said you would do.

Trigger phrases to treat as TODOs you owe in-tick: "I'll", "let me", "I will", "I'm going to", "happy to", "will raise", "will loop in", "will check with", "let me figure out who", "let me tee it up". When one of these appears in a reply you are about to send, treat it as a task to complete this tick, not flavor text. Either execute it before exit, or rewrite the reply so it does not promise the action.

When you execute the committed action (e.g., posting in another channel, pinging another teammate), follow the § 3a "Track what you touched" rule for the new thread or message you create, so replies to your downstream action surface on the next tick.

If the commitment genuinely needs to wait (e.g., "after my call with X tomorrow"), append a `schedule` watch entry to `watch.json` with `next_fire_ts` set to the awaited time and a `reason` describing the commitment, so triage re-dispatches at that time and the next worker resumes the work.

### 3c. Manual review queue (general rule — all quests)

Use this queue when you cannot or should not act immediately and the action needs the user's review first. The live dashboard (`$SIDEQUESTOR_RUNTIME_ROOT/dashboard.html` / `$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/ops/dashboard-server.py`) surfaces pending items; once reviewed, triage re-dispatches you to execute with the user's instructions applied.

**When to use it:**
1. `allow_send: false` in `meta.json` — any outbound action, regardless of channel.
2. A watch entry's `reason` contains `DRAFT ONLY` (case-insensitive) — any message to that target.
3. Your judgment: first message to an external party, file edits requested by a third party, messages in external shared channels, anything you are not confident enough to send unilaterally.

**Writing a review item:**

Use `sq approval write '<json>'` — it routes to the packaged approval helper and handles dedup, flock, and ID generation atomically. Pass a JSON object with: `quest_id`, `quest_title`, `action_type` (`slack_message` / `file_edit` / `remote_request`), `target` (`{channel_id, thread_ts}`), `message_text`, `context` (2-3 sentences: what triggered this, who is involved, why review is needed), `risk_reason`. The command prints the new approval ID on success, or nothing if an identical pending entry already exists (dedup by `quest_id` + target).

```bash
APPR_ID=$(sq approval write \
  '{"quest_id":"...","quest_title":"...","action_type":"slack_message",
    "target":{"channel_id":"C...","thread_ts":null},
    "message_text":"...","context":"...","risk_reason":"..."}')
```

`write` also arms the `approval` watch in the same call, which is why it is the only supported path (Edit/Write on `pending-approvals.json` is blocked by a hook): an approval with no watch is invisible to triage and strands forever. If `APPR_ID` is non-empty, log it with
`log-event.py '{"quest_id":"<qid>","event":"draft_posted","approval_id":"'"$APPR_ID"'","message_text":"<exact pre-review body>"}'`. The timeline copy is the immutable original even if review later edits the approval. Do NOT add a `slack_thread` watch — see §3a exception.

**Executing a reviewed item — when dispatched for a quest and you find `status: "reviewed"`:**

**Reclaimed item (`needs_reconcile: true`).** A previous worker claimed this action and its
lease expired, so the external action may already have happened. Reconcile before calling
`start`: check the target for the exact message, comment, reply, or requested change. If it is
present, close the item with `approval-helper.py done <id> <response_ts_or_url>` and log
`executed`; do not send or apply it again. If it is absent, claim and execute it normally. If the
outcome cannot be determined, log `blocked` and do not execute. For a `manual_instruction`, whose
arbitrary effects cannot be proven from one target, use `approval-helper.py abandon <id>
"<reason>"` rather than running it again. `done` clears `needs_reconcile` only after the outcome
has been resolved.

**Manual instructions.** An item with `action_type: "manual_instruction"` came directly from
the operator through the quest dashboard. Its `message_text` is the instruction to carry out,
not text to send. If several dispatched approval watches point to manual instructions, process
them in `created_at` order and claim/close each item separately. The instruction authorizes the
work request, but all normal quest safeguards still apply, including `allow_send`; do not infer
permission to send merely because the item is already reviewed.

For each manual instruction: claim it with `approval-helper.py start <id>`, perform the work in
the scope of this quest, then call `approval-helper.py done <id>` and ack that approval watch as
`handled`. Do not append a Slack watch merely because this is an approval item. If the work
actually sends a Slack message or creates a Telegram draft, use the matching helper and follow
the ordinary follow-up-watch rule where applicable: `slack-send.py` for Slack,
`telegram-send.py` for Telegram drafts. Continue processing
every other watch listed in this dispatch.

If a manual instruction is dispatched with an already-expired `executing` lease, its outcome is
uncertain and arbitrary work cannot be reconciled by checking one Slack thread. Do not run it
again. Log `blocked`, call `approval-helper.py abandon <id> "<reason>"`, and ack its approval
watch as `blocked`. The terminal cancellation prevents another paid dispatch; the operator can
submit a fresh instruction after checking the outcome.

1. Claim it: `sq approval start <id>`. If it prints `skip:<status>`, another worker beat you or it was cancelled — log a `note` and exit 0.
2. Read `review_note` first, then `message_text`. **`review_note` is the governing instruction and `message_text` is only a draft.** The dashboard's Approve and Request change buttons are both prompts to you; Approve differs only in that the item closes when you are done. So a note that countermands the draft wins over the draft: "send this to the other reviewer instead" means queue the retargeted action for a fresh review because the send helper binds approval to the original channel and thread, and "show me the updated draft first" means do NOT send at all, revise the text, and report back. `message_text` is what to send only when no note was given. Use your full LLM judgment.

   A single note can require several actions (a send, a file edit, an issue filed, a second message to someone else). Do all actions covered by the reviewed targets; queue any send to a new target for fresh review. Then report **one line per action** in your reply, each naming the surface and the target, so the review conversation shows everything the prompt caused rather than just the headline action. If the note told you not to send, say plainly that nothing was sent.
3. Execute the action through the destination helper. For Slack, use `slack-send.py`, passing the current `approval_id` in the JSON payload. For a reviewed Slack file upload (`action_type: remote_request`, `target.action: file_upload`), run `slack-file.py send` with the same `channel_id`, `file`, `filename`, `title`, `initial_comment` and `thread_ts` that produced the approval, plus `approval_id`; any difference (including different file bytes) is refused, and the fix is a fresh `approval-spec` and review, never editing the item. Exit 3 means the file may already be posted: read the thread before doing anything else, as for an expired lease below. **If the Slack send fails because the channel is restricted (e.g., `mcp_externally_shared_channel_restricted`):** retry through `slack-send.py` with `"draft": true`, saving the draft to the actual target thread with `channel_id` + `thread_ts`; then DM the user only the permalink to that thread. Do not paste the draft text in the DM — they can open the thread, find the draft in the compose box, and send it themselves. For Telegram, use `telegram-send.py`; it always uses `SaveDraftRequest` to create a native cloud draft and never delivers a message to the recipient. `allow_send` cannot turn this draft-only surface into a send.
4. Mark done: `sq approval done <id> <response_ts> "<report>"`. The third argument is your per-action report (one line per action) and lands in the review conversation, so pass it whenever the instruction produced anything beyond the obvious single send. An Approve is terminal, so this closes the item even when the instruction told you not to send.
5. Append a `slack_thread` watch to `watch.json` with `last_checked_ts = response_ts` (per §3a).
6. Log `executed` with `log-event.py`, including `approval_id`, the exact final `message_text`, the destination response identifier or URL, and a note listing every action the instruction produced. If `review_note` suppressed the send, log `executed` with an explicit "no send: <reason>" note rather than silently closing.

**Executing item whose lease expired.** A previous dispatch claimed this item and never closed it, so the send may or may not have landed. Do NOT resend blind. Read the target thread and look for the message. Present → close it with `approval-helper.py done <id> <response_ts>` and log `executed`. Absent → execute normally. Can't tell → log `blocked` and surface under Attention needed.

**Cancellation edge case:** `start` returns `skip:cancelled` if the user cancelled between triage's check and your dispatch — log a `note`, exit 0.

### 3d. Never escalate on your own initiative (general rule — all quests)

A thread going unanswered is not authorization to widen it. Escalation spends the user's political
capital on people who did not agree to the spend, and it lands on colleagues as an accusation
however carefully the sentence is phrased.

1. **One nudge, in the original thread.** Short, and with no reference to how long it has been.
2. **Then stop and report.** Surface it under Attention needed in the Output Contract so the user
   decides whether to escalate. Do not move the question to a wider channel, and do not tag anyone
   senior to the original audience, without an explicit go-ahead.
3. **When escalation is authorized**, do not date-stamp the silence ("unanswered since 4 Sep"), do
   not imply anyone dropped it, keep the people originally asked on the message rather than going
   around them, and do not attach loosely related links to make it feel urgent.
4. **Rule #10 in `yaas-answering-quality` still applies** inside the escalation: ask who owns it,
   never tell anyone to pick it up.

### 4. Log everything with `log-event.py`

Append one line per action **through `log-event.py`**. Never hand-write the JSON
line, and never write a `ts` yourself: you have no clock. Your context carries a
local date with no time of day, so a hand-written stamp lands hours off and, when
the local date is ahead of UTC, in the future — which sorts a finished action above
everything real on the dashboard. The helper stamps the true UTC time for you.

Logging is separate from summarizing. The timeline records what happened on every activation;
`context.md` only changes when the latest summary of things has materially changed.

```bash
sq log '{"quest_id":"<qid>","event":"note","note":"<what happened>"}'
```

`event` is one of `message_sent`, `draft_posted`, `executed`, `info_received`,
`status_change`, `note`, `blocked`. Any other key you pass (`channel_id`,
`thread_ts`, `message_text`, `link_url`, `reason`, …) is written through unchanged,
so record whatever the event needs. A `ts` you pass is ignored.

**Send through the surface helpers so the body is logged automatically.** For any quest action, use the matching helper instead of calling the underlying client directly and then logging separately:

```bash
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-send.py" '{"quest_id":"<qid>","approval_id":"<approval id, when executing a reviewed item>","channel_id":"C...","message":"<verbatim body>","thread_ts":"<parent ts, optional>","note":"<short summary>"}'
# add "draft": true to save a draft instead of sending; "event":"..." to override the default (message_sent / draft_posted)
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/telegram-send.py" '{"quest_id":"<qid>","peer":"@chat","message":"<verbatim body>","reply_to_message_id":"<message id, optional>","credential_id":"<optional credential id>","note":"<short summary>"}'
# Telegram saves a native cloud draft by default. Add "send":true and a unique "idempotency_key"
# only when allow_send=true or an exact claimed remote_request approval authorizes this send.
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/x-send.py" '{"quest_id":"<qid>","action":"reply","post_id":"<post id>","text":"<verbatim body>","credential_id":"default","idempotency_key":"<run id plus action coordinates>","approval_id":"<optional claimed remote_request approval>","note":"<short summary>"}'
# X writes require allow_send=true or an exact claimed approval. Never retry an indeterminate idempotency key blindly.
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-file.py" send '{"quest_id":"<qid>","approval_id":"<optional claimed remote_request approval>","channel_id":"C...","file":"<local path>","initial_comment":"<verbatim body, optional>","thread_ts":"<parent ts, optional>","note":"<short summary>"}'
# Posts a local file (e.g. an image) into Slack. Needs allow_send=true or a claimed remote_request approval.
# To queue one for review: run `slack-file.py approval-spec '<same json>'` and merge its whole output
# (action_type, target, message_text) into the `sq approval write` payload; it pins the file's SHA-256, title,
# comment and thread. Exit 3 means Slack may have posted it: check the conversation, never retry blindly.
# Exit 4 is safe to retry. Slack has no file drafts, so draft-first for a file means review via approval-spec.
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-file.py" fetch '{"channel_id":"C...","ts":"<message ts>"}'
# Downloads a message's attached images/PDFs (or one "file_id") to a private temp dir and prints the paths,
# so you can open them. Read-only. Add "any_type":true for other types; never pass an out_dir under state/.
```

The helper sends or drafts, then appends a timeline entry carrying the exact `message_text` in one step. Slack prints `{"response_ts":...,"permalink":...}` for the follow-up `watch.json` entry (§3a); Telegram drafts print `{"draft_saved":true}`, while sends print `{"delivered":true,"message_id":"..."}`. If the operation fails nothing is logged. This makes body-capture structural rather than something you have to remember.

**The underlying rule (why the helper matters):** the dashboard surfaces a message only when its timeline event carries a `message_text` field. A `note` summary alone shows in the full timeline but not in the Messages stream or the quest Conversation. Every outbound draft and reply event MUST therefore carry the exact body in `message_text` alongside its approval and destination identifiers. For approval-backed work, `draft_posted.message_text` preserves the exact pre-review body and the execution event preserves the exact final body. The mutable approval record is not evidence of the original draft. This applies to Reactions Fast Path replies too.

**Never write the NDJSON line yourself, for any event.** Slack goes through `slack-send.py` (files through `slack-file.py`), Telegram through `telegram-send.py`, X through `x-send.py`, and everything else through `log-event.py`; pass `message_text` to the helper rather than hand-rolling an entry around it. A hand-written line carries a `ts` you invented, and you have no clock: your context holds a local date with no time of day, so the stamp lands hours off and, when that date runs ahead of UTC, in the future, which sorts a finished action above everything real on the dashboard and pins it there.

**Non-Slack replies need their own link fields.** The dashboard renders an "open in <surface>" chip next to every logged reply, and it builds that link from what you log. So when a reply lands somewhere other than Slack, log the identifiers:

- **Jira comment** → `"jira":"PROJ-1234"` plus `"jira_comment_id":"<id from the POST response>"`.
- **GitHub PR comment / review** → `"repo":"<owner>/<repo>"`, `"pr":123`, and the comment id (`github_comment_id` for an issue comment, `review_comment_id` for an inline one), or simply the `html_url` that the `gh api` call returned.
- **Gmail reply** → `"gmail_thread_id"` plus `"sent_id"` (the id of the message you sent).
- Any surface at all: a logged `url` / `comment_url` / `html_url`, or the first entry of a `links` array, always wins over reconstruction, so when the API hands you a URL, log it.

When closing an approval whose action was a Jira/GitHub/Gmail post, pass that URL to `approval-helper.py done <id> <url>` instead of a Slack ts: it is stored as `result_url` and becomes the history link.


**Slack mention fan-out exception.** A `slack_mention` watch is one ledger item even when its
search returns many unrelated conversations. If you successfully read the complete dispatched
search window but one conversation's downstream action cannot finish, do not let that one action
block and replay the entire mention batch. Before continuing:

1. Log the specific blocker with its `channel_id`, `thread_ts`, and `message_ts`.
2. Before installing anything, perform a live thread read with the exact `channel_id` and parent
   `thread_ts`; confirm that the returned conversation contains the blocked `message_ts`. A reply
   timestamp is not a parent thread timestamp. If the live read fails or does not contain that
   message, do not transfer it.
3. Transfer retry responsibility to an exact `slack_thread` watch through `sq watch`, with
   `"ephemeral": true`, `"include_parent": true`, and
   `"one_shot_until_ts":"<blocked_message_ts>"`. Set its `last_checked_ts` to one microsecond
   before the blocked `message_ts`, so the triggering message itself reappears even when it is the
   top-level thread parent, and make the reason name the unfinished action. If several blocked
   messages belong to the same thread, use one watch starting just before the earliest one and set
   `one_shot_until_ts` to the latest one. After a successful ack advances through that timestamp,
   housekeeping retires the bounded-purpose watch. The checker caps the handoff at
   `one_shot_until_ts`, so later thread replies stay outside this retry. When the exact thread fires,
   check its messages against `timeline.ndjson` and retry only the transferred unfinished actions;
   never repeat a message already completed during the original mention dispatch. If `sq watch`
   prints `skip:duplicate`, confirm the existing thread watch's watermark is still before the
   earliest message and that it has `include_parent: true` plus the same bounded target when a
   blocked message equals `thread_ts`; never assume a duplicate has preserved the retry.
4. Continue processing every other mention in the dispatched window. After every result has been
   read and either completed, consciously skipped, or transferred, ack the original
   `slack_mention` item as `handled`, noting the transferred thread. The broad search watermark may
   then commit while the exact thread retries independently.

This exception is valid only when the dispatched mention item says `complete: true`, the full
mention window was read, and every blocked item was successfully transferred. If search coverage
is incomplete, source coordinates are missing, the exact thread cannot be verified by live read,
`sq watch` fails, a duplicate exact watch has already advanced past the blocked message, or a
duplicate lacks bounded parent inclusion for a blocked parent message, use the normal rule below
and ack the mention watch as `blocked`.

For every other case, if you **couldn't** complete an action (error, ambiguous situation, needed
user input), log it with `log-event.py` as a `blocked` event with details, and **stop without
finishing the rest of the work**. Surface the blocker in the Output Contract under "Errors". Ack
the item as `blocked` (§ 4a) so triage holds its watermark.

### 4a. Ack every dispatched item before you exit

`claude -p` exits 0 even when you only did half the work, so triage does not commit on your exit code. It commits per item, and only for items you close:

```bash
sq ack ack <run_id> <item_id> handled|nothing_to_do|blocked "<one-line note>"
```

One call per `watch_id` in `Exact dirty watches (JSON)` (item_id is `<emoji>:<msg_ts>` in a `reactions` dispatch). `handled` = you acted. `nothing_to_do` = you read it and it correctly needs no action. `blocked` = you couldn't finish. `handled` and `nothing_to_do` advance that watch's watermark; `blocked` and anything unacked hold it and come back next tick.

`blocked` comes back next tick, and keeps coming back: after `SIDEQUESTOR_UNACKED_PROMOTE` (default 3) dispatches with no progress the watch starts backing off (5m doubling to a 24h cap) but is never parked or abandoned, and the dashboard shows it as `backing off` with the last error. That is a backstop, not a plan — the retries get rarer, so three `blocked` acks in a row means the blocker needs saying out loud now rather than in a day. Name the actual cause in the ack note and in the `blocked` timeline event, and before you conclude a tool or credential is unavailable, prove it (run the command, read the error); a wrong capability assumption strands the watch and the person waiting behind it.

Two things the ledger cannot check for you:

- **An ack is a claim that you read the source.** A false `nothing_to_do` buries a real message, which is the failure this exists to prevent.
- **Ack as you go, not in a batch at the end.** If the watchdog kills the dispatch at 30 min, what you already acked commits and the rest correctly re-surfaces.

### 5. Move completed quests

- `meta.status = "completed"` → move the folder to `state/quests/completed/`.
- `meta.status = "cancelled"` → move to `state/quests/archived/`.
- Idle 7+ days with `awaiting_reply` → leave in `active/` but consider marking `blocked`.

---
