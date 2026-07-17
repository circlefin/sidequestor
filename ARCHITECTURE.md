# Yourself as a Service — Master Plan v2

A personal Slack/email agent that monitors threads, fires at scheduled times, and dispatches Claude Opus to act — at $0 cost when idle, only spending tokens when there's real work.

---

## Table of Contents

1. [High-level architecture](#1-high-level-architecture)
2. [LaunchD jobs](#2-launchd-jobs)
3. [Triage loop (triage.sh) — step by step](#3-triage-loop-triagesh--step-by-step)
4. [Checker plugins](#4-checker-plugins)
5. [Worker dispatch](#5-worker-dispatch)
6. [Quest system — data model and lifecycle](#6-quest-system--data-model-and-lifecycle)
7. [Reactions fast path](#7-reactions-fast-path)
8. [Skills system](#8-skills-system)
9. [Global state files](#9-global-state-files)
10. [Active quests](#10-active-quests)
11. [End-to-end trace: a single dirty tick](#11-end-to-end-trace-a-single-dirty-tick)
12. [Known issues and bugs](#12-known-issues-and-bugs)
13. [How to verify execution is working](#13-how-to-verify-execution-is-working)
14. [Personal vs generic separation](#14-personal-vs-generic-separation)

---

## 1. High-level architecture

```
macOS launchd (every 60s)
  └─> yaas-triage/triage.sh
        ├─ sources .env (CODA_API_KEY, YAAS_FROM_EMAIL, SLACK_* …)
        ├─ acquires flock (skip if previous run still in progress)
        ├─ for each quest in state/quests/active/*/
        │    └─ reads watch.json, dispatches to checkers/<type>.py
        │         ├─ slack_thread.py  → mcp-call.sh → Slack MCP API
        │         ├─ slack_channel.py → mcp-call.sh → Slack MCP API
        │         ├─ slack_dm.py      → mcp-call.sh → Slack MCP API
        │         ├─ schedule.py      → cron-due.py (local computation)
        │         └─ email.py         → gws CLI → Gmail API
        ├─ global reaction sweep → checkers/reactions.py
        │    └─ mcp-call.sh → Slack MCP API search
        │
        ├─ prune reaction state files at 1000 entries
        │
        ├─ [if all clean] advance all watermarks → exit 0 ($0 cost)
        │
        └─ [if any dirty] spawn Claude worker
              claude --model opus --output-format stream-json \
                --mcp-config worker.mcp.json --strict-mcp-config \
                -p "dispatch prompt + dirty target list"
                  │
                  ├─ reads CLAUDE.md (codebase instructions)
                  ├─ for each dirty quest → Quest Activation Protocol
                  │    ├─ reads context.md (quest brief)
                  │    ├─ reads meta.json/watch.json/timeline.ndjson only if needed
                  │    ├─ queries Slack/Gmail/Coda/Atlassian via MCP tools
                  │    └─ sends messages / creates drafts / logs to timeline.ndjson
                  └─ for "reactions" target → Reactions Fast Path (self-contained)
```

**Core design invariants:**
- Triage owns watermark advancement. Worker never touches `last_checked_ts` on existing entries.
- Worker exit 0 = triage advances dirty-quest watermarks. Worker exit non-zero = watermarks frozen, re-surface next tick.
- Idle ticks cost $0. Token cost only when work is dispatched.
- `yaas-triage/` is fully generic. All personal/install values live in `.env`.

---

## 2. LaunchD jobs

### `~/Library/LaunchAgents/com.yaas.triage.plist`

| Key | Value |
|---|---|
| StartInterval | 60 seconds |
| Program | `yaas-triage/triage.sh` |
| WorkingDirectory | `/Users/<you>/yourself-as-a-service` |
| StandardOutPath | `logs/triage.out.log` |
| StandardErrorPath | `logs/triage.err.log` |
| PATH | `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` |
| RunAtLoad | false |

Rendered from `yaas-triage/setup/com.yaas.triage.plist.template` by `yaas-triage/setup/install-launchd.sh`.

Control commands:
```bash
launchctl load   ~/Library/LaunchAgents/com.yaas.triage.plist   # start
launchctl unload ~/Library/LaunchAgents/com.yaas.triage.plist   # stop
launchctl list com.yaas.triage                                   # status
yaas-triage/setup/install-launchd.sh status                      # also shows plist
```

### `~/Library/LaunchAgents/com.yaas.solution-stage-advance.plist`

Separate daily job (not part of the core triage loop). Runs a solution-proposal stage advancer at 9:00 AM. Optional; safe to skip if not using solution proposals.

---

## 3. Triage loop (triage.sh) — step by step

**File:** `yaas-triage/triage.sh`

### Step 1 — Load .env

```bash
[ -f "$REPO_ROOT/.env" ] && set -a && source "$REPO_ROOT/.env" && set +a || true
```

`set -a` auto-exports every assignment so values are available to subprocesses (including `claude --mcp-config worker.mcp.json` which references `${CODA_API_KEY}` and `${CODA_MCP_PATH}`).

### Step 2 — Single-instance lock

```bash
LOCKFILE="$LOG_DIR/triage.lock"
exec 9>>"$LOCKFILE"
if ! perl -e 'use Fcntl qw(:flock); exit !flock(STDIN, LOCK_EX|LOCK_NB)' 0<&9; then
  log "SKIP — previous triage still running ..."
  exit 0
fi
echo "$$" > "$LOG_DIR/triage.lock.holder"
```

Non-blocking `flock`. If a previous tick is still running (typically because it dispatched a long worker), the new tick logs SKIP and exits 0. OS auto-releases lock on process exit.

**Trace:** `grep "SKIP — previous triage" logs/triage.log`.

### Step 3 — Watermark lag map

```bash
LAG_MAP="{}"
for _lagfile in "$SCRIPT_DIR/checkers/"*.lag; do
  _type=$(basename "$_lagfile" .lag)
  _lag=$(tr -d '[:space:]' < "$_lagfile")
  LAG_MAP=$(printf '%s' "$LAG_MAP" | jq --arg t "$_type" --argjson l "$_lag" '. + {($t): $l}')
done
```

Reads `checkers/<type>.lag` files. Currently only `email.lag` exists (`120`). Email watermarks advance to `NOW - 120` to give Gmail's search index time to catch up before the next tick claims "nothing new."

### Step 4 — Quest discovery + parallel checks

```bash
shopt -s nullglob
QUEST_DIRS=("$QUESTS_DIR"/*/)

# Each quest runs in a background job (max 8 parallel)
check_quest() {
  local watch_count; watch_count=$(jq '.watches // [] | length' "$watch")
  while [ "$i" -lt "$watch_count" ]; do
    entry=$(jq -c ".watches[$i]" "$watch")
    type=$(jq -r '.type // "unknown"' <<< "$entry")
    checker="$SCRIPT_DIR/checkers/$type.py"
    parsed=$(python3 "$checker" "$entry" 2>/dev/null || echo "error|checker failed")
    # → "dirty" on first hit (count > 0 OR error)
  done
}
```

`error` from a checker = dirty (forces dispatch, retries next tick).

**Trace:** `VERBOSE=1 ./yaas-triage/triage.sh` prints per-quest details.

### Step 5 — Global reaction sweep

```bash
python3 "$SCRIPT_DIR/checkers/reactions.py" "$MCP_CALL" "$CUTOFF_DATE" "$REPO_ROOT" "$PENDING_REACTIONS" 2>&1
```

`checkers/reactions.py` searches Slack for `hasmy::<emoji>: after:<cutoff>` (60-day window) for each of `claudeloading`, `writing_hand`, `floppy_disk`. Diffs against state files. New reactions → writes `state/triage/pending_reactions.json`, marks `REACTIONS_DIRTY=1`.

**Trace:** `grep DIRTY_REACTION logs/triage.out.log`.

### Step 6 — Retire stale thread watches

For each quest, read `retire_slack_threads_after_days` from `meta.json` (default from `.env` `YAAS_RETIRE_DEFAULT_DAYS`, falling back to `30`). Drop `slack_thread` watches whose parent `thread_ts` is older than that many days. `0` / `false` / `never` / `null` disables retirement for that quest (use for long-lived partner conversations). Other watch types are never retired here — they have semantic permanence.

### Step 7 — Prune reaction state files + old worker logs

- Reaction state files (`state/*_replied.json`, `*_saved.json`) trimmed to newest 1000 timestamps if over.
- Per-dispatch worker logs (`logs/worker-*.log`, `logs/worker-*.ndjson`) older than `YAAS_LOG_RETAIN_DAYS` (default 14, set in `.env`) are deleted. Set to `0` to disable.
- Append-only logs (`logs/triage.log`, `triage.out.log`, `triage.err.log`) are NOT touched — they're slower-growing and useful for cross-day debugging.

### Step 8 — Idle path

If `DIRTY_COUNT=0` AND `REACTIONS_DIRTY=0`:
- Advance all clean quests' watermarks to `NOW - lag[type]`
- Log `gate_idle` to `state/run-log.ndjson`
- Update `state/triage/last-run.json` counters
- Exit 0 — **$0 token cost**

**Trace:** `tail logs/triage.log | grep IDLE`.

### Step 9 — Worker dispatch

```bash
# Permission mode: YAAS_WORKER_PERMISSION_MODE in repo-root .env (default acceptEdits).
_EXITFILE=$(mktemp)
(
  claude --model opus --permission-mode "${YAAS_WORKER_PERMISSION_MODE:-acceptEdits}" \
    --mcp-config "$SCRIPT_DIR/worker.mcp.json" --strict-mcp-config \
    --tools "Read,Edit,Write,Bash,Glob,Grep,WebFetch,WebSearch" \
    --output-format stream-json --verbose \
    -p "YaaS worker dispatch: dirty targets: $TARGET_LIST..." \
    2>&1 | tee "$WORKER_NDJSON" | python3 format-stream.py >> "$WORKER_LOG"
  echo "${PIPESTATUS[0]}" > "$_EXITFILE"
) &
_BGPID=$!

# Watchdog kills worker after 900s
( sleep 900; kill_tree ... ) &
_WATCHDOG=$!

wait "$_BGPID" 2>/dev/null || true          # ← || true critical (see § 12)
kill "$_WATCHDOG" 2>/dev/null || true
wait "$_WATCHDOG" 2>/dev/null || true       # ← watchdog killed by SIGTERM → 143
EXIT=$(cat "$_EXITFILE" 2>/dev/null); EXIT=${EXIT:-1}
```

Pipeline: `claude → tee (raw ndjson) → format-stream.py (human log)`. PIPESTATUS[0] captures claude's exit code.

### Step 10 — Post-worker

`extract-tokens.py` parses the final `result` event from the worker ndjson and writes a `gate_dispatch_tokens` event to `state/run-log.ndjson` with token counts and `cost_usd`.

Then:
```bash
if [ "$EXIT" = "0" ]; then
  for qid in "${DIRTY_QUESTS[@]}"; do
    jq --arg now "$NOW_TS" --argjson lags "$LAG_MAP" '
      .watches[] |= (.last_checked_ts = (($now | tonumber) - ($lags[.type] // 0) | tostring))
    ' "$watch" > "$TMP" && mv "$TMP" "$watch"
  done
  rm -f "$PENDING_REACTIONS"
else
  log "WORKER FAILURE — watermarks left intact. Next tick will re-surface."
fi
```

**Trace:** `grep "Advanced watermark\|WORKER FAILURE" logs/triage.log`.

---

## 4. Checker plugins

All checkers live in `yaas-triage/checkers/<type>.py`. Interface contract:

**Input:** JSON string (the `watch.json` entry) as `argv[1]`
**Output to stdout:** `count|preview` or `0|` or `error|reason`

| Checker | Calls | Detail |
|---|---|---|
| `slack_thread.py` | `slack_read_thread` via `mcp-call.sh` | New replies since watermark |
| `slack_channel.py` | `slack_read_channel` via `mcp-call.sh` | New top-level messages |
| `slack_dm.py` | `slack_search_public_and_private` via `mcp-call.sh` | New DMs from user_id |
| `schedule.py` | `cron-due.py` (local) | Cron expression fired since watermark |
| `email.py` | `gws gmail users messages list/get` | Gmail query matched + `internalDate > since_ms` |
| `reactions.py` | `slack_search_public_and_private` via `mcp-call.sh` | **Global sweep, not per-entry.** Called once with 4 args by triage.sh. |

**Shared utilities:**
- `slack_utils.py` — `parse_slack_messages()` used by `slack_thread.py` and `slack_channel.py`
- `cron-due.py` — cron evaluator used by `schedule.py`
- `email.lag` — `120` (Gmail index lag in seconds)

**MCP auth path:** `mcp-call.sh` → macOS Keychain (`service=slack-xoxp-token, account=yaas`) → `https://mcp.slack.com/mcp` → JSON-RPC 2.0 over SSE.

---

## 5. Worker dispatch

### What Claude receives

The `-p` prompt (simplified):
```
YaaS worker dispatch: dirty targets: <quest_id>,<quest_id>,reactions.
For each target:
  - quest ID → Quest Activation Protocol in CLAUDE.md
  - "reactions" → Reactions Fast Path (self-contained)
Read ONLY context.md first; read meta.json/watch.json/timeline.ndjson only when needed.
Do NOT modify existing watch.json entries.
ACT SILENTLY (no narration between tool calls).
Emit output contract ONLY if something material happened.
```

### What Claude has access to

**Built-in tools (via `--tools`):** Read, Edit, Write, Bash, Glob, Grep, WebFetch, WebSearch

**MCP servers (`yaas-triage/worker.mcp.json`):**

| Server | Type | Auth | Source |
|---|---|---|---|
| Slack | HTTP (`https://mcp.slack.com/mcp`) | OAuth PKCE | `worker.mcp.json` |
| Coda | Local Node.js (`${CODA_MCP_PATH}`) | `${CODA_API_KEY}` from `.env` | `worker.mcp.json` + `.env` |
| Atlassian | HTTP (`https://mcp.atlassian.com/v1/mcp`) | OAuth | `worker.mcp.json` |

`--strict-mcp-config` means the worker only gets these three, independent of the user's interactive `.mcp.json`.

**Via Bash tool:** `gws` CLI for Gmail / Drive / Docs / Calendar / Sheets.

### Quest Activation Protocol (from CLAUDE.md)

1. **Read `context.md`** — always first, usually sufficient
2. **Read other files only when needed:**
   - `watch.json` — to inspect watermarks (e.g., email `query` for re-fetch)
   - `meta.json` — only before sending (check `allow_send`) or changing status
   - `timeline.ndjson` — only to check whether you already acted
3. **Query the source** (Slack thread, channel, Gmail) for content newer than watermark
4. **Act based on quest context:** reply, draft, log, complete, escalate
5. **Track threads you touched:** append new `slack_thread` entry to `watch.json watches[]` per § 3a
6. **Log to `timeline.ndjson`** every outbound action
7. **Exit 0** = success (triage advances watermarks); exit non-zero = retry next tick

### § 3a — Track what you touched

After sending/drafting a message, append a new entry to `watch.json` `watches[]`:

```json
{
  "type": "slack_thread",
  "channel_id": "C...",
  "thread_ts": "<parent_ts>",
  "last_checked_ts": "<response_ts from send, NOT current epoch>",
  "reason": "tracking reply to ..."
}
```

Using `response_ts` (not `now`) ensures replies posted between the send and the watch.json write are not silently missed.

### Output logging

`format-stream.py` converts stream-json to a human log:

| Claude event | Logged as |
|---|---|
| Text output | printed as-is |
| `tool_use` | `→ ToolName(key=<preview>)` |
| `tool_result` | `← ToolName [ok\|ERR]: <preview>` |
| `result` event | silent (captured by `extract-tokens.py`) |
| `rate_limit_event` | `[rate_limit_event] {...}` |

**Cost extraction:** `extract-tokens.py` parses the final `result` event and appends `gate_dispatch_tokens` to `state/run-log.ndjson` with input/output/cache token counts + `cost_usd` + `wall_sec`.

---

## 6. Quest system — data model and lifecycle

### Folder layout

```
state/quests/
├── active/
│   └── <quest_id>/
│       ├── context.md       ← narrative brief (worker reads this first)
│       ├── meta.json        ← status, priority, allow_send
│       ├── watch.json       ← watermarks (triage.sh owns last_checked_ts)
│       └── timeline.ndjson  ← append-only event log
├── completed/  (created on demand when status → completed)
└── archived/   (created on demand when status → cancelled)
```

### meta.json schema

```json
{
  "id": "quest-xyz-2026-04-28",        // must match folder name
  "title": "Human-readable title",
  "status": "active",                   // active | awaiting_reply | blocked | completed | cancelled
  "created": "2026-04-28T...",
  "priority": "normal",                 // high | normal | low
  "allow_send": false,                  // if true, worker can send directly; otherwise draft only
  "retire_slack_threads_after_days": 7  // optional: drop slack_thread watches whose parent
                                        // thread_ts is older than N days. Omit to use default
                                        // (YAAS_RETIRE_DEFAULT_DAYS, fallback 30).
                                        // false / 0 / "never" / null disables retirement.
}
```

### watch.json schema

```json
{
  "watches": [
    {"type":"slack_thread","channel_id":"C...","thread_ts":"1234.567",
     "last_checked_ts":"1234.567","reason":"why watching"},

    {"type":"slack_channel","channel_id":"C...",
     "last_checked_ts":"1234.567","reason":"..."},

    {"type":"slack_dm","channel_id":"D...","user_id":"U...",
     "last_checked_ts":"1234.567","reason":"..."},

    {"type":"schedule","id":"morning","cron":"30 8 * * *","tz":"Asia/Singapore",
     "last_checked_ts":"1234.567","reason":"Morning brief at 8:30am SGT"},

    {"type":"email","query":"from:foo@bar.com subject:URGENT",
     "last_checked_ts":"1234.567","reason":"..."}
  ]
}
```

**Watermark invariant:** `last_checked_ts` is a Unix epoch float (6 decimal places). Triage.sh is the ONLY process that writes to existing entries. Worker can only APPEND new entries.

**Note:** old quests still carry six dead arrays at the bottom (`threads`, `dm_partners`, `channels`, `schedules`, `emails`, `reactions`) — harmless legacy. New quests created via `yaas-triage/skills/yaas-quest-creation/new-quest.py` only have `watches[]`.

### timeline.ndjson events

`created`, `message_sent`, `draft_posted`, `info_received`, `status_change`, `note`, `blocked`, `email_replied`, `weekly_recap_posted`, `calendar_event_created`.

### Quest lifecycle

```
[yaas-quest-creation skill] → scaffolds 4 files in state/quests/active/
        ↓
[triage checks watches] → dirty → worker dispatched
        ↓
[worker acts] → updates meta.json, appends timeline.ndjson
        ↓
  status = "completed" → mv to state/quests/completed/
  status = "cancelled" → mv to state/quests/archived/
  idle 7+ days awaiting_reply → mark blocked
```

### Creating a quest

The user/worker invokes `yaas-triage/skills/yaas-quest-creation/SKILL.md`, which directs them to call:

```bash
python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '<spec_json>'
```

The script:
- Generates `quest-<slug>-YYYY-MM-DD` (collision-checked across active/completed/archived)
- Injects `last_checked_ts = now` into every watch entry
- Always writes `meta.json id` matching the folder name (prevents drift)
- Validates required fields by watch type before writing anything

---

## 7. Reactions fast path

### How it fires

`checkers/reactions.py` runs once per triage tick (separately from per-quest checks). It searches Slack with `hasmy::<emoji>:` for each of the three monitored emojis in a 60-day window. Diffs against state files. New timestamps → writes `state/triage/pending_reactions.json`, sets `REACTIONS_DIRTY=1`.

### Worker behavior for the `reactions` target

1. Read `state/triage/pending_reactions.json`:
   ```json
   {"claudeloading": ["1234.567", "1235.890"], "writing_hand": ["1236.000"]}
   ```
2. For each `(emoji, msg_ts)`:

| Emoji | Action | State file |
|---|---|---|
| `:claudeloading:` | Reply in-thread via `slack_send_message` | `state/claudeloading_replied.json` |
| `:writing_hand:` | Draft reply via `slack_send_message_draft` (never send). If channel externally restricted, DM the user the draft text + permalink. | `state/writing_hand_replied.json` |
| `:floppy_disk:` | Save context silently to `state/context-memory/` (no reply). | `state/floppy_disk_saved.json` |

3. Append `msg_ts` to the state file (processed set)
4. Exit 0 → triage deletes `pending_reactions.json`

### State file schemas

```json
// claudeloading_replied.json / floppy_disk_saved.json
{"replied_timestamps": ["1234.567", ...]}   // or "saved_timestamps"

// writing_hand_replied.json (includes conscious skips)
{"replied_timestamps": [...], "skipped_notes": {"1236.000": "thread resolved, no reply needed"}}
```

All three are auto-pruned to the newest 1000 entries each tick.

### Context memory (`:floppy_disk:`)

```
state/context-memory/
├── index.json            — master index {slack_ts, channel, permalink, saved_at, files:[...]}
├── people/<name-slug>.md — per-person: Expertise & Role, Key Context (dated bullets + permalink)
└── topics/<topic-slug>.md — per-topic: same structure
```

Rules: append only, never overwrite; kebab-case filenames; cite the Slack permalink on every bullet.

---

## 8. Skills system

Skills are folders at `skills/<name>/`. Each has a `SKILL.md`; some have additional script assets. They are NOT automatically loaded — the worker reads them on demand via `Read skills/<name>/SKILL.md`.

### Worker-tool skills (carry executable scripts)

| Skill | Asset | Purpose |
|---|---|---|
| `yaas-quest-creation` | `new-quest.py` | Deterministic quest scaffolding |
| `yaas-gmail-reply` | `gmail-reply.py` | RFC 2822 threaded Gmail reply (gws doesn't set In-Reply-To natively) |

### Behavior/quality skills (text only)

| Skill | When used |
|---|---|
| `yaas-answering-quality` | Loaded before composing any Slack reply |
| `yaas-ops` | State-file layout, debugging, setup reference |

### Adding your own skills

Personal skills live wherever you choose (commonly `<repo-root>/skills/`, gitignored from the public yaas-v2 repo). The worker reads them via `Read <path>/SKILL.md` whenever a quest's `context.md` says to. Reference them from your customized `CLAUDE.md`.

The four shipped skills cover quest creation, Gmail threaded replies, reply-quality guardrails, and operations reference — everything else is your domain.

---

## 9. Global state files

```
state/
├── triage/
│   ├── last-run.json            — run counters (runs_total, runs_idle, runs_dispatched, last_dispatch_utc)
│   └── pending_reactions.json   — TRANSIENT: created if reactions are new, deleted on worker success
├── run-log.ndjson               — append-only event journal (gate_idle, gate_dispatch, gate_dispatch_tokens, gate_dispatch_success/failure)
├── claudeloading_replied.json   — processed :claudeloading: timestamps (capped at 1000)
├── writing_hand_replied.json    — processed :writing_hand: timestamps + skipped_notes
├── floppy_disk_saved.json       — processed :floppy_disk: timestamps
├── context-memory/              — saved context from :floppy_disk: reactions
│   ├── index.json
│   ├── people/<slug>.md
│   └── topics/<slug>.md
└── quests/
    ├── active/     → current quests
    ├── completed/  → finished quests (created on demand)
    └── archived/   → cancelled quests

logs/
├── triage.log            — human-readable, written by log() function (mirrors stderr)
├── triage.out.log        — launchd stdout capture (slog() messages)
├── triage.err.log        — launchd stderr capture
├── worker-<stamp>.log    — human-readable worker transcript (format-stream.py output)
├── worker-<stamp>.ndjson — raw claude stream-json (metrics source)
├── worker-latest.log     — symlink to most recent
└── worker-latest.ndjson  — symlink to most recent
```

---

## 10. Example quest patterns

Quests are folders under `state/quests/active/`. Some common shapes:

| Pattern | Watch types | Example |
|---|---|---|
| **Channel monitor** | `slack_channel` | Watch a partner channel for new top-level messages; reply or draft per quest rules |
| **Thread follow-up** | `slack_thread` | After you reply somewhere, track the parent thread for the other side's response |
| **DM watcher** | `slack_dm` | Surface new DMs from a specific person |
| **Scheduled DM** | `schedule` | Fire a weekly check-in or daily briefing at a fixed local time |
| **Gmail trigger** | `email` | Auto-respond to emails matching a Gmail search query |
| **Mixed** | any combination | e.g., a weekly recap that fires on schedule AND watches replies to the posted recap |

Create new quests with `python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '<spec>'` — see `yaas-triage/skills/yaas-quest-creation/SKILL.md` for the spec schema.

---

## 11. End-to-end trace: a single dirty tick

**Scenario:** A new message arrives in a watched channel while you're sleeping.

1. **Tick N (idle, pre-message):** channel checker queries Slack, returns 0 new. Quest clean. Watermark advanced. Log: `IDLE — 8 quests checked, 0 dirty`.
2. **Message posts** at `ts = 1778500000.000000`.
3. **Tick N+1:** checker returns `1|"..."`. Quest marked dirty. Log: `DIRTY: <quest-id> — [slack_channel] 1 new — "..."`.
4. **Dispatch:** `claude --model opus ... -p "dirty targets: <quest-id>"`. Worker log symlinked to `logs/worker-latest.log`.
5. **Worker starts:** Reads `CLAUDE.md` → Mode A. Reads `state/quests/active/<quest-id>/context.md`.
6. **Worker queries Slack:** `slack_read_channel`. Sees the message.
7. **Worker decides:** Per `context.md` rules, drafts/sends reply (per `meta.json.allow_send`).
8. **Worker logs to timeline.ndjson:** `{"ts":"...","event":"message_sent","thread_ts":"..."}`.
9. **Worker tracks thread:** Appends a new `slack_thread` entry to `watch.json watches[]` with `last_checked_ts = response_ts` (per § 3a).
10. **Worker exits 0.** Output contract emitted.
11. **Post-worker:** `wait $_BGPID` returns 0. Watchdog killed (SIGTERM → 143, suppressed by `|| true`). `extract-tokens.py` writes cost event.
12. **Watermark advancement:** triage.sh advances all entries in the dirty quest's watch.json to `NOW` (or `NOW - lag` per type).
13. **Tick N+2:** New thread watch checked; 0 new. Quest clean. IDLE.

---

## 12. Known issues and bugs

### Bug 1 — `wait "$_WATCHDOG"` returns 143 → silent `set -e` abort (FIXED)

**Symptom:** Workers complete successfully but watermarks never advance. Same quests re-fire every tick. `triage.log` shows DISPATCH but never "Worker exited with" or "Advanced watermark".

**Cause:** `kill "$_WATCHDOG"` sends SIGTERM. `wait "$_WATCHDOG"` then returns `128+15=143`. With `set -eu` active, that aborts triage.sh before watermark advancement.

**Fix:** `|| true` on all three of `wait $_BGPID`, `kill $_WATCHDOG`, `wait $_WATCHDOG`.

### Bug 2 — Unquoted `.env` values with shell metacharacters (FIXED)

**Symptom:** `LastExitStatus = 512` on launchd job. Manual run shows `.env: line 16: syntax error near unexpected token 'newline'`. Triage silently failed for many ticks.

**Cause:** `.env` had `YAAS_FROM_EMAIL=Name <email@example.com>` — bash interpreted `<` as input redirection at source time.

**Fix:** Quote values containing `<`, `>`, spaces, or other shell metacharacters: `YAAS_FROM_EMAIL="Name <email@example.com>"`.

### Bug 3 — `threads[]` vs `watches[]` mismatch (FIXED)

**Symptom:** Worker appends thread watches that triage never checks.

**Cause:** CLAUDE.md § 3a told the worker to append to `watch.json threads[]`, but `check_quest()` only reads `.watches[]`. The legacy `threads[]` field was a dead drop.

**Fix:** CLAUDE.md § 3a now specifies `watches[]` with `type: "slack_thread"`. Existing dead entries migrated.

### Bug 4 — Mac sleep mid-worker

**Symptom:** `triage.err.log` shows `Exit 143 claude ...`.

**Cause:** macOS sent SIGTERM to the launchd job tree at sleep.

**Behavior:** Expected and handled correctly. On wake, watermarks are intact and the next tick re-surfaces the same quests.

---

## 13. How to verify execution is working

### Is triage running?

```bash
launchctl list com.yaas.triage
# PID = number (running) or - (between ticks)
# LastExitStatus = 0 (clean) or 36608 (143<<8, SIGTERM — handled)
# 512 (= 2<<8) = set -eu aborted — check .env syntax
```

### Recent activity

```bash
tail -20 logs/triage.log                                  # Triage starting + IDLE entries
grep DIRTY logs/triage.log | tail -10                     # detections
grep "Advanced watermark\|WORKER FAILURE" logs/triage.log | tail -10
cat logs/worker-latest.log                                 # last worker transcript
```

### Costs

```bash
grep gate_dispatch_tokens state/run-log.ndjson | tail -5 | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    print(f\"{d['ts']} targets={d['targets']} cost=\${d['cost_usd']:.4f} wall={d['wall_sec']}s in={d['input']:,} cr={d['cache_read']:,}\")
"
```

### Reaction sweep

```bash
grep "DIRTY_REACTION\|REACTIONS_DIRTY" logs/triage.out.log | tail -10
```

### Stuck quest detection

```bash
# All watermarks identical and old → worker may be exiting non-zero
jq '.watches[].last_checked_ts' state/quests/active/<quest_id>/watch.json
```

### Manual runs

```bash
DRY_RUN=1 VERBOSE=1 ./yaas-triage/triage.sh        # check phase only, no dispatch
./yaas-triage/triage.sh                             # full run (manual)
```

---

## 14. Personal vs generic separation

**`yaas-triage/`** — pure generic infrastructure. No personal data, no org names, no hardcoded paths. Drop it on any Mac, fill in `.env`, run `setup/setup.sh`, and it works.

**`.env`** (gitignored) — all per-install values:

| Variable | Used by |
|---|---|
| `SLACK_APP_ID` | `setup.sh` (OAuth + display) |
| `SLACK_CLIENT_ID` | `setup.sh` (OAuth) |
| `SLACK_WORKSPACE_NAME`, `SLACK_WORKSPACE_DOMAIN` | `setup.sh` (display only) |
| `CODA_API_KEY` | `worker.mcp.json` Coda MCP (via `${VAR}` expansion) |
| `CODA_MCP_PATH` | `worker.mcp.json` Coda MCP arg |
| `YAAS_FROM_EMAIL` | `yaas-triage/skills/yaas-gmail-reply/gmail-reply.py` |
| `YAAS_LOG_RETAIN_DAYS` | triage.sh — worker log file retention (default 14, `0`=disabled) |
| `YAAS_RETIRE_DEFAULT_DAYS` | triage.sh — default thread retirement age (default 30) |

**`.env.example`** — template a new user copies and fills in.

**macOS Keychain** — Slack OAuth user token (`service=slack-xoxp-token, account=yaas`). Stored by `setup.sh`, retrieved by `mcp-call.sh`.

**`CLAUDE.md`** — worker behavior + tone + your bot identity. Personal in voice but not in credentials.

**`state/`** — your runtime data: quests, watermarks, reaction history, logs. Gitignored except for `.gitkeep` placeholders.

**`skills/`** — your domain knowledge. Gitignored as personal IP. Other users would author their own.
