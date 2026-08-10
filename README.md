# Sidequestor

> *Yourself as a service, while you're AFK.*

You already have an AI agent on your machine — Claude Code, Codex, or Cursor — sitting in the
repo you work in, waiting for you to type something.

**Sidequestor is a sleeve you pull over it.** Same agent, same directory, same rules file. The
difference is that it stops waiting for you.

```
        ┌──────────────────────────────────────────────┐
        │  S I D E Q U E S T O R                       │  ← the sleeve
        │                                              │
        │   watches things · decides · wakes the agent │
        │                                              │
        │     ┌──────────────────────────────────┐     │
        │     │  your existing agent + repo      │     │  ← unchanged
        │     │  (claude / codex / cursor)       │     │
        │     └──────────────────────────────────┘     │
        └──────────────────────────────────────────────┘
```

It watches the Slack threads, channels, DMs, Gmail queries, Jira sets, GitHub PRs and cron
schedules you care about. When one of them genuinely moves, it wakes your agent, hands it just
that one thing, and writes down what happened. When nothing moves, it costs **nothing** —
the watching is shell and Python, about 150ms a tick.

Every piece of that work is a **quest** — an isolated compartment with one objective, its own
permission to send, and its own written-down storyline. That is the safety model: the agent only
ever acts inside a compartment, so you always know what it is watching, why it acted, and that a
mistake can't escape the quest it happened in. See [Everything is a quest](#everything-is-a-quest).

**$0 idle. Tokens only on dispatch.**

---

## The interesting problem

Doing the work is the easy half. The hard half:

> **How do you know it was actually done?**

A headless agent exits successfully whether it handled everything or quietly handled nothing.
So nothing here trusts an exit code. The agent must close each item it was handed in an
acknowledgment ledger; checkers must prove they read to the end of their window; spend has hard
ceilings; and a separate job watches the loop from outside it, because a health check inside the
loop cannot notice the loop being dead.

Every one of those exists because its absence caused a real incident. The stories are in
[ARCHITECTURE.md](ARCHITECTURE.md), which is short and mostly diagrams.

---

## Everything is a quest

The quest is the safety harness. An autonomous agent left to "watch my Slack and help out" is a
blob you cannot audit: you never quite know what it is tracking, why it acted, or whether it is
about to act on something it misread. Sidequestor refuses that. **Every piece of work is a quest:
a compartment with one objective, its own watchers, its own send permission, and its own memory.**
The agent only ever acts inside a compartment — never on the world at large.

That compartmentalisation is what makes it safe to leave running:

- **You always know what it's doing.** The active quests *are* the complete list of what your
  Sidequestor is paying attention to. Nothing is watched that isn't a quest; nothing is acted on
  outside the quest that triggered it. To audit the whole system, you list one directory.
- **You always know which storyline it's following.** Each quest's `context.md` is its script —
  the objective and the decision rules — and its `timeline.ndjson` is everything it has ever done
  on that storyline, in order. When it replies, you can trace exactly which quest drove it and why.
- **A mistake stays in its compartment.** Permission to send is per quest (`allow_send`). A watch
  that breaks, a thread that goes stale, a rate-limit — each is contained to its own quest and
  held there; it cannot bleed into the others or advance anything it didn't earn. The dashboard
  surfaces a stuck or throttled quest by name instead of letting it fail silently.

A quest is just a folder with four files. That's the whole data model.

```
state/quests/active/watch-my-channel/
├── context.md        the storyline: why this exists, and how to decide   ← the agent reads this first
├── meta.json         status, priority, may-it-send
├── watch.json        where to look, and how far it has got (the watermark)
└── timeline.ndjson   everything it has ever done on this quest
```

No database. No migrations. You can read your agent's entire memory — everything it watches and
everything it has done — with `cat`.

---

## Install

### 1. What you need

| Tool | Why | Get it |
|---|---|---|
| `claude` CLI | the default agent | https://docs.claude.com/en/docs/claude-code |
| `codex` / `cursor-agent` | only if you prefer those | `brew install codex` · Cursor CLI installer |
| `jq` ≥ 1.6 | the orchestrator speaks JSON | `brew install jq` |
| `python3` ≥ 3.9 | everything else | macOS system Python is fine |
| `perl` | file locking | already on your Mac |
| `gws` CLI | only for Gmail / Drive watches | Google Workspace CLI |
| `node` | only for the Coda MCP | `brew install node` |

macOS only for now: it leans on `launchd` and the Keychain.

### 2. Clone and configure

```bash
git clone https://github.com/circlefin/sidequestor.git
cd sidequestor
cp .env.example .env
cp CLAUDE.example.md CLAUDE.md
```

Two files to edit, and only two:

**`.env`** — the Slack app details, and your sender identity for email replies. Every other knob
has a working default, and `.env.example` explains each one and why it's set where it is. Worth
a glance at the spend ceilings before you leave them alone.

**`CLAUDE.md`** — your agent's rules: who it is, how it writes, what it must never do. Add your
voice here. Leave the protocol sections alone; the orchestrator has a contract with them.

> If you already have a `CLAUDE.md` in this repo for your own interactive use, that's the point
> — the sleeve reads the same file your agent already reads.

### 3. Install

```bash
./yaas-triage/setup/setup.sh
```

Walks you through Slack OAuth (PKCE, no client secret), puts the token in your Keychain — never
in the repo — checks connectivity, and offers to install the background jobs.

Then the watchdog, which is a **separate** job on purpose:

```bash
./yaas-triage/setup/install-launchd-heartbeat.sh
```

A check running inside the triage loop cannot detect the loop being dead. That is not
theoretical: one outage ran 6.5 hours because nothing was watching the watcher.

### 4. Confirm it's real

```bash
./yaas-triage/ops/doctor.sh                    # is this MACHINE set up?
python3 yaas-triage/ops/health-monitor.py      # is it WORKING right now?
yaas-triage/tests/run-all.sh                   # is the CODE correct?
```

Three different questions, three different tools. The tests use throwaway fixtures and touch
none of your real state, so they're safe to run whenever.

Expect: `All checks passed`, `healthy`, and `28 suite(s) passed`.

---

## Your first quest

```bash
python3 yaas-triage/skills/yaas-quest-creation/new-quest.py '{
  "title": "Watch #my-channel for replies",
  "priority": "normal",
  "allow_send": false,
  "context": "Track new top-level messages in #my-channel; reply when warranted.",
  "watches": [
    {"type": "slack_channel", "channel_id": "C012345ABCD", "reason": "monitor activity"}
  ]
}'
```

Note `allow_send: false`. Start there. The agent will draft instead of sending, and the drafts
wait for you in the dashboard. Turn it on per quest once you trust what it writes.

Things you can watch:

| Type | Watches for |
|---|---|
| `slack_thread` · `slack_channel` · `slack_dm` | replies · new posts · DMs |
| `slack_mention` | anyone @mentioning you, anywhere |
| `email` | a Gmail search query |
| `jira` · `github_pr` | an issue set changing · PR activity |
| `schedule` | a cron expression or a one-off time |
| `approval` | you reviewing one of its drafts |

---

## Living with it

Once installed, there is nothing to do. It ticks every 60 seconds.

```bash
./yaas-triage/ops/dashboard-start.sh     # the control panel, localhost:8877
```

That's where drafts wait for you, every quest's messages are one click from Slack, and a pulsing
pill appears when a worker is running — click it to watch the agent think in real time. Quests
that are stuck (misconfigured) or being throttled (rate limited) sort to the top with a badge,
and the **Config** tab shows the knobs that shape a tick — how many quests are checked at once
(the peak Slack ping rate), the dispatch and spend ceilings, the promotion thresholds — each as
its live value with its default.

When you want to look under the hood:

```bash
tail -f logs/triage.log                  # what the loop is deciding
tail -f logs/worker-latest.log           # what the agent is doing right now
python3 yaas-triage/ops/health-monitor.py            # anything silently stuck?
python3 yaas-triage/dispatch/spend-window.py state/run-log.ndjson | jq .   # what it's costing

python3 yaas-triage/tick.py              # run one tick now
DRY_RUN=1 VERBOSE=1 python3 yaas-triage/tick.py   # look, don't touch
```

**Nothing dispatching despite obvious activity?** Every gate says so in
`state/run-log.ndjson`:

| Event | Means |
|---|---|
| `gate_budget_exceeded` | you hit a spend ceiling; raise it or wait for the window to roll |
| `gate_slack_down` | Slack didn't answer, so Slack-dependent quests are waiting |
| `gate_dispatch_deferred` | too many at once; next tick |
| `gate_target_breaker_open` | one quest is hogging the hour |
| `gate_watch_misconfigured` | a watch has stopped being checked and needs you |
| `gate_watch_ratelimited` | Slack throttled a watch this tick; held, retries next tick (shows on the dashboard as "rate limited") |
| `gate_bad_env_knob` | a value in `.env` isn't a number, so a ceiling would silently be no ceiling |

**One gotcha:** editing `triage-loop.sh` or `dashboard-server.py` does nothing until that job
restarts, because launchd holds one long-lived process and neither bash nor Python re-reads a
running file. Edits to `tick.py` (and its `tick_*.py` imports) and the checkers apply on the very
next tick, because the loop re-invokes `tick.py` fresh each time.

```bash
launchctl kickstart -k "gui/$(id -u)/com.yaas.triage"
```

---

## Going away for a while

Turn it off, take a week off, come back. Nothing is lost — every watch holds its place, and
when it resumes it picks up from exactly where each watermark paused.

The trap in a backlog is staleness: a week-old question shouldn't be answered as though it just
arrived. That's handled at the one place it matters, the send path: **any reply to a conversation
that has been quiet for more than 24 hours is drafted to your approval queue instead of sent**
(`YAAS_STALE_REPLY_HOURS`, default 24). So a stale answer is always a human decision, never an
auto-send — while fresh activity keeps flowing normally, with no whole-system pause to release.

(There is also a manual `YAAS_FORCE_DRAFT=1` switch that drafts *every* reply for review,
regardless of age, when you want a fully hands-on stretch.)

---

## Pick your agent

```bash
YAAS_AGENT=claude   # default
YAAS_AGENT=codex
YAAS_AGENT=cursor
```

**Recommended default: Claude with Opus 5 at low thinking** — `YAAS_CLAUDE_MODEL=claude-opus-5`,
`YAAS_CLAUDE_EFFORT=low`. Triage work is bounded and well-scoped (each quest's `context.md`
carries the decision rules), so low thinking is fast and cheap while still running the most
capable model. Raise `YAAS_CLAUDE_EFFORT` per-install if your quests need deeper reasoning.

Only one file knows the difference (`dispatch/dispatch-agent.sh`); everything else is
backend-agnostic. Each harness reads its own rules file — `CLAUDE.md`, `AGENTS.md`, or
`.cursorrules` — and `setup.sh` can symlink them so you maintain one.

---

## What it costs

Idle ticks are free. You pay per dispatch, and dispatch cost scales with how big your
`CLAUDE.md` and quest context files are — the agent reads them every time.

Ceilings are on by default and live in `.env`: rolling spend windows, a per-tick fan-out cap,
and a per-quest hourly breaker. After a day of real use, run the `spend-window.py` command above
to see your own rate rather than guessing.

---

## Safety

- **The quest is the compartment.** The agent only ever acts inside one, so what it watches is
  auditable (list the active quests), why it acted is traceable (that quest's storyline), and a
  failure is contained to the quest it happened in. See [Everything is a quest](#everything-is-a-quest).
- **Nothing sends by surprise.** `allow_send: false` per quest, a 24-hour staleness guard, and
  an approval queue for anything held back.
- **Tokens live in the Keychain**, never in `.env` and never in the repo.
- **The agent cannot rewrite history.** It may append a new watch; any edit to an existing one
  is reverted automatically.
- **Everything is a file you can read**, and `logs/` plus `state/run-log.ndjson` record every
  decision, including the ones where it chose to do nothing.

The honest limits are listed in [ARCHITECTURE.md §13](ARCHITECTURE.md) rather than buried.
Worth reading before you hand it anything sensitive.

---

## For contributors

```
yaas-triage/
├── tick.py            one tick (the live orchestrator)
├── tick_state.py      config/loading   tick_check.py  the six-way verdict   tick_dispatch.py  the gates
├── checkers/          "is there anything new?"     one plugin per watch type
├── dispatch/          "run a worker"
├── ledger/            "owns a state file, atomically"
├── surfaces/          "talk to the outside"
├── ops/               "keep it alive and visible"
└── tests/             28 suites + 31 goldens + 9 mutations
```

Adding a watch type means dropping one file in `checkers/`, named for the type and **marked executable** (`chmod +x`) — triage resolves `checkers/<type>.py` and gates on the executable bit, so a non-executable checker is treated as missing and its watches are held as `misconfig` rather than silently skipped. Runtime requires neither `advance_to` nor `complete` — `complete` defaults to true and a missing `advance_to` falls back to a time-based guess — but emitting both is the safe contract, and the only way your checker can hold its own watermark when it could not drain the window. Omit them and you inherit a guess that is only correct for a source that cannot lag. Nothing else changes.

Before you send a patch: `yaas-triage/tests/run-all.sh`, then
`yaas-triage/tests/differential/run.sh check`. The second one runs a real tick against a
throwaway repo and compares the decisions to recorded goldens — it is how a refactor proves it
changed nothing it didn't mean to.

See [CONTRIBUTING.md](CONTRIBUTING.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[SECURITY.md](SECURITY.md).

---

## License

Apache 2.0. See [LICENSE](LICENSE).
