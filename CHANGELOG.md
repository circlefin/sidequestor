# Changelog

All notable changes to Sidequestor (YaaS). The current version also lives in `VERSION`.

Versions are dated by the day the snapshot was published.

## Unreleased

Package `sidequestor` 0.1.2.dev0.

### Changed
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
