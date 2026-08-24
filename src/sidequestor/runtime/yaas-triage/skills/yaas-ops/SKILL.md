---
name: yaas-ops
description: Operating and extending the yaas triage system — adding new channel types, understanding the checker plugin architecture, state file layout, setup, and migration runbook. Load when asked to add a new watcher, debug triage, or understand how the system is wired.
---

# yaas-ops

Operational reference for the Yourself-as-a-Service triage system. For the
full architecture, see `ARCHITECTURE.md` at the repo root.

---

## System layout

```
yaas-triage/
├── tick.py                   ← launchd-driven main loop, one tick (every ~60s) — THE ORCHESTRATOR
│   ├── tick_state.py         ←   config/loading: repo root, knobs, lag map, active quests
│   ├── tick_check.py         ←   classify(): the six-way per-watch verdict
│   └── tick_dispatch.py      ←   the dispatch gates: slack-need, budget/fanout/slice
├── triage-loop.sh            ← the launchd wrapper that sleeps between ticks (runs tick.py)
├── checkers/                 ← one script per watch type (plugin directory)
│   ├── slack_thread.py · slack_channel.py · slack_dm.py · slack_mention.py
│   ├── schedule.py · email.py
│   ├── jira.py               ← Jira issues via ../surfaces/jira-call.sh (no MCP needed)
│   ├── github_pr.py          ← PR activity via `gh search prs`
│   ├── github_issue.py       ← issue activity via `gh search issues` (excludes PRs)
│   ├── reactions.py          ← global reaction sweep (not per-quest)
│   ├── result.py             ← the outcome contract every checker returns
│   ├── slack_utils.py        ← shared drain()/parse helpers
│   ├── cron-due.py           ← cron evaluation logic (used by schedule.py)
│   └── *.lag                 ← per-type watermark lag in seconds
├── dispatch/                 ← "run a worker": run-agent.py, plan.py, spend-window.py,
│                               slack-read-health.py, extract/translate-stream.py, worker.mcp.json
├── ledger/                   ← "owns a state file, atomically": ack-watch.py, commit.py,
│                               housekeep.py, checker-health.py, watch-guard.py, add-watch.py,
│                               approval-helper.py, ensure-watch-ids.py
├── surfaces/                 ← "talk to the outside": mcp-call.sh, jira-call.sh,
│                               slack-send.py, slack-react.sh, react-lifecycle.py
├── ops/                      ← "keep it alive and visible": dashboard-server.py, health-monitor.py,
│                               rotate-logs.py, notify.py, doctor.sh, sync-yaas-v2.sh
├── tests/                    ← unit/ + behaviour/ + differential/ (goldens + mutations)
├── setup/                    ← one-time install (setup.sh, install-launchd*.sh, plist templates)
└── skills/                   ← generic worker skills (loaded on demand)
    ├── yaas-quest-creation/  ← scaffolds new quest folders (new-quest.py)
    ├── yaas-gmail-reply/     ← threaded Gmail reply utility (gmail-reply.py)
    ├── yaas-answering-quality/ ← bot reply quality rules
    └── yaas-ops/             ← this file

state/
├── triage/
│   ├── last-run.json         ← triage run counters + last completion time
│   ├── worker-current.json   ← atomic worker lifecycle + heartbeat for the dashboard
│   └── pending_reactions.json  ← transient: reactions awaiting worker (deleted on success)
├── quests/
│   ├── active/               ← ongoing quests (4-file folders)
│   ├── completed/            ← finished quests (kept for audit)
│   └── archived/             ← cancelled or stale
├── run-log.ndjson            ← append-only log of every triage tick + dispatch
├── claude_intensifies_replied.json ← processed `process` reaction timestamps
├── writing_hand_replied.json  ← processed `draft` reactions + skipped notes
├── floppy_disk_saved.json     ← processed `save` reaction timestamps
└── context-memory/           ← `save` reaction context (people/, topics/)

logs/
├── triage.log                ← human-readable triage run log
├── triage.out.log · triage.err.log  ← launchd stdout/stderr
├── worker-latest.log         ← symlink to most recent worker run (human-readable)
├── worker-latest.ndjson      ← symlink to most recent worker run (raw stream-json)
└── worker-<stamp>.{log,ndjson}  ← per-run worker logs (pruned after YAAS_LOG_RETAIN_DAYS)

.env                          ← per-install secrets + knobs (gitignored)
.env.example                  ← template for new installs
CLAUDE.md / AGENTS.md          ← optional user-owned worker instructions (never written by Sidequestor)
ARCHITECTURE.md               ← full system design
```

---

## Checker plugin architecture

### How it works

The orchestrator's `check_quest` (`tick.py`, ported from the original shell orchestrator) iterates over every entry in a quest's `watches[]` array. For each entry it looks up `yaas-triage/checkers/<type>.py` and runs it:

```
python3 checkers/<type>.py '<watch_entry_json>'
```

The checker outputs one line to stdout:

| Output | Meaning |
|---|---|
| `0\|` | Nothing new — clean |
| `N\|<preview>` | N new items — quest is dirty, dispatch worker |
| `error\|<reason>` | Failure (network, API error, malformed response) — treat as dirty so worker retries |

For Slack checkers specifically: a non-JSON response is treated as `error|` and surfaces in the log, except for the carve-outs `thread_not_found` / `channel_not_found` (permanent conditions) which return `0|`.

Environment variables available to all checkers (exported by the orchestrator after merging `.env`):
- `MCP_CALL` — path to `mcp-call.sh`
- `GWS_BIN` — path to `gws` CLI

### The reactions sweep

`checkers/reactions.py` is special — it doesn't follow the per-entry interface. It runs once per triage tick (called directly by the orchestrator with 4 positional args) and sweeps Slack globally for the configured process, draft, save, and adopt reactions across a 60-day window. `.env.example` lists their defaults and override variables. The configured names still map to fixed semantic state files under `state/`. New reactions → writes `state/triage/pending_reactions.json`.

### Watermark lag

If a channel type has indexing latency (e.g. Gmail's search index takes ~60s to reflect new mail), create a companion file `checkers/<type>.lag` containing the lag in integer seconds. the orchestrator reads these at startup and subtracts the lag when advancing watermarks, so clean ticks never claim "I've seen everything up to now" when the source hasn't indexed it yet.

Currently: `email.lag = 120` (Gmail's index is slow), `slack_mention.lag = 90`, `github_pr.lag = 30` (GitHub's search index is eventually consistent), `jira.lag = 15`. Types with no `.lag` file get lag = 0.

Keep the lag as small as the source allows. Every second of lag widens the window in which an already-seen change gets re-reported, and each re-report costs a full dispatch that finds nothing. Note `*.lag` is gitignored, so a fresh clone starts at lag 0 for every type; recreate them from the values above.

### watch.json schema

```json
{
  "watches": [
    {"type": "slack_thread",  "channel_id": "C...", "thread_ts": "1234.567", "last_checked_ts": "0", "reason": "..."},
    {"type": "slack_channel", "channel_id": "C...", "last_checked_ts": "0", "reason": "..."},
    {"type": "slack_dm",      "user_id": "U...",    "last_checked_ts": "0", "reason": "..."},
    {"type": "slack_mention", "user_id": "U...",    "last_checked_ts": "0", "reason": "..."},
    {"type": "schedule",      "cron": "0 9 * * 1", "tz": "Asia/Singapore", "last_checked_ts": "0", "reason": "..."},
    {"type": "email",         "query": "from:partner@company.com subject:Re:", "last_checked_ts": "0", "reason": "..."},
    {"type": "jira",          "jql": "labels=my-label", "last_checked_ts": "0", "reason": "..."},
    {"type": "github_pr",     "repo": "owner/repo", "last_checked_ts": "0", "reason": "..."}
    {"type": "github_issue",  "repo": "owner/repo", "last_checked_ts": "0", "reason": "..."}
  ]
}
```

`jira` needs the REST bridge (`yaas-triage/surfaces/jira-call.sh`, Basic-auth API token in Keychain `jira-api-token`/`yaas`) because the Atlassian MCP is interactive-OAuth only and is absent in headless dispatch. `github_pr` and `github_issue` both accept optional `search` (extra GitHub qualifiers), `limit` (default 100), and `gh_account` (a `gh` login whose token to use, for a repo the ACTIVE `gh` account cannot see; resolved per-run via `gh auth token -u`, never persisted). Read the warning in `checkers/github_pr.py`'s docstring before adding a `search`, since repeated qualifiers AND rather than OR and can silently match nothing. A `search` may contain qualifiers only: anything starting with `-` is refused as `misconfig`, because the string is spliced into argv ahead of gh's own flags and e.g. `--include-prs` would make a `github_issue` watch double-report every PR against a `github_pr` watch on the same repo.

`.lag` note: `github_issue.lag = 30`, same as `github_pr` and for the same reason (GitHub's search index is eventually consistent).

`last_checked_ts` is always a Unix epoch float string. The orchestrator is the sole owner of this field — the worker must never modify existing entries (it may only append new ones).

Old quests may carry six dead arrays at the bottom (`threads`, `dm_partners`, `channels`, `schedules`, `emails`, `reactions`) — harmless legacy. New quests created via `new-quest.py` only have `watches[]`.

### meta.json schema

```json
{
  "id": "quest-...",                           // must match folder name
  "title": "...",
  "status": "active",                          // active | awaiting_reply | blocked | completed | cancelled
  "created": "2026-...T...Z",
  "priority": "normal",                        // high | normal | low
  "allow_send": false,                         // true = worker can send directly
  "retire_slack_threads_after_days": 14        // optional; int / false / null — see ARCHITECTURE.md §6
}
```

---

## Adding a new channel type

Example: adding Telegram.

**1. Store credentials in keychain (once):**
```bash
security add-generic-password -s telegram-bot-token -a yaas -w "<BOT_TOKEN>"
```

**2. Create `yaas-triage/checkers/telegram_chat.py`:**
```python
#!/usr/bin/env python3
# Input:  watch entry JSON as argv[1]
#         {"type":"telegram_chat","chat_id":"-100...","last_checked_ts":"..."}
# Output: count|preview  or  error|reason
import sys, os, json, subprocess

def main():
    entry = json.loads(sys.argv[1])
    chat_id = str(entry["chat_id"])
    since_ts = float(entry.get("last_checked_ts", "0"))

    token = subprocess.run(
        ["security", "find-generic-password", "-s", "telegram-bot-token", "-a", "yaas", "-w"],
        capture_output=True, text=True
    ).stdout.strip()
    if not token:
        print("error|no telegram-bot-token in keychain"); return

    r = subprocess.run(
        ["curl", "-sS", f"https://api.telegram.org/bot{token}/getUpdates?limit=20"],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"error|telegram API call failed (exit {r.returncode})"); return

    data = json.loads(r.stdout)
    if not data.get("ok"):
        print(f"error|telegram API: {data.get('description','unknown')}"); return

    new_msgs = [
        u["message"] for u in data["result"]
        if "message" in u
        and str(u["message"]["chat"]["id"]) == chat_id
        and u["message"]["date"] > since_ts
    ]
    if not new_msgs:
        print("0|"); return

    preview = new_msgs[0].get("text", "")[:80]
    print(f"{len(new_msgs)}|{preview}")

if __name__ == "__main__":
    try: main()
    except Exception as e: print(f"error|{e}")
```

**3. Add entries to a quest's `watch.json`:**
```json
{"type": "telegram_chat", "chat_id": "-1001234567890",
 "last_checked_ts": "0", "reason": "watch partner group chat"}
```

**4. (Optional) Add `yaas-triage/checkers/telegram_chat.lag`** with integer seconds for indexing lag (Telegram is real-time, so no lag file needed).

**5. Document in the quest's `context.md`** how the worker should fetch full message content and reply (typically via `curl` to the Telegram Bot API).

That's the entire change. The orchestrator requires no modification — it autodiscovers the new checker by filename.

---

## Worker utilities

### `yaas-triage/ledger/approval-helper.py`

Besides ordinary review drafts, the approval ledger carries dashboard-submitted manual quest
instructions. `enqueue-instruction <json>` durably records one distinct reviewed item without
touching `watch.json`; `tick.py` calls `arm-pending-instructions` under the global triage lock on
the next cycle. Workers claim with `start`, close successful work with `done`, and use
`abandon <id> <reason>` only when an expired manual-instruction lease makes the outcome uncertain.

### `yaas-triage/skills/yaas-gmail-reply/gmail-reply.py`

Builds a threaded Gmail reply (RFC 2822 `In-Reply-To` + `References` headers) and sends via `gws gmail users messages send`. Used by quests that watch email and need to reply in-thread.

```bash
GWS_BIN="${GWS_BIN:-$(command -v gws)}" \
  python3 yaas-triage/skills/yaas-gmail-reply/gmail-reply.py <gmail_message_id> --body "<reply text>"
# Prints sent message ID on success, exits 1 on failure.
```

Reads `YAAS_FROM_EMAIL` from env for the From header. See `yaas-triage/skills/yaas-gmail-reply/SKILL.md`.

### `yaas-triage/skills/yaas-quest-creation/new-quest.py`

Deterministic quest scaffolding. Takes a JSON spec on argv or stdin, creates the four-file folder under `state/quests/active/`, validates fields, injects `last_checked_ts` so the worker can never forget it. See `yaas-triage/skills/yaas-quest-creation/SKILL.md`.

---

## Setup (first time or new colleague)

```bash
git clone <this repo>
cd <repo>
cp .env.example .env             # configure the adapters you use
./yaas-triage/setup/setup.sh
./yaas-triage/ops/doctor.sh          # is this machine configured (real creds, PATH, plist)
python3 yaas-triage/ops/health-monitor.py   # is it working right now
yaas-triage/tests/run-all.sh     # is the code correct (fixtures, safe any time)
```

With local Slack checking enabled, `setup.sh` walks through Slack OAuth (PKCE flow, no client secret needed), stores the complete rotating user-token bundle
in macOS Keychain at `(service=slack-oauth-token-bundle, account=yaas)`, runs a connectivity check,
and offers the triage, independent heartbeat, and dashboard launchd jobs. It can also initialize
opt-in daily template tracking and run verification. Each user creates their own Slack app at
https://api.slack.com/apps from the manifest printed by `setup.sh --manifest`; the manifest and
OAuth flow share `setup/yaas-app-config.json`, so their scopes cannot drift.

The deterministic Slack adapter refreshes within five minutes of access-token expiry. It also
refreshes and retries once after a definitive token rejection. `python3 yaas-triage/surfaces/slack_credentials.py status`
reports redacted credential health; `refresh-now` performs an immediate manual rotation.

Set `YAAS_SLACK_CHECKERS_ENABLED=0` when no local Slack app is available. Setup and doctor then
treat Slack app fields and the Keychain token as intentionally absent. The tick skips all
`slack_*` checker entries and the global reaction sweep without advancing their watermarks;
schedule and non-Slack checks continue, and paid workers retain their own MCP access.

For the interim paid-sweep pattern, give the quest a recurring `schedule` watch and state in its
`context.md` that the schedule must inspect the quest's dormant `slack_*` entries through Slack
MCP. The schedule watch's watermark is the shared sweep cursor. The worker must block that
schedule acknowledgement if any required Slack read fails, so the window is retried rather than
skipped. This is intentionally quest-scoped; a dispatch never scans other quests' folders.

### Manual launchd control

```bash
./yaas-triage/setup/install-launchd.sh           # install + load
./yaas-triage/setup/install-launchd.sh status    # show plist + load state
./yaas-triage/setup/install-launchd.sh uninstall # unload + remove plist
./yaas-triage/setup/install-launchd-heartbeat.sh  # independent dead-man switch
./yaas-triage/setup/install-launchd-dashboard.sh  # optional always-on localhost UI

launchctl unload ~/Library/LaunchAgents/com.yaas.triage.plist   # temp disable
launchctl load   ~/Library/LaunchAgents/com.yaas.triage.plist   # temp enable

DRY_RUN=1 VERBOSE=1 python3 yaas-triage/tick.py   # one tick, no dispatch

tail -f logs/triage.log
tail -f logs/worker-latest.log
```

---

## Configurable knobs (in `.env`)

`.env.example` is the canonical list with full reasoning. The knobs most likely to
come up while debugging:

| Variable | Default | Effect |
|---|---|---|
| `YAAS_SLACK_CHECKERS_ENABLED` | 1 | `0` disables all local `slack_*` Python checks and the reaction sweep. Slack watermarks are held; schedules and worker MCP access are unaffected. |
| `YAAS_MAX_SPEND_1H` | 40 | Hourly dollar ceiling. On breach, checks still run but the dispatch is withheld and `gate_budget_exceeded` is logged. This is the first thing to check when nothing is dispatching despite dirty quests. |
| `YAAS_MAX_SPEND_24H` | 250 | Daily dollar ceiling. |
| `YAAS_MAX_DISPATCH_6H` | 250 | Dispatch-count ceiling. The only ceiling that works under the codex/cursor backends, which report no cost. |
| `YAAS_MAX_TARGET_DISPATCH_PER_HOUR` | 25 | Per-target breaker; logs `gate_target_breaker_open`. |
| `YAAS_MAX_DISPATCH_FANOUT` | 4 | Max agent invocations per tick. Extra targets are deferred (`gate_dispatch_deferred`) with watermarks untouched. |
| `YAAS_UNACKED_PROMOTE` | 3 | Dispatches a watch may go unacked in the ledger before it starts backing off (5m doubling to a 24h cap). It keeps retrying forever and is never parked; the dashboard shows it as `backing off` with the worker's last error. |
| `YAAS_CHECKER_ERROR_PROMOTE` | 6 | Consecutive checker errors before backoff becomes `misconfig`. |
| `YAAS_TRIAGE_MAX_PARALLEL` | 3 | Peak concurrent Slack calls. Raising this makes rate-limit trips much more likely. |
| `YAAS_LOG_RETAIN_DAYS` | 14 | Per-dispatch worker logs older than N days are deleted each tick. `0` disables. |
| `YAAS_RETIRE_DEFAULT_DAYS` | 14 | Default age threshold for retiring stale `slack_thread` watches. Per-quest override via `retire_slack_threads_after_days` in meta.json (`false` or `0` = never). |

Current spend against the ceilings:
`python3 yaas-triage/dispatch/spend-window.py state/run-log.ndjson | jq .`

---

## Debugging

| Symptom | Check |
|---|---|
| Quest never fires | `DRY_RUN=1 VERBOSE=1 python3 yaas-triage/tick.py` — see per-type checker output |
| Checker returns `0\|` when it should find messages | Run checker directly: `MCP_CALL=yaas-triage/surfaces/mcp-call.sh python3 yaas-triage/checkers/slack_thread.py '<entry_json>'` |
| Email quest misses messages | Watermark too far ahead? Check `watches[].last_checked_ts` in the quest's `watch.json`. Gmail indexes ~60s after delivery; `email.lag=120` gives a 2-min buffer |
| Worker keeps retrying the same error | Check `logs/worker-latest.log` and the watch's checker-health entry. Never delete an existing watch by hand; resolve or archive the quest, or change its objective through the supported quest workflow. |
| Triage lock stuck | Check `logs/triage.lock.holder`. If PID is dead, the OS releases the lock automatically on next tick |
| `LastExitStatus = 512` on launchd | Exit code 2 — a bad `.env` knob (a spend/limit value that isn't numeric) makes the orchestrator refuse to run (`gate_bad_env_knob`). Fix the value in `.env`; run `python3 yaas-triage/tick.py` manually to see it. (The old bash orchestrator exited 2 the same way on `.env` syntax errors under `set -eu`.) |
| `LastExitStatus = 36608` on launchd | Exit 143 (SIGTERM). Expected if the worker watchdog killed a runaway dispatch; or if macOS slept mid-run. |
