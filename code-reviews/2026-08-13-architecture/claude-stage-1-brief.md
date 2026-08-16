# Stage 1 execution brief (for Codex)

Architecture is decided. Your job is execution only. Do not redesign, do not widen scope.
Baseline commit: `24c05b5`. Full plan and rationale: `claude-plan.md` in this directory.

**Scope is Stage 1 only.** Do not start Stage 2, 3, 4 or 5. If you find something from a later
stage that looks tempting, write it down in the report and leave the code alone.

Files you will touch: `yaas-triage/ops/dashboard-server.py`, `dashboard.html`, and new test files
under `yaas-triage/tests/`. Nothing else without saying why in the report.

---

## Ground rules

1. **Read before you write.** `claude-plan.md` Stage 1, then the code it cites. The line numbers
   are from `24c05b5` and are accurate as of that commit.
2. **Tests first (1.1), then fixes.** The characterization tests must be written and passing
   against current behaviour before you change any behaviour.
3. **Do not refactor.** No transition table, no new modules, no moving `is_stalled` into shared
   code. That is Stage 3 and it is deliberately not yours. Inline duplication is acceptable here
   and will be cleaned up later.
4. **Follow the existing test harness.** Look at `yaas-triage/tests/behaviour/*.test.sh` and
   `yaas-triage/tests/lib/` and match the conventions you find. Do not invent a new harness.
5. **Run the suite before you finish.** Report the exact command you ran and its real output. If
   something fails and you could not fix it, say so plainly. Do not report success on a red suite.
6. **One commit per numbered item**, message in the repo's existing style. Do not push. Do not
   touch `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` or `.env`.

---

## 1.1 Characterization tests (do this first)

Pin current behaviour before changing it. Two files:

- `yaas-triage/tests/behaviour/approval-transitions.test.sh`: for every `(status, action)` pair,
  assert what `_update_approval` accepts and rejects **today**. Statuses: `pending_review`,
  `needs_reply`, `reviewed`, `executing`, `executed`, `cancelled`. Actions: `review`, `revise`,
  `cancel`, `edit`. Read `dashboard-server.py:187-219` and `:1812-1813` for the current rules.
- `yaas-triage/tests/behaviour/approval-projection.test.sh`: for each status, assert which of
  `needs_you`, `other_actions`, `queued_items` `build_messages` currently emits it into. This
  test will encode the double-emit bug as current behaviour. That is correct and intended; 1.4
  updates it.

These must pass against unmodified code. If one does not, you have misread the behaviour: fix
the test, not the source.

---

## 1.2 Commit the `edit` route

Already present in the tree as of `24c05b5` (`dashboard-server.py:1682` includes `"edit"`).
Nothing to implement. Add an endpoint test that `POST /api/edit/<id>` succeeds from
`pending_review` and that its `message_text` change is persisted. Confirm in the report that you
verified this rather than assuming it.

---

## 1.3 Give `stalled` a producer

`_approval_card` must emit `stalled: bool`.

- True when `status == "executing"` and `lease_expires_at` is in the past.
- Missing or unparseable `lease_expires_at` means **not** stalled. Match
  `checkers/approval.py:101-110` exactly, including its `TypeError`/`ValueError` handling. Do not
  invent a different fallback and do not touch `health-monitor.py`.
- Inline in `dashboard-server.py`. Do not create a shared module.

`dashboard.html:854` already consumes `item.stalled`, so the reclaim UI becomes reachable.

---

## 1.4 Mutually exclusive partition

Today `build_messages` emits `reviewed` and `executing` items into both the review arrays
(`:1079-1089`) and `queued_items` (`:1153-1166`). Fix so each non-terminal status lands in exactly
one surface:

| status | surface |
|---|---|
| `pending_review` | review arrays (`needs_you` / `other_actions`) |
| `needs_reply` | review arrays |
| `reviewed` | `queued_items` only |
| `executing`, lease live | `queued_items` only |
| `executing`, lease expired (`stalled`) | review arrays only, with `stalled: true` |
| `executed`, `cancelled` | neither |

**`needs_reply` stays in the review arrays.** This is deliberate and is not a bug. See
`dashboard-server.py:191-194`: the reviewer must be able to act on it even while a worker is also
working it. Do not move it to `queued_items` no matter how much it looks like an in-flight state.

Update `approval-projection.test.sh` from 1.1 to the new table in the same commit.

---

## 1.5 Split `undo` and `reclaim` (the item that needs the most care)

Currently `dashboard.html:950` maps the reclaim action onto `/api/undo/`, and the server routes
neither. Two distinct endpoints:

**`/api/undo/<id>`** is a real undo of a reviewer action.
- Legal from `reviewed` and `cancelled` only.
- Restores `pending_review` and clears `reviewed_at` / `cancelled_at`.
- Returns **409** from `executing`, `executed`, `pending_review` and `needs_reply`. The 409 from
  `executing` is the important one: it is what stops the toast undoing something a worker has
  already picked up.

**`/api/reclaim/<id>`** is recovery from a dead worker.
- Legal **only** from `executing` with an expired lease. 409 otherwise, including a live lease.
- Sets `pending_review`, clears the lease, and sets `needs_reconcile: true`.
- Returns 409, not 404, when the lease is still live.

`needs_reconcile` exists because the outcome is genuinely unknown. `checkers/approval.py:96-99`
says an expired claim means "the send may or may not have landed... the worker's job then is to
reconcile (read the thread, look for the message) and NOT to blindly resend." A plain reset to
`pending_review` invites exactly that blind resend, which is why undo and reclaim cannot be the
same endpoint. Do not merge them back together.

Client change in `dashboard.html`: reclaim must POST `/api/reclaim/`, not `/api/undo/`. Fix the
ternary at `:950`.

Tests: `yaas-triage/tests/behaviour/approval-undo-reclaim.test.sh`, covering every legal
transition and every 409 listed above. The `executing` 409 on undo and the live-lease 409 on
reclaim are the two that matter most.

---

## 1.6 Route/action parity contract test

`yaas-triage/tests/contract/dashboard-routes.test.sh`. Assert that every action the client can
POST is routed by `do_POST`, and that every routed action has an endpoint test.

**Do not implement this by grepping `dashboard.html` for `fetch('/api/...')` string literals.**
That approach fails on exactly the case that caused this bug: `:950` builds its path as
`` `/api/${endpoint}/${id}` `` from a ternary, so a literal scrape finds nothing. Drive it off the
server's routed-action list and assert each one answers something other than 404 for a
well-formed request. If you can find a sound way to also enumerate the client side, good, but
correctness beats coverage here: a test that silently checks nothing is worse than a smaller
honest one.

---

## Report back

- What you changed, per numbered item.
- The exact test command and its real output, pass or fail.
- Anything in the plan that turned out to be wrong or impossible when you got into the code. Say
  so directly. The line numbers and behavioural claims above were verified at `24c05b5`, but if
  one is wrong, report it rather than working around it silently.
- Anything you deliberately left alone.
