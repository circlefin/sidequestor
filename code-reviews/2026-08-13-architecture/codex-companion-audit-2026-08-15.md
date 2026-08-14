# Companion and Architecture Audit

**Date:** 2026-08-15

**Reviewed point:** `.git-yaas-v2` commit `b67a633`

**Mode:** Report only. No runtime, test, skill, setup, or documentation changes were made.

## Verdict

Stages 1 through 5 landed successfully in the runtime design, and the current test baseline is green. The companion material has not fully caught up, however. The public README, architecture guide, environment example, setup output, one skill description, and the coverage reporter contain stale assumptions from before the staged refactor.

The remaining risks do not justify another broad rewrite. Two narrow runtime seams should be hardened: configuration defaults still have competing sources, and malformed watch manifests can take down dashboard projection routes even though the triage loop deliberately survives them. The rest is companion correction and contract-test coverage.

## Current Evidence

- `bash yaas-triage/tests/run-all.sh`: 42 suites passed, 0 failed.
- Differential harness: 29 golden scenarios passed, 0 failed.
- The checker registry is manifest-driven and the tick path has the intended failure isolation.
- `AGENTS.md` and `GEMINI.md` are symlinks to `CLAUDE.md`, so the three active agent instruction surfaces do not currently drift from each other.

## Companion Status

| Surface | Status | Assessment |
|---|---|---|
| Runtime tests | Mostly current | Core behavior is well characterized, but companion/setup contracts have blind spots. |
| Checker manifests | Current | The ten shipped watch types are registered through executable checker plus `.watch.json` manifest pairs. |
| Runtime skills | Mostly current | The checker-authoring procedure is current, but its description and one compatibility note still describe the old registration model. |
| README | Stale | Test counts, watch-authoring instructions, and dashboard restart guidance are wrong. |
| Architecture guide | Stale | Test counts and the watch file/extension model predate Stage 4. |
| `.env.example` | Stale | Two command paths refer to files that moved into `dispatch/` and `ops/`. |
| Setup/install output | Stale | Dashboard and heartbeat manual-run instructions print incorrect commands. |
| Coverage reporter | Misleading | New manifests and indirectly tested shared modules appear as uncovered source. |
| Historical private design docs | Historical | They retain old protocols. They should be explicitly marked archival rather than treated as live operator documentation. |

## Findings

### High: Configuration defaults still have two runtime contracts

`yaas-triage/tick_state.py` declares defaults of 8 parallel dispatches, a 1,800 second tick budget, and a 60 second minimum slice. `yaas-triage/tick.py` bypasses that interface and directly defaults to 3, 3,600, and 300. The environment example, dashboard, and operator skill match the latter values.

The running tick therefore still behaves correctly today. The defect is that `Config.knob()` returns different answers for the same settings, so a future caller, test, or dashboard cleanup can silently change scheduling behavior. This is the split-brain configuration problem Stage 2 was intended to remove.

**Recommendation:** Make one exported numeric-knob map canonical, set it to the actual 3/3,600/300 behavior, and make Tick and dashboard projections consume it. Add a contract test against the documented `.env.example` defaults.

### High: Maintainer instructions teach the obsolete checker extension model

`README.md` and `ARCHITECTURE.md` both say a new watch type requires one checker file and that nothing else changes. Stage 4 requires an executable `<type>.py` and a `<type>.watch.json` manifest, with optional lag metadata and behavior/documentation work where relevant. The architecture file map also omits the required manifest and implies every checker has a lag file.

Following the published instructions can create an undiscoverable or contract-invalid checker.

**Recommendation:** Replace the obsolete prose with the manifest contract, link the checker-authoring skill, and make the documentation contract test assert the authoring steps rather than only table entries.

### High: Setup and example commands point at moved files

- `.env.example` still uses `yaas-triage/spend-window.py`; the implementation is `yaas-triage/dispatch/spend-window.py`.
- `.env.example` still uses `yaas-triage/health-monitor.py`; the implementation is `yaas-triage/ops/health-monitor.py`.
- `yaas-triage/setup/setup.sh` prints `yaas-triage/dashboard-start.sh`; the script is under `yaas-triage/ops/`.
- `yaas-triage/setup/install-launchd-heartbeat.sh` describes the triage installer, shows the wrong installer command, and tells the operator to run `tick.py` manually instead of the health monitor.

These are operator-facing breakages even though the underlying templates install the correct programs.

**Recommendation:** Correct the commands and add a non-interactive setup-output contract test. Extend path-reference scanning to `*.example` and validate variable-composed setup paths explicitly.

### Medium: Dashboard availability is weaker than tick availability

The tick loads and isolates individual checker failures as designed. The dashboard loads all watch manifests while building open-item and quest-detail projections. A missing or malformed manifest can therefore produce HTTP 500 responses for those dashboard routes while triage continues operating.

**Recommendation:** Validate and cache the manifest projection at dashboard startup, or catch registry failure and return a visible configuration warning while keeping unaffected routes available. Do not make Tick consume the dashboard cache or add a new registry framework.

### Medium: Green companion tests do not cover the stale surfaces

`doc-contracts.test.sh` verifies selected watch-type tables and lag values, but not the prose that tells maintainers how to add a checker, suite counts, restart commands, or setup paths. `path-references.test.sh` excludes `.example` files and cannot resolve shell-variable paths such as `$TRIAGE_DIR/dashboard-start.sh`.

This explains why all tests pass alongside multiple broken commands.

**Recommendation:** Add narrow assertions for executable documented commands and generated setup guidance. Avoid snapshotting whole documents.

### Medium: Coverage reporting lost signal after manifest extraction

The coverage script treats `.watch.json` manifests as ordinary source and reports them as missing unit tests even though checker-contract and tick-state tests validate them collectively. It also reports shared modules such as `approval_state.py` and `github.py` as uncovered despite behavior being exercised through their consumers.

**Recommendation:** Teach the reporter about manifest contract coverage and documented integration coverage. Do not create one empty test file per manifest merely to satisfy file-name accounting.

### Low: Skill descriptions retain pre-refactor language

The body of `yaas-checker-authoring/SKILL.md` correctly specifies checker plus manifest authoring. Its frontmatter still says all registration points must be updated together, and a later note overstates the remaining hardcoded type lists. `CLAUDE.example` repeats the old "registration points" wording.

**Recommendation:** Update the descriptions to emphasize the two-file plugin boundary and enumerate only genuine behavior/documentation exceptions.

### Low: Published test counts are obsolete

README and architecture documentation say 29 suites. The current runner executes 42 suites; 29 is now the differential golden-scenario count.

**Recommendation:** State both values with their meanings, or generate the suite count in the test output without promising a fixed number in prose.

## Architecture Assessment

No Stage 6 or Stage 7 structural rewrite is warranted. `tick.py` is large but now orchestrates modules with clear ownership: state/configuration, checking, and dispatch. `dashboard-server.py` is also large, but mutation and approval persistence have already been extracted behind useful seams. Splitting either file by line count would mostly add forwarding layers without deleting concepts.

Keep the independent health-monitor stall calculation. Its duplication is intentional dead-man isolation and is protected by parity tests. Also keep Tick's ownership of manifest loading; making the loop depend on a dashboard or shared service would weaken the current failure boundary.

The next work should improve contracts and delete duplicate declarations, not introduce new abstractions.

## Ponytail Audit

`shrink`: Three launchd installers repeat most install, uninstall, status, plist, and help logic; retain their stable entry-point wrappers over one small setup helper. `[yaas-triage/setup/install-launchd.sh, yaas-triage/setup/install-launchd-dashboard.sh, yaas-triage/setup/install-launchd-heartbeat.sh]`

`shrink`: Numeric defaults are repeated across Tick, `tick_state`, dashboard config, `.env.example`, and operator docs; one canonical runtime map plus projections removes the split-brain behavior. `[yaas-triage/tick.py, yaas-triage/tick_state.py, yaas-triage/ops/dashboard-server.py, .env.example]`

`yagni`: Do not split Tick or dashboard into additional manager/service/repository layers solely because of file size; the current extracted seams already isolate the dangerous state mutations. `[yaas-triage/tick.py, yaas-triage/ops/dashboard-server.py]`

`delete`: The legacy `count|preview` checker-result parser can be removed only after a documented compatibility window confirms no external custom checker depends on it; the native JSON result is the replacement. `[yaas-triage/tick_check.py]`

net: -170 lines, -0 deps possible.

## Proposed Work

### Patch 1: Companion correction

Correct README, architecture, `.env.example`, setup output, heartbeat help, checker-authoring skill metadata, and `CLAUDE.example`. Expand path and document contract tests. Repair coverage classification. This patch should not change runtime behavior.

### Patch 2: Canonical defaults

Write failing contract tests, align `NUMERIC_KNOBS` with current behavior, and route Tick/dashboard defaults through the canonical values. This is a small runtime change with scheduling consequences, so it should be isolated from documentation edits.

### Patch 3: Dashboard manifest resilience

Add a route-level failure fixture, then make dashboard projections degrade visibly instead of returning a generic 500 when registry metadata is invalid. Keep the change local to the dashboard process.

### Optional Patch 4: Installer deduplication

Consolidate the shared launchd installer mechanics behind the existing three commands. This is worthwhile simplification but not a correctness prerequisite and should wait until setup-output tests exist.

## Go/No-Go

Proceed with Patches 1 through 3. Treat Patch 4 as optional cleanup. Do not start another broad architecture stage.

## Implementation Result

All four patches were completed on 2026-08-15 after this report was accepted.

- Patch 1 corrected the README, architecture guide, environment example, setup output,
  heartbeat help, checker-authoring skill, and agent example. Path and documentation contracts
  now cover those surfaces, and the coverage report has zero unexplained source files.
- Patch 2 made `NUMERIC_KNOBS` authoritative for the 3-worker concurrency limit, 3,600-second
  tick budget, and 300-second minimum dispatch slice. Tick and dashboard consume those defaults.
- Patch 3 keeps quest-detail routes available when a watch manifest is invalid. The projection
  returns no inferred open threads and displays the registry error instead of returning HTTP 500.
- Patch 4 retained the three installer commands over one shared launchd helper. Installer
  implementation dropped from 281 to 200 lines, a measured reduction of 81 lines with no new
  dependency.

Verification after implementation: 43 suites passed, 29 differential scenarios passed, and all
12 mutations were caught with zero survivors.
