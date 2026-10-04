# Changelog

All notable changes to Sidequestor (YaaS). The package version is declared in `pyproject.toml`.

Versions are dated by the day the snapshot was published.

## 0.1.41 - 2026-10-05

### Fixed
- Slack file uploads reconcile `internal_error` and `fatal_error` completion responses,
  which can follow a partial success. An unsettled result exits 3 rather than inviting a
  retry that could duplicate the file and its comment.
- Git upgrades preserve the recorded pipx installation suffix so they upgrade the selected
  environment instead of installing or replacing the unsuffixed tool.

## 0.1.40 - 2026-10-05

### Fixed
- `sq upgrade --installer pip` works again for pip installs that automatic detection cannot
  confirm, such as a virtualenv under a `venvs/` directory or a pyenv, conda, or `--user`
  interpreter. Since 0.1.37 these were refused even though the earlier pip-only upgrader handled
  them. The override is still refused when a uv or pipx receipt or tool directory claims the
  environment, or when pip is not installed in it. Affected installs on 0.1.37 through 0.1.39
  cannot reach this fix through `sq upgrade`; run `python -m pip install --upgrade sidequestor`
  with that installation's interpreter once, then `sq sync-resources` and `sq start`.

## 0.1.39 - 2026-10-02

### Added
- `sq slack-file send` accepts `"interactive": true` for an explicitly requested manual upload
  without a quest. It is refused inside a dispatch, with a `quest_id` or `approval_id`, on a stale
  thread, and in force-draft mode, and it writes no quest timeline entry. Dispatch detection uses
  `SIDEQUESTOR_DISPATCH_TARGET`, so this refusal guards against mistakes rather than acting as a
  security boundary.
- The packaged `yaas-slack-file` skill documents Slack uploads and downloads. Resource sync links it
  into `skills/`, `.agents/skills/`, and `.claude/skills/`.

## 0.1.38 - 2026-10-02

### Added
- `sq slack-file send` posts a local file (such as an image) into a Slack conversation, and
  `sq slack-file fetch` downloads the images and PDFs attached to a message so a worker can open
  them. The Slack MCP can do neither. Uploads go through the same checks as `slack-send.py`:
  dispatch-target matching, `allow_send` or a claimed `remote_request` approval pinned to the
  file's SHA-256 (from `sq slack-file approval-spec`), the stale-thread hold, and timeline logging.
  An upload whose final step may have landed exits 3 instead of being retried. Downloads only send
  the token to Slack-owned HTTPS hosts, stream with a size cap, and never write under `state/`.

### Changed
- The Slack app requests `files:read`, `files:write`, `channels:read`, `groups:read`, `im:read`,
  `im:write`, `mpim:read`, `mpim:write`, `dnd:read`, `emoji:read`, `search:read.users` and the
  `*:write.topic` scopes. Generate the updated manifest with `sq setup --manifest`, add the
  scopes to the existing app, reinstall it, then run `sq setup` to re-authorize.

## 0.1.37 - 2026-10-01

### Changed
- `uv tool install sidequestor` (or `pipx install sidequestor`) is now the recommended install: one
  shared `sq` command works in every terminal without activating a virtualenv. Workspace
  virtualenvs remain supported.
- `sq upgrade` detects whether uv, pipx, or pip owns the installation and upgrades with that tool,
  keeping installed extras. It refuses before stopping jobs when ownership is unclear, validates
  explicit installer overrides against the running environment, and lists other running workspaces
  that share the installation.
- `sq gdoc-comment` checks that its browser driver can start before reserving the idempotency key.
  A missing Playwright installation or a misconfigured driver override now fails without leaving
  an indeterminate record, so the same key can be retried after the fix.
- Launchd jobs and runtime scripts put the installation's own `bin` directory first on PATH, as an
  activated virtualenv did, and keep the caller's PATH after it. When the selected agent CLI is
  still missing, `sq start` consults the login shell's PATH. `SIDEQUESTOR_PATH` pins the PATH
  explicitly. `sq start` and `sq doctor` report the agent CLI the jobs will use.

## 0.1.36 - 2026-09-30

### Changed
- Telegram authorization may omit the API ID when `TELEGRAM_API_ID` is configured in the
  workspace `.env`; the explicit API ID argument remains supported.

## 0.1.35 - 2026-09-30

### Fixed
- The dashboard and open-items views clear a transient watcher watermark hold once that watch
  successfully advances, without clearing an unrelated quest blocker. Unrecovered holds remain
  visible.

## 0.1.34 - 2026-09-30

### Changed
- The dashboard separates watches that need a configuration or authorization fix from checker or
  worker backoffs under investigation and temporary rate limits that will retry automatically.
  Watch cards explain the recorded cause and the next action; resolved checker warnings clear
  from the current-tick snapshot.
- A live dispatch shows as **Dispatching** even when the previous triage tick finished long ago.
  Stopped worker heartbeats, overdue workers, and ticks past the dispatch ceiling have distinct
  dashboard states.

### Fixed
- Long-running but healthy dispatches no longer trigger a false triage-stalled macOS alert.
  Transient worker faults and long gaps between workers require confirmation before notification,
  and notification cooldown survives brief recoveries or changes in the worker fault.
- Slack and Jira checker outcomes retain enough error detail to distinguish access and watch
  configuration problems from throttling and other temporary failures.

## 0.1.33 - 2026-09-24

### Added
- Managed draft-readiness and action-closure policies now require current authoritative context,
  explicit decision states, bounded execution routes, and durable triggers for delayed
  commitments in outbound communication and action-capable quests.

### Fixed
- Slack replies automatically held for stale-thread review now preserve the exact original body
  in the immutable quest timeline, alongside the approval record.
- Delayed commitments now use schedule watches only for time-based triggers and the matching
  surface watch for observable Slack, GitHub, Jira, or email conditions.

## 0.1.32 - 2026-09-24

### Changed
- Gmail reply and fresh-send helpers now delegate threading, quoted history, and message
  construction to the native `gws gmail +reply`, `+reply-all`, and `+send` helpers. Replies
  require an explicit sender-only or reply-all mode and an explicit draft-or-send choice, with
  support for reply-all recipient edits, HTML bodies, attachments, and send-as aliases.

## 0.1.31 - 2026-09-18

### Added
- `sq gdoc-comment` and the managed `yaas-gdoc-anchored-comments` skill add verified,
  text-anchored Google Doc comments through a dedicated Chrome profile. The writer enforces
  quest scope, exact approvals, idempotency, exclusive browser access, Drive verification, and
  timeline logging. The capability is enabled by default and can be disabled with
  `SIDEQUESTOR_GDOC_COMMENTS_ENABLED=0`.
- `sq credentials status` reports Slack credential-helper migration state without reading the
  token, while `sq credentials repair-keychain` provides an explicit foreground recovery flow.

### Changed
- Slack refresh-token access now uses one immutable, versioned Keychain helper per macOS user
  instead of a helper tied to a workspace or Python installation. Existing installs securely
  adopt their previous helper identity on first upgrade; fresh installs use the stable identity
  from their first authorization.
- `sq upgrade` records the instance's actual published dashboard port, waits up to 60 seconds for
  that port to become available after shutdown, and restarts on the same port. If upgrade checks
  or credential migration fail, the affected instance remains stopped for safe recovery.
- Playwright 1.48 or newer is installed as a core dependency for the guarded Google Docs browser
  workflow.

### Fixed
- Slack credential refresh and repair are serialized globally for the current user, preventing
  concurrent workspaces from rotating the same refresh token or racing a repair. Background
  processes fail closed until the foreground helper migration is complete, avoiding repeated
  unattended Keychain prompts; foreground repair allows up to 120 seconds for a response.
- Anchored comment writes now validate CDP as loopback-only before any connection, use exact-case
  unique matching, click Post Comment only once, and preserve a completed idempotency record if
  timeline logging fails. Each idempotent invocation is limited to one anchor to prevent partial
  multi-comment batches, and test-driver overrides require explicit test mode.

## 0.1.30 - 2026-09-17

### Changed
- Quest dispatches now read the complete watched Slack thread before deciding whether the agent
  has a conversational turn, rather than treating every new message as an invitation to reply.
- Routing messages, acknowledgements, intermediate hand-offs, and human-to-human exchanges now
  wait silently; the dispatch watermark still identifies the new activity after full context is
  loaded.

## 0.1.29 - 2026-09-17

### Changed
- Worker replies no longer assign work to colleagues unless the operator explicitly supplied the
  instruction or the recipient already volunteered for that exact work in the same thread.
- Unanswered quest threads receive at most one in-thread nudge before escalation is surfaced to
  the operator; widening the audience or tagging senior colleagues now requires explicit approval.

## 0.1.28 - 2026-09-09

### Fixed
- Complete Slack mention scans transfer an individually blocked conversation to a verified,
  one-shot exact thread watch that includes top-level parent messages, allowing unrelated mentions
  and the broad search watermark to commit without losing the blocked action's retry. Parent
  inclusion is rejected outside this ephemeral bounded handoff protocol, and reads stop at the
  transferred target so later thread replies cannot widen the retry.
- GitHub issue and pull-request watch identity now includes the repository, search qualifiers, and
  pinned account, with empty optional fields normalized before deduplication on both new and legacy
  entries. This permits distinct scoped watches without creating phantom empty-string duplicates.
- GitHub pull-request dispatches ignore updates caused only by their own prior comment or review,
  preventing `involves:<self>` watches from producing repeated responses.

## 0.1.27 - 2026-09-09

### Fixed
- Quest dispatches acknowledge new human replies and comments on the same conversational surface
  before processing them, and retain the event for retry when the connector cannot write.
- Slack DM and mention watches drain paginated search results safely, bank completed prefixes when
  backlogs or transient failures interrupt a scan, and avoid skipping messages or parking their
  watermarks on saturated result pages.

## 0.1.26 - 2026-09-07

### Added
- X user-account watches now cover mentions, selected-user posts, the home timeline, and incoming
  direct messages alongside recent search.
- `sq x-send` supports posts, replies, threads, direct messages, media uploads, and common social
  actions with quest policy, approval binding, and idempotency safeguards.
- `sq telegram-send --send` can explicitly deliver through the authorized Telegram user session;
  drafts remain the default, while quest sends enforce approval and idempotency safeguards.

### Changed
- X authentication now uses OAuth 2.0 Authorization Code with PKCE and rotating user refresh
  tokens instead of app-only bearer tokens.

## 0.1.25 - 2026-09-07

### Changed
- `watch_mode` is now treated as inert legacy watch data: existing and arbitrary values remain
  accepted and preserved on every watch type, but no longer restrict Slack sends or appear in
  the dashboard. Slack send authorization relies on `allow_send` and target-scoped claimed
  approvals.
- Blanket guidance that prohibited worker replies in internal escalation threads has been
  removed. Quests with `allow_send: true` may now reply there when their objective and context
  call for it; use `allow_send: false` when outbound messages require review.

## 0.1.24 - 2026-09-07

### Changed
- Managed workspace resources refresh their generated environment and settings examples during
  engine synchronization and expose engine skills through `.agents/skills` when available.
  `.env.example` and `settings.json.example` are Sidequestor-owned and regenerated, so local
  configuration belongs in `.env` and `settings.json` instead.

## 0.1.23 - 2026-09-07

### Changed
- Triage checker subprocesses run serially by default to avoid connector and state contention.

### Fixed
- Slack watch creation rejects member IDs where conversation IDs are required, threaded sends
  reject unresolved member destinations, and send logging records the resolved DM conversation.
- Slack DM watch guidance uses the correct channel and member identifiers.

## 0.1.22 - 2026-09-06

### Fixed
- `sq stop` removes its workspace's LaunchAgent plists after unloading the jobs, so a stopped
  instance remains stopped across login and reboot while `sq start` can recreate it.
- The quest instruction composer remains mounted across dashboard polls, preserving drafts,
  focus, selection, scroll position, and in-progress text while live quest details refresh.
- Instruction drafts remain scoped to their selected quest, including during detail-fetch and
  submit races, so text cannot be queued against a different quest.

## 0.1.21 - 2026-09-04

### Added
- Telegram replies can be saved as native cloud drafts through `sq telegram-send`; the surface
  uses `SaveDraftRequest` exclusively, never delivers to recipients, and logs drafts to the quest
  timeline for every worker backend.

### Changed
- Telegram draft targets must already exist in the authorized account's dialogs, draft bodies are
  limited to Telegram's 4096-character maximum, and saving replaces that dialog's existing draft.
- The optional Telegram dependency now requires Telethon 1.44 or newer.

### Fixed
- Telegram checker subprocesses use the same Python interpreter as the Sidequestor runtime, so
  optional Telethon installations do not drift between parent and helper processes.

## 0.1.17 - 2026-09-03

### Fixed
- Production upgrades now snapshot and drain every process in each launchd job before installing
  or restarting, including detached worker and dashboard child processes.

## 0.1.16 - 2026-09-03

### Fixed
- Slack approvals authorize only their reviewed channel and thread, including when a recent review
  refreshes the stale-reply guard; an approval for another destination cannot bypass either check.
- Read-only monitoring is limited to exact Slack threads, completed quests cannot send, and watch
  retirement replays tolerate malformed timeline records without losing an earlier audit event.

## 0.1.15 - 2026-09-02

### Fixed
- Slack Connect author metadata is accepted by thread and channel-style message parsers, so
  external replies no longer enter safe backoff or get silently skipped.

## 0.1.14 - 2026-08-31

### Fixed
- Slack Connect process reactions now fall back to an in-thread draft when direct sending is
  restricted, and blocked reaction work is retained across checker sweeps instead of being lost.

## 0.1.13 - 2026-08-31

### Added
- Cursor is now a first-class worker backend in `sq setup`, with optional
  `SIDEQUESTOR_CURSOR_MODEL` pinning and Cursor's account/CLI model default when unset.

### Changed
- The setup template and README document `claude`, `codex`, and `cursor` consistently.
- Cursor setup no longer creates Claude/Codex-only reasoning-effort or permission-mode settings.
- Setup rejects an invalid existing worker backend instead of silently carrying it forward.

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
- The bootstrap dispatch verifies proposed channel, thread, person, repository, and query
  identifiers with a live source read before installing a watch; unverifiable targets remain
  blocked instead of immediately entering checker backoff.
- Regression builds export a clean committed snapshot and explicitly exclude the intentionally
  removed `dispatch/manual-dispatch.sh` path.

### Added
- `sq upgrade` upgrades the package from PyPI by default, or from an explicit HTTPS GitHub
  repository and branch/tag/commit with `--source` and `--ref`. It stops previously running jobs,
  installs through the command's own Python interpreter, synchronizes resources and validates from
  a fresh process, then restores the prior running state only after those checks succeed.

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
