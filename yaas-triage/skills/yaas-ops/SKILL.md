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
├── triage.sh                 ← launchd-driven main loop (every 60s)
├── mcp-call.sh               ← Slack MCP HTTP helper (used by Slack checkers)
├── format-stream.py          ← formats claude --output-format stream-json for human logs
├── extract-tokens.py         ← parses worker ndjson, writes cost event to run-log
├── worker.mcp.json           ← MCP server config for the claude worker process
├── doctor.sh                 ← health check
├── README.md
├── checkers/                 ← one script per watch type (plugin directory)
│   ├── slack_thread.py · slack_channel.py · slack_dm.py
│   ├── schedule.py · email.py
│   ├── jira.py               ← Jira issues via ../jira-call.sh (no MCP needed)
│   ├── github_pr.py          ← PR activity via `gh search prs`
│   ├── reactions.py          ← global reaction sweep (not per-quest)
│   ├── slack_utils.py        ← shared parse_slack_messages()
│   ├── cron-due.py           ← cron evaluation logic (used by schedule.py)
│   └── *.lag                 ← per-type watermark lag in seconds:
│                               email 120 · slack_mention 90 · github_pr 30 · jira 15
├── setup/                    ← one-time install
│   ├── setup.sh              ← first-time Slack OAuth + keychain + optional launchd install
│   ├── install-launchd.sh    ← install / reload / uninstall the launchd job
│   ├── com.yaas.triage.plist.template
│   └── yaas-app-config.json  ← generic Slack scopes & PKCE flow config
└── skills/                   ← four generic worker skills (loaded on demand)
    ├── yaas-quest-creation/  ← scaffolds new quest folders (new-quest.py)
    ├── yaas-gmail-reply/     ← threaded Gmail reply utility (gmail-reply.py)
    ├── yaas-answering-quality/ ← bot reply quality rules
    └── yaas-ops/             ← this file

state/
├── triage/
│   ├── last-run.json         ← triage run counters + last completion time
│   └── pending_reactions.json  ← transient: reactions awaiting worker (deleted on success)
├── quests/
│   ├── active/               ← ongoing quests (4-file folders)
│   ├── completed/            ← finished quests (kept for audit)
│   └── archived/             ← cancelled or stale
├── run-log.ndjson            ← append-only log of every triage tick + dispatch
├── claude_intensifies_replied.json ← processed :claude-intensifies: timestamps
├── writing_hand_replied.json  ← processed :writing_hand: + skipped notes
├── floppy_disk_saved.json     ← processed :floppy_disk: timestamps
└── context-memory/           ← :floppy_disk: saves (people/, topics/)

logs/
├── triage.log                ← human-readable triage run log
├── triage.out.log · triage.err.log  ← launchd stdout/stderr
├── worker-latest.log         ← symlink to most recent worker run (human-readable)
├── worker-latest.ndjson      ← symlink to most recent worker run (raw stream-json)
└── worker-<stamp>.{log,ndjson}  ← per-run worker logs (pruned after YAAS_LOG_RETAIN_DAYS)

.env                          ← per-install secrets + knobs (gitignored)
.env.example                  ← template for new installs
CLAUDE.example.md             ← starter worker instructions (template)
CLAUDE.md                     ← your customized worker instructions (copy from CLAUDE.example.md)
ARCHITECTURE.md               ← full system design
```

---

## Checker plugin architecture

### How it works

`triage.sh`'s `check_quest()` iterates over every entry in a quest's `watches[]` array. For each entry it looks up `yaas-triage/checkers/<type>.py` and runs it:

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

Environment variables available to all checkers (exported by triage.sh after sourcing `.env`):
- `MCP_CALL` — path to `mcp-call.sh`
- `GWS_BIN` — path to `gws` CLI

### The reactions sweep

`checkers/reactions.py` is special — it doesn't follow the per-entry interface. It runs once per triage tick (called directly by triage.sh with 4 positional args) and sweeps Slack globally for `:claude-intensifies:`, `:writing_hand:`, `:floppy_disk:` reactions across a 60-day window. Diffs against `state/*_replied.json` / `state/*_saved.json`. New reactions → writes `state/triage/pending_reactions.json`.

### Watermark lag

If a channel type has indexing latency (e.g. Gmail's search index takes ~60s to reflect new mail), create a companion file `checkers/<type>.lag` containing the lag in integer seconds. triage.sh reads these at startup and subtracts the lag when advancing watermarks, so clean ticks never claim "I've seen everything up to now" when the source hasn't indexed it yet.

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
  ]
}
```

`jira` needs the REST bridge (`yaas-triage/jira-call.sh`, Basic-auth API token in Keychain `jira-api-token`/`yaas`) because the Atlassian MCP is interactive-OAuth only and is absent in headless dispatch. `github_pr` accepts optional `search` (extra GitHub qualifiers) and `limit` (default 100) — read the warning in `checkers/github_pr.py`'s docstring before adding a `search`, since repeated qualifiers AND rather than OR and can silently match nothing.

`last_checked_ts` is always a Unix epoch float string. triage.sh is the sole owner of this field — the worker must never modify existing entries (it may only append new ones).

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
  "retire_slack_threads_after_days": 30        // optional; int / false / null — see ARCHITECTURE.md §6
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

That's the entire change. triage.sh requires no modification — it autodiscovers the new checker by filename.

---

## Worker utilities

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
cp .env.example .env             # then fill in SLACK_*, YAAS_FROM_EMAIL, etc.
cp CLAUDE.example.md CLAUDE.md   # customize as needed
./yaas-triage/setup/setup.sh
./yaas-triage/doctor.sh          # verify install is healthy
```

`setup.sh` walks through Slack OAuth (PKCE flow, no client secret needed), stores the user `xoxp` token in macOS keychain at `(service=slack-xoxp-token, account=yaas)`, runs a connectivity check, and optionally installs the launchd job. Each user creates their own Slack app at https://api.slack.com/apps with the scopes listed in `setup/yaas-app-config.json`.

### Manual launchd control

```bash
./yaas-triage/setup/install-launchd.sh           # install + load
./yaas-triage/setup/install-launchd.sh status    # show plist + load state
./yaas-triage/setup/install-launchd.sh uninstall # unload + remove plist

launchctl unload ~/Library/LaunchAgents/com.yaas.triage.plist   # temp disable
launchctl load   ~/Library/LaunchAgents/com.yaas.triage.plist   # temp enable

DRY_RUN=1 VERBOSE=1 bash yaas-triage/triage.sh   # one tick, no dispatch

tail -f logs/triage.log
tail -f logs/worker-latest.log
```

---

## Configurable knobs (in `.env`)

| Variable | Default | Effect |
|---|---|---|
| `YAAS_LOG_RETAIN_DAYS` | 14 | Per-dispatch worker logs older than N days are deleted each tick. `0` disables. |
| `YAAS_RETIRE_DEFAULT_DAYS` | 30 | Default age threshold for retiring stale `slack_thread` watches. Per-quest override via `retire_slack_threads_after_days` in meta.json. |

---

## Debugging

| Symptom | Check |
|---|---|
| Quest never fires | `DRY_RUN=1 VERBOSE=1 bash yaas-triage/triage.sh` — see per-type checker output |
| Checker returns `0\|` when it should find messages | Run checker directly: `MCP_CALL=yaas-triage/mcp-call.sh python3 yaas-triage/checkers/slack_thread.py '<entry_json>'` |
| Email quest misses messages | Watermark too far ahead? Check `watches[].last_checked_ts` in the quest's `watch.json`. Gmail indexes ~60s after delivery; `email.lag=120` gives a 2-min buffer |
| Worker keeps retrying the same error | Check `logs/worker-latest.log`. If the quest has a permanently-deleted thread, remove it from `watches[]` |
| Triage lock stuck | Check `logs/triage.lock.holder`. If PID is dead, the OS releases the lock automatically on next tick |
| `LastExitStatus = 512` on launchd | Exit code 2 — `set -eu` aborted. Almost always `.env` syntax (unquoted `<` `>` `&` etc.). Run triage.sh manually for the error. |
| `LastExitStatus = 36608` on launchd | Exit 143 (SIGTERM). Expected if the worker watchdog killed a runaway dispatch; or if macOS slept mid-run. |
