# Codex comparison with Claude's architecture review

Compared on 2026-08-14:

- `claude-verification.md`
- `claude-plan.md`
- `../architecture-review-2026-08-13-codex-plan.md`

## Conclusion

The reviews match on the main verdict. Both conclude that the source critique is accurate, put
the approval state machine first, confirm the watch-type and GitHub-checker duplication, endorse
the structural-gate extraction, and agree that the dashboard read projections and impure tick
sequencer should remain intact.

They do not fully match on implementation. Claude's verification is stronger for the historical
snapshot, while the Codex plan is safer on approval recovery and more conservative about merging
different backoff policies. The best remediation plan should use Claude's exact snapshot evidence
and urgent bug-fix staging, then use the Codex domain boundaries and transition semantics.

## Factual Agreement

Both reviews confirm the current-tree findings:

- `/api/undo/<id>` is called by the client but not routed by the server.
- `/api/edit/<id>` was broken in the reviewed snapshot and is fixed only in the current
  uncommitted working tree.
- `_approval_card()` does not produce `stalled`, so the reclaim UI is unreachable.
- `reviewed` and `executing` approvals are emitted in both review-card arrays and `queued_items`.
- Watch-type metadata is repeatedly enumerated and fits the existing sidecar discovery pattern.
- The two GitHub checkers duplicate watermark and failure-handling doctrine.
- Backoff timestamp parsing, lease expiry, and the structural checker gate have duplicate
  implementations.
- `startswith("slack_")` is correct today but encodes an undeclared upstream grouping.
- Prose tables need contract checks rather than generation.
- The dashboard read path, `tick.py` sequencing, `reaction_config.py`, and
  `dispatch/slack-read-health.py` should not be split merely because they are large.

## Verification Differences

### Claude has the stronger historical verification

Claude checked commit `309aab8` through the public mirror at `.git-yaas-v2` and confirmed the
one-file, 141-insertion change exactly. Codex checked the main repository's current object database,
where `309aab8` is absent, and deliberately limited its claims to the current working tree.

Use Claude's verification for statements about the reviewed snapshot. Keep Codex's current-tree
qualification for implementation planning, because the uncommitted `edit` route changes what is
still broken now.

Claude also independently checked the cited line counts and duplication measurements more closely.
Neither review verified the headline counts of 16 type enumerations and 21 import edges, and both
correctly treat those numbers as nonessential.

## Plan Differences

### 1. Immediate fixes versus refactor-first characterization

Claude adds a Phase 0 that ships the live defect fixes before the architectural change. Codex starts
with characterization tests and then repairs the lifecycle through the shared domain/store modules.

Claude's staging is better for urgency: the dead endpoints and duplicate rendering should not wait
for the full refactor. Codex's tests and transition matrix should still precede each fix. The merged
sequence should be characterization tests, narrowly safe live fixes, then extraction into shared
modules.

### 2. Undo and reclaim semantics differ materially

Claude proposes one `undo` transition from `reviewed`, `executing`, or `cancelled` back to
`pending_review`, and lets reclaim reuse that endpoint. Codex treats undo and reclaim as separate
domain actions and records enough prior-transition metadata for undo to restore the actual previous
state.

The Codex approach is safer. An expired `executing` item has an unknown external outcome: the send
may already have landed. Resetting it directly to `pending_review` permits a later blind resend and
contradicts the existing dispatch instruction to reconcile the target first. Reclaim should expose
an explicit recovery workflow, not masquerade as undo. Undo should also return `409` once a worker
has advanced the item.

Recommendation: do not implement Claude Phase 0.1 literally. Add separate `/api/undo/<id>` and
`/api/reclaim/<id>` actions with table-driven legal transitions and worker-progress conflicts.

### 3. Shared approval logic placement differs

Claude places `TRANSITIONS` and `is_stalled()` in `ledger/approval-helper.py`. Codex proposes an
importable `yaas-triage/approval_state.py` plus an approval store module, leaving the existing helper
as a CLI adapter.

The Codex boundary is preferable. `approval-helper.py` is hyphenated and cannot be imported by the
dashboard, checker, and health monitor through normal Python imports. The shared state machine
should be pure and importable; the store should separately own flock and atomic replacement. This
also prevents HTTP routing concerns from leaking back into the ledger CLI.

### 4. In-flight partition coverage differs

Claude's Phase 0 names only `reviewed` and `executing` as queued states. Codex includes
`needs_reply` as in-flight because the approval checker dispatches it to the worker for a response,
and the client comment already describes it as in-flight.

Codex is more complete here. The transition matrix should explicitly assign every non-terminal
status to exactly one projection:

- `pending_review`: review queue
- `reviewed`, `needs_reply`, live `executing`: in-flight
- expired `executing`: recovery surface
- `executed`, `cancelled`: neither active surface

### 5. Client contract differs

Claude follows the source proposal closely: the client switches on `item.status`. Codex has the
server emit `available_actions` as well as status and stalled state.

Server-authored `available_actions` better satisfies the one-definition goal because the client
does not recreate the status-to-action table. A status switch can still select visual treatment.
Claude's proposed client-route guardrail is valuable, but a grep over `fetch()` literals will miss
dynamic paths such as `` `/api/${endpoint}/${id}` ``. Prefer a contract test over declared actions
and registered routes, plus endpoint behavior tests.

### 6. Backoff consolidation differs materially

Claude adopts the source proposal's `is_due()` and `apply_backoff()` pair in `tick_check.py` for
all sites. Codex consolidates retry timestamp parsing and due/not-due comparison but keeps the
checker-error and unacked-dispatch mutation policies separate.

Codex is safer. Checker errors currently back off from 60 seconds to 1 hour. Unacked paid
dispatches back off from 5 minutes to 24 hours and incorporate a promotion threshold. A generic
`apply_backoff(rec, now_ts, promote)` does not express those differences clearly. Share the pure
time predicate and remove the two byte-identical unacked write blocks, but retain separately named
policy functions.

### 7. Manifest scope and migration differ

Both plans endorse `<type>.watch.json`. Codex adds schema versioning, `user_creatable`, validation,
explicit treatment of schedule's alternative required fields, and a checker-to-manifest bijection.
Claude proposes a smaller manifest and makes upstream migration optional.

Codex's schema is more robust, especially because `approval` is runtime-only and `schedule` cannot
be represented accurately as a flat required-field list. Claude's incremental caution is useful:
the `upstream` field can be introduced with manifests while replacing `startswith("slack_")` in a
separate, behavior-pinned change.

### 8. Documentation assertions differ

Both reject generated prose and preserve judgement-heavy instructions. Claude proposes direct
set equality across several named tables and all manifests. Codex proposes explicitly marked
tables with audience-specific projections such as runtime types versus user-creatable types.

Codex's scoped assertions are safer. Not every table is intended to enumerate the same set: the
`.lag` documentation naturally lists only types with nonzero lag, and creation guidance treats
runtime-only `approval` differently. Tests should declare which manifest projection each table is
expected to cover rather than assume universal equality.

### 9. GitHub consolidation is substantially aligned

Both plans keep `github_pr.py` and `github_issue.py` as executable adapters and move shared doctrine
to non-executable `checkers/github.py`. Codex specifies shared adapter-contract tests and preserves
type-specific safety tests in more detail. This is complementary rather than contradictory.

## Recommended Reconciliation

1. Adopt Claude's exact `309aab8` verification and its separate urgent-fix phase.
2. Add Codex's transition characterization before changing behavior.
3. Do not allow generic undo from `executing`; create a distinct reconciliation-aware reclaim.
4. Put pure approval rules and locked storage in importable modules, not the CLI script.
5. Include `needs_reply` in the mutually exclusive in-flight partition.
6. Let the server emit legal actions and test route/action parity without scraping dynamic
   JavaScript strings.
7. Share retry timestamp logic while keeping the two backoff ladders separate.
8. Use versioned manifests and audience-scoped documentation contracts.
9. Apply the common GitHub checker extraction as described by both plans.

With those reconciliations, the reviews are consistent on diagnosis and priority. Their real
disagreement is limited to how aggressively to unify code and how safely to model recovery from
an approval whose external side effect is uncertain.
