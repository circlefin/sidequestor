# Architecture Review Validation and Implementation Plan

Source reviewed: `2026-08-13-architecture/source-review.html` (archived in this directory)

Validation date: 2026-08-14

Repository state: current working tree at `0c6988d`, including uncommitted changes. The source
review names commit `309aab8`, but that object is not present in this repository's local Git
object database. The verdicts below therefore describe the current files, not an assumed exact
reconstruction of that snapshot.

## Verdict

The central critique is accurate: approval lifecycle rules and watch-type metadata are spread
across several consumers, and the duplication has produced observable mismatches. The proposed
direction is sound, but candidates 4, 5, 8, and 9 need narrower implementations than the report
suggests.

| Candidate | Verdict | Current-tree evidence and qualification |
|---|---|---|
| 1. One approval state machine | Confirmed, highest priority | `/api/undo/<id>` is called by `dashboard.html` but is not routed by `dashboard-server.py`. `build_messages()` puts `reviewed` and `executing` items into the review arrays and `queued_items`, so they render twice. `_approval_card()` still emits no `stalled`. `/api/edit/<id>` has since been implemented, so the report's claim that both undo and edit always fail is no longer fully current. |
| 2. Watch-type manifests | Confirmed | The type set and type properties are repeated in `add-watch.py`, `new-quest.py`, the scenario harness, checker-contract tests, the dashboard, and prose. A sidecar manifest fits the existing `.lag` discovery model. It will not make a new type a two-file change: checker-specific tests, dispatch instructions, and operator documentation can still require deliberate edits. |
| 3. Shared GitHub checker core | Confirmed | `github_pr.py` and `github_issue.py` repeat the GitHub invocation, error taxonomy, search-token validation, watermark bounding, tie handling, and timestamp parsing. Keep two executable entry-point adapters because runtime dispatch resolves `checkers/<type>.py`; move the common implementation into a non-executable library. |
| 4. Shared backoff predicate | Partially confirmed | Retry timestamp parsing and the due/not-due comparison are duplicated and should be one pure helper. Do not merge the mutation ladders: checker failures use 60 seconds to 1 hour, while unacked paid dispatches use 5 minutes to 24 hours and a promotion threshold. A generic `apply_backoff()` would hide materially different policies. |
| 5. Shared stalled predicate | Confirmed with a placement change | `approval.py` treats a missing or malformed lease as live, while `health-monitor.py` falls back to `executing_at`; the dashboard has no producer for `stalled`. Put the predicate in an importable approval-domain module, not directly in the hyphenated CLI file `approval-helper.py`. Define whether legacy missing leases use an age fallback once and test it. |
| 6. Lift dashboard writes | Confirmed | The dashboard's locked read-modify-write and transition validation are approval-domain behavior. Move them to the shared approval store/state modules while leaving the large read projections in `dashboard-server.py`. |
| 7. Shared structural gate | Confirmed | `tick.py` repeats the first four gates in `tick_check.classify()` solely to avoid running a checker. A pure `structural_verdict()` removes the duplicate without splitting the orchestrator. |
| 8. Declared upstream | Valid only as part of candidate 2 | `startswith("slack_")` currently works, so this is not an independent priority. Once manifests exist, use their `upstream` field for ordering, shared-rate-limit accounting, and dispatch metadata. Do not build a second registry just for this change. |
| 9. Assert prose tables | Partially confirmed | Contract tests should compare manifests with executable checker entry points and explicitly marked documentation tables. Do not scrape every prose mention or require every document to list every type: `approval` is runtime-only and dispatch instructions contain per-type judgement that cannot be generated safely. |

The report's "leave alone" conclusions are also reasonable. This plan does not split the
projection-heavy dashboard read path, the impure tick sequencer, `reaction_config.py`, or
`dispatch/slack-read-health.py`.

## Implementation Plan

### Phase 1: Characterize and repair the approval lifecycle

1. Add table-driven tests covering every current status and action before moving code. At a
   minimum, pin `review`, `revise`, `edit`, `cancel`, `undo`, and `reclaim`; verify success,
   conflict, not-found, malformed payload, and worker-progress races.
2. Add `yaas-triage/approval_state.py` as a pure domain module. Define status constants, terminal
   statuses, legal transitions, transition-specific field updates, and `is_stalled(item, now)`.
   Keep timestamps injectable so all edge cases are deterministic in tests.
3. Add an importable approval store module that owns the sidecar lock, atomic temp/fsync/replace,
   lookup, and compare-and-transition operation. Make both `ledger/approval-helper.py` and the
   dashboard call it. Preserve the existing CLI as a thin adapter so skills and scripts do not
   change command shape.
4. Model undo explicitly instead of treating it as a blind status reset. Record enough transition
   metadata to restore the immediately preceding human action, allow undo only while the worker
   has not advanced the item, and return `409` after worker progress. Keep reclaim separate:
   reclaim is recovery of an expired `executing` lease, not undo of a human click.
5. Replace the dashboard POST action allowlist with the domain module's declared HTTP actions.
   Route `/api/undo/<id>` and `/api/reclaim/<id>` and keep `/api/edit/<id>` constrained to
   `reviewed` items.
6. Make `build_messages()` a mutually exclusive projection:
   `pending_review` goes to the review carousel; `reviewed`, `needs_reply`, and live `executing`
   go to the in-flight strip; expired `executing` goes to the recovery surface; terminal items go
   to neither. Emit `stalled` and `available_actions` from the server.
7. Simplify `dashboard.html` to render the server-authored status and available actions. Retain
   optimistic dismissal as presentation state, but remove lifecycle inference and map reclaim to
   its own endpoint.
8. Update approval checker and health-monitor tests to use identical lease semantics, including
   exact expiry, timezone-aware values, malformed timestamps, legacy missing leases, and
   `needs_reply`. Run the approval behavior tests and dashboard endpoint tests before proceeding.

Acceptance criteria:

- Every action exposed by the client has a server route and a legal transition test.
- Each non-terminal approval appears in exactly one dashboard collection.
- Stalled approvals are server-authored, visible, and reclaimable without permitting a blind
  resend.
- Dashboard and worker writes use one locking and atomic-write implementation.
- Undo conflicts safely once a worker has claimed or completed the item.

### Phase 2: Introduce the watch-type registry

1. Define and validate a versioned `<type>.watch.json` schema. Start with `required`, `identity`,
   `user_creatable`, `open_loop`, `upstream`, and a minimal example entry. Represent exceptional
   validation such as schedule's alternative fields explicitly rather than pretending a simple
   required-field list is sufficient.
2. Add one manifest for each dispatchable watch type. Treat the executable `checkers/<type>.py`
   file as the adapter and the manifest as its metadata; helpers such as `result.py`,
   `slack_utils.py`, `cron-due.py`, and `reactions.py` remain outside the registry.
3. Add a small loader beside `tick_state.load_lag_map()`. It must reject malformed JSON,
   duplicate type declarations, unknown schema versions, missing executable adapters, and unsafe
   field shapes with clear diagnostics.
4. Replace executable registries in `ledger/add-watch.py`, quest creation, dashboard open-loop
   classification, test scenario setup, and checker-contract tests with manifest projections.
   Keep domain-specific validation functions where a manifest cannot express the rule clearly.
5. Replace Slack name-prefix grouping with `upstream == "slack"` only after all manifests pass the
   contract test. Preserve current ordering and rate-limit behavior with a differential test.
6. Mark documentation tables intended to enumerate types and assert the correct projection for
   each audience, such as all runtime types versus user-creatable types. Leave narrative and
   per-type operating instructions hand-authored.
7. Update `yaas-checker-authoring/SKILL.md`: adding a type should begin with checker, manifest, and
   tests; the contract suite should identify any remaining intentional documentation work.

Acceptance criteria:

- There is one executable declaration of the complete watch-type set.
- Every manifest has exactly one executable checker adapter, and every executable watch checker
  has exactly one manifest.
- Adding a fixture watch type requires no edits to core registries.
- Slack ordering and shared rate-limit behavior are unchanged.
- Documentation checks distinguish runtime-only from user-creatable types.

### Phase 3: Consolidate the GitHub checkers

1. Add a non-executable `checkers/github.py` containing token resolution, error classification,
   qualifier validation, bounded ascending search, exact-boundary filtering, tie safety, and
   result emission.
2. Reduce `github_pr.py` and `github_issue.py` to executable adapters that declare the search
   noun, JSON fields, preview formatting, and issue/PR-specific safety constraints.
3. Move duplicated tests into a shared contract exercised against both adapters. Retain focused
   tests for issue exclusion of PRs, issue author/label previews, PR preview behavior, and any
   different identity semantics.
4. Run both checker suites with the fake `gh` seam and compare their JSON output with the
   pre-refactor fixtures, especially capped pages with tied timestamps and transient/misconfig
   errors.

Acceptance criteria:

- Watermark and GitHub failure doctrine have one implementation.
- Both `github_pr.py` and `github_issue.py` remain executable dispatch targets.
- Existing output contracts and safe watermark advancement are unchanged.

### Phase 4: Concentrate the remaining pure predicates

1. Add a tolerant `retry_is_due(next_retry_ts, now_ts)` helper and, if dashboard display needs it,
   a companion `retry_remaining(...)`. Reuse it in tick analysis, checker-health's `due` command,
   and dashboard badge projections. Preserve each caller's documented malformed-value policy.
2. Keep `_backoff_for()` in checker health and `unacked_backoff_for()` in the dispatch domain.
   Remove only duplicated timestamp parsing/comparison and duplicated unacked record-update code.
3. Extract `tick_check.structural_verdict(...) -> verdict | None`; call it both before checker
   execution in `tick.py` and at the start of `classify()`. Add parity tests proving all four
   gates return the same verdict and that a held watch never executes its checker.
4. Run unit, behavior, differential, and mutation suites. Treat golden changes as regressions
   unless they are the intended approval UI/API corrections from Phase 1.

Acceptance criteria:

- Retry timestamp tolerance is tested once and consumed everywhere it governs behavior.
- Checker-error and unacked-dispatch backoff policies remain independently named and configured.
- Structural hold conditions exist in one pure function.
- The full triage test suite and checker contract suite pass.

## Delivery Sequence

Deliver the phases as separate changes, with Phase 1 split into characterization, domain/store,
and UI wiring commits if needed. Do not combine approval repair with the manifest migration: both
touch central paths, and separating them makes rollback and behavior review tractable. Phase 3
depends on the manifest contract only for cleaner tests, not at runtime; Phase 4 can follow either
Phase 2 or Phase 3.

Before each phase, capture the current dirty-tree diff or work from a clean branch so unrelated
local changes are not folded into the refactor. No migration should rewrite existing quest
`watch.json` files or approval ledger data in place; loaders and transition code must remain
backward-compatible with currently persisted records.
