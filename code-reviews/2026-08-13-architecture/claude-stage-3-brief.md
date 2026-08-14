# Stage 3 execution brief (for Codex)

Architecture is decided. Execution only. Do not redesign, do not widen scope.
Baseline: `474fd5e` in `.git-yaas-v2` (**the git tree of record**), working tree clean. The
`cp "$HERE/tick_check.py"` fixture fix for `tests/contract/dashboard-routes.test.sh` is already
committed there, so 3.0 is only about wiring that directory into the runner.
Plan and rationale: `claude-plan.md`, Stage 3 section.

**Scope is Stage 3 only.** Do not start Stage 4 (watch-type manifest) or Stage 5. If you spot
something from a later stage, write it in the report and leave the code alone.

Unlike Stage 2, **this stage deliberately changes structure**, so the differential goldens for
`tick.py` are still a hard constraint (they must not change) but the approval tests will legitimately
need updating. Be explicit in the report about every test you change and why.

---

## Ground rules

1. **`tick.py` differential goldens must stay green with NO golden edits.** Stage 3 should barely
   touch `tick.py`; if you find yourself editing a golden, stop and report.
2. **Do not commit.** Commits are made for you. Leave work in the working tree.
3. Run `bash yaas-triage/tests/run-all.sh` before finishing. **Correction to earlier briefs: it
   does NOT swallow failures.** It ends with `[ "$FAIL" -eq 0 ]` and exits non-zero correctly. My
   earlier claim came from misreading a backgrounded wrapper's exit code. Still read the
   `N suite(s) passed, M failed` line, but `$?` is trustworthy.
4. Report the real numbers. Do not claim completion on a red suite.
5. Never touch `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` or `.env`.

---

## 3.0 First: wire `tests/contract/` into the runner (my error from Stage 1, fix it before anything else)

`run-all.sh:32` only globs `unit/` and `behaviour/`:

    for t in $(find "$HERE/unit" "$HERE/behaviour" -name '*.test.sh' 2>/dev/null | sort); do

`tests/contract/` did not exist before Stage 1; my Stage 1 brief invented that path, so the 1.6
route/action parity test **has never run in any suite**. It fails hard without its fixture fix and
nobody would have known.

Either add `"$HERE/contract"` to that `find`, or move `dashboard-routes.test.sh` into `behaviour/`
alongside the existing `checker-contract.test.sh`. **Prefer moving it**, since two directories for
the same idea is what caused this. Then confirm it actually runs and passes inside `run-all.sh`.

The `cp "$HERE/tick_check.py"` line in that file is already committed at `474fd5e` and is
required. Keep it, and carry it with the file if you move it.

---

## 3.1 Pre-flight: survey the fixtures BEFORE writing the module

Stage 2's only failure was that sharing a module into a standalone CLI broke six minimal test
fixtures with `ModuleNotFoundError`. Stage 3 shares a much wider dependency into three consumers,
so do this first, not last.

**Corrected list, fourteen files.** My original nine-file list was incomplete: I grepped for
`cp "$HERE/...` and missed every fixture that uses `$SCRIPT_DIR` instead. Do not trust this list
either — re-derive it yourself with a pattern that catches both, plus any other variable spelling:

    tests/behaviour/approval-edit-route.test.sh
    tests/behaviour/approval-projection.test.sh
    tests/behaviour/approval-stalled-producer.test.sh
    tests/behaviour/approval-transitions.test.sh
    tests/behaviour/approval-undo-reclaim.test.sh
    tests/behaviour/manual-instruction-queue.test.sh
    tests/behaviour/dashboard-routes.test.sh        (after the 3.0 move)
    tests/behaviour/approval-lease.test.sh          ($SCRIPT_DIR)
    tests/behaviour/reaction-approval-routing.test.sh  ($SCRIPT_DIR)
    tests/behaviour/tick-offline-gate.test.sh       ($SCRIPT_DIR, checkers/approval.py)
    tests/behaviour/unacked-backoff.test.sh         ($SCRIPT_DIR, checkers/approval.py)
    tests/unit/surfaces/slack-send.test.sh          ($SCRIPT_DIR, approval-helper.py)
    tests/unit/ops/doctor.test.sh
    tests/unit/ops/health-monitor.test.sh

Every fixture that loads a module you give a new import must also copy `approval_state.py` and the
store module into its tree, in the same change that adds the import. Do not leave this to the end.

---

## 3.2 `yaas-triage/approval_state.py` — pure, importable, no I/O

Holds, and is the single answer to, the approval state machine:

- `TRANSITIONS`: the `(status, action) -> updates` table.
- `available_actions(item, now) -> list[str]`.
- `is_stalled(item, now) -> bool`.
- `LEASE_MINUTES`, moved from `approval-helper.py:117` (keep the `YAAS_APPROVAL_LEASE_MIN` env
  override and the 45 default).

**It must be importable and must not do I/O.** No file reads, no locking, no HTTP. Pure functions
over dicts, so it is trivially unit-testable.

**It must NOT be `ledger/approval-helper.py`.** That file is hyphenated and is invoked purely as a
subprocess (`tick.py:496`, `dashboard-server.py:1718`, `surfaces/slack-send.py:213`), so no
consumer can import it. The helper becomes a thin CLI adapter over this module.

Reach it the same way Stage 2 reached `tick_check`: the existing byte-identical `_repo_root()`
walk-up, then `sys.path.insert`. **Do not try to share `_repo_root` itself** — the comment at
`checker-health.py:87` explains that a shared version would need `sys.path` handling that itself
depends on knowing the repo root, and `repo-root.test.sh` pins it byte-identical.

### The interface is a function, not a static dict

A static `(status, action) -> updates` mapping cannot express what these transitions actually do:
review outcomes depend on the payload, timestamps depend on `now`, reclaim depends on lease
expiry, and `edit` depends on the submitted text. So the public interface is:

    apply_transition(item, action, payload, now) -> updates | ILLEGAL

`TRANSITIONS` may still exist internally as the legality table, but the payload-dependent parts
live in the function. Do not force everything into a dict literal.

### HTTP actions are a SUBSET of all transitions

The dashboard actions below are not the whole state machine. The worker also drives `start`,
`answer`, `done`, `abandon` and automatic cancellation, which live in `ledger/approval-helper.py`
(see `cmd_answer` at `:418`, and its siblings). **If those land in the same table and 3.5 derives
routes from every entry, internal worker transitions become HTTP-reachable.** That is a real
exposure, not a tidiness issue.

Mark them explicitly. Either an `http_actions` set, or per-entry metadata like
`{"http": True}`. 3.5 derives routes from **that subset only**, never from the full table. Add a
test asserting no worker-only action is routable.

The six HTTP actions, from the current code:

| action | legal from | notes |
|---|---|---|
| `review` | `pending_review`, `needs_reply` | see 3.4 for the `"?"` rule |
| `revise` | `pending_review`, `needs_reply` | to `needs_reply` |
| `cancel` | `pending_review`, `needs_reply`, `reviewed` | note `reviewed` is allowed |
| `edit` | `reviewed` | |
| `undo` | `reviewed`, `cancelled` | never from `executing` |
| `reclaim` | `executing` with EXPIRED lease only | sets `needs_reconcile` |

Current sources of truth to read first: `dashboard-server.py:192-199` (`_update_approval` and its
default `from_status`), `:1769`, `:1834`, `:1883-1885`.

---

## 3.3 A separate store module for locked read-modify-write

`pending-approvals.json` currently has three independent lock-and-write implementations
(`approval-helper.py` has 20 `flock` references, `dashboard-server.py` 7, `rotate-logs.py` 2).
Pure rules and durable storage are different jobs; do not put them in one file.

The store owns the `.lock` sidecar, `LOCK_EX`, and temp + fsync + replace. Note the review found
**durability and locking already agree across all four writers** — only transition validation
diverges. So this is a consolidation, not a bug fix. Do not change the locking discipline; move it.

**Transition validation MUST happen inside the exclusive-lock callback**, not before it. If the
server checks a status or a lease, then takes the lock and writes, it can lose the race and persist
a decision based on a stale read. Today `_update_approval` is correctly a read-modify-write inside
one `flock`; preserve that shape. The store's API should therefore take a callback that receives
the freshly-read item and returns the updates, e.g.
`store.mutate(approval_id, lambda item: apply_transition(item, action, payload, now))`, so
validation and write are inside one critical section. A `check-then-write` split here is a
regression even if every test passes.

---

## 3.4 Move the `"?"`-in-note heuristic into the table

`dashboard-server.py:1869-1870` silently rewrites a `review` into `needs_reply` when the note
contains a `"?"` and the text was not edited. That is a transition rule living in an HTTP handler.
Move it into `approval_state.py` and keep the behaviour identical, including the `not edited`
condition. If you think the rule is wrong, say so in the report; do not change it here.

---

## 3.5 Derive the route list from the table

`do_POST` (`dashboard-server.py:1692`) stops hardcoding action names; any action in `TRANSITIONS`
is routable and anything else 404s. After this, a new transition cannot produce a dead client
button, which is what the 1.6 parity test was compensating for. Keep that test: it goes from
catching a live bug to pinning an invariant.

---

## 3.6 Server emits `available_actions`

`_approval_card` ships `available_actions` alongside `status` and `stalled`. The client renders the
buttons the server declares legal instead of recreating the status-to-action table.

`switch(item.status)` may still select visual treatment. Do not have the client decide *which
actions a status permits* — that split brain is what produced the reclaim and undo defects in the
first place.

Delete the now-dead client-side status branching. Expect roughly 60 lines to go.

---

## 3.7 `is_stalled` gets its single call-site set

Replace the divergent copies with calls into `approval_state.is_stalled`:

- `checkers/approval.py:101` (currently: missing lease means not expired)
- `_approval_card` in `dashboard-server.py` (currently computes it inline from Stage 1.3)

**`ops/health-monitor.py` is EXCLUDED. Do not make it import `approval_state`.** My earlier
instruction to do so was wrong; revert it if you already have.

Read its header comment (`health-monitor.py:20-36`). It is a dead-man switch: it runs as its own
launchd job, "shares no code path with the triage loop", and is "deliberately dependency-free" so
that "nothing it does can be broken by the thing it watches." It exists because of two multi-hour
silent outages, including a stray `.pth` that crashed every tick for 6.5 hours while every surface
reported healthy. An import of a triage module is exactly the coupling it was built to forbid: a
syntax error in `approval_state.py` would disable the monitor that is supposed to notice.

Leave its lease check independent, including its `executing_at` / `APPROVAL_STUCK_MIN` fallback.
Instead, **pin parity with a test**: assert the monitor's verdict matches
`approval_state.is_stalled` across a table of items (live lease, expired lease, missing lease,
malformed lease). That gets the anti-divergence benefit without the coupling. If the two ever
genuinely need to differ, the test documents it.

Preserve the missing-or-unparseable-lease rule: **not stalled**. Match `checkers/approval.py`'s
current `TypeError`/`ValueError` handling exactly.

---

## Exit criteria

- **No client-side action-eligibility rule in `dashboard.html`.** The client must not decide which
  actions a status permits; it renders `available_actions`. Status checks for layout, history
  badges and toast behaviour are legitimate and stay. (The earlier phrasing, "no status literal
  outside a render switch", was too broad.)
- No transition rule anywhere in `dashboard-server.py`.
- One definition of `LEASE_MINUTES`. One transition function. `is_stalled` defined once and used by
  the checker and the dashboard, with `health-monitor.py` deliberately independent and pinned by a
  parity test.
- No worker-only action (`start`, `answer`, `done`, `abandon`) is HTTP-routable, asserted by a test.
- The route-parity test runs inside `run-all.sh`.
- Full suite green (`$?` is trustworthy), differential 29/0 with no golden edited.

## Report back

- What changed per numbered item.
- **Every test you modified, and why.** This stage legitimately changes test expectations, so this
  list is how the change gets reviewed.
- The real summary line from `run-all.sh`, plus explicit confirmation that no golden was edited.
- Anything in this brief that turned out to be wrong in the code. Line numbers were verified at
  `474fd5e`.
- Anything you deliberately left alone.
