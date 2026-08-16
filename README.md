# Sidequestor <img src="assets/sidequestor-mark.png" width="35" height="35" alt="Sidequestor pixel robot mark">

> *Safely expose your local interactive agent setup as a service.*

**Sidequestor (YaaS, short for Yourself-as-a-Service)** keeps your local agent tending the work
you can't sit and watch: it turns the loose ends scattered across your channels into things your
own agent chases down while you're doing something else.

**It is Slack-centric.** It assumes most of your communication runs through Slack, so
that's where the most expressive usage is possible: react to a message with an
emoji and your agent picks that message up. Gmail, Jira, GitHub and cron plug in alongside, which
is what lets one quest follow a thing from the Slack message that started it through to the ticket
that closes it.

You already have an AI agent on your machine (Claude Code, Codex, or Cursor), sitting in the repo
you work in, waiting for you to type something.

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
that one thing, and writes down what happened.

When nothing moves, it costs **nothing**. The watching is shell and Python, about 150ms a tick.

**$0 idle. Tokens only on dispatch.**

## A day with it

Three quests running, two of them trusted to send. You are in meetings.

```
08:30  [brief]      schedule fires → SENDS you one DM: what moved
                    overnight, what's waiting on you

09:14  [bug-report] a customer reports an API error in the shared channel
                    → SENDS an ack, so nobody is left waiting
09:16  [bug-report] reproduces it against sandbox with the keys already
                    in your .env → it's real, and it isn't their code
09:18  [bug-report] files the ticket, reproduction attached, and SENDS
                    the link to the escalation channel

09:52  you react :writing_hand: from your phone to a question you keep
       skipping → no quest needed. DRAFT in the compose box

10:31  [migration]  a PR review comment contradicts the quest's own
                    notes → doesn't guess. Flags it and holds

11:02  nothing moved. No dispatch, no tokens. This is most ticks

14:47  [bug-report] a second report of the same thing → SENDS a pointer
                    to the open ticket instead of filing a duplicate

16:20  someone revives a nine-day-dead thread → DRAFT, not sent.
       Anything quiet over 24h needs a human
```

Four things went out without you. Two are waiting for you when you sit down: you approve one and
rewrite the other.

- **09:16 is the whole point.** Your agent has your keys, CLIs and repo, so it can *test* a claim
  rather than relay it.
- **Sending is per quest.** `bug-report` is trusted to acknowledge and escalate on its own, so it
  does. A quest you haven't trusted yet drafts instead. You choose, one quest at a time.
- **The bracketed tags are quests**, sealed off from each other. `bug-report` cannot touch the
  migration PR.

## Install with your agent

The agent installs itself. The two blocks below are **prompts, not shell commands**: start
`claude` (or `codex` / `cursor-agent`), and paste one of them at the chat prompt as you would any
other request. Don't run them in a terminal.

**Prompt 1, into a fresh folder** (start your agent anywhere, it makes the directory):

```text
Install https://github.com/circlefin/sidequestor.git into ./sidequestor. Follow its README's
fresh-installation path, and stop when human approval is required.
```

**Prompt 2, into an existing repository** (start your agent *inside that repository* first):

```text
Attach https://github.com/circlefin/sidequestor.git to this repository. Follow its README's
existing-repository contract exactly, preserve all existing work, and stop on any unsafe collision.
```

Both prompts stop and ask you before Slack authorization and before background jobs are installed.
Prefer the fresh folder unless Sidequestor has to live inside an existing repository. Manual
route and prerequisites: [Pack the kit](#pack-the-kit).

> **macOS only.** It runs on `launchd` and keeps its Slack token in the Keychain, so it will not
> run as-is on Linux or Windows. Early project, no support commitment.

Sidequestor is built for solutions engineers, architects, PMs, support and ops leads, researchers,
and anyone whose work crosses too many channels. If your day means following Slack threads, email,
Jira and GitHub; testing a claim or an integration; triaging what changed; verifying a fix really
landed; and remembering who still owes whom a reply, you are the target user. Sidequestor is the
open-source YaaS runtime.

---

## Field Note 01: Proof

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

## Field Note 02: Every Goal Becomes a Quest

The quest is the safety harness. An agent told to "watch my Slack and help out" is a blob you
cannot audit: you never quite know what it is tracking, why it acted, or whether it is about to
act on something it misread.

**A quest is a compartment.** One durable goal, with its own watchers, decisions, follow-ups,
send permission, and history. The agent only ever acts inside a compartment, never on the world
at large. That buys three things:

- **You always know what it's doing.** The active quests *are* the complete list of what it is
  paying attention to. To audit the whole system, you list one directory.
- **You always know which storyline it's following.** Each quest's `context.md` is its script and
  its `timeline.ndjson` is everything it has ever done. When it replies, you can trace exactly
  which quest drove it and why.
- **A mistake stays put.** Permission to send is per quest. A broken watch, a stale thread, a
  rate-limit: each is contained and surfaced by name on the dashboard instead of failing silently.

However large the mission gets, its durable representation is a folder with four files:

```
state/quests/active/watch-my-channel/
├── context.md        the storyline: why this exists, and how to decide   ← the agent reads this first
├── meta.json         status, priority, may-it-send
├── watch.json        where to look, and how far it has got (the watermark)
└── timeline.ndjson   everything it has ever done on this quest
```

No database. No migrations. You can read your agent's entire memory with `cat`.

---

## Pack the Kit

| Tool | Why | Get it |
|---|---|---|
| `claude` CLI | the default agent | https://docs.claude.com/en/docs/claude-code |
| `codex` / `cursor-agent` | only if you prefer those | `brew install codex` · Cursor CLI installer |
| `jq` ≥ 1.6 | the orchestrator speaks JSON | `brew install jq` |
| `python3` ≥ 3.9 | everything else | macOS system Python is fine |
| `perl` | file locking | already on your Mac |
| `gws` CLI | only for Gmail / Drive watches | Google Workspace CLI |
| `node` | only for the Coda MCP | `brew install node` |

Python 3.9 is a hard floor (`zoneinfo`, `str.removeprefix`). On anything older the first symptom
is a confusing `TypeError` mid-dispatch, so `doctor.sh` checks the version explicitly and says so.

### What the install prompt does

You paste the prompt; the agent does the work. It clones, creates `.env` and your agent-rules
file, prints the Slack manifest, runs setup, installs the background jobs, and verifies the whole
thing with the doctor, the health monitor, and the test suite. It stops and asks before Slack
authorization, before installing background jobs, and before any commit.

Four things it cannot do for you. They are the whole of your install:

**1. Create the Slack app.** Sidequestor ships no shared Marketplace app on purpose: the polling
loop needs a credential it can use without waking a model, and that credential should belong to
you rather than to a vendor. So the app has to exist in **your** workspace first.

Your agent prints a manifest with every scope and the redirect URL already filled in. Take it to
<https://api.slack.com/apps> → **Create New App** → **From an app manifest**, pick your workspace,
install it, and paste back the `App ID` and `Client ID` from Basic Information.

Budget more than two minutes if your workspace restricts app installs. `reactions:read` and
`reactions:write` are the scopes most often held for admin review; without them the reaction
triggers do nothing. Grant them later and ask your agent to re-authorize.

**2. Approve the OAuth.** A browser window, once. PKCE, no client secret. The token lands in your
Keychain, never in the repo.

**3. Say what goes in `.env` beyond Slack.** Your sender identity for email replies, reaction emoji
overrides, and the spend ceilings. Everything except the four `SLACK_*` values has a default, and
your agent will walk you through the ones worth changing. Worth a glance at the ceilings.

**4. Put your voice in the agent-rules file.** `CLAUDE.md` for Claude, `AGENTS.md` for Codex, or
the addendum merged into your harness's own project-rules file. Who your agent is, how it writes,
what it must never do. The Sidequestor protocol sections stay untouched, since the orchestrator
has a contract with them, but everything above them is yours.

> Already have a `CLAUDE.md` in this repo for your own interactive use? That's the point. The
> sleeve reads the same file your agent already reads.

Say yes when it offers all three background jobs. Triage is the loop, the heartbeat watches the
loop, and the dashboard is where you'll actually live.

The heartbeat is a **separate launchd job** on purpose. A check running inside the triage loop
cannot detect the loop being dead. Not theoretical: one outage ran 6.5 hours because nothing was
watching the watcher.

### Pin the dashboard

Your agent opens `localhost:8877` at the end of the install. Do one thing by hand: **pin it to
your Dock as an app.** This is the change that makes Sidequestor feel like software instead of a
script, because drafts are worthless if you don't see them.

- **Chrome / Edge / Arc:** open `localhost:8877` → menu → *Cast, Save and Share* → **Install page
  as app** (Arc: *Add to Dock*). It gets its own window and its own Dock icon, with no tab strip
  and no URL bar.
- **Safari:** *File* → **Add to Dock**.
- Then right-click the Dock icon → *Options* → **Keep in Dock**, and **Open at Login** if you took
  the dashboard launchd job.

You want one click between "an agent drafted something" and "I approved it." A pinned window
gets you there; a tab lost in a window of forty does not.

### Check the compass

The install ends with three separate questions answered, and your agent should report all three:
**is this machine set up** (the doctor), **is it working right now** (the health monitor), and
**is the code correct** (the test suite). Expect `All checks passed`, `healthy`, and a green
suite.

If you ever want to re-check later, "is my Sidequestor healthy?" is the whole command. The tests
use throwaway fixtures and never touch your real state, so they're safe to run whenever.

### Existing-repository contract

The attach prompt above delegates to these rules. They are stricter than the fresh path because
the repository already belongs to someone. There is deliberately no blind one-liner: runtime,
agent rules, state, and working directory share one repository root, and a naive extraction can
clobber an existing `CLAUDE.md`, `.env.example`, or README.

- Record the repository root and Git status, clone into a temp directory, and inventory incoming
  paths before writing anything.
- Preserve every existing file, modification, untracked file, instruction, and Git history. Never
  stash, reset, clean, delete, rename, or silently overwrite.
- Copy missing runtime files with executable modes intact. At collisions, merge only the minimum
  Sidequestor requirement, including one bounded protocol block in the existing agent rules.
- Add only missing `.env` and `.gitignore` entries. Stop when required runtime content is
  genuinely incompatible.
- Keep the existing `.git` as the only writable history. Template sync stays available, through
  the separate `.git-yaas-v2` git-dir described below, and must never write to the host `.git`.
- Show the complete diff and run the tests and doctor before asking approval for OAuth,
  background jobs, sending, or a commit.

### Staying in sync (both trails)

`setup.sh` offers daily auto-sync from the upstream template. **Say yes even inside an existing
repository.** Attaching Sidequestor to a repo you already own does not condemn you to a frozen
copy.

It works because the sync never touches your repository's history. Sidequestor keeps its own
git-dir, `.git-yaas-v2`, pointed at the same working tree:

```
your-repo/
├── .git             ← yours. Your commits, your branches. Sync never writes here.
├── .git-yaas-v2/    ← Sidequestor's. Tracks the upstream template, read-only.
├── yaas-triage/     ← the runtime, tracked by BOTH
└── src/             ← your work, tracked only by yours
```

Two git-dirs, one worktree. `.git-yaas-v2` tracks only the files the template ships, so
`sync-yaas-v2.sh` can pull runtime updates while your own history sees nothing but the resulting
file changes, to stage or discard as you like.

Four properties make it safe to leave on:

- **Opt-in.** Gated on `settings.json` → `sync.yaas_v2_auto_pull`, off until you say yes.
- **Fast-forward only.** A diverged history stops the sync instead of auto-merging.
- **Your edits win.** If any template-shipped file has uncommitted local changes, the pull is
  skipped for the day rather than clobbering them. Customize whatever you want; you just sync
  those files manually when you're ready.
- **It never checks out.** The setup uses `read-tree` to populate an index without writing to the
  worktree, so wiring it up cannot overwrite a file. Outcome lands in
  `state/yaas-v2-sync-status.json`, so you can check it without reading logs.

`.git-yaas-v2/` is gitignored, which matters most here: it's a git object store, not project
content, and an existing repo's `git add -A` would otherwise commit the whole thing into your
history.

---

## Set Out: Your First Quest

Tell your agent. In plain language, in the same session you already use:

```text
Make me a quest that watches #my-channel for new messages. Don't let it send anything yet.
```

It knows where quest creation lives, so it interviews you for anything missing, resolves the
channel ID itself, scaffolds the four files, and tells you what it made. You never hand-write a
watch or look up an ID.

Describing the *outcome* is enough. All of these are complete instructions:

```text
Track that customer bug thread in the shared channel. Reproduce anything they report
against sandbox before escalating, and file the ticket if it's real.
```
```text
Watch for emails from the design review alias and summarise them for me each morning.
```
```text
Follow PRs on the payments repo and tell me when one's been sitting unreviewed for two days.
```

Notice the first one has no watch types in it. That's the point: the agent picks them.

**Start with sending off.** Your agent should set `allow_send: false` on a new quest by default,
so it drafts instead of sends and the drafts wait in the dashboard. Ask it to turn sending on for
one quest once you trust what that quest writes.

Things you can watch:

| Type | Watches for |
|---|---|
| `slack_thread` · `slack_channel` · `slack_dm` | replies · new posts · DMs |
| `slack_mention` | anyone @mentioning you, anywhere |
| `email` | a Gmail search query |
| `jira` · `github_pr` · `github_issue` | an issue set changing · PR activity · issue activity |
| `schedule` | a cron expression or a one-off time |
| `approval` | you reviewing one of its drafts |

---

## The Other Way In: React to a Message

A quest is for something ongoing. Most of the time you just want *this one message* handled, and
you are already looking at it in Slack. So point at it with an emoji.

React with 🤖 `:claude-intensifies:` on any Slack message and your agent picks it up on the next
tick: reads the thread, researches the question, and replies in-thread. No quest, no setup, no
switching to your terminal. It works on any message you can see, in any channel you're in.

Four reactions, four behaviours:

| React with | Your agent |
|---|---|
| `:claude-intensifies:` | researches the thread and **replies** in it |
| `:writing_hand:` | researches and leaves a **draft** in the compose box, for you to edit and send |
| `:floppy_disk:` | **saves** the context silently, so a later question has it. No reply |
| `:incoming_envelope:` | **adopts** the message into whichever quest already watches that channel |

You can watch it work. The reaction you added swaps to a loading emoji when it picks the message
up, then to a done emoji when it has finished, so the message itself is the progress bar. If it
gets stuck, the emoji stays on loading rather than lying to you.

`:writing_hand:` is the one to start with, and the honest recommendation for anything customer
facing. You get the research done for you and still write the last word yourself.

> **Try this now:** find a Slack question you've been putting off, react `:writing_hand:`, and go
> get a coffee. The draft will be in the compose box when you come back.

All four emoji are configurable in `.env` (`YAAS_REACTION_*_EMOJI`) if these clash with something
your workspace already uses. `:claude-intensifies:` and the loading/done emoji are custom ones you
may need to add to your workspace first; your agent can tell you which are missing.

---

## Read the Map

Once installed, there is nothing to do. It ticks every 60 seconds.

The pinned dashboard at `localhost:8877` is the whole interface. That's where drafts wait for you and every quest's messages are one click from Slack. A pulsing
pill appears when a worker is running: click it to watch the agent think in real time. Stuck
(misconfigured) or throttled (rate limited) quests sort to the top with a badge. **Control** is
the live map, **Audit** keeps prior approvals and dispatch runs, and **Field guide** in the header
explains quests and Slack reaction spells.

The dashboard is the map. The files remain the truth, and your agent is the one that reads them.

For anything the dashboard doesn't show, ask. Your agent knows the ops surface, so questions work
better than commands:

```text
Is anything stuck?                     What has this cost me today?
Why hasn't the customer quest fired?   Show me what the loop decided in the last hour.
Run one tick now so I can watch it.    Pause the payments quest until Monday.
```

It picks the right tool: the health monitor, the spend window, `logs/triage.log`,
`logs/worker-latest.log`, or a single dry-run tick that looks without touching. You don't need to
remember which.

**Nothing dispatching despite obvious activity?** Ask your agent why, and it will read the gate
events from `state/run-log.ndjson`. These are what it will find:

| Event | Means |
|---|---|
| `gate_budget_exceeded` | you hit a spend ceiling; raise it or wait for the window to roll |
| `gate_slack_down` | Slack didn't answer, so Slack-dependent quests are waiting |
| `gate_dispatch_deferred` | too many at once; next tick |
| `gate_target_breaker_open` | one quest is hogging the hour |
| `gate_watch_misconfigured` | a watch has stopped being checked and needs you |
| `gate_watch_ratelimited` | Slack throttled it this tick; held, retries next tick |
| `gate_bad_env_knob` | a value in `.env` isn't a number, so a ceiling would silently be none |

**One gotcha, if you start editing the runtime:** changes to `triage-loop.sh` or
`dashboard-server.py` do nothing until that launchd job restarts, because launchd holds
long-lived processes and neither bash nor Python re-reads a running file. Tell your agent you
edited one and it will kick the right job: `com.yaas.triage` for `triage-loop.sh`,
`com.yaas.dashboard` for `dashboard-server.py`. Edits to `tick.py` (and its `tick_*.py` imports)
and to the checkers apply on the next tick, because the loop invokes `tick.py` fresh each time.

---

## Leave Camp for a While

Turn it off, take a week off, come back. Nothing is lost. Every watch holds its place and resumes
from exactly where its watermark paused.

The trap in a backlog is staleness: a week-old question shouldn't be answered as though it just
arrived. That's handled at the one place it matters, the send path. **Any reply to a conversation
quiet for more than 24 hours is drafted to your approval queue instead of sent**
(`YAAS_STALE_REPLY_HOURS`, default 24). A stale answer is always a human decision, never an
auto-send, while fresh activity keeps flowing with no whole-system pause to release.

There is also `YAAS_FORCE_DRAFT=1`, which drafts *every* reply regardless of age, for when you
want a fully hands-on stretch.

---

## Pick Your Agent

One value in `.env` (or ask your agent to switch it):

```ini
YAAS_AGENT=claude   # default
YAAS_AGENT=codex
YAAS_AGENT=cursor
```

**Recommended: Claude with Opus 5 at low thinking** (`YAAS_CLAUDE_MODEL=claude-opus-5`,
`YAAS_CLAUDE_EFFORT=low`). Triage work is bounded and well-scoped, since each quest's `context.md`
carries the decision rules, so low thinking is fast and cheap on the most capable model. Raise the
effort if your quests need deeper reasoning.

Exactly one file knows the difference (`dispatch/dispatch-agent.sh`); everything else is
backend-agnostic. Each harness reads its own rules file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, or
Cursor's project rules), so keep the Sidequestor addendum in whichever one your harness loads.

---

## Rations and Cost

Idle ticks are free. You pay per dispatch, and dispatch cost scales with how big your `CLAUDE.md`
and quest context files are, because the agent reads them every time.

Ceilings are on by default and live in `.env`: rolling spend windows, a per-tick fan-out cap, and
a per-quest hourly breaker. After a day of real use, ask your agent what it has cost you, and set
the ceilings from your own rate instead of guessing.

---

## Trail Rules

- **The quest is the compartment.** What it watches is auditable, why it acted is traceable, and
  a failure is contained to the quest it happened in.
- **Nothing sends by surprise.** `allow_send: false` per quest, a 24-hour staleness guard, and an
  approval queue for anything held back.
- **Tokens live in the Keychain**, never in `.env` and never in the repo.
- **The agent cannot rewrite history.** It may append a new watch; any edit to an existing one is
  reverted automatically.
- **Everything is a file you can read**, and `logs/` plus `state/run-log.ndjson` record every
  decision, including the ones where it chose to do nothing.

The honest limits are listed in [ARCHITECTURE.md §13](ARCHITECTURE.md) rather than buried. Worth
reading before you hand it anything sensitive.

---

## Cartographer's Notes

```
yaas-triage/
├── tick.py            one tick (the live orchestrator)
├── tick_state.py      config/loading   tick_check.py  the six-way verdict   tick_dispatch.py  the gates
├── checkers/          "is there anything new?"     one plugin per watch type
├── dispatch/          "run a worker"
├── ledger/            "owns a state file, atomically"
├── surfaces/          "talk to the outside"
├── ops/               "keep it alive and visible"
└── tests/             unit + behaviour suites, 29 goldens, 12 mutations
```

Adding a watch type takes two files: an executable `checkers/<type>.py` and a
`checkers/<type>.watch.json` manifest. Triage discovers only valid pairs; a missing or
non-executable checker is held as `misconfig` rather than silently skipped. Add an optional
`checkers/<type>.lag` only when the source needs a watermark delay. Follow
`yaas-triage/skills/yaas-checker-authoring/SKILL.md` for the result contract, behavior fixture,
and documentation checks.

Before you send a patch: `yaas-triage/tests/run-all.sh`, then
`yaas-triage/tests/differential/mutations.sh`. The goldens run a real tick against a throwaway
repo and compare the decisions to recorded output, which is how a refactor proves it changed
nothing it didn't mean to. The mutation suite is the one `run-all.sh` leaves out (it takes ~2
min): it breaks the orchestrator on purpose and fails if the goldens *don't* notice, which is what
stops the goldens quietly decaying into a suite that passes no matter what you do.

See [CONTRIBUTING.md](CONTRIBUTING.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[SECURITY.md](SECURITY.md).

---

## License

Apache 2.0. See [LICENSE](LICENSE).
