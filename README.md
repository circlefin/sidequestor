# Yourself as a Service (yaas) — v2

A personal Slack/email/calendar agent that monitors threads, channels, DMs,
Gmail queries, and cron schedules. Dispatches Claude Opus only when there's
real new activity.

**$0 when idle. Tokens spent only on dispatch.**

> macOS launchd fires `yaas-triage/triage.sh` every 60 seconds. Pure bash
> orchestrator + Python checker plugins. The worker dispatch is
> **harness-agnostic**: it runs on Claude Code (default), Codex, or Cursor,
> selected with `YAAS_AGENT` in `.env`. See
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
- **Optional:** `CODA_API_KEY` + `CODA_MCP_PATH` if you use the Coda MCP server; `YAAS_LOG_RETAIN_DAYS` (default 14); `YAAS_RETIRE_DEFAULT_DAYS` (default 30).

Edit `CLAUDE.md` to add your bot identity, tone rules, and references to any personal skills. The protocol sections must stay intact — they're contracted by `triage.sh`.

One rule worth adding in your tone section: **email replies should read like emails** — proper greeting, prose paragraphs, sign-off. Not like Slack messages with bullets and bold headers.

### 3. Install

```bash
./yaas-triage/setup/setup.sh
```

This walks you through Slack OAuth, stores the user `xoxp` token in macOS Keychain, runs a connectivity check, and optionally installs the launchd job. It also offers to opt into a **daily auto-sync from this template** (off by default) — say yes if you just want to run the latest YAAS and don't plan to customize `dashboard.html` / `yaas-triage/*` yourself. See `settings.json.example` for what it controls.

### 4. Verify

```bash
./yaas-triage/doctor.sh
```

Should report "All checks passed." If anything's off, the failed line tells you what to fix.

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
| **Codex** | `codex` | `~/.codex/config.toml` | Runs bounded (`workspace-write`, `approval_policy=never`, network on) — **not** the `--dangerously-bypass` flag. The native Slack plugin is disabled at dispatch so all Slack goes through `mcp-call.sh` (uniform sender identity, and it sidesteps Codex's write-approval gate). |
| **Cursor** | `cursor-agent` | `.cursor/mcp.json` | Runs with `--approve-mcps`. Slack goes through `mcp-call.sh` when no native Slack MCP is configured. |

**How Slack works across harnesses.** Every harness reaches Slack through the same underlying `mcp.slack.com` endpoint. If a harness has a native Slack tool, it uses it; otherwise the worker calls `yaas-triage/mcp-call.sh` (a shell bridge using your Keychain token) with the *same* tool names and arguments. This is documented in the "Slack access" section of `CLAUDE.example.md`, so any harness knows to fall back to it. Sends routed through `mcp-call.sh` carry your consistent Slack app identity.

### Model selection

Each backend's model is overridable via `.env` (leave unset to use that CLI's own default):

```bash
YAAS_CLAUDE_MODEL=opus          # default: opus
YAAS_CODEX_MODEL=               # default: ~/.codex/config.toml model
YAAS_CURSOR_MODEL=              # default: cursor's "auto"
```

> **Cost note.** Only the Claude path reports a dollar cost (`gate_dispatch_tokens` with `$`). Codex/Cursor report raw token counts only. Watch the reasoning-effort setting on Codex especially — a high-effort model can spend 1M+ input tokens on a single tick.

### Switching harnesses

Edit `YAAS_AGENT` in `.env` and the next tick picks it up (`triage.sh` re-sources `.env` every run). No restart needed. The dispatch log line shows which backend ran: `DISPATCH — invoking yaas worker (backend=codex) for: …`.

---

## Daily operation

Once installed, the launchd job runs `triage.sh` every 60s automatically. You don't need to do anything.

```bash
tail -f logs/triage.log                       # human-readable triage log
tail -f logs/worker-latest.log                # most recent worker dispatch
launchctl list com.yaas.triage                # current launchd status
./yaas-triage/doctor.sh                       # full health check

./yaas-triage/triage.sh                       # one-shot manual run
DRY_RUN=1 VERBOSE=1 ./yaas-triage/triage.sh   # manual dry run (no dispatch)

grep gate_dispatch_tokens state/run-log.ndjson | tail -10   # cost history

./yaas-triage/setup/install-launchd.sh uninstall   # stop
./yaas-triage/setup/install-launchd.sh             # reinstall
```

### Dashboard

```bash
./yaas-triage/dashboard.sh   # starts the server (localhost:8877) and opens it
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

Watch types: `slack_thread`, `slack_channel`, `slack_dm`, `schedule` (5-field cron + IANA TZ), `email` (Gmail search query).

---

## Repository layout

```
yourself-as-a-service/
├── README.md                        ← this file
├── ARCHITECTURE.md                  ← full system design + verification commands
├── CLAUDE.example.md                ← worker-instruction starter (copy → CLAUDE.md, customize)
├── CLAUDE.md                        ← your customized worker instructions (gitignored)
├── dashboard.html                   ← live dashboard UI, served by dashboard.sh
├── .env.example                     ← per-install template (copy → .env, gitignored)
├── settings.json.example            ← per-install template (copy → settings.json, gitignored)
├── LICENSE
├── yaas-triage/                     ← generic triage infrastructure
│   ├── triage.sh                    ← entry point (launchd target)
│   ├── dispatch-agent.sh            ← launches the worker on YAAS_AGENT backend (claude/codex/cursor)
│   ├── translate-stream.py          ← normalizes codex/cursor event stream → exit/tokens/final
│   ├── dashboard-server.py          ← dashboard backend + live state API
│   ├── dashboard.sh                 ← starts the dashboard and opens it
│   ├── sync-yaas-v2.sh              ← opt-in daily pull from this template
│   ├── mcp-call.sh                  ← Slack MCP HTTP wrapper (used by checkers + any harness lacking native Slack)
│   ├── format-stream.py             ← worker stream-json → human log (Claude schema; other backends pass through)
│   ├── extract-tokens.py            ← post-worker cost extractor (Claude)
│   ├── worker.mcp.json              ← MCP servers passed to the Claude worker
│   ├── doctor.sh                    ← health check
│   ├── checkers/                    ← one .py per watch type
│   ├── setup/                       ← OAuth flow, launchd installer, yaas-v2 tracking
│   └── skills/                      ← four generic worker skills
│       ├── yaas-quest-creation/     ← new-quest.py + SKILL.md
│       ├── yaas-gmail-reply/        ← gmail-reply.py + SKILL.md (RFC 2822 threading)
│       ├── yaas-answering-quality/  ← reply quality guardrails
│       └── yaas-ops/                ← operations + extending the system
├── skills/                          ← your personal/domain skills (gitignored)
├── state/                           ← runtime: quests, watermarks, reaction history (gitignored)
└── logs/                            ← triage + worker logs (gitignored)
```

---

## Cost model

| Tick outcome | Cost |
|---|---|
| Idle (no dirty quests, no new reactions) | **$0.00** |
| One dirty quest, brief worker | typically $0.05–$0.30 |
| Multiple dirty quests with research | up to ~$0.50–$1.50 |

A typical install on 60s cron spends nothing on >95% of ticks and a few dollars per day at most when active.

---

## Security model

- Slack user OAuth token (`xoxp-…`) stored in macOS Keychain (`service=slack-xoxp-token, account=yaas`). Never in `.env`, never in the repo.
- OAuth uses PKCE — no client secret needs to live anywhere.
- Token identity = the human who installed. Slack audit logs attribute every action to you, not a shared bot.
- Revocation: visit Slack → Manage Apps → remove. The next launchd tick will fail authentication and stop posting.
- Coda API key (optional) lives in `.env` and is passed to the local Coda MCP process via environment variable.

---

## AI Safety

YAAS v2 was designed with safety features and safeguards in observance of Circle's AI Safety rules ([reference link](<insert-link>)). Use, configure, and extend it with those principles in mind. Built-in defaults aligned with that posture:

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
