# Architecture

YAAS watches the places you get asked things, and wakes an AI agent only when something
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
        │               │    "is there anything new?"  → clean | dirty | error
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

One plugin per type, resolved at runtime as `checkers/$type.py`. Adding a type means dropping
in a file; nothing else changes.

| Type | Fires when |
|---|---|
| `slack_thread` | a new reply in a thread |
| `slack_channel` | a new top-level message in a channel |
| `slack_dm` | a new DM from a watched person |
| `slack_mention` | you are @mentioned anywhere Slack search can see |
| `email` | a Gmail query matches something new |
| `jira` | an issue in a JQL set changed |
| `github_pr` | PR activity — a PR review does **not** bump the linked Jira, hence both |
| `schedule` | a cron expression or a one-shot time came due |
| `approval` | a human reviewed a queued draft |

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
└── timeline.ndjson   append-only log of every action taken
```

Lifecycle: `active/` → `completed/` or `archived/`. A quest is a folder. No database, no schema
migration.

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
│   └── <type>.py, <type>.lag     per-type checker + its watermark lag
│
├── dispatch/       "RUN A WORKER"
│   ├── run-agent.py              one invocation: watchdog, log tee, kill tree
│   ├── dispatch-agent.sh         backend-agnostic (claude / codex / cursor)
│   ├── manual-dispatch.sh        a dashboard-initiated run with an instruction
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
│   └── slack-send.py             send/draft + log the body + the stale-reply guard
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

**Three tools, three different questions:**

```
  tests/run-all.sh       is the CODE correct?      (fixtures; passes on an unconfigured box)
  ops/doctor.sh          is this MACHINE set up?   (real Keychain, PATH, .env, plist)
  ops/health-monitor.py  is it WORKING right now?  (runtime, continuous)
```

---

## 12. How this is tested

```
   29 suites ─┬─ tests/unit/        mirrors the source tree 1:1
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

---

## 14. Backends

`YAAS_AGENT` picks the agent: `claude` (default), `codex`, or `cursor`. Only
`dispatch/dispatch-agent.sh` knows the difference; everything upstream is backend-agnostic.
Cost reporting differs — Claude reports dollars, the others raw token counts.
