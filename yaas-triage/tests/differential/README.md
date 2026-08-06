# Differential harness — the regression net for the triage port

The port replaces a 1,601-line shell orchestrator with a set of small Python modules.
The risk is not that the new code crashes; a crash is loud and easy. The risk is that it
gets a **watermark decision** subtly wrong and silently buries real messages, which is a
failure we have already shipped twice.

So the port is not verified by review. It is verified by running both orchestrators
against the same scenarios and comparing what they decided.

```
scenario.json ──> scenario.py builds a throwaway repo ──> orchestrator runs a real tick
                                                              │
                              snapshot.py reduces it to a time-independent verdict
                                                              │
                                            compared against goldens/<name>.json
```

## The three files

| File | Job |
|---|---|
| `../lib/scenario.py` | Builds a fixture repo and stubs the four external seams. |
| `../lib/snapshot.py` | Reduces a finished tick to a comparable verdict. |
| `run.sh` | Records goldens, or checks an orchestrator against them. |
| `mutations.sh` | Breaks the orchestrator on purpose and asserts the harness notices. |

## Why it needs no instrumentation

Every external call the orchestrator makes already goes through a path held in a
variable, so a copied tree with four stubs is indistinguishable from the real thing:

| Seam | Stubbed with |
|---|---|
| `checkers/<type>.py` | a canned verdict from the scenario |
| `run-agent.py` | a scripted ack writer (the scenario decides what the "worker" closes) |
| `mcp-call.sh` | a scripted Slack responder (health gate + reactions sweep) |
| `notify.py` | a no-op, so a test never fires a real desktop notification |

Everything else is the real code under test: `ack-watch.py`, `add-watch.py`,
`watch-guard.py`, `spend-window.py`, `checker-health.py`, and the commit logic itself.

## Usage

```bash
./run.sh check                  # current orchestrator vs the goldens
./run.sh check tick.py          # the ported orchestrator vs the SAME goldens
./run.sh check -k slack         # only scenarios matching "slack"
./run.sh keep                   # leave fixture trees behind to inspect
./run.sh record                 # re-record goldens (see the warning below)
./mutations.sh                  # prove the harness still catches real breakage
```

A full check is ~17 scenarios at roughly 4 seconds each, so about 70 seconds. That is why
it is a separate command rather than part of `tests/run-all.sh`.

## Two rules that keep this honest

**1. `record` is a deliberate act, not a fix.** If a check fails, the default assumption
is that the orchestrator regressed. Re-recording makes the failure disappear without
fixing anything, which converts the whole harness into decoration. Only record when a
behaviour change is *intended*, and let the golden diff be the reviewable evidence of
exactly what changed.

**2. Scenarios cannot hardcode a timestamp.** Triage compares timestamps against the
real clock, so a fixed epoch drifts into "stale" and the retire rules delete the watches
before anything gets tested. Every time in a scenario is written relative: `@now-3600`.
The first version of these goldens was recorded with fixed epochs and every single one
came back "all watches REMOVED, nothing dispatched" — twelve confidently green files
that tested nothing.

For the same reason `snapshot.py` records the *classification* of a watermark move
(`held` / `advanced_to_now` / `advanced_to_checker_value`) and never the number.

## What the scenarios protect

| Scenario | Guarantee |
|---|---|
| `clean_tick` | Nothing dirty, nothing dispatched. Cost control. |
| `dirty_acked_handled` | The happy path advances. |
| `dirty_unacked_holds` | Agent exits 0 having acked nothing → watermark holds. |
| `dirty_acked_blocked_holds` | An explicit "couldn't finish" holds. |
| `nothing_to_do_advances` | Read-and-correctly-ignore advances. |
| `partial_ack_isolates_items` | **Isolates the commit predicate.** Only acked items advance. |
| `mixed_ack_statuses` | The three ack statuses are not collapsed into a boolean. |
| `incomplete_window_holds` | An undrained window holds even when acked `handled`. |
| `advance_to_exact_value` | A checker-supplied boundary is used verbatim. |
| `checker_error_holds` | An error is not a clean read. |
| `slack_down_gates_dispatch` | Slack outage does not burn an invocation. |
| `non_slack_dispatches_while_slack_down` | **Frozen known defect** (see below). |
| `two_quests_isolated` | One quest failing to ack cannot hold another's watermark. |
| `worker_appends_watch` | The one allowed mutation survives. |
| `agent_timeout_keeps_banked_acks` | A watchdog kill honours acks banked before it. |
| `agent_hard_failure_holds` | A crashed agent holds everything. |
| `reactions_target` | Reactions dispatch independently and touch no `watch.json`. |
| `nothing_to_do_with_saturated_window_holds` | **The 2026-08-05 livelock.** An honest `nothing_to_do` plus `complete:false` never advances, so the item re-fires until the watch is parked. |

`partial_ack_isolates_items` exists because of a real gap found while validating this
harness: a dispatch that acks *nothing* takes a separate `gate_dispatch_unacked` path and
never evaluates the commit predicate, so it cannot catch a predicate regression at all.
Only a *partial* ack reaches the predicate.

## The frozen known defect

`non_slack_dispatches_while_slack_down` records behaviour that is wrong on purpose.

The Slack health gate is *computed* per target (`_target_needs_slack`) but *applied*
globally: the first Slack-needing target sets the flag and the whole tick exits, so an
email-only quest is stalled by a Slack outage it does not depend on. With roughly 183
`gate_slack_down` events a day, this bites regularly.

It is frozen rather than fixed so the port can be proven faithful first. Fixing it is a
separate change, with its own intentional golden update.
