# Yourself as a Service — Architecture

A personal Slack/email agent that watches threads, inboxes, schedules, Jira and GitHub,
and dispatches an LLM worker to act. Idle ticks cost nothing; tokens are spent only when
there is real work.

The design problem is not "how does the agent do the work" but **how do you know the work
was actually done**. A headless `claude -p` exits 0 whether it handled everything or
quietly handled nothing, so most of what follows is the machinery that makes progress
*provable* rather than assumed: a per-item acknowledgment ledger, checkers that report
whether they drained their window, spend ceilings, and a dead-man switch that runs
outside the loop it watches.

## Table of Contents

1. [High-level architecture](#1-high-level-architecture)
2. [LaunchD jobs](#2-launchd-jobs)
3. [Triage loop (triage.sh) — step by step](#3-triage-loop-triagesh--step-by-step)
4. [Checker plugins and the result contract](#4-checker-plugins-and-the-result-contract)
5. [Worker dispatch and the ack ledger](#5-worker-dispatch-and-the-ack-ledger)
6. [Quest system — data model and lifecycle](#6-quest-system--data-model-and-lifecycle)
7. [Reactions fast path](#7-reactions-fast-path)
8. [Skills system](#8-skills-system)
9. [Global state files](#9-global-state-files)
10. [Example quest patterns](#10-example-quest-patterns)
11. [End-to-end trace: a single dirty tick](#11-end-to-end-trace-a-single-dirty-tick)
12. [Known issues and bugs](#12-known-issues-and-bugs)
13. [How to verify execution is working](#13-how-to-verify-execution-is-working)
14. [Personal vs generic separation](#14-personal-vs-generic-separation)
15. [Guardrails: what is enforced by code](#15-guardrails-what-is-enforced-by-code)
16. [Test suites](#16-test-suites)

---

## 1. High-level architecture

Two independent launchd jobs. The second exists because a health check inside the first
cannot detect the first being dead.

```
com.yaas.triage — KeepAlive loop, one tick every YAAS_TRIAGE_INTERVAL (60s)
  └─> yaas-triage/triage-loop.sh ──> triage.sh
        ├─ source .env; acquire flock (skip tick if a previous run holds it)
        ├─ stamp tick_started_utc
        ├─ ensure every watch has a watch_id (ensure-watch-ids.py)
        │
        ├─ for each quest in state/quests/active/*/          [3 quests in parallel]
        │    └─ for each entry in watch.json watches[]  → checkers/<type>.py
        │         ├─ slack_thread / slack_channel        → mcp-call.sh → Slack
        │         ├─ slack_dm / slack_mention            → mcp-call.sh → Slack
        │         ├─ email                               → gws CLI → Gmail
        │         ├─ jira                                → jira-call.sh → Jira REST
        │         ├─ github_pr                           → gh CLI
        │         ├─ schedule                            → cron-due.py (local)
        │         └─ approval                            → pending-approvals.json (local)
        │       each returns ONE line of JSON: outcome + count + advance_to + complete
        │       outcomes: clean | dirty | ratelimited | error | misconfig
        │
        ├─ global reaction sweep → checkers/reactions.py → Slack search
        ├─ advance CLEAN quests' watermarks
        ├─ retire stale thread / fired one-shot / completed approval watches
        ├─ prune reaction state, worker logs, ack manifests, checker health
        │
        ├─ [nothing dirty] → gate_idle, exit 0                       ($0)
        ├─ [budget ceiling breached] → gate_budget_exceeded, exit 0   ($0, work held)
        ├─ [Slack needed but unreachable] → gate_slack_down, exit 0   ($0, work held)
        │
        └─ [dirty] ONE INVOCATION PER TARGET, sequentially, each committing only itself
              for target in rotate(dirty_targets)[:YAAS_MAX_DISPATCH_FANOUT]:
                ├─ ack-watch.py open <run_id>   → state/triage/dispatch-<run_id>.json
                ├─ dispatch-agent.sh (claude | codex | cursor)
                │     │  reads CLAUDE.md / AGENTS.md
                │     ├─ Quest Activation Protocol for ONE quest
                │     ├─ acts: reply, draft, queue for review, adopt, log
                │     └─ ack-watch.py ack <run_id> <watch_id> handled|nothing_to_do|blocked
                ├─ watchdog: min(1800s, remaining tick budget)
                └─ commit: advance ONLY watches that were (a) dispatched,
                           (b) acked, and (c) reported complete by their checker
        └─ stamp last_triage_completed_utc  (in the EXIT trap, at true end of tick)

com.yaas.heartbeat — KeepAlive loop, every YAAS_HEARTBEAT_INTERVAL (300s)
  └─> yaas-triage/heartbeat-loop.sh ──> health-monitor.py --notify
        reads state files only. No lock, no triage code, stdlib only.
        alerts on: no completed tick / tick started but never finished /
                   consecutive tick failures / checker past promotion threshold /
                   approval stuck mid-execution / budget or misconfig events
        publishes state/health-status.json
```

**Core design invariants.** These are the load-bearing ones; everything else is detail.

- **Triage owns watermarks.** The worker may only APPEND to `watch.json`, and only via
  `add-watch.py` — a `PreToolUse` hook blocks the raw write.
- **Exit 0 commits nothing by itself.** A watch advances only if it was dispatched this
  run, the worker acked it, and the checker proved it drained its window. Anything else
  keeps its old watermark and re-surfaces.
- **One invocation per target.** A failure in one quest cannot commit another's.
- **A checker error never dispatches.** It holds the watermark and backs off
  exponentially, then promotes to `misconfig`, which is visible and needs a human.
- **A bounded window that came back full does not advance.** No checker may claim
  coverage it cannot prove.
- **Withholding spend never loses work.** Every ceiling and gate skips the dispatch while
  leaving watermarks intact.
- **Idle ticks cost $0.**
- **`yaas-triage/` is generic.** All per-install values live in `.env`.

## 2. LaunchD jobs

### `~/Library/LaunchAgents/com.yaas.triage.plist`

| Key | Value |
|---|---|
| KeepAlive | true (NOT StartInterval — see below) |
| Program | `yaas-triage/triage-loop.sh` |
| WorkingDirectory | `/Users/<you>/yourself-as-a-service` |
| StandardOutPath | `logs/triage.out.log` |
| StandardErrorPath | `logs/triage.err.log` |
| PATH | `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` |
| RunAtLoad | false |

Rendered from `yaas-triage/setup/com.yaas.triage.plist.template` by `yaas-triage/setup/install-launchd.sh`.

**Why KeepAlive and not StartInterval.** On macOS 26.x the interval spawn stops being
delivered (`pended nondemand spawn = interval` that never fires), even after a clean
bootout/bootstrap. That silently killed all dispatch once. `triage-loop.sh` paces
itself at `YAAS_TRIAGE_INTERVAL` (default 60s) instead, and `triage.sh` holds its own
flock so a brief double-spawn during a reload serialises safely.

**Note:** because launchd keeps ONE long-lived `triage-loop.sh` process alive, edits to
`triage-loop.sh` itself do not take effect until the job is restarted
(`launchctl kickstart -k gui/$UID/com.yaas.triage`). Edits to `triage.sh` apply on the
next tick, since the loop re-executes it each iteration.

### `~/Library/LaunchAgents/com.yaas.heartbeat.plist`

The dead-man switch. KeepAlive job running `yaas-triage/heartbeat-loop.sh`, which
calls `health-monitor.py --notify` every `YAAS_HEARTBEAT_INTERVAL` (default 300s).

Deliberately a **separate** job: a health check living inside triage cannot detect
triage being dead, which is exactly what happened twice (a 6.5h crash loop hidden by
`triage-loop.sh`'s `|| true`, and StartInterval silently ceasing to fire). It takes no
lock, executes no triage code, and is stdlib-only, so it cannot be wedged or muted by
the thing it watches. Writes `state/health-status.json`; install with
`yaas-triage/setup/install-launchd-heartbeat.sh install`.

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

### Step 3 — Watermark lag map, and the health snapshot

`checkers/<type>.lag` files give a per-type fallback cursor offset, used only when a
checker supplies no `advance_to` of its own: `email` 120s, `slack_mention` 90s,
`github_pr` 30s, `jira` 15s. A malformed lag file is skipped rather than aborting the
tick.

`state/triage/checker-health.json` is read ONCE here into a variable the parallel
subshells inherit. Reading it per watch would cost 120+ extra process spawns a tick.

### Step 4 — Quest discovery + parallel checks

`ensure-watch-ids.py` first backfills a deterministic `watch_id` on any entry lacking one
(idempotent, atomic). A quest whose `watch.json` cannot be parsed is skipped with a
`gate_quest_unreadable` event rather than taking the tick down.

Then each quest is checked in its own background job, `YAAS_TRIAGE_MAX_PARALLEL` (3) at a
time. That cap is the peak number of concurrent Slack calls and is what trips rate-limit
detection — it was 8 during the 2026-07-24 runaway.

Within a quest, entries are evaluated **non-Slack first**. A rate-limited Slack watch
short-circuits the rest of the quest, so if a Slack watch sat ahead of a `reviewed`
approval or a due `schedule`, the skip would shadow that local dirty signal and the quest
would never dispatch. Ordering local checkers first makes a local signal always win.

Per entry, in order:

1. **Promotion gate.** A watch dispatched `YAAS_UNACKED_PROMOTE` (3) times without ever
   progressing becomes `misconfig` — held, recorded, no longer dispatched.
2. **Backoff gate.** A watch inside its `next_retry_ts` window is skipped as `backoff`.
3. **Run the checker**, parse its JSON result (§ 4).
4. **Route the outcome:**

| Outcome | Dirty? | Watermark | Notes |
|---|---|---|---|
| `clean` | no | advanced | |
| `dirty` | yes | advanced only if acked AND `complete` | |
| `ratelimited` | no | held | transient; retry next tick |
| `error` | **no** | held | `checker-health.py fail` → exponential backoff, 60s doubling to 1h; promoted to `misconfig` after `YAAS_CHECKER_ERROR_PROMOTE` (6) |
| `misconfig` | no | held | permanent, needs a human, `gate_watch_misconfigured` |

A successful check clears any recorded failure, but only spawns a process if that watch
actually had one.

**Trace:** `VERBOSE=1 ./yaas-triage/triage.sh`.

### Step 5 — Global reaction sweep

`checkers/reactions.py` searches Slack for `hasmy::<emoji>:` over a 60-day window for
`claude-intensifies`, `writing_hand`, `floppy_disk` and `incoming_envelope`, diffs against
the processed-set state files, and writes `state/triage/pending_reactions.json` if
anything is new.

### Step 6 — Retirement passes

Three, all append-safe:

- **Stale thread watches.** `slack_thread` entries whose parent `thread_ts` is older than
  the quest's `retire_slack_threads_after_days` (default `YAAS_RETIRE_DEFAULT_DAYS`, 30).
  `0`/`false`/`never`/`null` disables it per quest. Other types are never retired here.
- **Completed approval watches.** `approval` entries whose item is `executed` or
  `cancelled` can never fire again.
- **Fired one-shot schedules.** A `schedule` with `next_fire_ts` and no `cron` fires
  exactly once; without this they accumulated and showed as permanent duplicate open items.

### Step 7 — Pruning

Reaction state files trimmed to the newest 1000 timestamps; worker logs older than
`YAAS_LOG_RETAIN_DAYS` (14); ack manifests older than `YAAS_MANIFEST_RETAIN_DAYS` (7);
checker-health entries older than `YAAS_CHECKER_HEALTH_RETAIN_DAYS` (30). Append-only
`triage.log`/`.out`/`.err` are not touched.

### Step 8 — Idle path

Nothing dirty and no new reactions: log `gate_idle` with the held/misconfigured/truncated
counts, increment `runs_idle`, exit 0. **$0.**

### Step 9 — Gates before spending anything

Three, in order. All three preserve watermarks and pending reactions, so skipping never
loses work.

1. **DRY_RUN** — `gate_dirty_dry_run`, report and exit.
2. **Spend ceilings** (`spend-window.py`, reading `cost_usd` out of the run log):
   `YAAS_MAX_SPEND_1H` (40), `YAAS_MAX_SPEND_24H` (250), `YAAS_MAX_DISPATCH_6H` (250).
   The count ceiling is the fastest tripwire and the only one that works under the
   codex/cursor backends, which report no cost. Breach → `gate_budget_exceeded`, notify,
   exit 0. Fails **open** if the run log is unreadable, so corruption cannot wedge
   dispatch forever.
3. **Slack health** — if any dispatched target needs Slack, one cheap probe first. Down →
   `gate_slack_down`, exit 0.

### Step 10 — Dispatch, one invocation per target

Targets are rotated by a persisted `dispatch_cursor` so a permanently stuck target cannot
occupy the front of every tick and starve the tail (`reactions` is appended last, so it
was the natural victim). Then, up to `YAAS_MAX_DISPATCH_FANOUT` (4) of them, sequentially:

```
ack-watch.py open <run_id> <target> <kind> <items>     # the manifest
  → dispatch-agent.sh "<single-target prompt + run_id>"
      → tee raw ndjson → format-stream.py → human log
  watchdog: min(WORKER_TIMEOUT 1800s, remaining tick budget)
  per-dispatch: Slack MCP infra guard, source-evidence check, token extraction
  → commit_quest <target>   or   commit_reactions
```

Sequential is deliberate: two concurrent workers would append to the same `watch.json` and
`timeline.ndjson`, and fan-out multiplies the blast radius of a runaway. The triage flock
already makes overlapping *ticks* impossible.

A target beyond the fan-out cap, or one whose remaining budget is under
`YAAS_MIN_DISPATCH_SLICE` (300s), or one already dispatched
`YAAS_MAX_TARGET_DISPATCH_PER_HOUR` (25) times this hour, is deferred with an event
(`gate_dispatch_deferred`, `gate_target_breaker_open`) and re-detected next tick.

### Step 11 — Commit

Per target, in `commit_quest`:

1. **Exit code.** Non-zero holds everything — except 124 (our own watchdog kill), where
   already-acked items still commit, because an ack is written only after that item's work
   completed and re-doing it risks a duplicate outbound reply. Exit 9 (our synthetic
   "Slack was needed and down") holds everything, since there the acks are suspect.
2. **Blocked-event guard.** A `{"event":"blocked"}` line in the quest's timeline during
   this dispatch holds the whole quest.
3. **Ack ledger.** `ack-watch.py acked <run_id>` lists what closed as `handled` or
   `nothing_to_do`. A missing or corrupt manifest exits non-zero and is treated as a hard
   hold, not as "acked nothing".
4. **Advance**, for each watch that is in the dirty manifest AND acked AND
   `complete != false`, to the checker's own `advance_to` or else `NOW - lag[type]`.
5. **Record no-progress** for everything else, into `state/triage/unacked-counts.json`.

`commit_reactions` does the same per `(emoji, msg_ts)` pair: unacked and blocked pairs stay
in `pending_reactions.json`, and a pair that has gone `YAAS_UNACKED_PROMOTE` dispatches
without progress is parked into that emoji's `skipped_notes`.

**Trace:** `grep -E "ACK SUMMARY|Advanced|NO ACKS|BACKLOG|BUDGET" logs/triage.log`.

---

## 4. Checker plugins and the result contract

All checkers live in `yaas-triage/checkers/<type>.py`. Adding a watch type means dropping
in a new script; nothing in `triage.sh` needs to change.

**Input:** the `watch.json` entry as a JSON string in `argv[1]`.

**Output:** ONE line of JSON, per `checkers/result.py`:

```json
{"outcome":"dirty","count":3,"preview":"...","advance_to":"1785920000.000000","complete":true}
```

| Field | Meaning |
|---|---|
| `outcome` | `clean` \| `dirty` \| `ratelimited` \| `error` \| `misconfig` |
| `count` | new items found (meaningful for clean/dirty) |
| `preview` | short string for the log line |
| `advance_to` | optional: the newest timestamp this check actually covered. Triage advances to this instead of guessing `now - lag`. |
| `complete` | `false` means the window saturated and older items may be unseen. Triage then refuses to advance that cursor **at all**, even if the worker acked it. |

Those last two fields are the point. Every checker reads a BOUNDED window, and a window
that comes back full does not prove there is nothing older. Before this contract existed,
triage advanced to `now` regardless and those older items were skipped permanently.

The legacy `count|preview` form is still parsed, so an unconverted or third-party checker
keeps working (as `complete=true`, no `advance_to`).

| Checker | Reads via | Window and drain behaviour |
|---|---|---|
| `slack_thread.py` | `slack_read_thread` | Pages 50/request up to 5 pages, stopping when it sees a message at or below the watermark, which proves the gap is covered |
| `slack_channel.py` | `slack_read_channel` | Same |
| `slack_dm.py` | `slack_search_public_and_private` | 50 hits; `complete=false` if saturated |
| `slack_mention.py` | `slack_search_public_and_private` | 50 hits, global (no channel); same |
| `email.py` | `gws gmail users messages list/get` | 50; post-filters on `internalDate`; `advance_to` = newest message |
| `jira.py` | `jira-call.sh` → Jira REST | Pages a JQL set; reports `complete=false` when it hit its page cap |
| `github_pr.py` | `gh search prs` | `limit` (100); `complete=false` when saturated |
| `schedule.py` | `cron-due.py` (local) | Cron or one-shot `next_fire_ts`; always complete |
| `approval.py` | `pending-approvals.json` (local) | `reviewed`/`needs_reply` → dirty; `executing` past its lease → dirty with outcome unknown |
| `reactions.py` | `slack_search_public_and_private` | **Global sweep, not per-entry.** Called once with 4 args. Still on the legacy output path. |

**Shared:** `result.py` (the contract), `slack_utils.py` (`drain()` + the page parser),
`cron-due.py`, and the `*.lag` files.

**Auth paths:** `mcp-call.sh` → macOS Keychain (`service=slack-xoxp-token, account=yaas`)
→ `https://mcp.slack.com/mcp`. `jira-call.sh` → Keychain (`jira-api-token`) → Jira REST.
`gh` and `gws` use their own logins.

---

## 5. Worker dispatch and the ack ledger

### One target per invocation

`dispatch-agent.sh` is a thin, backend-agnostic launcher: it starts the configured CLI
headless, streams its raw event JSONL to stdout, and exits with the agent's exit code.

| `YAAS_AGENT` | Command | Notes |
|---|---|---|
| `claude` (default) | `claude --model $YAAS_CLAUDE_MODEL --permission-mode … --mcp-config worker.mcp.json --strict-mcp-config --tools "Read,Edit,Write,Bash,Glob,Grep,WebFetch,WebSearch" --output-format stream-json -p …` | The only backend that reports `cost_usd` |
| `codex` | `codex exec --json -s workspace-write -c approval_policy=never` | Native Slack plugin disabled so there is exactly one Slack path |
| `cursor` | `cursor-agent -p --output-format stream-json --approve-mcps` | |

### What the prompt contains

```
Yaas worker dispatch: dirty target: <ONE quest_id, or "reactions">.
Exact dirty watches (JSON): [{"item_id":"watch-…","type":"slack_thread"}, …]
  → process EVERY listed watch_id; select it with jq on watch_id, never scan watch.json
ACK LEDGER (REQUIRED): run_id <run_id>. Close every item with exactly one call each:
  python3 yaas-triage/ack-watch.py ack <run_id> <item_id> handled|nothing_to_do|blocked "<note>"
watch.json is not editable: append with yaas-triage/add-watch.py per § 3a (a hook blocks the raw write).
ACT SILENTLY. OUTPUT CONTRACT: emit only if something material happened, under 8 lines.
```

### The ack ledger

The reason this exists: `claude -p` exits 0 whenever the model finishes its output
normally, even if it handled three of five watches and quietly skipped the rest. So the
commit is evidence-based rather than exit-code-based.

`state/triage/dispatch-<run_id>.json`:

```json
{
  "run_id": "run-20260805T091402Z-4711-0",
  "target": "quest-foo-2026-04-28",
  "kind":   "quest",
  "items": [
    {"item_id":"watch-a1b2c3d4e5f6a7b8","type":"slack_thread",
     "status":"pending","note":"","acked_utc":null}
  ]
}
```

`item_id` is a `watch_id` for a quest dispatch and `<emoji>:<msg_ts>` for the reactions
dispatch, so one mechanism covers both.

| Ack status | Meaning | Commits? |
|---|---|---|
| `handled` | acted: replied, drafted, queued for review, adopted, saved state | yes |
| `nothing_to_do` | read the new activity, it correctly needs no action | yes |
| `blocked` | could not finish | no, and counts as no-progress |
| *(unacked)* | never closed | no, and counts as no-progress |

`ack-watch.py` writes crash-atomically: an exclusive lock on a **sidecar** lockfile, then
tmp + fsync + `os.replace` + directory fsync. Locking the manifest inode itself would be
incompatible with replacing that inode, and a half-written manifest would read as
"nothing acked" and silently re-dispatch finished work.

Acking an unknown `run_id` or `item_id` exits non-zero and changes nothing, so a typo is
loud rather than silently accepted as coverage.

**What this does NOT solve.** The worker writes its own acks, so the ledger catches
accidental omission but not a false `nothing_to_do` on a source it never read. Closing that
requires correlating the ack against an observed source read in the worker's own event
stream, which `worker-source-evidence.py` already does for Slack.

### Quest Activation Protocol (from CLAUDE.md)

1. Read `context.md` — always first, usually sufficient.
2. Read `watch.json` / `meta.json` / `timeline.ndjson` only when a decision needs them.
3. Query the source for content newer than the watermark.
4. Act: reply, draft, queue to the approval queue, escalate, log.
5. Track threads touched — `add-watch.py`, never a raw write (§ 15).
6. Log every outbound action to `timeline.ndjson`, via `slack-send.py` for Slack so the
   message body is captured structurally.
7. **Ack every dispatched item.** This, not the exit code, is what commits.

### Output logging and metrics

`format-stream.py` converts the raw event stream to a human transcript
(`→ ToolName(...)` / `← ToolName [ok|ERR]: ...`). `extract-tokens.py` parses the final
`result` event and appends `gate_dispatch_tokens` with token counts, `cost_usd` and
`wall_sec`; for non-Claude backends `translate-stream.py` reports raw token counts and no
cost. Per-dispatch logs are `logs/worker-<stamp>-<target>.{log,ndjson}`, with
`worker-latest.*` symlinked to the invocation in flight.

### Post-run guards, per dispatch

- **Slack MCP infra guard.** If the init event shows the Slack server `failed` or
  `needs-auth` AND this target needed Slack, force exit 9 so nothing commits. Note MCP
  servers connect asynchronously, so `pending` with zero Slack tools at init is the normal
  healthy case — counting init tools would flag every run.
- **Source evidence.** A successful Slack read observed in the worker's stream is what
  allows a previously-blocked quest to be marked recovered. Triage's own curl checkers are
  a different execution path and cannot prove the agent had Slack.

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
     "watch_id":"watch-a1b2c3d4e5f6a7b8","last_checked_ts":"1234.567",
     "watch_mode":"read_only","reason":"why watching"},

    {"type":"slack_channel","channel_id":"C...","watch_id":"watch-…",
     "last_checked_ts":"1234.567","reason":"...",
     "filter_user_ids":["U..."],"filter_keywords":["invoice"]},

    {"type":"slack_dm","channel_id":"D...","user_id":"U...","watch_id":"watch-…","last_checked_ts":"..."},
    {"type":"slack_mention","user_id":"U...","watch_id":"watch-…","last_checked_ts":"..."},

    {"type":"schedule","id":"morning","cron":"30 8 * * *","tz":"Asia/Singapore",
     "watch_id":"watch-…","last_checked_ts":"...","reason":"Morning brief 8:30 SGT"},
    {"type":"schedule","next_fire_ts":"1785920000","watch_id":"watch-…",
     "last_checked_ts":"...","reason":"one-shot: chase the partner reply"},

    {"type":"email","query":"from:foo@bar.com subject:URGENT","watch_id":"watch-…","last_checked_ts":"..."},
    {"type":"jira","jql":"project = MYPROJ AND assignee = currentUser()","watch_id":"watch-…","last_checked_ts":"..."},
    {"type":"github_pr","repo":"owner/repo","limit":100,"watch_id":"watch-…","last_checked_ts":"..."},
    {"type":"approval","approval_id":"appr-…","watch_id":"watch-…","last_checked_ts":"..."}
  ]
}
```

| Field | Notes |
|---|---|
| `watch_id` | `watch-<16 hex>`, deterministic from quest id + index + entry identity. Backfilled by `ensure-watch-ids.py`. This is what the dispatch manifest and the ack ledger key on. |
| `last_checked_ts` | Epoch, usually a 6-decimal float. `approval` writes an integer and `schedule` accepts ISO, so consumers normalise with `float()` rather than relying on the format. |
| `watch_mode` | `read_only` on internal escalation threads: monitor the outcome, do not post again. |
| `filter_user_ids` / `filter_keywords` | Optional narrowing on the Slack thread/channel checkers. Note these do NOT affect saturation detection, which is a statement about the time window covered, not about which messages you care about. |

**Watermark invariant.** Triage is the only writer of an existing entry. The worker may
only append, and only through `add-watch.py`; the raw write is blocked (§ 15).

**Legacy:** old quests may still carry dead top-level arrays (`threads`, `dm_partners`,
`channels`, `schedules`, `emails`, `reactions`). Harmless; nothing reads them.

### timeline.ndjson events

`created`, `message_sent`, `draft_posted`, `info_received`, `status_change`, `note`,
`blocked`, `executed`, `email_replied`, `reply_sent`, `dm_sent`, plus quest-specific ones
(`weekly_recap_posted`, `calendar_event_created`).

Two rules that matter more than the list:

- **The dashboard renders a message only if its event carries `message_text`.** A `note`
  summary shows in the raw timeline but not in the Messages stream or the quest
  conversation. `slack-send.py` sends and logs in one step precisely so the body is
  captured structurally rather than from memory.
- **A non-Slack reply needs its own link fields** so the "open in …" chip can be built:
  `jira` + `jira_comment_id`, `repo` + `pr` + comment id, `gmail_thread_id` + `sent_id`,
  or simply the `html_url` the API returned.

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
   {"claude-intensifies": ["1234.567"], "writing_hand": ["1236.000"]}
   ```
2. For each `(emoji, msg_ts)`:

| Emoji | Action | State file |
|---|---|---|
| `:claude-intensifies:` | Research, reply in-thread | `state/claude_intensifies_replied.json` |
| `:writing_hand:` | Research, save a draft, never send | `state/writing_hand_replied.json` |
| `:floppy_disk:` | Save context silently to `state/context-memory/` | `state/floppy_disk_saved.json` |
| `:incoming_envelope:` | Adopt the message into the quest that watches its channel: append a `slack_thread` watch, log `info_received`. Never replies. | `state/incoming_envelope_adopted.json` |

The first, second and fourth carry a visible three-state lifecycle on the message, driven
via `slack-react.sh` (the Slack MCP surface has no remove-reaction tool): the trigger
emoji is swapped for `:claudeloading:` when work starts, and for `:updatedone:` when it
finishes. Left at `:claudeloading:` if the work could not complete. `:floppy_disk:` is
one-shot with no lifecycle.

3. **Ack each pair** in the ledger with `item_id` = `<emoji>:<msg_ts>` (§ 5).
4. Append the ts to the emoji's processed-set file, and exit 0.

**Commit is per pair, not all-or-nothing.** Exit 0 used to delete the whole pending file,
burying every reaction the worker had skipped. Now unacked and `blocked` pairs are retained
for the next tick, and a pair that goes `YAAS_UNACKED_PROMOTE` dispatches without progress
is parked into that emoji's `skipped_notes` so it stops re-dispatching and is visible in
state.

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
│   ├── last-run.json              tick_started_utc, last_triage_completed_utc, run counters,
│   │                              quests_*/watches_* counts, dispatch_cursor
│   ├── pending_reactions.json      TRANSIENT: unhandled reactions; pruned per-pair on ack
│   ├── dispatch-<run_id>.json      ack manifest, one per invocation (§ 5)
│   ├── dispatch-<run_id>.lock      sidecar lock for the above
│   ├── unacked-counts.json         per (scope|item) dispatches that made NO progress
│   ├── checker-health.json         per-watch consecutive_errors + next_retry_ts
│   ├── checker-health.lock         sidecar lock for the above
│   ├── consecutive-tick-failures   written by triage-loop.sh, read by the monitor
│   └── health-alerts.json          notification dedup for health-monitor.py
├── run-log.ndjson                  append-only event journal (see below)
├── health-status.json              the monitor's published verdict
├── pending-approvals.json          the human review queue
├── dashboard-token                 loopback dashboard auth cookie
├── claude_intensifies_replied.json processed reaction sets, capped at 1000 each
├── writing_hand_replied.json       + skipped_notes for conscious skips / parked pairs
├── floppy_disk_saved.json
├── incoming_envelope_adopted.json
├── context-memory/                 saved context from :floppy_disk:
│   ├── index.json
│   ├── people/<slug>.md
│   └── topics/<slug>.md
└── quests/
    ├── active/      current quests
    ├── completed/   status → completed
    └── archived/    status → cancelled

logs/
├── triage.log                      human-readable, mirrors stderr
├── triage.out.log / .err.log       launchd captures
├── heartbeat.out.log / .err.log    the monitor job's captures
├── worker-<stamp>-<target>.log     per-dispatch human transcript
├── worker-<stamp>-<target>.ndjson  per-dispatch raw event stream (metrics source)
└── worker-latest.{log,ndjson}      symlinks to the invocation in flight
```

**run-log.ndjson event vocabulary.** This is the machine-readable history the dashboard,
the spend gate and the health monitor all read.

| Event | Meaning |
|---|---|
| `gate_idle` / `gate_idle_no_quests` | nothing to do |
| `gate_skip_locked` | a previous tick still held the lock |
| `gate_dispatch` | the tick's whole dirty manifest |
| `gate_dispatch_tokens` | per-invocation tokens + `cost_usd` + `wall_sec` |
| `gate_dispatch_success` / `gate_dispatch_failure` | per-target verdict |
| `gate_dispatch_deferred` | past the fan-out cap or out of budget slice |
| `gate_dispatch_unacked` | worker exited 0 without closing anything |
| `gate_ack_manifest_failed` / `_unreadable` | could not open or read a manifest |
| `gate_quest_blocked` | the quest logged a blocked event this dispatch |
| `gate_watch_backlog` | a saturated window; cursor held |
| `gate_watch_misconfigured` | permanent watch fault, needs a human |
| `gate_quest_unreadable` | invalid `watch.json` |
| `gate_budget_exceeded` | a spend or count ceiling was hit |
| `gate_target_breaker_open` | one target dispatched too often this hour |
| `gate_slack_down` | pre-dispatch Slack probe failed |
| `gate_reactions_partial` | some reactions unacked, retained |
| `manual_dispatch` / `manual_dispatch_done` | dashboard-initiated run |
| `worker_stopped` | dashboard stop button |

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

**Scenario:** a message arrives in a watched channel while you are asleep, and the quest
also has a second watch that is rate-limited.

1. **Tick N.** Both watches check clean. Watermarks advance to each checker's `advance_to`.
   `gate_idle`. $0.
2. **The message posts** at `ts = 1778500000.000000`.
3. **Tick N+1.** `slack_channel.py` pages the channel, sees one message newer than the
   watermark and one older (which proves the gap is covered), and returns
   `{"outcome":"dirty","count":1,"advance_to":"1778500000.000000","complete":true}`. The
   second watch returns `ratelimited` and is held. The quest is dirty.
4. **Gates.** Under every spend ceiling; Slack probe answers. Proceed.
5. **Manifest.** `ack-watch.py open` writes `dispatch-<run_id>.json` listing exactly the
   one dirty `watch_id`. The rate-limited watch is NOT in it.
6. **Dispatch.** One invocation, this quest only. `worker-latest.log` points at it.
7. **Worker.** Reads `CLAUDE.md` → Mode A → this quest's `context.md`. Selects the named
   `watch_id` from `watch.json` by id. Reads the channel.
8. **Acts.** Per `context.md` and `meta.json.allow_send`: either sends via
   `slack-send.py` (which logs the body + permalink + `response_ts` in one step), or
   queues to the approval queue via `approval-helper.py write` (which arms the tracking
   `approval` watch in the same call).
9. **Tracks the thread.** `add-watch.py` appends a `slack_thread` watch with
   `last_checked_ts` = its own `response_ts`. The raw write would have been refused.
10. **Acks.** `ack-watch.py ack <run_id> <watch_id> handled "replied in thread"`.
11. **Exits 0.**
12. **Guards.** Slack MCP status was healthy; a successful Slack read is observed in the
    event stream; `extract-tokens.py` records `gate_dispatch_tokens` with `cost_usd`.
13. **Commit.** The ledger reports one item `handled`. That watch was dispatched, acked and
    `complete`, so its cursor advances to `1778500000.000000` — the checker's own figure,
    not `now`. The rate-limited watch keeps its old watermark. The newly appended thread
    watch keeps the `response_ts` it was created with.
14. **End of tick.** `_on_exit` stamps `last_triage_completed_utc`.
15. **Within 5 minutes,** the heartbeat job reads that stamp, finds it fresh, and stays
    silent.
16. **Tick N+2.** The new thread watch checks clean. The rate-limited watch retries. Idle.

**The counterfactual.** Under the pre-2026-08 design, step 13 advanced *every* watch in the
quest on the strength of the exit code alone — including the rate-limited one, whose window
had never been read. That is the silent data loss this architecture is built to prevent.

---

## 12. Known issues and bugs

The four bugs previously listed here were all fixed or expected-and-handled months ago
and described none of the current failure modes. Current state:

### Open

| Issue | Shape | Status |
|---|---|---|
| Approval stuck in `executing` | If a worker dies between `approval-helper.py start` and `done`, the item is invisible on the dashboard (which allowlists two statuses), clean to `approval.py`, and keeps its watch. A human-approved message can be lost with no surface. | Item 7. `health-monitor.py` now ALERTS on it (`approval_stuck`), so it is no longer silent, but nothing re-drives it yet. |
| Ack ledger is self-attestation | The worker writes its own acks, so the ledger catches accidental omission but not a false `nothing_to_do` on a message it never read. | Fix is to correlate an ack against an observed source read in the worker event stream, which `worker-source-evidence.py` already does for Slack. |
| `reactions.py` bounds unreported | Still on the legacy `count\|preview` path, with `limit: 20` per page and a 30-page stop, neither surfaced to triage. The last place a bounded window can silently truncate. | |
| `jira.py` / `github_pr.py` do not drain | They report `complete` from their existing limit checks, so a truncated result correctly holds the cursor, but they do not page to resolve it. | |
| Invariants only in prose | No `PreToolUse` hook guards `watch.json` or `pending-approvals.json`; the `SubagentStop` hook still targets `yaas-worker`, an agent this architecture removed. | Item 9. |

### Recurring, and how it now surfaces

| Symptom | What it means | Where it shows |
|---|---|---|
| `Exit 143` in `triage.err.log` | macOS sent SIGTERM to the job tree at sleep. Expected: watermarks are intact and the next tick re-surfaces the work. | Handled, no action. |
| Bursts of `error — ... exit 6` | curl could not resolve host, i.e. a network blip. Every Slack checker errors at once, and the pre-dispatch Slack health gate also fails, so the tick logs `gate_slack_down` and skips. | `checker-health.json`, cleared on the next successful check. |
| Nothing dispatches despite dirty quests | Most likely a spend ceiling. Then: the Slack health gate, the fan-out cap, or a target breaker. | `gate_budget_exceeded` / `gate_slack_down` / `gate_dispatch_deferred` / `gate_target_breaker_open` in `run-log.ndjson`, and a desktop notification. |
| A quest silently stops being checked | A watch was promoted to `misconfig`, either by an unknown type, repeated checker errors, or repeated unacked dispatches. | `misconfigured` in the dashboard stat strip, `gate_watch_misconfigured`, and a notification. |

## 13. How to verify execution is working

### First: ask the monitor

```bash
python3 yaas-triage/health-monitor.py --json     # exit 0 = healthy, 1 = problems
cat state/health-status.json                      # same verdict, as published
```

It checks: has a tick completed recently, did a tick start and never finish, how many
consecutive tick failures, any checker past its promotion threshold, any approval stuck
mid-execution, any budget/misconfig/backlog/breaker event in the last hour. If it says
healthy, the loop is alive and nothing is silently held.

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

**`.env`** (gitignored) — all per-install values.

**`.env.example` is the canonical list.** It carries every variable with its default
and the reasoning behind it, and it is verified against the code by a coverage check
(every `${YAAS_*}` the scripts read must appear there, and nothing unused may). This
table used to duplicate that list and fell five variables behind within weeks, so it
now only names the groups:

| Group | Examples | Notes |
|---|---|---|
| Slack app | `SLACK_APP_ID`, `SLACK_CLIENT_ID`, `SLACK_WORKSPACE_*` | Public PKCE identifiers, workspace-specific. The token itself lives in the Keychain, never in `.env`. |
| Integrations | `JIRA_*`, `CODA_API_KEY`, `CODA_MCP_PATH`, `YAAS_FROM_EMAIL` | Optional; blank disables. |
| Spend ceilings | `YAAS_MAX_SPEND_1H`, `YAAS_MAX_SPEND_24H`, `YAAS_MAX_DISPATCH_6H`, `YAAS_MAX_TARGET_DISPATCH_PER_HOUR` | The circuit breaker. Breakers, not throttles: set well above your normal rate. |
| Dispatch shape | `YAAS_MAX_DISPATCH_FANOUT`, `YAAS_TICK_DISPATCH_BUDGET`, `YAAS_MIN_DISPATCH_SLICE`, `YAAS_UNACKED_PROMOTE` | Per-target fan-out and the ack-ledger promotion threshold. |
| Backend | `YAAS_AGENT`, `YAAS_{CLAUDE,CODEX,CURSOR}_MODEL`, `YAAS_{CLAUDE,CODEX}_PERMISSION_MODE` | Which CLI runs the worker, and how sandboxed. |
| Loop and checkers | `YAAS_TRIAGE_INTERVAL`, `YAAS_TRIAGE_MAX_PARALLEL`, `YAAS_CHECKER_ERROR_PROMOTE` | `MAX_PARALLEL` is the peak concurrent Slack call count; raising it caused a runaway once. |
| Retention | `YAAS_LOG_RETAIN_DAYS`, `YAAS_RETIRE_DEFAULT_DAYS`, `YAAS_MANIFEST_RETAIN_DAYS`, `YAAS_CHECKER_HEALTH_RETAIN_DAYS` | |

Read your own spend numbers before setting the ceilings:
`python3 yaas-triage/spend-window.py state/run-log.ndjson | jq .`

**`.env.example`** — template a new user copies and fills in.

**macOS Keychain** — Slack OAuth user token (`service=slack-xoxp-token, account=yaas`). Stored by `setup.sh`, retrieved by `mcp-call.sh`.

**`CLAUDE.md`** — worker behavior + tone + your bot identity. Personal in voice but not in credentials.

**`state/`** — your runtime data: quests, watermarks, reaction history, logs. Gitignored except for `.gitkeep` placeholders.

**`skills/`** — your domain knowledge. Gitignored as personal IP. Other users would author their own.

---

## 15. Guardrails: what is enforced by code

The recurring lesson from every incident in § 12 is that a rule living only in `CLAUDE.md`
is a rule the model may or may not follow. These are the ones that no longer depend on it.

| Invariant | Enforced by | What used to happen |
|---|---|---|
| `watch.json` is append-only | `PreToolUse` hook (`.claude/hooks/deny-state-writes.sh`) blocks Edit/Write/redirect/`tee`/`mv`/`sed -i`; `add-watch.py` is the only path | Appends went to a `threads[]` key no checker read, so those watches were never checked |
| An approval always has a watch | Same hook on `pending-approvals.json`; `approval-helper.py write` arms the watch in the same call | A hand-written item stranded in `needs_reply`, invisible to triage forever |
| A dispatched item is only committed if handled | `ack-watch.py` + `commit_quest` | Exit 0 committed everything the tick had named |
| A cursor never passes an undrained window | `complete` in the checker contract | A full page read as "nothing older exists" |
| A checker error never costs money | `checker-health.py` backoff → `misconfig` | A repeatable failure woke a paid worker every 60s |
| Spend is bounded | `spend-window.py` + three ceilings + a per-target breaker | A runaway loop ran >$1k over 13.5h |
| A dead loop is noticed | `com.yaas.heartbeat` + honest tick stamps | A crash loop ran 6.5h while launchd reported health |
| An approval cannot be lost mid-send | `lease_expires_at` + reconcile-not-resend | A worker dying between `start` and `done` stranded it silently |

The hook has two deliberate limits, both documented in it. It is not airtight — a
determined agent with Bash can find a path; the job is to turn silent corruption into a
loud refusal that names the right tool. And for Bash it matches command *text*, so a
command that merely quotes such a write as data is refused too, which is why its own tests
live in a file rather than inline.

`add-watch.py` deliberately does NOT default `last_checked_ts` for you. That value is
judgment: it must be the `response_ts` of your own reply, because "now" silently swallows a
reply that lands between the send and the write.

---

## 16. Test suites

Nine suites, all runnable directly, no fixtures in the repo. Each provokes real failure
modes against a temp tree rather than asserting on mocks.

| Suite | Covers |
|---|---|
| `test-dirty-watch-dispatch.sh` | per-target dispatch, partial-ack commit, exit-code isolation between targets, acked-`blocked` as no-progress, reactions partial commit, `advance_to`, the saturation hold |
| `test-checker-contract.sh` | every checker emits one parseable line with an in-enum outcome and a `complete` field, on both a plausible and a nonsense entry; a `reason` naming an internal exception fails, because that can only mean the checker's own code is broken |
| `test-budget-gate.sh` | window arithmetic, cap precedence, and end-to-end proof that a breach withholds the dispatch, logs the event, and **holds the watermark** |
| `test-health-monitor.sh` | all seven conditions, the healthy case, and notification dedup three ways |
| `test-approval-lease.sh` | the full approval lifecycle through an expired lease and reconciliation |
| `test-add-watch.sh` | append-only against a pre-existing entry, idempotency, six validation failures |
| `test-state-write-hook.sh` | every write path blocked, every read and helper allowed, scratch fixtures allowed, no hook referencing the removed `yaas-worker` subagent |
| `test-watch-ids.sh` | deterministic id assignment and migration |
| `test-notify.sh` | desktop notification watermarking and caps |

```bash
for t in yaas-triage/test-*.sh; do printf '%-34s ' "$t"; bash "$t" >/dev/null 2>&1 && echo PASS || echo FAIL; done
```

Several of these were written after a negative control showed the obvious assertion would
have passed on broken code — `test-checker-contract.sh` exists because a module-shadowing
bug produced perfectly well-formed output and only the `reason` field gave it away. When
adding a suite, prove it fails when the mechanism is reverted.

---
