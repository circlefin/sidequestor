# Sidequestor — v2

> *Yourself-as-a-service, while you're AFK.*

**Sidequestor** (formerly "Yourself as a Service", still `yaas` internally) is a
personal Slack, email, and calendar agent that acts as you, and fits like a
jacket over your local working directory for Claude Code, Codex, or Cursor.
Drop it into the repo you already work in and it wraps your existing harness
instead of replacing it. The live control panel — where drafts wait for your
review and every quest's messages are one click from Slack — is the
**Sidequestor dashboard** (`yaas-triage/dashboard-start.sh`, http://localhost:8877).

Everything worth your attention becomes a **quest**: a first-class unit of work
with its own objective, watchers, timeline, and memory. yaas keeps your quests
moving, watching the threads, channels, DMs, Gmail queries, and cron schedules
each one cares about, and waking an LLM worker only when that quest has genuine
new activity to act on.

The monitoring is **intelligent and cost-free**: a pure-bash triage loop sweeps
every watched source on a 60-second heartbeat, tracks a per-watch watermark so
nothing is seen twice or missed, and spends zero tokens until something real
lands.

**$0 when idle. Tokens spent only on dispatch.**

The harder problem is not doing the work but **knowing it was done**. A headless
agent exits successfully whether it handled everything or quietly handled
nothing, so nothing here commits on an exit code: the worker closes each
dispatched item in an acknowledgment ledger, checkers report whether they
actually drained their window, spend has hard ceilings, and a separate launchd
job watches the loop from outside it. Every one of those exists because its
absence caused a real incident.

> One launchd job keeps `yaas-triage/triage-loop.sh` alive, which paces
> `triage.sh` at `YAAS_TRIAGE_INTERVAL` (60s). A second job runs the health
> monitor. Pure bash orchestrator + Python checker plugins. The worker dispatch
> is **harness-agnostic**: Claude Code (default), Codex, or Cursor, selected
> with `YAAS_AGENT` in `.env`. See
> **[Choosing a harness](#choosing-a-harness-claude--codex--cursor)**.

For the full architecture, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Setup (first time)

### 1. Prerequisites

| Tool | Why | Install |
|---|---|---|
| `claude` CLI | worker dispatch (default harness) | https://docs.claude.com/en/docs/claude-code |
| `codex` / `cursor-agent` | only if running those harnesses (see [Choosing a harness](#choosing-a-harness-claude--codex--cursor)) | `brew install codex` / Cursor CLI installer |
| `gws` CLI | Gmail / Drive / Docs (worker + email checker) | internal Google Workspace tool |
| `jq` ≥ 1.6 | JSON parsing in triage.sh | `brew install jq` |
| `perl` | `flock` lock acquisition | preinstalled on macOS |
| `python3` ≥ 3.9 | all `.py` files | macOS system Python is fine |
| `node` | Coda MCP (optional) | `brew install node` |

### 2. Clone and configure

```bash
git clone https://github.com/<your-org>/yourself-as-a-service-v2.git yourself-as-a-service
cd yourself-as-a-service
cp .env.example .env
cp CLAUDE.example.md CLAUDE.md
```

Edit `.env` with your values:
- **Slack app:** create your own Slack app (PKCE OAuth, no client secret) and fill the `SLACK_*` values in `.env`. `.env.example` has the app-creation steps (scopes, redirect URL); `setup.sh` then OAuth-authorizes it under your Slack identity.
- **`YAAS_FROM_EMAIL`** (quoted, e.g. `"Jane Doe <jane@example.com>"`) — sender identity for the Gmail reply skill.
- **Optional:** `CODA_API_KEY` + `CODA_MCP_PATH` if you use the Coda MCP server, and `JIRA_EMAIL` + `JIRA_BASE_URL` for Jira watches.
- **Everything else is optional with a working default.** `.env.example` is the canonical list of knobs, grouped into spend ceilings, dispatch shape, agent backend, loop/checker tuning, and retention, each with its default and the reasoning. The spend ceilings are worth a look before you leave them alone: run `python3 yaas-triage/spend-window.py state/run-log.ndjson | jq .` after a day to see your own rate, since cost per dispatch scales with how large your `CLAUDE.md` and quest context files are.

Edit `CLAUDE.md` to add your bot identity, tone rules, and references to any personal skills. The protocol sections must stay intact — they're contracted by `triage.sh`.

One rule worth adding in your tone section: **email replies should read like emails** — proper greeting, prose paragraphs, sign-off. Not like Slack messages with bullets and bold headers.

### 3. Install

```bash
./yaas-triage/setup/setup.sh
```

This walks you through Slack OAuth, stores the user `xoxp` token in macOS Keychain, runs a connectivity check, and optionally installs the launchd jobs. It also offers to opt into a **daily auto-sync from this template** (off by default) — say yes if you just want to run the latest YAAS and don't plan to customize `dashboard.html` / `yaas-triage/*` yourself. See `settings.json.example` for what it controls.

Then install the health monitor, which is a **separate** job on purpose — a check running inside the triage loop cannot detect the loop being dead, which is exactly how one outage ran for 6.5 hours:

```bash
./yaas-triage/setup/install-launchd-heartbeat.sh
```

It checks every 5 minutes for: no completed tick, a tick that started and never finished, consecutive tick failures, a checker stuck past its retry threshold, an approval stranded mid-send, and any budget or misconfiguration event. Alerts land as desktop notifications and in `state/health-status.json`.

### 4. Verify

```bash
./yaas-triage/doctor.sh                          # is this machine configured
python3 yaas-triage/health-monitor.py            # is the loop alive and unblocked?
yaas-triage/tests/run-all.sh                     # every suite, ~30s, touches no real state
```

`doctor.sh` should report "All checks passed"; the failed line tells you what to fix. `health-monitor.py` prints `healthy` and exits 0. All 14 test suites should pass — they run against temp fixtures and touch no real state, so they are safe to run any time.

---

## Choosing a harness (Claude / Codex / Cursor)

The worker dispatch is backend-agnostic. `triage.sh` does the same thing regardless of harness — sweep quests, decide dirtiness, and (only when there's real activity) launch one headless agent. Which agent it launches is set by **`YAAS_AGENT`** in `.env`:

```bash
YAAS_AGENT=claude   # default — Claude Code (claude CLI)
YAAS_AGENT=codex    # OpenAI Codex CLI
YAAS_AGENT=cursor   # Cursor Agent CLI (cursor-agent)
```

`yaas-triage/dispatch-agent.sh` is the only backend-specific piece: it maps `YAAS_AGENT` to the right binary and headless flags, streams the agent's raw event JSONL back to `triage.sh`, and exits with the agent's exit code. Everything else (quests, watches, approvals, state) is identical across harnesses.

### The rules file each harness reads

Each CLI loads its own instruction file from the repo root. They should all point at the same customized `CLAUDE.md`, so create symlinks after `cp CLAUDE.example.md CLAUDE.md`:

```bash
ln -s CLAUDE.md AGENTS.md    # Codex and Cursor both read AGENTS.md
ln -s CLAUDE.md GEMINI.md    # (optional) Gemini CLI
```

This keeps one source of truth — edit `CLAUDE.md`, every harness sees the change.

### Per-harness prerequisites and notes

| Harness | CLI | MCP / tool config (yours to set up) | Notes |
|---|---|---|---|
| **Claude** | `claude` | `yaas-triage/worker.mcp.json` (Slack/Coda/Atlassian) | Default. Native Slack MCP. Full token+cost accounting. |
| **Codex** | `codex` | `~/.codex/config.toml` | Runs bounded by default (`workspace-write`, `approval_policy=never`, network on). Set `YAAS_CODEX_PERMISSION_MODE=bypassPermissions` to use `--dangerously-bypass-approvals-and-sandbox`. The native Slack plugin is disabled at dispatch so all Slack goes through `mcp-call.sh` with a uniform sender identity. |
| **Cursor** | `cursor-agent` | `.cursor/mcp.json` | Runs with `--approve-mcps`. Slack goes through `mcp-call.sh` when no native Slack MCP is configured. |

**How Slack works across harnesses.** Every harness reaches Slack through the same underlying `mcp.slack.com` endpoint. If a harness has a native Slack tool, it uses it; otherwise the worker calls `yaas-triage/mcp-call.sh` (a shell bridge using your Keychain token) with the *same* tool names and arguments. This is documented in the "Slack access" section of `CLAUDE.example.md`, so any harness knows to fall back to it. Sends routed through `mcp-call.sh` carry your consistent Slack app identity.

### Model selection

Each backend's model is overridable via `.env` (leave unset to use that CLI's own default):

```bash
YAAS_CLAUDE_MODEL=opus          # default: opus
YAAS_CODEX_MODEL=               # default: ~/.codex/config.toml model
YAAS_CURSOR_MODEL=              # default: cursor's "auto"

YAAS_CLAUDE_PERMISSION_MODE=acceptEdits   # any Claude --permission-mode value
YAAS_CODEX_PERMISSION_MODE=workspace-write # workspace-write or bypassPermissions
```

> **Cost note.** Only the Claude path reports a dollar cost (`gate_dispatch_tokens` with `$`). Codex/Cursor report raw token counts only. Watch the reasoning-effort setting on Codex especially — a high-effort model can spend 1M+ input tokens on a single tick.

### Switching harnesses

Edit `YAAS_AGENT` in `.env` and the next tick picks it up (`triage.sh` re-sources `.env` every run). No restart needed. The dispatch log line shows which backend ran: `DISPATCH — invoking yaas worker (backend=codex) for: …`.

---

## Daily operation

Once installed, the loop runs a tick every 60s and the monitor checks in every 5 minutes. You don't need to do anything.

```bash
# is it alive, and is anything silently held?
python3 yaas-triage/health-monitor.py            # exit 0 = healthy
cat state/health-status.json                     # the same verdict, as published

tail -f logs/triage.log                          # human-readable triage log
tail -f logs/worker-latest.log                   # the dispatch in flight
launchctl list | grep com.yaas                   # all jobs

# spend, against your ceilings
python3 yaas-triage/spend-window.py state/run-log.ndjson | jq .

# manual runs
./yaas-triage/triage.sh                          # one tick now
DRY_RUN=1 VERBOSE=1 ./yaas-triage/triage.sh      # check phase only, never dispatches

./yaas-triage/setup/install-launchd.sh uninstall  # stop the loop
./yaas-triage/setup/install-launchd.sh            # reinstall
```

**If nothing is dispatching despite dirty quests,** check in this order — each writes its own event to `state/run-log.ndjson`:

| Event | Meaning |
|---|---|
| `gate_budget_exceeded` | a spend or dispatch ceiling was hit; raise it in `.env` or wait for the window to roll |
| `gate_slack_down` | the pre-dispatch Slack probe failed |
| `gate_dispatch_deferred` | past the per-tick fan-out cap; it will run next tick |
| `gate_target_breaker_open` | one quest has been dispatched too often this hour |
| `gate_watch_misconfigured` | a watch has permanently stopped being checked and needs you |

**One thing worth knowing:** editing `triage-loop.sh` or `dashboard-server.py` has no effect until that job is restarted, because launchd keeps one long-lived process and neither bash nor Python re-reads a running file. Edits to `triage.sh` and the checkers apply on the next tick.

```bash
launchctl kickstart -k "gui/$(id -u)/com.yaas.triage"
```

### Dashboard

```bash
./yaas-triage/dashboard-start.sh   # starts the server (localhost:8877) and opens it
```

Shows active quests, pending approvals, recent activity, and daily/weekly briefs. A pulsing pill in the top-right header appears whenever a worker is currently dispatched — click it to expand a live transcript of what it's doing.

`setup.sh` offers to install it as a launchd job too, so it survives reboots and restarts itself if it ever dies — say yes there, or run it yourself any time:

```bash
./yaas-triage/setup/install-launchd-dashboard.sh             # install (KeepAlive + RunAtLoad)
./yaas-triage/setup/install-launchd-dashboard.sh uninstall   # stop
./yaas-triage/setup/install-launchd-dashboard.sh status      # check
```

---

## Creating a quest

A quest is a folder under `state/quests/active/` with four files: `meta.json`, `watch.json`, `context.md`, `timeline.ndjson`. Create them via the scaffolding script (validates inputs, sets timestamps correctly, writes canonical schema):

```bash
python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '{
  "title": "Watch #my-channel for replies",
  "priority": "normal",
  "allow_send": false,
  "context": "Track new top-level messages in #my-channel; reply when warranted.",
  "watches": [
    {
      "type": "slack_channel",
      "channel_id": "C012345ABCD",
      "reason": "monitor channel activity"
    }
  ]
}'
```

See [`yaas-triage/skills/yaas-quest-creation/SKILL.md`](yaas-triage/skills/yaas-quest-creation/SKILL.md) for the full spec.

Watch types: `slack_thread`, `slack_channel`, `slack_dm`, `slack_mention` (any message @mentioning a user_id, global), `schedule` (5-field cron + IANA TZ), `email` (Gmail search query).

---

## Repository layout

```
yourself-as-a-service/
├── README.md                        ← this file
├── ARCHITECTURE.md                  ← full system design + verification commands
├── CLAUDE.example.md                ← worker-instruction starter (copy → CLAUDE.md, customize)
├── CLAUDE.md                        ← your customized worker instructions (gitignored)
├── dashboard.html                   ← live dashboard UI, served by dashboard-start.sh
├── .env.example                     ← per-install template (copy → .env, gitignored)
├── settings.json.example            ← per-install template (copy → settings.json, gitignored)
├── LICENSE
├── yaas-triage/                     ← generic triage infrastructure
│   ├── triage.sh                    ← one tick: sweep, gate, dispatch, commit
│   ├── triage-loop.sh               ← KeepAlive driver (launchd target)
│   ├── dispatch-agent.sh            ← launches the worker on the YAAS_AGENT backend
│   ├── ack-watch.py                 ← the acknowledgment ledger: what actually got handled
│   ├── add-watch.py                 ← the ONLY way to append a watch (append-only, validated)
│   ├── approval-helper.py           ← the human review queue + execution leases
│   ├── spend-window.py              ← rolling spend/dispatch windows and the ceilings
│   ├── checker-health.py            ← per-watch exponential backoff on checker failures
│   ├── health-monitor.py            ← the dead-man switch (own launchd job)
│   ├── heartbeat-loop.sh            ← KeepAlive driver for the monitor
│   ├── ensure-watch-ids.py          ← backfills the stable watch_id everything keys on
│   ├── source-evidence.py    ← proves a real source read happened in the worker stream
│   ├── slack-send.py                ← send + log the body in one step (dashboard needs the body)
│   ├── mcp-call.sh / jira-call.sh   ← Slack MCP and Jira REST bridges (Keychain auth)
│   ├── format-stream.py             ← worker event stream → human log
│   ├── extract-tokens.py            ← per-dispatch cost into the run log
│   ├── translate-stream.py          ← normalizes codex/cursor streams
│   ├── notify.py / rotate-logs.py   ← desktop notifications; log + queue rotation
│   ├── manual-dispatch.sh           ← dashboard-initiated run (advances no watermarks)
│   ├── dashboard-server.py          ← dashboard backend + live state API
│   ├── dashboard-start.sh                 ← starts the dashboard and opens it
│   ├── sync-yaas-v2.sh              ← opt-in daily pull from this template
│   ├── doctor.sh                    ← is this machine configured (setup validation)
│   ├── test-*.sh                    ← nine suites; see ARCHITECTURE.md § 16
│   ├── checkers/                    ← one .py per watch type, plus result.py (the contract)
│   ├── setup/                       ← OAuth flow, launchd installers, template tracking
│   └── skills/                      ← four generic worker skills
│       ├── yaas-quest-creation/     ← new-quest.py + SKILL.md
│       ├── yaas-gmail-reply/        ← gmail-reply.py + SKILL.md (RFC 2822 threading)
│       ├── yaas-answering-quality/  ← reply quality guardrails
│       └── yaas-ops/                ← operations + extending the system
├── .claude/hooks/                   ← deny-state-writes.sh: the lock on watch.json
├── skills/                          ← your personal/domain skills (gitignored)
├── state/                           ← runtime: quests, watermarks, reaction history (gitignored)
└── logs/                            ← triage + worker logs (gitignored)
```

---

## Cost model

| Tick outcome | Cost |
|---|---|
| Idle (nothing dirty, no new reactions) | **$0.00** |
| Held back by a gate (budget, Slack down, deferred) | **$0.00**, and no work is lost |
| One dirty quest | typically $0.30–$1.50 per invocation |
| Several dirty quests | one invocation each, sequentially, capped by `YAAS_MAX_DISPATCH_FANOUT` |

Most ticks cost nothing. The figure that matters is per *invocation*, not per tick, because
each one reloads your full `CLAUDE.md` plus that quest's context — so cost scales with how
large those files are, and a tick with four dirty quests pays it four times. That is the
deliberate trade for a failure in one quest being unable to bury another's activity.

**Read your own numbers before trusting anyone's estimate:**

```bash
python3 yaas-triage/spend-window.py state/run-log.ndjson | jq .
```

**Ceilings are on by default** and are circuit breakers, not throttles: `YAAS_MAX_SPEND_1H`
(40), `YAAS_MAX_SPEND_24H` (250), `YAAS_MAX_DISPATCH_6H` (250), and a per-quest hourly
breaker. On a breach, checks still run and watermarks still hold — only the dispatch is
withheld, so the work re-surfaces once the window rolls forward. Tune them in `.env` from
your measured rate.

The dispatch-count ceiling matters most under `YAAS_AGENT=codex` or `cursor`, which report
token counts but no cost, so the dollar ceilings cannot see them.

---

## Security model

- Slack user OAuth token (`xoxp-…`) stored in macOS Keychain (`service=slack-xoxp-token, account=yaas`). Never in `.env`, never in the repo.
- OAuth uses PKCE — no client secret needs to live anywhere.
- Token identity = the human who installed. Slack audit logs attribute every action to you, not a shared bot.
- Revocation: visit Slack → Manage Apps → remove. The next launchd tick will fail authentication and stop posting.
- Coda API key (optional) lives in `.env` and is passed to the local Coda MCP process via environment variable.

---

## AI Safety

YAAS v2 was designed with safety features and safeguards in observance of Circle's AI Safety rules. Use, configure, and extend it with those principles in mind, alongside whatever AI-use policy applies where you run it. Built-in defaults aligned with that posture:

- **Drafts by default.** New quests scaffold with `allow_send: false`. The worker drafts via `slack_send_message_draft` (or `gws gmail users drafts create`) until the user explicitly authorizes sending. See "drafts-only mode" behavior in the [Quest Activation Protocol](CLAUDE.example.md).
- **Human-initiated reaction triggers.** The only path by which the worker sends without a quest's `allow_send: true` is the `:claudeloading:` reaction — and that requires a human to place the emoji on a specific message, which is itself the authorization.
- **Full audit trail.** Every outbound action (send/draft/reply) is appended to the quest's `timeline.ndjson` with a permalink to the resulting Slack/Gmail message.
- **Individual-identity tokens.** Slack actions are attributed to the human who ran `setup.sh`, never to a shared service account. Revocation is one click.
- **Per-quest decision rules.** What the worker does on each watch fire is governed by that quest's own `context.md` — you write the rules, the worker follows them.
- **Privacy by default.** CLAUDE.example.md Behavioral Rules §6: "Respect privacy absolutely. Never share info about one person with another unless directly relevant or explicitly asked. When in doubt, share less."

When extending YAAS, follow the same posture: default to drafts, log everything, scope tokens to the individual, and write conservative `context.md` decision rules.

---

## Contributing

`yaas-triage/` is generic infrastructure with no personal data. Improvements welcome. Personal customizations (your bot tone, your watched channels, your skills) belong in `CLAUDE.md` and `skills/` — both gitignored from this public repo.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for system internals before opening a PR that touches the core loop.

---

## License

See [LICENSE](LICENSE).
