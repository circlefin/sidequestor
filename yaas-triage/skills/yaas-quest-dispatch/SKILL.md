---
name: yaas-quest-dispatch
description: How to handle a Sidequestor quest dispatch — read the fired watches, act (draft-first), track threads, log, and ack. Load on an autonomous Mode A quest dispatch, or when interactively (Mode B) steering a quest. Uses only the locking helpers so it is safe while the loop ticks.
---

# yaas-quest-dispatch

**Mode A (autonomous):** you were dispatched with `dirty target: <quest_id>` and an item list. Work only that quest, follow §1-5, and ack every listed item before exiting.

**Mode B (interactive):** you may use §1-5 to inspect or steer a quest for a human — but with three constraints, because the loop may be handling the same quest right now (see the Coexistence section of CLAUDE.md): (a) use the SAME helpers below, never hand-edit state files; (b) do NOT open or ack the ack ledger — that is Mode A's; (c) before sending or editing `context.md`/`meta.json`, confirm no in-flight `state/triage/dispatch-*.json` manifest and no just-written timeline entry for this quest, else route through the review queue.

The loop owns watermark advancement in both modes: never edit an existing `watch.json` entry.

## Quest Activation Protocol (Mode A)

### ⚠️ Invariant: you do NOT modify existing `watch.json` entries

The triage orchestrator is the **sole owner** of watermark state. It advances `last_checked_ts` for clean watches immediately, and for a dispatched watch only when you closed it in the ack ledger (§ 4a) AND the checker proved it drained its window. Your exit code alone advances nothing. **Never edit an existing entry in `watch.json`** (never change a `last_checked_ts`, never remove an entry). If you do, you'll corrupt the termination-safety guarantee.

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
├── watch.json      ← watermarks — READ ONLY for you     (the orchestrator owns)
└── timeline.ndjson ← append-only log of prior actions   (you append)
```

**Default: read only `context.md`.** It tells you the objective and the per-quest decision rules. That is usually enough to know what to do with the new activity.

Read the other files **only when a specific decision requires them**:

- **`watch.json`** — select every exact `watch_id` named in the dispatch prompt to get its source coordinates and watermark. For email watches, use that entry's `query` and `last_checked_ts`, then re-run `gws gmail users messages list` + `get` to fetch full content. For Slack watches, query the source named by the selected entry directly.
- **`meta.json`** — only when you're about to send a message (check `allow_send`) or transition status.
- **`timeline.ndjson`** — only when you need to check whether you already acted on this thread/message in a prior tick.

Never read all four as a reflex. Each file read costs a model round-trip. After `context.md`, select and process every exact dirty watch named in the dispatch prompt.

### 2. Figure out what's actually new

**Slack watch types** (`slack_thread`, `slack_channel`, `slack_dm`): query with the appropriate MCP tool (`slack_read_thread`, `slack_read_channel`, `slack_search_public_and_private`).

**Slack mention watch type** (`slack_mention`): fires on any new message that @mentions the entry's `user_id`, anywhere Slack search can see (global, not channel-scoped). The entry has no channel, so read `watch.json` for the entry's `last_checked_ts`, re-run `slack_search_public_and_private` with query `<@USER_ID> after:<date>`, keep only results newer than the watermark (skipping `[BOT]` authors and the watched user's own posts), then `slack_read_thread` on each hit before acting.

**Email watch type** (`email`): read `watch.json` to get each entry's `query` and `last_checked_ts`. Then:
1. `gws gmail users messages list --params '{"userId":"me","q":"<query> after:<YYYY/MM/DD>","maxResults":10}'`
2. For each message ID, `gws gmail users messages get --params '{"userId":"me","id":"<id>","format":"full"}'`. Post-filter by `internalDate/1000 > last_checked_ts`.

**Schedule watch type** (`watches[]` with `"type": "schedule"`): the cron fired. No content to fetch — act based on what the quest says to do at that scheduled time.

**Jira watch type** (`jira`): fires when an issue in the entry's `jql` set changed (status transition, new comment, any field edit — Jira bumps `updated` on all of them). An interactive Atlassian MCP is typically NOT exposed in headless dispatch, so do not reach for `searchJiraIssues`; it returns `tool_not_found` there. Use the REST bridge:
1. `yaas-triage/surfaces/jira-call.sh GET '/rest/api/3/search/jql?jql=<url-encoded>&fields=status,summary,updated&maxResults=100'` — re-read the set and diff it against what the quest last recorded.
2. `yaas-triage/surfaces/jira-call.sh GET '/rest/api/3/issue/<KEY>/comment'` — read new comments when the change was a reply rather than a transition. A reviewer question needs an answer (draft to the approval queue unless the quest sets `allow_send`).

Post a comment with `POST /rest/api/3/issue/<KEY>/comment` only when the quest authorizes it. The checker already confirmed something moved; your job is to identify what and act.

**GitHub PR watch type** (`github_pr`): fires when a PR in the entry's `repo` changed (new PR, new commit, review, comment, or merge). Note that a PR review comment does NOT bump the linked Jira issue, which is why this watch exists alongside `jira`. Use `gh`:
- `gh pr view <n> --repo <repo> --json number,title,state,updatedAt,comments,reviews` — activity on one PR.
- `gh pr diff <n> --repo <repo>` — what actually changed, when reviewing a fix.
- `gh search prs --repo <repo> --sort updated --order desc --limit 20 --json number,title,state,updatedAt` — re-locate what moved.

The watch is usually repo-wide, so it fires on PRs unrelated to the quest. **If the changed PR is out of scope, log nothing and exit** — do not investigate it, comment on it, or add a watch for it.

**GitHub issue watch type** (`github_issue`): fires when an issue in the entry's `repo` changed (opened, commented, relabelled, closed). Pull requests are excluded, so it never double-reports with a `github_pr` watch on the same repo; pair the two when you want both halves. Use `gh`:
- `gh issue view <n> --repo <repo> --json number,title,body,state,author,labels,comments` — the issue and its discussion.
- `gh search issues --repo <repo> --sort updated --order desc --limit 20 --json number,title,state,updatedAt` — re-locate what moved.

Repo-wide, and the same out-of-scope rule applies. Two extra traps specific to this type: a fire may be a **comment or label change on an issue already handled**, which is not an instruction to act again, and it may be **the quest's own write** coming back around, which must never trigger a second round. Check the quest's `timeline.ndjson` for the issue number before acting.

If the entry carries `gh_account`, every `gh` call for that repo needs the same identity: prefix `GH_TOKEN=$(gh auth token -u <account>)`. Never run `gh auth switch` — it mutates global state and breaks every other repo for the rest of the run.

**`gh` write access is per-action, not all-or-nothing. Probe before declaring a block.** Run `gh api repos/<owner>/<repo> -q .permissions` and read the actual grant. With `pull: true, push: false`, all of these still work: PR comments (`gh api repos/<r>/issues/<n>/comments -X POST`), replies to inline review comments (`.../pulls/<n>/comments/<id>/replies`), new inline review comments including a ` ```suggestion ` block (`.../pulls/<n>/comments` with `commit_id`, `path`, `line`, `side`), and submitting a review (`.../pulls/<n>/reviews`). Only pushing a commit or branch returns `403`.

So a reviewer question, a correction, or a one-line fix is **never** blocked by `push: false` — answer it in-tick per §3b, using a suggestion block when the fix is a line or two so the author can commit it. Only a genuine multi-line or multi-file code change needs the approval queue or a DM. Never write "no gh write access" as a blanket reason without having run the permissions probe; a wrong capability assumption strands a reviewer for days. The same applies to any bridged service: try the REST bridge before concluding the service is unreachable because its MCP server is absent.

### 3. Act

Based on the quest's `context.md`, the watch type that fired, and the new content, decide:

- **Someone replied to a tracked thread** → evaluate whether the quest's objective is met. If yes, update `meta.json` status to `completed`. If not, decide if you need to reply, escalate, or keep waiting. Log to `timeline.ndjson`.
- **A DM arrived from a watched partner** → read the thread context, compose a response (draft first unless quest explicitly authorizes `allow_send`), log action.
- **A new top-level message in a watched channel** → apply the quest's `context.md` decision rules. If the common fast-path is "log and ignore," just exit without any file edits.
- **A new email matching a watched query** → read the full message, apply the quest's `context.md` decision rules. **Always acknowledge the email** with a reply (via `yaas-triage/skills/yaas-gmail-reply/gmail-reply.py`) before or immediately after taking action — even if the action is just "request submitted, will follow up." Exception: bulk, automated, or notification emails where a human reply would be inappropriate. Log `info_received` or `message_sent` to `timeline.ndjson`.

Reactions are never handled here — they are their own dispatch target, see § Reactions Fast Path.

### 3a. Track what you touched (general rule — all quests)

Any time you send a message or post a draft, watch the thread so the next tick picks up replies:

```bash
python3 yaas-triage/ledger/add-watch.py <quest_id> '{"type":"slack_thread","channel_id":"C...","thread_ts":"<parent_ts>","last_checked_ts":"<response_ts>","reason":"why","watch_mode":"read_only"}'
```

`add-watch.py` is the only way to add a watch: Edit/Write on `watch.json` is blocked by a hook. It appends, validates the type's fields, assigns the `watch_id`, and prints `skip:duplicate` if the thread is already watched, so you can call it without checking first.

**Pass `last_checked_ts` explicitly, as the `response_ts` of your own reply.** This is the one thing the script cannot decide for you, and the default is wrong for your case: your reply's ts is the correct boundary, whereas "now" silently swallows any reply posted between your send and this call. With no send ts (a draft), omit it and the script falls back to the parent `thread_ts`; triage then re-surfaces your own draft next tick, which you ignore as self-authored.

`watch_mode: "read_only"` for internal escalation threads (§ 3c); omit it for customer-facing ones.

**DMs need a second watch, and it MUST be marked `ephemeral: true`.** When you initiate a top-level DM (not a reply inside an existing thread), append BOTH a `slack_thread` watch on your outbound `message_ts` AND a `slack_channel` watch on the DM channel itself, the latter with `"ephemeral": true`. In a 1-1 DM, the recipient's natural reply is a new top-level message in the channel, not a threaded reply, so a `slack_thread` watch alone will miss it. The `slack_channel` watch covers the top-level case. Set `last_checked_ts` to your outbound `response_ts` on both. This dual-watch rule applies only to DM channels (IM type); for public/private channels and existing threads, a single `slack_thread` watch is enough because the threading convention holds.

The `ephemeral` flag is not optional and is not decoration: it is what lets `housekeep.py` retire the watch after `YAAS_RETIRE_EPHEMERAL_HOURS` (default 168, one week). You are opening an *unbounded* watch to catch a *bounded* reply, and without the flag nothing ever closes it. On 2026-08-08 two such watches were still firing 3 and 12 days after their question was answered, one of them acted on a completely unrelated message, and the same content was posted to Slack twice. Mark it, or you are creating that bug again. Conversely, do NOT mark a watch that is meant to persist (a quest whose job is monitoring a bot's DMs or a channel) — unmarked is permanent, and that is the safe default.

**Filter DM channel watches by quest relevance.** When a DM `slack_channel` watch fires with a new top-level message, read the message and decide whether it is a response on the original topic that established the watch. If it is on-topic, act per the quest objective. If it is unrelated (the person DMed you about something else entirely), log it to `timeline.ndjson` as an `info_received` event with `relevant: false` and a one-line note on what the message was about, and exit without composing a reply. A DM watch set up to track a specific outbound question is NOT an open licence to auto-reply on every future DM from that person. Scope is the specific outbound that established the watch.

**Exception — manual review queue (§3d):** when writing an action to `state/pending-approvals.json` instead of executing it immediately, do NOT append a `slack_thread` watch. Append an `approval` watch instead (see §3d). The `slack_thread` watch is added only after the approved action is actually executed, using the real `response_ts` as `last_checked_ts`.

### 3b. Execute your own commitments before exiting (general rule — all quests AND reactions)

This rule binds every outbound reply you compose, including Reactions Fast Path replies (§ Reactions) — "general rule" means the dispatch target doesn't matter. A reaction-workflow `process` reply that says "will rerun X and confirm" owes the rerun in that same tick.

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

Use this queue when you cannot or should not act immediately and the action needs the user's review first. The live dashboard (`dashboard.html` / `yaas-triage/ops/dashboard-server.py`) surfaces pending items; once reviewed, triage re-dispatches you to execute with the user's instructions applied.

**When to use it:**
1. `allow_send: false` in `meta.json` — any outbound action, regardless of channel.
2. A watch entry's `reason` contains `DRAFT ONLY` (case-insensitive) — any message to that target.
3. Your judgment: first message to an external party, file edits requested by a third party, messages in external shared channels, anything you are not confident enough to send unilaterally.

**Writing a review item:**

Use `yaas-triage/ledger/approval-helper.py write <json>` — it handles dedup, flock, and ID generation atomically. Pass a JSON object with: `quest_id`, `quest_title`, `action_type` (`slack_message` / `file_edit` / `remote_request`), `target` (`{channel_id, thread_ts}`), `message_text`, `context` (2-3 sentences: what triggered this, who is involved, why review is needed), `risk_reason`. The script prints the new approval ID on success, or nothing if an identical pending entry already exists (dedup by `quest_id` + target).

```bash
APPR_ID=$(python3 yaas-triage/ledger/approval-helper.py write \
  '{"quest_id":"...","quest_title":"...","action_type":"slack_message",
    "target":{"channel_id":"C...","thread_ts":null},
    "message_text":"...","context":"...","risk_reason":"..."}')
```

`write` also arms the `approval` watch in the same call, which is why it is the only supported path (Edit/Write on `pending-approvals.json` is blocked by a hook): an approval with no watch is invisible to triage and strands forever. If `APPR_ID` is non-empty, log `draft_posted` with `"approval_id": "$APPR_ID"` to `timeline.ndjson`. Do NOT add a `slack_thread` watch — see §3a exception.

**Executing a reviewed item — when dispatched for a quest and you find `status: "reviewed"`:**

**Manual instructions.** An item with `action_type: "manual_instruction"` came directly from
the operator through the quest dashboard. Its `message_text` is the instruction to carry out,
not text to send. If several dispatched approval watches point to manual instructions, process
them in `created_at` order and claim/close each item separately. The instruction authorizes the
work request, but all normal quest safeguards still apply, including `allow_send`; do not infer
permission to send merely because the item is already reviewed.

For each manual instruction: claim it with `approval-helper.py start <id>`, perform the work in
the scope of this quest, then call `approval-helper.py done <id>` and ack that approval watch as
`handled`. Do not append a Slack watch merely because this is an approval item. If the work
actually sends a Slack message, use `slack-send.py` and follow the ordinary follow-up-watch rule
for that send. Continue processing every other watch listed in this dispatch.

If a manual instruction is dispatched with an already-expired `executing` lease, its outcome is
uncertain and arbitrary work cannot be reconciled by checking one Slack thread. Do not run it
again. Log `blocked`, call `approval-helper.py abandon <id> "<reason>"`, and ack its approval
watch as `blocked`. The terminal cancellation prevents another paid dispatch; the operator can
submit a fresh instruction after checking the outcome.

1. Claim it: `python3 yaas-triage/ledger/approval-helper.py start <id>`. If it prints `skip:<status>`, another worker beat you or it was cancelled — log a `note` and exit 0.
2. Read `message_text` from the item (the user may have edited it in the dashboard). Read `review_note` if present — apply it as an instruction: rewrite tone, change format, adjust recipients, whatever it says. Use your full LLM judgment.
3. Execute the action. **If the send fails because the channel is restricted (e.g., `mcp_externally_shared_channel_restricted`):** save the draft to the actual target thread via `slack_send_message_draft` (with `channel_id` + `thread_ts`), then DM the user only the permalink to that thread. Do not paste the draft text in the DM — they can open the thread, find the draft in the compose box, and send it themselves.
4. Mark done: `python3 yaas-triage/ledger/approval-helper.py done <id> <response_ts>`.
5. Append a `slack_thread` watch to `watch.json` with `last_checked_ts = response_ts` (per §3a).
6. Log `executed` to `timeline.ndjson` with `approval_id`, `response_ts`, and a note on any changes applied from `review_note`.

**Executing item whose lease expired.** A previous dispatch claimed this item and never closed it, so the send may or may not have landed. Do NOT resend blind. Read the target thread and look for the message. Present → close it with `approval-helper.py done <id> <response_ts>` and log `executed`. Absent → execute normally. Can't tell → log `blocked` and surface under Attention needed.

**Cancellation edge case:** `start` returns `skip:cancelled` if the user cancelled between triage's check and your dispatch — log a `note`, exit 0.

### 4. Log everything to timeline.ndjson

Append one line per action:

```
{"ts":"<utc_iso>", "event":"<draft_posted|message_sent|executed|info_received|status_change|note|blocked>", "...":"..."}
```

**Send Slack messages through `slack-send.py` so the body is logged automatically.** For any quest send or draft, use the helper instead of calling `slack_send_message` / `slack_send_message_draft` (native or via `mcp-call.sh`) and then logging separately:

```bash
python3 yaas-triage/surfaces/slack-send.py '{"quest_id":"<qid>","channel_id":"C...","message":"<verbatim body>","thread_ts":"<parent ts, optional>","note":"<short summary>"}'
# add "draft": true to save a draft instead of sending; "event":"..." to override the default (message_sent / draft_posted)
```

It sends (or drafts), then appends a timeline entry carrying the exact `message_text`, `response_ts`, `permalink`, and your `note` in one step, and prints `{"response_ts":...,"permalink":...}` for the follow-up `watch.json` entry (§3a). If the send fails nothing is logged. This makes body-capture structural rather than something you have to remember.

**The underlying rule (why the helper matters):** the dashboard surfaces a message only when its timeline event carries a `message_text` field. A `note` summary alone shows in the full timeline but not in the Messages stream or the quest Conversation. If you ever log a send by hand (any `message_sent` / `reply_sent` / `dm_sent` / `executed`(slack or email) / `email_replied` event), you MUST include the exact text as `message_text` alongside `note` + `permalink` + `response_ts`. This applies to Reactions Fast Path replies too. (Drafts routed through the approval queue already carry their body in `pending-approvals.json`, so a `draft_posted` with an `approval_id` needs no `message_text`.)

**Non-Slack replies need their own link fields.** The dashboard renders an "open in <surface>" chip next to every logged reply, and it builds that link from what you log. So when a reply lands somewhere other than Slack, log the identifiers:

- **Jira comment** → `"jira":"PROJ-1234"` plus `"jira_comment_id":"<id from the POST response>"`.
- **GitHub PR comment / review** → `"repo":"<owner>/<repo>"`, `"pr":123`, and the comment id (`github_comment_id` for an issue comment, `review_comment_id` for an inline one), or simply the `html_url` that the `gh api` call returned.
- **Gmail reply** → `"gmail_thread_id"` plus `"sent_id"` (the id of the message you sent).
- Any surface at all: a logged `url` / `comment_url` / `html_url`, or the first entry of a `links` array, always wins over reconstruction, so when the API hands you a URL, log it.

When closing an approval whose action was a Jira/GitHub/Gmail post, pass that URL to `approval-helper.py done <id> <url>` instead of a Slack ts: it is stored as `result_url` and becomes the history link.


If you **couldn't** complete an action (error, ambiguous situation, needed user input), log it as a `blocked` event in `timeline.ndjson` with details, and **stop without finishing the rest of the work**. Surface the blocker in the Output Contract under "Errors". Ack the item as `blocked` (§ 4a) so triage holds its watermark.

### 4a. Ack every dispatched item before you exit

`claude -p` exits 0 even when you only did half the work, so triage does not commit on your exit code. It commits per item, and only for items you close:

```bash
python3 yaas-triage/ledger/ack-watch.py ack <run_id> <item_id> handled|nothing_to_do|blocked "<one-line note>"
```

One call per `watch_id` in `Exact dirty watches (JSON)` (item_id is `<emoji>:<msg_ts>` in a `reactions` dispatch). `handled` = you acted. `nothing_to_do` = you read it and it correctly needs no action. `blocked` = you couldn't finish. `handled` and `nothing_to_do` advance that watch's watermark; `blocked` and anything unacked hold it and come back next tick.

`blocked` comes back next tick, and keeps coming back: after `YAAS_UNACKED_PROMOTE` (default 3) dispatches with no progress the watch starts backing off (5m doubling to a 24h cap) but is never parked or abandoned, and the dashboard shows it as `backing off` with the last error. That is a backstop, not a plan — the retries get rarer, so three `blocked` acks in a row means the blocker needs saying out loud now rather than in a day. Name the actual cause in the ack note and in the `blocked` timeline event, and before you conclude a tool or credential is unavailable, prove it (run the command, read the error); a wrong capability assumption strands the watch and the person waiting behind it.

Two things the ledger cannot check for you:

- **An ack is a claim that you read the source.** A false `nothing_to_do` buries a real message, which is the failure this exists to prevent.
- **Ack as you go, not in a batch at the end.** If the watchdog kills the dispatch at 30 min, what you already acked commits and the rest correctly re-surfaces.

### 5. Move completed quests

- `meta.status = "completed"` → move the folder to `state/quests/completed/`.
- `meta.status = "cancelled"` → move to `state/quests/archived/`.
- Idle 7+ days with `awaiting_reply` → leave in `active/` but consider marking `blocked`.

---
