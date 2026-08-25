# Changelog

All notable changes to Sidequestor (YaaS). The package version is declared in `pyproject.toml`.

Versions are dated by the day the snapshot was published.

## 0.1.6.dev0 - 2026-08-25

### Changed
- Dashboard quest creation now uses explicit `sidequestor_bootstrap` quest metadata and a
  dedicated synthetic dispatch instead of a placeholder one-shot schedule.
- Bootstrap state clears only after the dispatch is acknowledged and triage verifies a real
  watch or terminal quest state; failures use the existing no-progress backoff.
- Legacy dashboard placeholders migrate only when their exact historical reason matches, so
  ordinary one-shot schedules are never reclassified as bootstrap work.
- Dashboard initialising state reads the explicit flag, and copied terminal commands shell-quote
  workspace paths.
- Regression builds export a clean committed snapshot and explicitly exclude the intentionally
  removed `dispatch/manual-dispatch.sh` path.

## 0.1.5 - 2026-08-24

Package `sidequestor` 0.1.5.

### Changed
- `sq start` and `sq setup` now wait for the managed dashboard to bind and print its selected
  free loopback URL alongside the triage and heartbeat jobs.
- `sq stop` stops the complete workspace lifecycle, including a matching foreground dashboard
  started with the explicit `sq dashboard serve` developer escape hatch.
- Bare `sq dashboard` now inspects the current URL; foreground serving requires `sq dashboard serve`.

## 0.1.4 - 2026-08-24

Package `sidequestor` 0.1.4.

### Added
- The dashboard header, `sq --version` and `sq doctor` all report the running build: package
  version, short commit, and engine version, with the full sha, ref and source on the chip's
  tooltip. The commit is read from the `direct_url.json` pip writes for a `git+https://` install,
  so no build-time stamping is needed; a source checkout falls back to `git rev-parse`, and both
  degrade to a blank commit rather than raising. Also served as a `build` block on `/api/control`.
- A "How do I upgrade?" section in the README, which did not exist. It names the trap: re-running
  the documented `pip install 'git+…@branch'` is a no-op when the branch moves but the version
  string does not, so `--upgrade --force-reinstall` is required. It also says what survives an
  upgrade and what does not.
- README sections for prerequisites, the Slack app walkthrough and troubleshooting, and a
  `tests/test_reaction_config.py` covering emoji defaults, override precedence, colon stripping
  and validation — none of which had any test.

### Changed
- The reaction workflow defaults are now all standard Unicode emoji, present in every Slack
  workspace: `process` → `robot_face`, `loading` → `hourglass_flowing_sand`, `done` →
  `white_check_mark`. The previous three were custom emoji, so a fresh install's reaction workflow
  silently never triggered — the sweep searched `hasmy:` for an emoji nobody could react with.
  Items already queued under an old emoji in `state/triage/pending_reactions.json` are not picked
  up again and a message already wearing the old loading emoji keeps it; pin
  `SIDEQUESTOR_REACTION_PROCESS_EMOJI` / `_LOADING_EMOJI` / `_DONE_EMOJI` to keep the old set. The
  four dedup state filenames still carry the old names on purpose — they are keyed by role, and
  renaming them would discard "already replied" history.
- The workspace chip in the dashboard header shows the name only; the full path moved to the
  hover tooltip it was already populating, freeing horizontal space in a crowded header.
- The Slack setup instructions name the actual UI: **Agents → "Slack Model Context Protocol (MCP)
  Server"** must be enabled by hand (the manifest cannot set it), and **OAuth and Permissions →
  User Token Scopes** is where the 18 requested user scopes are verified. `reactions:read` may
  need admin approval and reaction monitoring silently finds nothing without it; `reactions:write`
  is required or every lifecycle transition fails `missing_scope`; granting a scope later means
  reinstalling the app and re-running `sq setup`.
- The README and the setup wizard copy are written to be read by a first-time installer rather
  than to specify behavior. User-visible copy says Sidequestor rather than yaas; the `yaas` CLI
  alias, keychain names and `YAAS_*` env prefixes are unchanged.
- Codex is the default worker backend everywhere, and a codex install now provisions
  `gpt-5.6-luna` at `high` reasoning effort. `sq setup` fills only the SELECTED backend's blank
  model and effort, and `.env.example` says so rather than claiming the Claude values are unset
  while shipping them preset. A workspace whose `.env` never pinned `SIDEQUESTOR_AGENT` switches
  from claude to codex on upgrade: pin it explicitly to keep the old backend.
- The optional instruction block printed by `sq setup` and `sq setup --instructions` targets
  `CLAUDE.md` only under the Claude backend; every other backend (codex, cursor) gets `AGENTS.md`.
  Neither file is ever created or edited by Sidequestor.
- Skill cross-references that pointed at a workspace `CLAUDE.md` now name what actually holds the
  rule: `§3d` lives in the `yaas-quest-dispatch` skill, and coexistence and shared-state rules live
  in `OPERATING.md`. Since `sq init` no longer writes a `CLAUDE.md`, those pointers resolved to
  nothing on a fresh workspace.
- Every Markdown file in `state/briefs/` is now displayed. Briefing names are free-form, ordering
  comes from filesystem creation time, and cadence words in names are optional display hints.

### Fixed
- The dashboard's reaction field guide honors a `SIDEQUESTOR_REACTION_*_EMOJI` override. It built
  its lookup from the legacy `YAAS_*` names only, so an override the checker and tick respected
  was invisible in the UI.
- `setup.sh` reads `SIDEQUESTOR_SLACK_CHECKERS_ENABLED`, falling back to the legacy name. A
  workspace where `sq setup` wrote the canonical toggle as `0` still took the Slack-enabled branch
  and demanded the four `SLACK_*` values.
- `ENGINE_VERSION` tracks the package version instead of being pinned at `0.1.0.dev0`, so engine
  directories are genuinely versioned rather than sharing one slot. Stale sibling directories are
  pruned after the `current` symlink is repointed, never before. `migrations.py` and
  `workspace.py` no longer hardcode the same stale string.

### Removed
- `doctor.sh` no longer has a "Worker instructions" section. The dispatch prompt carries the
  operating contract, so a workspace instruction file is optional and user-owned — the check could
  only ever pass, and reporting on it either way was noise. Remaining sections are renumbered.

## 2.5.1 - 2026-08-17

A same-day patch release: eight defects found by auditing the 2.5 snapshot, plus one reported from
using the dashboard. No new features, no config changes required.

### Fixed
- Watermark claims are truncated to Slack's 6 decimals, never rounded. `checkers/result.py`
  formatted `advance_to` with `:.6f`, which is round-half-even and could move a claim FORWARD of the
  point actually proven covered; a message sitting exactly on the rounded microsecond was then read
  as already-seen forever. `slack_dm` and `slack_mention` were also pre-rounding at the call site,
  which made the emit-side fix a no-op for the two checkers that reach it.
- `result.emit()` no longer raises on a non-finite claim, as its docstring promises.
- `classify()` no longer erases a numeric `0` watermark claim through falsiness. An erased claim is
  not a hold: the commit layer falls back to `now - lag` and jumps the watermark to NOW.
- The dashboard no longer rebuilds DOM it has not changed. Every 2s poll rewrote whole subtrees even
  when the payload was identical, which made the prompt box flicker and reset a long draft's scroll
  in the review interface. Writes now compare first, and a textarea's value is left alone when it
  matches.
- Briefings are no longer rebuilt on every poll and discarded (~114ms and 75KB of JSON per poll on a
  150-file archive). They are served on demand from `/api/briefs`.
- Briefings have one canonical timestamp, `at`, derived from the filename with an explicit UTC
  offset. Dates were previously read from the filename as a bare local wall clock, or from the file's
  mtime, which are different things.
- `build_briefs()` checks the `<date>_<hhmm>_<type>` filename prefix, so a stray `.md` in
  `state/briefs/` can no longer sort above every dated file and be served as the newest briefing. A
  trailing segment is kept in the type rather than silently trimmed.
- The markdown renderer's link placeholder can no longer be forged from prose, and a `$&` in a URL
  or label is inserted literally instead of being expanded as a substitution pattern.

### Changed
- A fractional value for a whole-number knob is now refused at startup instead of being floored to 0
  and silently disabling the cap it was meant to set. Knobs whose reader honours a fraction
  (`YAAS_STALE_REPLY_HOURS`, `YAAS_MAX_SPEND_*`) still accept one.

### Added
- `yaas-triage/tests/unit/dashboard-render.test.sh`: the dashboard's renderer, date helpers and
  write guards are tested by behaviour, running the shipped implementations rather than asserting
  that the file contains certain strings. Skips cleanly where `node` is absent, naming what it did
  not cover.

## 2.5 - 2026-08-17

The first versioned release. Everything below landed after the initial public import.

### Added
- Dashboard v2: a manual-review surface with its own overlay, worker state, clearer metrics, a
  revise-and-resubmit path, and the Field Guide theme.
- A quick start (`QUICKSTART.md`) written for someone who has never run the loop.
- Unit coverage for watermark precision, the re-armed approval watch, and the edit route.
- A `doctor.sh` Python version check, so an unsupported interpreter fails with a readable message
  instead of a `TypeError` mid-dispatch.

### Changed
- Watermarks are stored at Slack's 6-decimal precision, so a message can no longer be re-read or
  skipped because of a rounded timestamp.
- Timeline events are stamped by the logging helper rather than the worker, which has no clock.
- README rewritten around the agent-driven install and the Slack-first workflow; `ARCHITECTURE.md`
  and the shipped skills realigned with the runtime that actually runs.
- The approval watch re-arms on every non-terminal transition, so a reviewed item cannot stall.
- Quests are documented as full local agent missions, including how their watches adapt.
- Python 3.9 is supported again; duplicated helpers consolidated.

### Fixed
- Security hardening: path traversal, the approval gate, the parser, a dispatch loop, and token
  exposure.
- Uninitialised quests are distinguished from empty ones, and the create modal no longer
  overpromises.
- The dashboard logo is served from a file with `no-store` instead of an inlined blob.

## Earlier

Pre-2.5 history is in the git log; the initial public import is the root of this repository.
