# Changelog

All notable changes to Sidequestor (YaaS). The current version also lives in `VERSION`.

Versions are dated by the day the snapshot was published.

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
