# Sidequestor Architecture

Sidequestor watches the places you get asked things, and wakes an AI agent only when something
actually changed. The whole design exists to answer one question safely:

> **"Has this been dealt with, or does it just look like it has?"**

Everything below is downstream of that.

---

## 1. The shape of it

```
        ┌─────────────────────────────────────────────────────────┐
        │  launchd  ·  three long-lived jobs, restarted on death  │
        └───────┬─────────────────────┬───────────────────┬───────┘
                │                     │                   │
          triage-loop.sh        heartbeat-loop.sh    dashboard-server.py
          (every ~60s)          (watches the         (localhost UI,
                │                watcher)             approval queue)
                ▼
        ┌───────────────┐
        │   tick.py     │   ONE TICK
        │               │
        │  1. CHECK ────┼──▶ checkers/<type>.py, one per watch, in parallel
        │               │    "is there anything new?"  → one of six outcomes
        │  2. DECIDE ───┼──▶ gates: cost, Slack health, backoff
        │  3. DISPATCH ─┼──▶ one AI worker per dirty target, isolated
        │  4. COMMIT ───┼──▶ move watermarks — but ONLY on evidence (§4)
        └───────────────┘
                │
                ▼
        state/quests/active/<quest>/   ← the durable truth
```

**No LLM runs in steps 1 or 2.** Checking is shell and Python, roughly 150ms when idle. An
agent costs money, so one is woken only for a target with genuinely new activity.

### Why Slack polling has its own workspace app

Slack is used in two different execution planes:

```
   POLLING PLANE                              DISPATCH PLANE
   tick.py + deterministic checkers           Claude / Codex / Cursor
   runs every ~60s                            runs only after a dirty watch
   must cost no model tokens                  uses Sidequestor's named Slack surface
```

A Slack credential identifies two parties, not one: the person granting access and the app
asking for it. Slack binds the token to that user, app, workspace, and approved scope set. There
is no free-standing "Guangmian's Slack password" that any local program may reuse.

Native agent connectors already have suitable tokens, but those tokens belong to the Claude,
Codex, or Cursor Slack app. Each agent keeps its credential and refresh lifecycle inside its own
runtime. None currently exposes a supported raw Slack tool-call interface that `tick.py` can use
without invoking a model. Reverse-engineering the credential store would couple polling to one
vendor's private storage format, make Sidequestor appear as that vendor in Slack's audit trail,
and break as soon as storage or token refresh changes. The worker therefore uses the same local
Sidequestor identity through `surfaces/mcp-call.sh` and `surfaces/slack-send.py`; native Slack
plugins are not the canonical send path.

Sidequestor therefore asks each workspace to create or approve its own internal Slack app. Local
OAuth uses PKCE, stores the resulting user token in the operating-system Keychain, and gives the
Python checkers a supported credential they can use directly. The app is the workspace-approved
identity; the token is the local key that lets the no-LLM polling plane act as that identity.

The alternative is one published Sidequestor Marketplace app shared by every workspace. That
would reduce setup to an install-and-approve flow, but it would make every installation depend on
a centrally owned app identity, its publication status, and its ongoing support. A hosted OAuth
or MCP service would add central token custody and uptime as well. Sidequestor intentionally
chooses repeated local setup over those shared dependencies for now.

This decision should be revisited if workspace-app setup becomes the main adoption barrier, if
Sidequestor adopts a managed service, or if Slack or agent vendors expose a supported way for
local deterministic clients to reuse native connector authorization without invoking a model.

---

## 2. The one idea that matters: the watermark

Every watch remembers `last_checked_ts` — "I have dealt with everything up to here."

```
        watermark                                          now
            │                                               │
   ─────────┼───────────── the gap ─────────────────────────▶│
            │                                               │
       processed          NEW, unprocessed
```

Moving that mark forward is the only irreversible act in the system. Move it too early and a
message is buried **silently** — no error, no retry, no trace. Every mechanism below exists to
make that impossible.

**Who may move it:** the orchestrator (`tick.py`), and only the orchestrator. A worker may
*append* new watches but never edit an existing one, and `ledger/watch-guard.py` reverts it if
it tries.

---

## 3. Prefix, not suffix

The least obvious constraint here. Two separate outages came from getting it wrong.

Most APIs return the **newest** N items, which is the wrong end of the gap:

```
   watermark                                              now
       │                                                   │
       ├──── 250 unread, NEVER FETCHED ────┼── newest 50 ───┤
                                           ▲
                        a SUFFIX. The unread part sits directly
                        above the watermark, so the mark can
                        never move. Same 50 next tick. Forever.
```

Bound **both** ends and you get something the watermark can cross:

```
   watermark        +slice                                now
       │              │                                    │
       ├─ all 50 ─────┤────── still unread, but ABOVE ─────┤
       └── a PREFIX ──┘   → commit to here; backlog shrinks
```

| Source | How it gets a prefix |
|---|---|
| Slack | `slack_utils.drain()` — bounded window, forward slice, sized from observed density |
| GitHub | `gh search prs --order asc` + `updated:>=<watermark>` |
| Jira | `updated >= "..." ORDER BY updated ASC` |

**`complete` means "everything up to `advance_to` has been seen"** — *not* "the whole gap is
done". A full page is still a committable prefix. Reporting `complete: false` on a prefix holds
the watermark and recreates the livelock, with code that looks perfectly correct.

### The tie trap

Ascending order alone is **not** enough:

```
   a capped page ending mid-tie:
       … 13:00 │ 14:00 │ 14:00 │ ← page full here
                        ▲
        advancing to 14:00 makes the next run filter "> 14:00"
        and permanently skip any unseen rows ALSO at 14:00
```

Timestamps have no tiebreak, so on a **capped** page the only provable boundary is strictly
*below* the final row's timestamp. If every new row shares that timestamp, nothing is provable
and the checker returns `hold`. An *uncapped* page may advance onto a tie, because nothing more
exists.

---

## 4. Evidence-based commit

`claude -p` exits 0 whether it did the work or not. So the exit code decides nothing.

```
   dispatch ──▶ worker ──▶ worker closes each item in the ACK LEDGER
                                    │
                                    ├─ handled        → may advance
                                    ├─ nothing_to_do  → may advance
                                    ├─ blocked        → HOLD
                                    └─ (never acked)  → HOLD
```

A watermark moves only when **all three** hold:

```
   ┌────────────────────────────────────────────────────┐
   │  1. the item was dispatched this tick              │
   │  2. the worker closed it handled / nothing_to_do   │
   │  3. the checker proved it drained its window       │
   └────────────────────────────────────────────────────┘
                    ALL THREE, or the mark stays put
```

**One target, one invocation, one commit.** Previously a single agent run carried every dirty
quest, so one exit 0 committed all of them — a worker that handled quest A, errored on B and
never opened C still advanced all three. Now B's failure cannot touch A or C.

**Acks are written as work completes, not batched at the end.** If the watchdog kills the run
at 30 minutes, whatever was banked still commits and the rest correctly re-surfaces.

---

## 5. Where the guards sit

```
   TICK START
     │
     ├─ single-instance lock ......... only one tick at a time
     ├─ env knob validation .......... a malformed ceiling reads as NO ceiling → refuse to run
     ├─ CHECK all watches (parallel)
     │    └─ checker error → exponential backoff 60s → 1h, then misconfig
     │
     ├─ advance clean watermarks
     ├─ retire + prune ............... stale threads, done approvals, fired schedules, logs
     │
     ├─ spend ceilings ............... rolling windows + per-target hourly cap
     ├─ Slack health gate ............ PER TARGET: an email quest still runs during an outage
     ├─ fairness rotation ............ sorted list + persisted cursor, so no quest starves
     │
     ├─ DISPATCH per target .......... watchdog, process-tree kill
     └─ COMMIT per target ............ the three conditions above
```

Everything that *can* hold a watermark **holds** it. Nothing skips forward on doubt.

The dispatch list is **sorted** before rotating. Quests are checked in parallel, so the arrival
order is nondeterministic — rotating a random list shuffles rather than distributes.

---

## 6. Watch types

One plugin per type, discovered from an executable `checkers/<type>.py` paired with a required
`checkers/<type>.watch.json` manifest. The manifest declares validation, identity, creation, and
upstream metadata. Add `<type>.lag` only when the source needs a watermark delay, then update the
checker behavior fixture and documented watch surfaces described by the checker-authoring skill.

| Type | Fires when |
|---|---|
| `slack_thread` | a new reply in a thread |
| `slack_channel` | a new top-level message in a channel |
| `slack_dm` | a new DM from a watched person |
| `slack_mention` | you are @mentioned anywhere Slack search can see |
| `email` | a Gmail query matches something new |
| `jira` | an issue in a JQL set changed |
| `github_pr` | PR activity — a PR review does **not** bump the linked Jira, hence both |
| `github_issue` | issue activity in a repo (create, comment, label, close); excludes PRs |
| `schedule` | a cron expression or a one-shot time came due |
| `approval` | a human reviewed a queued draft, or a dashboard instruction is ready |

Every one returns the same contract:

```json
{"outcome":"dirty","count":3,"preview":"…","advance_to":"1785920000.0","complete":true}
```

`outcome` ∈ `clean` · `dirty` · `hold` · `error` · `ratelimited` · `misconfig`

---

## 7. Nothing sends itself by accident

```
                      worker wants to reply
                               │
                     ┌─────────┴──────────┐
                     │  allow_send=false? │──── yes ──▶ approval queue
                     └─────────┬──────────┘
                               │ no
                     ┌─────────┴───────────────────┐
                     │  target thread quiet >24h?  │── yes ──▶ approval queue
                     └─────────┬───────────────────┘
                               │ no
                     ┌─────────┴──────────┐
                     │  thread unreadable?│──── yes ──▶ approval queue  (fails CLOSED)
                     └─────────┬──────────┘
                               │ no
                               ▼
                             SEND
```

Dashboard instructions use the same durable approval ledger but skip the human-review state:
the operator already authorized the instruction by submitting it. The dashboard writes a
`manual_instruction` item as `reviewed`; the next tick, while holding the global triage lock,
arms its approval watch and dispatches it through the normal ack and lease lifecycle. Each
submission has its own generated approval id. If an execution lease expires, the item is
reclaimed with `needs_reconcile: true`: the next worker must inspect the external target before
doing anything. A proven prior action is closed without replay; a proven absence may be executed;
an unknowable or arbitrary `manual_instruction` is abandoned rather than run twice.

The 24h rule matters most. After any pause the checkers hand the worker the **oldest** unread
slice first (§3), so without it the agent answers a week-old question and then walks forward
through the backlog, each reply blind to everything that followed it.

It lives in `surfaces/slack-send.py`, the one sanctioned send path — not in a prompt.
**A rule the model can forget is not a guard.**

---

## 8. Coming back after a long silence

There is **no whole-system hold**. After any pause each watch simply resumes from its own
watermark, and staleness is handled at the one place it matters — the send path (§7): any reply
to a conversation quiet for more than `YAAS_STALE_REPLY_HOURS` (default 24) is drafted to the
review queue instead of sent. So a stale answer is always a human decision, while fresh activity
keeps flowing with nothing to release.

(An earlier design held the *entire* tick after N hours of silence and waited for a manual
`release`. It was removed in 2026-08 because it fired on ordinary operator pauses and laptop
sleeps, not just real outages, blocking fresh activity for no benefit the 24h draft guard didn't
already provide.)

---

## 9. Quests are just four files

```
state/quests/active/<quest_id>/
├── context.md        why this exists, and the decision rules   (worker reads FIRST)
├── meta.json         status, priority, allow_send
├── watch.json        the watermarks       ← tick.py owns; a worker may only APPEND
└── timeline.ndjson   append-only log of every action taken (written by the surfaces,
                       never by hand: the worker has no clock to stamp `ts` with)
```

Lifecycle: `active/` → `completed/` or `archived/`. A quest is a folder. No database, no schema
migration.

Workspace-level briefings are separate from quests:

```
state/briefs/
└── *.md                       free-form Markdown briefing names
```

`state/briefs/` is the canonical briefing store. The dashboard reads and renders these files;
sorts them newest-first by filesystem creation time; and treats cadence words in filenames only as
optional display hints. Slack posts are delivery copies. A briefing does not belong in a quest
folder because it may summarize several quests and other workspace activity.

---

## 10. The file map

Grouped by **the question each part answers**.

```
yaas-triage/
├── tick.py                one tick — the live orchestrator
│   ├── tick_state.py         config/loading (repo root, knobs, lag map, quests)
│   ├── tick_check.py         the six-way per-watch verdict (classify)
│   └── tick_dispatch.py      the dispatch gates (slack-need, budget/fanout/slice)
├── triage-loop.sh         the launchd wrapper that sleeps between ticks (runs tick.py)
│
├── checkers/       "IS THERE ANYTHING NEW?"    one plugin per watch type
│   ├── result.py                 the contract every checker returns
│   ├── slack_utils.py            drain(): bound both ends of the gap
│   └── <type>.py, <type>.watch.json, [<type>.lag]
│                                executable checker + manifest + optional watermark lag
│
├── dispatch/       "RUN A WORKER"
│   ├── run-agent.py              one invocation: watchdog, log tee, kill tree
│   ├── dispatch-agent.sh         backend-agnostic (claude / codex / cursor)
│   ├── manual-dispatch.sh        legacy direct-run utility; dashboard instructions use the approval queue
│   ├── format|translate-stream   event stream → transcript / common facts
│   ├── extract-tokens.py         cost and token counts
│   ├── slack-read-health.py      did worker Slack access recover after an outage?
│   └── spend-window.py           rolling spend; evaluates the ceilings
│
├── ledger/         "OWNS A STATE FILE, ATOMICALLY"   (lock, fsync, os.replace)
│   ├── ack-watch.py              the ack ledger: the commit evidence
│   ├── add-watch.py              the only way to add a watch; append-only
│   ├── watch-guard.py            reverts any edit to an existing watch
│   ├── ensure-watch-ids.py       backfills ids onto older entries
│   ├── approval-helper.py        the human review queue
│   └── checker-health.py         per-watch exponential backoff
│
├── surfaces/       "TALK TO THE OUTSIDE"
│   ├── client.py                 the ONE HTTP client: Slack MCP, Slack Web, Jira
│   ├── mcp|jira|react .sh        named doors the worker knocks on → client.py
│   ├── slack-send.py             send/draft + log the body + the stale-reply guard
│   └── log-event.py              append any other timeline entry, stamped by the clock
│
├── ops/            "KEEP IT ALIVE AND VISIBLE"
│   ├── health-monitor.py         the dead-man switch, runs OUTSIDE triage
│   ├── heartbeat-loop.sh         its own launchd job
│   ├── doctor.sh                 is THIS MACHINE configured?
│   ├── notify.py                 desktop notifications
│   ├── rotate-logs.py            daily rotation of the append-only files
│   └── dashboard-server|start    the localhost UI
│
├── setup/  skills/  tests/
```

Two pairings that look wrong and are not:

- **`checkers/approval.py` vs `ledger/approval-helper.py`** — the checker asks "has the human
  reviewed this yet?"; the helper owns the file. The checker must stay in `checkers/`, because
  triage resolves it at runtime from the watch's `type`.
- **`dispatch/spend-window.py` is not in `ledger/`** — it only *reads* the run log to decide
  whether to dispatch. It owns no file, so it fails the `ledger/` rule.

**Every writer of a shared file must agree on one lock.** `pending-approvals.json` is written
by three different processes, and because the writes replace the inode, the lock is a **sidecar**
file — a lock on the data file would be a lock on a soon-to-be-deleted inode and would exclude
nobody.

**The repo root** is found by walking up to the directory containing `yaas-triage/`. Never by
counting `..`, which breaks the moment a file moves.

---

## 11. Watching the watcher

If the loop dies, the agent goes quiet and *looks* idle. So a separate launchd job checks:

| Condition | Means |
|---|---|
| `triage_stalled` | no tick completed recently — the loop is dead |
| `tick_hung` | a tick started and never finished |
| `tick_failures` | consecutive non-zero exits |
| `checker_stuck` | a watch was promoted to `misconfig` |
| `approval_stuck` | a reviewed draft is stuck mid-execution |
| `state_unreadable` | `last-run.json` missing or corrupt |

The verdict goes to `state/health-status.json`, so the dashboard and `doctor.sh` show the same
answer. Notifications de-duplicate, so a persistent fault does not shout every five minutes.
The dashboard is intentionally a map over these files, not a second source of truth: it reads
quest state and timelines, and routes edits, reviews, and instructions through the same locking
helpers used by the worker.

**Three tools, three different questions:**

```
  tests/run-all.sh       is the CODE correct?      (fixtures; passes on an unconfigured box)
  ops/doctor.sh          is this MACHINE set up?   (real Keychain, PATH, .env, plist)
  ops/health-monitor.py  is it WORKING right now?  (runtime, continuous)
```

---

## 12. How this is tested

```
   test suites ─┬─ tests/unit/        mirrors the source tree 1:1
                └─ tests/behaviour/   named by FAILURE CLASS, may span files

   29 goldens ── tests/differential/   a REAL tick against a throwaway repo,
                     │                  run against tick.py
                     └─ 12 mutations ── break the orchestrator on purpose,
                                       assert the goldens NOTICE
```

The differential harness builds a fixture repo, stubs the four external seams — checkers, agent,
Slack, notifier — then runs a real tick and reduces it to a **time-independent verdict**: did
each watermark *hold*, *advance to now*, or *advance to the checker's boundary*?

`tests/coverage.sh` lists every source file with no unit test. Exemptions need a stated reason,
because a bare allowlist becomes the place gaps hide.

Three rules keep this honest:

1. **Re-recording a golden is a deliberate act, not a fix.** If a check fails, assume the code
   regressed. Re-recording makes the failure vanish without fixing anything.
2. **Mutations only mean something against a green baseline.** Run on a broken harness they
   report "caught" for everything and prove nothing.
3. **A flaky golden is a bug in the test, not noise to tolerate.** One flaky ordering assertion
   is what revealed that the fairness rotation was shuffling a randomly-ordered list.

---

## 13. Known limits

Worth knowing before trusting the system further than it has earned.

| Limit | Consequence |
|---|---|
| Acks are self-attestation | A worker claiming `handled` or `nothing_to_do` can advance a complete checker window. The ledger prevents accidental omission; it does not prove the worker read or understood the source. Reliable proof would require structured receipts from each source wrapper, not inference from agent event streams. |
| Slack gating is by trigger, not by action | an email-triggered quest whose reply goes to Slack is still dispatched during an outage. The send fails, the item is acked `blocked`, so it costs an invocation rather than data. |
| Prune rules have no golden | they delete logs, not watches, so a mistake costs history rather than tracking. |
| Runtime and workspace share one root | `yaas-triage/`, agent rules, personal work, `.env`, state, and logs are resolved from the directory containing `yaas-triage/`. This keeps the worker's world legible, but makes blind installation into an arbitrary existing repository unsafe when paths collide. |

### Installation boundary

The shared root is a deliberate current constraint, not a packaging abstraction. More than one
runtime process finds its root by walking upward to `yaas-triage/`, and then derives `.env`,
`state/`, `logs/`, the dashboard, and the worker's current directory from it. Ambient
`REPO_ROOT` is intentionally ignored by most components to prevent stale environment variables
from silently splitting state across two trees.

Consequences:

- A fresh standalone checkout is deterministic to install.
- An existing repository needs collision-aware merging of rules, configuration, and docs.
- Moving runtime code outside the workspace requires a central runtime/workspace path contract
  first; symlinking `yaas-triage/` is not a safe substitute because path resolution uses real
  paths.

The intended future seam is an immutable Sidequestor runtime plus a user-owned workspace that
remains the agent's working directory. Until that seam exists, locality is safer than pretending
the two roots are independent.

---

## 14. Backends

`YAAS_AGENT` picks the agent: `claude` (default), `codex`, or `cursor`. Only
`dispatch/dispatch-agent.sh` knows the difference; everything upstream is backend-agnostic.
Cost reporting differs — Claude reports dollars, the others raw token counts.
