---
name: yaas-quest-creation
description: Scaffold a new quest folder under state/quests/active/. Use this when the user wants to start tracking a Slack thread, a reaction-triggered follow-up, or an ongoing conversation with a specific person. Invoke interactively — ask the user for title and watch targets, then create the 4-file quest folder. Does NOT create a quest without user confirmation.
---

# yaas-quest-creation

Scaffold a new yaas quest. A quest is a folder under `state/quests/active/` containing exactly four files. This skill creates that folder with correctly-shaped content and tells the user what was created.

## When to invoke

- User says "track this thread", "let's make a quest for X", "I want to follow up on Y"
- User pastes a Slack permalink and asks to be reminded when someone replies
- User mentions a recurring conversation they want the triage loop to monitor

## When NOT to invoke

- If the user is just asking a question about a thread
- If the task can be completed inside the current session (no follow-up needed)
- If the user is updating an existing quest — use the locking helpers and Mode B coexistence
  rules from `yaas-quest-dispatch`; never hand-edit shared JSON

## Run loop

### 1. Gather inputs interactively

Ask the user for, in order:

1. **Title** — one short line, used both as the display name and to generate the quest ID slug.
2. **What to watch** — at least one entry, each with a `type`:
   - **`slack_thread`** — provide Slack permalink(s). Extract `channel_id` and `thread_ts` from each URL. Use when watching a specific thread for new replies.
   - **`slack_channel`** — provide channel name or ID. Watches entire channel for new top-level messages. Does NOT catch replies inside existing threads; for those, add the specific thread as `slack_thread`.
   - **`slack_dm`** — needs BOTH `user_id` and `channel_id` (the DM channel, `D…`). Resolve a name → user ID via `mcp__slack__slack_search_users`, then resolve that user's DM channel; a `user_id` is not a channel. Fires on new DMs from that person.
   - **`slack_mention`** — a Slack `user_id`. Fires on any new message that @mentions that user, anywhere the searcher can see (global, not channel-scoped). No channel field; the worker re-runs the mention search to locate each hit. Use for "back me up: respond when anyone @mentions me." Skips `[BOT]` authors and the watched user's own messages.
   - **`schedule`** — fires the quest at a time you choose, in one of two shapes:
     - **repeating**: `cron` (5-field) + `tz` (IANA timezone). `tz` is required alongside `cron` — a bare cron is ambiguous.
     - **one-shot**: `next_fire_ts` (epoch seconds), and no `cron`. Fires once, then the watch is spent.
     Cron cannot express "every other week" and similar; for those, schedule the inner interval and have `context.md` gate on a state file the worker writes after it acts.
   - **`email`** — field name is `query` (a Gmail search string, e.g. `from:partner@example.com subject:invoice`). Matches any email matching the query that arrived after the watermark.
   - **`jira`** — a JQL string (e.g. `labels=my-label`, `project=PROJ AND assignee=currentUser()`). Fires when any issue in that set changes: status transition, new comment, or any field edit. Reads through `yaas-triage/surfaces/jira-call.sh`, so it works headless (the Atlassian MCP does not). Requires the Keychain API token `jira-api-token`/`yaas`. Do NOT include `ORDER BY` in the JQL: the checker adds its own ordering, and a caller-supplied one disables its early stop and makes every tick page to the cap.
   - **`github_pr`** — a `repo` as `owner/name`. Fires on any PR update in that repo: new PR, new commit, review, comment, or merge. Use it alongside `jira` when a fix lands as a PR, because **a PR review comment does not bump the linked Jira issue**, so the `jira` watch cannot see it. Optional `search` (extra GitHub qualifiers) and `limit` (default 100). Two traps before you add a `search`: repeated qualifiers AND rather than OR (`author:a author:b` matches nothing and the watch reports clean forever), and full-text terms only match what a PR happens to say. Also avoid `is:open`, which hides merges. Verify recall against a known PR set first; the details are in `checkers/github_pr.py`'s docstring.

   - **`github_issue`** — a `repo` as `owner/name`. Fires on any issue update in that repo: new issue, comment, label change, close. Pull requests are excluded (`gh search issues` omits them unless asked), so it never double-reports with `github_pr` — pair the two when you want both halves. Optional `search` (extra qualifiers, same AND-not-OR trap as `github_pr`), `limit` (default 100), and `gh_account` (a `gh` login whose token to use, for a private repo the ACTIVE `gh` account cannot see; resolved per-run via `gh auth token -u`, never persisted).

   All three of the above are repo/project-wide by nature, so they can fire on items unrelated to the quest. Say so in the `reason`, and make the quest's `context.md` tell the worker to exit immediately without acting when the changed item is out of scope.
   - **`approval`** — effectively runtime-only: the worker appends one itself when it queues a manual-review item (§3d of CLAUDE.md). Do not put one in a creation spec — there is no approval to track before the quest exists. `new-quest.py` does accept the type (with a required `approval_id`) so its type list matches `checkers/`, so this is a rule you follow, not one the script enforces.
   - **Anything else** — there is no other type. `new-quest.py` rejects a type with no
     `checkers/<type>.py`, because an unknown type would otherwise scaffold cleanly and then be
     skipped silently on every tick. To add one, write the checker first, then add
     `checkers/<type>.watch.json` beside it so the manifest defines the required fields.
     `yaas-triage/skills/yaas-ops/SKILL.md` has the state-file reference.

   **Prefer a pre-dispatch filter over waking Opus to decide relevance.** `slack_channel`
   and `slack_thread` watches accept two optional filter fields, evaluated inside the
   checker scripts (not the orchestrator) before the worker is woken — so a non-matching message costs nothing:
   - **`filter_user_ids`** — list of Slack user IDs; only messages authored by one of
     them wake the worker.
   - **`filter_keywords`** — list of strings; a message must contain at least one
     (case-insensitive **substring** match) to wake the worker.

   When both are set they are AND-ed (right author AND a keyword hit). Use these whenever
   the quest is "watch channel X for person Y saying Z" — e.g. a command prefix
   (`filter_keywords: ["/add-josh-safely"]`), a person's posts on a topic, or an alerting
   keyword set. Without them the worker wakes on every channel message and pays a model
   round-trip just to reject it. Reference: any quest of the form `watch channel X for person Y saying Z`.

   Two cautions: (a) substring match means `filter_keywords: ["/cmd"]` also matches a
   message that merely mentions `/cmd` mid-sentence, so if you need a true prefix, re-check
   `startswith` in the worker after wake; (b) extra watch fields are passed through
   verbatim and NOT validated, so a typo like `filter_keyword` (singular) is silently
   written and silently ignored — spell them exactly.

   **`watch_mode: "read_only"`** — add this to any watch on a thread in an internal routing or
   expert channel (`#help-*`, `#cpn-se-questions`, `#cpn-*`, `#api-key-permissions`, any
   `#oncall-*` or `#eng-*` escalation channel). It tells the worker to read replies and relay
   outcomes, but never post back into that thread. Without it, the bot will reply in expert
   channels and annoy the humans there. `new-quest.py` validates that the only legal value is
   `"read_only"` — any typo is caught at creation time.

   **Note:** reaction-workflow triggers (`process`, `draft`, `save`, and `adopt`) are tracked globally by the triage orchestrator and are NOT a per-quest watch input. Do not include them in `watches[]`.
3. **Priority** — high / normal / low. Default to normal if unspecified.
4. **Context** — ask the user to paste or describe what this quest is about. This becomes the body of `context.md`. If they already gave context earlier in the conversation, use that; don't re-ask.

### 2. Generate the quest ID

Format: `quest-<slug>-YYYY-MM-DD`

- Slug: lowercase title, non-alphanumerics replaced with `-`, collapsed, trimmed, max 40 chars. Example: "Correct EURC→USDC mechanism" → `correct-eurc-usdc-mechanism`.
- Date: today's UTC date, appended at the end.

Full example: `quest-correct-eurc-usdc-mechanism-2026-04-27`

### 3. Create the folder

**Always use `yaas-triage/skills/yaas-quest-creation/new-quest.py` — never write the files manually.**

The script handles all timestamps, ID generation, and correctly-shaped files. Manual `Write` calls have caused bugs (wrong `id` in meta.json, missing fields in watch.json). The script prevents all of that. Note: `watch_id` values are NOT in the scaffolded output — `ensure-watch-ids.py` injects them on the first triage tick. That is intentional and safe.

Build a JSON spec and pass it to the script via Bash:

```bash
python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '<spec_json>'
```

Spec fields:
- `title` (required) — display name
- `watches` (required) — entries WITHOUT `last_checked_ts`; the script injects it
- `priority` — `"high"` | `"normal"` | `"low"` (default: `"normal"`)
- `allow_send` — `true` | `false` (default: `false`)
- `requires_initial_run`: `true` only when the creation surface schedules an immediate
  first worker request. Omit it for ordinary monitoring and recurring-schedule quests.
- `context` — body text for `context.md`
- `note` — short note for the `created` timeline event (defaults to first 80 chars of context)
- `retire_slack_threads_after_days` — non-negative int or `false`:
  - omitted → the global default, `SIDEQUESTOR_RETIRE_DEFAULT_DAYS` (**14** days)
  - positive int N → drop `slack_thread` watches whose parent `thread_ts` is older than N days
  - `false` **or `0`** → never retire (use for long-lived partner conversations); `housekeep.py`
    treats both the same, along with any unparseable value
  - Examples: `7` for ephemeral chat (self-DM), `false` for partner monitoring

Example call:
```bash
python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '{
  "title": "Partner question — follow-up tracker",
  "priority": "normal",
  "allow_send": true,
  "context": "A partner asked a technical question; we replied and want to track the follow-up.",
  "watches": [
    {
      "type": "slack_thread",
      "channel_id": "C0123456789",
      "thread_ts": "1700000000.123456",
      "reason": "watching for partner follow-up on the technical question"
    },
    {
      "type": "schedule",
      "cron": "0 9 * * 1",
      "tz": "Asia/Singapore",
      "reason": "weekly Monday recap"
    }
  ]
}'
```

The script will:
1. Generate a unique quest ID from the title + today's UTC date
2. Set all `last_checked_ts` to the current epoch (so triage only looks forward)
3. Write all 4 files with the full canonical schema
4. Print a confirmation summary

Do NOT include `last_checked_ts` in the watches — the script will reject it.
Do NOT include reaction watch entries — reactions are tracked globally.

After the script runs, update `context.md` via `Edit` if you need to add more detail (Slack
permalinks, links, richer current-state narrative) that wasn't in the spec. Before editing it in
an interactive session, confirm no dispatch manifest is active for the quest; creation and the
background loop may overlap immediately after the folder appears.

### 4. Confirm to the user

The script prints its own confirmation. Relay it to the user and note any manual context.md updates you made.

## Extracting channel_id + thread_ts from a Slack permalink

A permalink looks like:
`https://<workspace>.slack.com/archives/<CHANNEL_ID>/p<TS_WITH_DOT_REMOVED>?thread_ts=<PARENT_TS>`

- **channel_id** = the segment after `/archives/`
- **thread_ts** = the `thread_ts` query param **if present**, otherwise the path `p...` segment converted back to `<10digits>.<6digits>` format

Example:
- `https://acme.slack.com/archives/C0123456789/p1700000000123456`
  → channel_id = `C0123456789`, thread_ts = `1700000000.123456`
- `https://acme.slack.com/archives/C0123456789/p1700000800000000?thread_ts=1700000000.123456`
  → channel_id = `C0123456789`, thread_ts = `1700000000.123456` (parent, not the reply)

Always watch the **parent thread_ts**, not a reply's ts.

## Validation checks before writing

**The script enforces these** — it exits non-zero rather than scaffolding something broken:

- Quest ID uniqueness across `state/quests/active/`, `archived/` **and `completed/`**. On a
  collision it appends `-2`, `-3`, … itself.
- `watches[]` has at least one entry, and every entry has a `type` and a `reason`.
- The `type` has an executable `checkers/<type>.py`, and carries that type's required fields.
- `watch_mode`, if present, is exactly `"read_only"`.
- No `last_checked_ts` — the script sets it.

**These are yours to check; the script does NOT** — a malformed ID scaffolds cleanly and then
fails at runtime, where it is much harder to notice:

- Channel IDs start with `C` (public), `G` (private group), `D` (DM), or `MP` (mpim). Anything
  else is almost certainly a user ID pasted into a channel field. Flag it to the user.
- User IDs start with `U`.
- The IDs point at what the user actually meant. Resolve names via
  `mcp__slack__slack_search_users` and confirm before writing them in.

## Error handling

If the user's input is ambiguous or missing required fields, ask a clarifying question. Never create a quest with placeholder content — either get real values or don't create the quest.

## Resolving names to user IDs

If the user gives a name instead of a Slack user ID, look it up via:

```
mcp__slack__slack_search_users with query="<name>"
```

Present the result for confirmation before using it in watch.json. If multiple matches, list them and ask which one.
