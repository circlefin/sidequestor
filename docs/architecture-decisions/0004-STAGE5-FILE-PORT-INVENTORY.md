# Stage 5 File Port Inventory

Date: 2026-08-22

This is the file-level inventory of the 232 paths tracked by `.git-yaas-v2`.
The original inventory was recorded before the public YAAS to Sidequestor
rebrand. The amendment below records the post-inventory package changes. The
comparison is against the package source snapshot used to build the final
wheel, `sidequestor-0.1.0.dev0`. `WHOLESALE` means the file bytes are
unchanged in the packaged runtime copy. `AMENDED` means the file remains
recognisably the same battle-tested implementation but has a narrow package or
workspace adapter. `SOURCE-ONLY` means it remains available in the source
repository but is intentionally not runtime package data. `EXCLUDED` means it
is not shipped in the wheel; tests remain available in the source repository
and are exercised separately.

## Rebrand Amendment (2026-08-22)

Stage 5 is complete with a compatibility-preserving public rebrand. The
following changes apply to the package source and its standalone publication
tree; they do not edit `.git-yaas-v2`, the legacy `yaas-triage/` tree, or any
existing workspace state.

| Area | Post-rebrand result | Compatibility boundary |
|---|---|---|
| Python package | Canonical package namespace is `sidequestor`; package metadata is `sidequestor`. | `src/yaas_triage/` contains only three thin import/module shims and no second implementation. |
| Commands | `sidequestor` is canonical; `sq` is a short alias; `yaas` remains an installation alias. | All three invoke `sidequestor.cli:main`. |
| Workspace state | Existing `.yaas/` layout is retained exactly. | There is no automatic `.sidequestor/` migration, so existing quests and watermarks remain readable. |
| Runtime | The unchanged runtime remains bundled under `sidequestor/runtime/yaas-triage/`. | Existing helper paths, skill names, and YAAS runtime keys continue to work. |
| Environment | New templates document `SIDEQUESTOR_*`. | The package boundary maps `SIDEQUESTOR_*` to the unchanged runtime `YAAS_*` contract, with YAAS names still accepted. |
| Launchd | New package-rendered jobs use `com.sidequestor.*` labels and `python -m sidequestor`. | Existing `com.yaas.*` jobs are not renamed or unloaded by the package. |
| Dashboard and help | User-facing package output says Sidequestor. | Dashboard state, routes, and worker protocol are unchanged. |
| OSS metadata | Standalone public tree includes `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, and `VERSION`. | These remain source/publication files, not workspace state. |

The implementation changes are narrow adapters around the battle-tested
runtime. No legacy runtime file was renamed in place, and no active quest,
watermark, checker, or dashboard data was rewritten.

## Import-Boundary Amendment (2026-08-22)

The first public package smoke test exposed a source-layout assumption in four
directly executable helpers. They tried to import runtime modules from
`REPO_ROOT/yaas-triage`, which is valid in the legacy checkout but invalid in
an installed package because `REPO_ROOT` is the user's workspace.

The packaged versions now resolve imports from the installed runtime directory
derived from `__file__`, with `YAAS_RUNTIME_ROOT` taking precedence when the
orchestrator supplies it. Workspace state continues to resolve from
`YAAS_WORKSPACE`; these roots are intentionally not conflated.

| File | Fix | Regression prevented |
|---|---|---|
| `yaas-triage/ledger/approval-helper.py` | Import `approval_state` from the packaged runtime directory and invoke the packaged `add-watch.py`. | `sq setup` failing before OAuth with `ModuleNotFoundError: approval_state`; later approval writes would also fail to arm watches from an installed workspace. |
| `yaas-triage/ledger/add-watch.py` | Resolve checker manifests from the packaged runtime directory when called without `YAAS_RUNTIME_ROOT`. | Approval-watch arming would otherwise look for manifests under the workspace and fail after installation. |
| `yaas-triage/ledger/checker-health.py` | Import `tick_check` from the packaged runtime directory. | Checker health execution failing when invoked directly from the wheel. |
| `yaas-triage/checkers/approval.py` | Import `approval_state` from the packaged runtime directory while retaining the checker directory import. | Approval checks failing outside a source checkout. |
| `yaas-triage/skills/yaas-quest-creation/new-quest.py` | Derive the packaged runtime root from the script location when no runtime override exists. | Quest creation reading manifests from a nonexistent workspace `yaas-triage/`. |

These are narrow import-path amendments only. No triage, approval, checker,
watermark, or dispatch logic was rewritten.

## Summary

| Disposition | Count | Result |
|---|---:|---|
| Wholesale runtime copy | 71 | Bundled under `sidequestor/runtime/yaas-triage/` |
| Amended runtime copy | 24 | Bundled under the same path with narrow path/workspace, rebrand, and setup adapters |
| Wholesale top-level resource | 1 | `settings.json.example` is packaged/materialized wholesale |
| Amended top-level package resource | 2 | `.env.example` and `dashboard.html` are packaged/materialized with the rebrand adapters |
| Amended top-level workspace instruction | 1 | `CLAUDE.example.md` becomes generated `CLAUDE.md` |
| Source-only top-level files | 11 | Repository metadata, docs, CI, and versioning; not workspace runtime files |
| Excluded tests and fixtures | 122 | Not in the wheel; retained in `.git-yaas-v2` and run from the source tree |
| **Total tracked paths** | **232** | Protected-tree verification passed unchanged |

The package does not copy `yaas-triage/` into an initialized workspace. Runtime
files stay in the installed wheel. A workspace receives state, logs, control
metadata, the managed operating instructions, and the managed skill subset
under `.yaas/engine/current/`.

## Top-Level Files

| Source path | Disposition | New-world destination and reason |
|---|---|---|
| `.env.example` | AMENDED | `sidequestor/package_data/env.example`; materialized as workspace `.env.example` with canonical `SIDEQUESTOR_*` names. Runtime YAAS aliases remain accepted. |
| `.github/CODEOWNERS` | SOURCE-ONLY | Public source-repository ownership metadata; not runtime package data. |
| `.github/workflows/scan.yml` | SOURCE-ONLY | Repository CI/security workflow; not runtime package data. |
| `.gitignore` | SOURCE-ONLY | Source-repository ignore policy; workspace behavior is implemented by the package. |
| `ARCHITECTURE.md` | SOURCE-ONLY | Architecture documentation remains in the source repository. Runtime workers use packaged `OPERATING.md`. |
| `CHANGELOG.md` | SOURCE-ONLY | Release documentation; package version is taken from `pyproject.toml`. |
| `CLAUDE.example.md` | AMENDED | Becomes a generated workspace `CLAUDE.md` that points to `.yaas/engine/current/OPERATING.md` and managed skills. The source example referenced source-tree paths, which do not exist in a package workspace. |
| `CONTRIBUTING.md` | SOURCE-ONLY | Contributor instructions; not installed into a workspace. |
| `LICENSE` | SOURCE-ONLY | Remains in the source repository; the current wheel metadata does not bundle it as runtime data. |
| `QUICKSTART.md` | SOURCE-ONLY | Source/repository onboarding documentation; package commands are exposed through CLI help and packaged operating instructions. |
| `README.md` | SOURCE-ONLY | Repository/readme content; not workspace runtime data. |
| `SECURITY.md` | SOURCE-ONLY | Repository security policy; not runtime package data. |
| `VERSION` | SOURCE-ONLY | The package version is declared in `pyproject.toml`; this standalone source marker is not copied. |
| `dashboard.html` | AMENDED | `sidequestor/runtime/dashboard.html`; the dashboard branding is Sidequestor while routes and state behavior remain unchanged. |
| `settings.json.example` | WHOLESALE | `sidequestor/package_data/settings.json.example`; materialized into a new workspace. This was added during this audit because the wholesale `setup.sh` references it. |

## Wholesale Runtime Files

All paths in this section are copied byte-for-byte to
`sidequestor/runtime/yaas-triage/<path after yaas-triage/>` in the wheel.
They are not copied into the workspace itself.

```text
yaas-triage/approval_state.py
yaas-triage/assets/sidequestor-mark.png
yaas-triage/checkers/approval.watch.json
yaas-triage/checkers/cron-due.py
yaas-triage/checkers/email.lag
yaas-triage/checkers/email.py
yaas-triage/checkers/email.watch.json
yaas-triage/checkers/github.py
yaas-triage/checkers/github_issue.lag
yaas-triage/checkers/github_issue.py
yaas-triage/checkers/github_issue.watch.json
yaas-triage/checkers/github_pr.lag
yaas-triage/checkers/github_pr.py
yaas-triage/checkers/github_pr.watch.json
yaas-triage/checkers/jira.lag
yaas-triage/checkers/jira.py
yaas-triage/checkers/jira.watch.json
yaas-triage/checkers/result.py
yaas-triage/checkers/schedule.py
yaas-triage/checkers/schedule.watch.json
yaas-triage/checkers/slack_channel.py
yaas-triage/checkers/slack_channel.watch.json
yaas-triage/checkers/slack_dm.py
yaas-triage/checkers/slack_dm.watch.json
yaas-triage/checkers/slack_mention.lag
yaas-triage/checkers/slack_mention.py
yaas-triage/checkers/slack_mention.watch.json
yaas-triage/checkers/slack_thread.py
yaas-triage/checkers/slack_thread.watch.json
yaas-triage/checkers/slack_utils.py
yaas-triage/dispatch/extract-tokens.py
yaas-triage/dispatch/format-stream.py
yaas-triage/dispatch/manual-dispatch.sh
yaas-triage/dispatch/plan.py
yaas-triage/dispatch/slack-read-health.py
yaas-triage/dispatch/spend-window.py
yaas-triage/dispatch/translate-stream.py
yaas-triage/dispatch/worker.mcp.json
yaas-triage/ledger/commit.py
yaas-triage/ledger/ensure-watch-ids.py
yaas-triage/ledger/housekeep.py
yaas-triage/ops/doctor.sh
yaas-triage/ops/heartbeat-loop.sh
yaas-triage/ops/sync-yaas-v2.sh
yaas-triage/setup/com.yaas.dashboard.plist.template
yaas-triage/setup/com.yaas.heartbeat.plist.template
yaas-triage/setup/com.yaas.triage.plist.template
yaas-triage/setup/init-yaas-v2-tracking.sh
yaas-triage/setup/install-launchd-common.sh
yaas-triage/setup/install-launchd-dashboard.sh
yaas-triage/setup/install-launchd-heartbeat.sh
yaas-triage/setup/install-launchd.sh
yaas-triage/setup/yaas-app-config.json
yaas-triage/skills/yaas-answering-quality/SKILL.md
yaas-triage/skills/yaas-checker-authoring/SKILL.md
yaas-triage/skills/yaas-gmail-reply/SKILL.md
yaas-triage/skills/yaas-gmail-reply/gmail-reply.py
yaas-triage/skills/yaas-gmail-reply/send_fresh.py
yaas-triage/skills/yaas-ops/SKILL.md
yaas-triage/skills/yaas-quest-creation/SKILL.md
yaas-triage/skills/yaas-quest-dispatch/SKILL.md
yaas-triage/skills/yaas-reactions/SKILL.md
yaas-triage/surfaces/jira-call.sh
yaas-triage/surfaces/keychain-helper.c
yaas-triage/surfaces/mcp-call.sh
yaas-triage/surfaces/react-lifecycle.py
yaas-triage/surfaces/slack-react.sh
yaas-triage/surfaces/slack_credentials.py
yaas-triage/surfaces/timeline_io.py
yaas-triage/tick_check.py
yaas-triage/tick_dispatch.py
```

The wholesale `setup.sh` remains a bundled legacy onboarding helper. The
package-facing `yaas setup` command owns workspace launchd rendering and
shadow-job lifecycle. The shell helper is not silently invoked by the CLI.

## Amended Runtime Files

These 24 files retain the original logic and have only the changes listed.

| Source path | Amendment | Where it lives and why |
|---|---|---|
| `yaas-triage/approval_store.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; approval state must be read from the selected workspace, not the installed wheel or checkout. |
| `yaas-triage/checkers/approval.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; approval watches belong to the selected workspace. |
| `yaas-triage/checkers/reactions.py` | Added initialization watermark lookup and exact timestamp filtering. | Packaged runtime copy; a new workspace must ignore reactions older than `yaas init`, even when Slack search returns same-day results. |
| `yaas-triage/dispatch/dispatch-agent.sh` | Resolves the packaged runtime directory and adds it to Claude’s allowed directory set. | Packaged runtime copy; workers must read installed skills/helpers without a source-tree path. |
| `yaas-triage/dispatch/run-agent.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; worker dispatch state and context must use the selected workspace. |
| `yaas-triage/ledger/ack-watch.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; acknowledgements must update workspace ledgers. |
| `yaas-triage/ledger/add-watch.py` | Added `YAAS_WORKSPACE` and `YAAS_RUNTIME_ROOT` resolution. | Packaged runtime copy; watches belong to the workspace while checker manifests come from the wheel. |
| `yaas-triage/ledger/approval-helper.py` | Added workspace root and packaged runtime helper resolution. | Packaged runtime copy; approval writes stay in workspace state and helper imports stay inside the wheel. |
| `yaas-triage/ledger/checker-health.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; checker backoff/health is workspace state. |
| `yaas-triage/ledger/watch-guard.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; guard snapshots must protect the selected workspace. |
| `yaas-triage/ops/dashboard-server.py` | Added workspace override, packaged runtime root, package asset/manifest paths, and read-only workspace identity in dashboard snapshots. | Packaged runtime copy; dashboard state remains workspace-local while HTML, logo, and manifests come from the wheel, and the UI can identify its selected workspace without inferring from process state. |
| `yaas-triage/ops/health-monitor.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; health state follows the selected workspace. |
| `yaas-triage/ops/notify.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; notifications and state must not resolve to the source checkout. |
| `yaas-triage/ops/rotate-logs.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; log rotation operates on workspace logs. |
| `yaas-triage/ops/dashboard-start.sh` | Rebranded its comment and startup message from YAAS to Sidequestor. | Packaged runtime copy; this helper remains legacy-compatible while user-facing output uses the package brand. |
| `yaas-triage/reaction_config.py` | Gives `SIDEQUESTOR_*` emoji settings precedence while retaining `YAAS_*` fallback names. | Packaged runtime copy; fresh package workspaces use canonical environment names without breaking existing YAAS configurations. |
| `yaas-triage/setup/setup.sh` | Adds `--workspace` and `--oauth-only`, resolves `.env` from the selected workspace, and leaves launchd ownership to the package CLI. | Packaged runtime copy; setup can run from an installed wheel without treating the wheel location as mutable workspace state. |
| `yaas-triage/skills/yaas-quest-creation/new-quest.py` | Added workspace and packaged runtime root resolution. | Packaged runtime copy; quest folders are created in workspace state and watcher manifests come from the wheel. |
| `yaas-triage/surfaces/client.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; Slack surface state/locks follow the selected workspace. |
| `yaas-triage/surfaces/log-event.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; timeline writes must stay inside the selected workspace. |
| `yaas-triage/surfaces/slack-send.py` | Added `YAAS_WORKSPACE` root resolution. | Packaged runtime copy; drafts, sends, and logs must use selected workspace state. |
| `yaas-triage/tick.py` | Added packaged helper paths, package skill instructions, absolute runtime hints, and `YAAS_RUN_AGENT` adapter selection. | Packaged runtime copy; the worker prompt must direct the model to `.yaas/engine/current` and installed helpers, while tests can inject a fake worker. Core dirty-watch, ack, watermark, budget, and dispatch decisions remain unchanged. |
| `yaas-triage/tick_state.py` | Added workspace/runtime root resolution and changed checker validation from executable-bit dependence to regular-file dependence. | Packaged runtime copy; wheel extraction may not preserve executable bits for Python checker files, while the selected interpreter executes them. |
| `yaas-triage/triage-loop.sh` | Resolves failure state under `YAAS_WORKSPACE` instead of assuming a source-tree sibling. | Packaged runtime copy; loop failure counters belong to the workspace. |

## Workspace and launchd amendment (2026-08-23)

All workspace-aware commands use one resolver. It canonicalizes the supplied
`--workspace`, canonical `SIDEQUESTOR_WORKSPACE`, legacy `YAAS_WORKSPACE`, or
current directory and walks toward the
filesystem root until it finds `.yaas/instance.json`. This applies to nested
directories and to `doctor`, `instances doctor/register/rekey`, `migrate`,
`sync-resources`, setup, start/stop, tick/loop, dashboard, ledger, and isolated
surface commands. An explicit instance ID is resolved through the advisory
registry and must identify exactly one entry. `init` remains path-explicit
because it creates a workspace rather than discovering one.

Production launchd jobs are native package adapters rather than amended legacy
runtime files. Their accepted behavior is:

- Labels include the workspace instance ID and manifests record both that ID
  and the canonical workspace path. Status, stop, and uninstall reject copied
  or stale manifests that identify another workspace.
- The interpreter path is made absolute without resolving the venv's Python
  symlink. Persistent jobs therefore continue using the environment where
  Sidequestor was installed.
- Launchd exports both canonical `SIDEQUESTOR_WORKSPACE`/
  `SIDEQUESTOR_RUNTIME_ROOT` names and their YAAS runtime aliases. Registry
  storage similarly gives `SIDEQUESTOR_CONFIG_HOME` precedence over
  `YAAS_CONFIG_HOME`.
- The dashboard production job requests port `0`; the adapter allocates a free
  loopback port and writes `state/dashboard-url.txt` only after the server has
  bound successfully. It removes the readiness file when the server exits.
- Dashboard control and full snapshots expose the selected workspace's display
  name, canonical path, and instance ID. The top bar shows the name and an
  ellipsized path, retains the full path as a tooltip, and hides only the path
  on narrow screens. This prevents operators from acting on the wrong local
  instance while preserving the existing dashboard hierarchy.
- `sq stop` with no selector discovers the containing workspace, unloads only
  that instance's jobs, and retains its manifest and plists for restart.
  `sq setup --production uninstall` unloads the same jobs and removes their
  package-owned plists and manifest.
- Reinstall unloads prior labels for that workspace, writes and bootstraps the
  current UUID-scoped jobs, marks the manifest running, and removes superseded
  package-owned plist paths after success.
- Rekeying is refused while shadow or production launchd manifests exist. The
  operator must uninstall first so a new UUID cannot orphan old labels.
- The dashboard adapter forwards `SIGTERM` and `SIGINT` to its server child.
  This prevents `launchctl bootout` from leaving an orphan listener or stale
  readiness URL.

## Tests, Fixtures, and Goldens

All 122 of these tracked files are intentionally excluded from the wheel. They
were not copied into the initialized workspace. The package has its own
command-surface and behavior suite, while these original tests remain in the
public source tree as the regression reference.

```text
yaas-triage/tests/behaviour/approval-edit-route.test.sh
yaas-triage/tests/behaviour/approval-lease.test.sh
yaas-triage/tests/behaviour/approval-projection.test.sh
yaas-triage/tests/behaviour/approval-stalled-producer.test.sh
yaas-triage/tests/behaviour/approval-transitions.test.sh
yaas-triage/tests/behaviour/approval-undo-reclaim.test.sh
yaas-triage/tests/behaviour/budget-gate.test.sh
yaas-triage/tests/behaviour/checker-contract.test.sh
yaas-triage/tests/behaviour/dashboard-routes.test.sh
yaas-triage/tests/behaviour/dirty-watch-dispatch.test.sh
yaas-triage/tests/behaviour/doc-contracts.test.sh
yaas-triage/tests/behaviour/log-event.test.sh
yaas-triage/tests/behaviour/manual-instruction-queue.test.sh
yaas-triage/tests/behaviour/path-references.test.sh
yaas-triage/tests/behaviour/reaction-approval-routing.test.sh
yaas-triage/tests/behaviour/repo-root.test.sh
yaas-triage/tests/behaviour/slack-checkers-toggle.test.sh
yaas-triage/tests/behaviour/tick-offline-gate.test.sh
yaas-triage/tests/behaviour/timeline-helper-dedup.test.sh
yaas-triage/tests/behaviour/unacked-backoff.test.sh
yaas-triage/tests/coverage.sh
yaas-triage/tests/differential/README.md
yaas-triage/tests/differential/goldens/advance_to_exact_value.json
yaas-triage/tests/differential/goldens/agent_hard_failure_holds.json
yaas-triage/tests/differential/goldens/agent_timeout_keeps_banked_acks.json
yaas-triage/tests/differential/goldens/checker_emits_hold.json
yaas-triage/tests/differential/goldens/checker_error_holds.json
yaas-triage/tests/differential/goldens/clean_tick.json
yaas-triage/tests/differential/goldens/dirty_acked_blocked_holds.json
yaas-triage/tests/differential/goldens/dirty_acked_handled.json
yaas-triage/tests/differential/goldens/dirty_unacked_holds.json
yaas-triage/tests/differential/goldens/fairness_rotation.json
yaas-triage/tests/differential/goldens/incomplete_window_holds.json
yaas-triage/tests/differential/goldens/mixed_ack_statuses.json
yaas-triage/tests/differential/goldens/no_active_quests.json
yaas-triage/tests/differential/goldens/non_slack_dispatches_while_slack_down.json
yaas-triage/tests/differential/goldens/nothing_to_do_advances.json
yaas-triage/tests/differential/goldens/nothing_to_do_with_saturated_window_holds.json
yaas-triage/tests/differential/goldens/partial_ack_isolates_items.json
yaas-triage/tests/differential/goldens/reactions_dispatch_first.json
yaas-triage/tests/differential/goldens/reactions_dont_starve_quests.json
yaas-triage/tests/differential/goldens/reactions_target.json
yaas-triage/tests/differential/goldens/retire_completed_approval.json
yaas-triage/tests/differential/goldens/retire_fired_one_shot_schedule.json
yaas-triage/tests/differential/goldens/retire_respects_custom_window.json
yaas-triage/tests/differential/goldens/retire_respects_never.json
yaas-triage/tests/differential/goldens/retire_stale_thread.json
yaas-triage/tests/differential/goldens/slack_down_gates_dispatch.json
yaas-triage/tests/differential/goldens/two_quests_isolated.json
yaas-triage/tests/differential/goldens/watch_ratelimited_surfaces.json
yaas-triage/tests/differential/goldens/worker_appends_watch.json
yaas-triage/tests/differential/mutations.sh
yaas-triage/tests/differential/run.sh
yaas-triage/tests/differential/scenarios/advance_to_exact_value.json
yaas-triage/tests/differential/scenarios/agent_hard_failure_holds.json
yaas-triage/tests/differential/scenarios/agent_timeout_keeps_banked_acks.json
yaas-triage/tests/differential/scenarios/checker_emits_hold.json
yaas-triage/tests/differential/scenarios/checker_error_holds.json
yaas-triage/tests/differential/scenarios/clean_tick.json
yaas-triage/tests/differential/scenarios/dirty_acked_blocked_holds.json
yaas-triage/tests/differential/scenarios/dirty_acked_handled.json
yaas-triage/tests/differential/scenarios/dirty_unacked_holds.json
yaas-triage/tests/differential/scenarios/fairness_rotation.json
yaas-triage/tests/differential/scenarios/incomplete_window_holds.json
yaas-triage/tests/differential/scenarios/mixed_ack_statuses.json
yaas-triage/tests/differential/scenarios/no_active_quests.json
yaas-triage/tests/differential/scenarios/non_slack_dispatches_while_slack_down.json
yaas-triage/tests/differential/scenarios/nothing_to_do_advances.json
yaas-triage/tests/differential/scenarios/nothing_to_do_with_saturated_window_holds.json
yaas-triage/tests/differential/scenarios/partial_ack_isolates_items.json
yaas-triage/tests/differential/scenarios/reactions_dispatch_first.json
yaas-triage/tests/differential/scenarios/reactions_dont_starve_quests.json
yaas-triage/tests/differential/scenarios/reactions_target.json
yaas-triage/tests/differential/scenarios/retire_completed_approval.json
yaas-triage/tests/differential/scenarios/retire_fired_one_shot_schedule.json
yaas-triage/tests/differential/scenarios/retire_respects_custom_window.json
yaas-triage/tests/differential/scenarios/retire_respects_never.json
yaas-triage/tests/differential/scenarios/retire_stale_thread.json
yaas-triage/tests/differential/scenarios/slack_down_gates_dispatch.json
yaas-triage/tests/differential/scenarios/two_quests_isolated.json
yaas-triage/tests/differential/scenarios/watch_ratelimited_surfaces.json
yaas-triage/tests/differential/scenarios/worker_appends_watch.json
yaas-triage/tests/fixtures/codex_bad.ndjson
yaas-triage/tests/fixtures/codex_ok.ndjson
yaas-triage/tests/fixtures/cursor_bad.ndjson
yaas-triage/tests/fixtures/cursor_ok.ndjson
yaas-triage/tests/lib/scenario.py
yaas-triage/tests/lib/snapshot.py
yaas-triage/tests/run-all.sh
yaas-triage/tests/unit/approval_store.test.sh
yaas-triage/tests/unit/checkers/github_issue.test.sh
yaas-triage/tests/unit/checkers/github_pr.test.sh
yaas-triage/tests/unit/checkers/jira.test.sh
yaas-triage/tests/unit/checkers/reactions.test.sh
yaas-triage/tests/unit/checkers/slack_utils.test.sh
yaas-triage/tests/unit/checkers/transient-cause.test.sh
yaas-triage/tests/unit/dashboard-render.test.sh
yaas-triage/tests/unit/dispatch/plan.test.sh
yaas-triage/tests/unit/dispatch/run-agent.test.sh
yaas-triage/tests/unit/dispatch/slack-read-health.test.sh
yaas-triage/tests/unit/ledger/add-watch.test.sh
yaas-triage/tests/unit/ledger/commit.test.sh
yaas-triage/tests/unit/ledger/ensure-watch-ids.test.sh
yaas-triage/tests/unit/ledger/housekeep.test.sh
yaas-triage/tests/unit/ledger/watch-guard.test.sh
yaas-triage/tests/unit/ops/dashboard-server.test.sh
yaas-triage/tests/unit/ops/doctor.test.sh
yaas-triage/tests/unit/ops/health-monitor.test.sh
yaas-triage/tests/unit/ops/notify.test.sh
yaas-triage/tests/unit/ops/rotate-logs.test.sh
yaas-triage/tests/unit/setup/install-launchd.test.sh
yaas-triage/tests/unit/setup/setup.test.sh
yaas-triage/tests/unit/skills/yaas-quest-creation/new-quest.test.sh
yaas-triage/tests/unit/surfaces/client.test.sh
yaas-triage/tests/unit/surfaces/react-lifecycle.test.sh
yaas-triage/tests/unit/surfaces/slack-credentials.test.sh
yaas-triage/tests/unit/surfaces/slack-send.test.sh
yaas-triage/tests/unit/tick_check.test.sh
yaas-triage/tests/unit/tick_dispatch.test.sh
yaas-triage/tests/unit/tick_security.test.sh
yaas-triage/tests/unit/tick_state.test.sh
yaas-triage/tests/unit/watermark-precision.test.sh
```

## New Package Files

These files were introduced by the package layer and have no `.git-yaas-v2`
source-path equivalent:

```text
pyproject.toml
README.md
.gitignore
src/sidequestor/__init__.py
src/sidequestor/__main__.py
src/sidequestor/cli.py
src/sidequestor/dashboard.py
src/sidequestor/isolated.py
src/sidequestor/launchd.py
src/sidequestor/migrations.py
src/sidequestor/native.py
src/sidequestor/resources.py
src/sidequestor/workspace.py
src/sidequestor/package_data/OPERATING.md
src/sidequestor/package_data/settings.json.example
src/sidequestor/runtime/yaas-triage/dispatch/fake-worker.py
src/yaas_triage/__init__.py
src/yaas_triage/__main__.py
src/yaas_triage/cli.py
tests/command_surface/test_stage2.py
tests/behavior/test_stage2.py
tests/full_workspace/test_stage3.py
tests/test_launchd_lifecycle.py
tests/test_runtime_imports.py
tests/test_setup.py
```

The package also creates build-only files such as `.venv/`, `dist/`, wheel
metadata, compatibility caches, and the development `live-runtime` symlink.
These are not workspace files and are not included in the wheel.

The former `src/yaas_triage/` package adapters were renamed to
`src/sidequestor/`. Only the three compatibility shims listed above retain the
old Python namespace. The runtime payload is still copied wholesale or with
the narrow amendments listed earlier, and remains physically under
`src/sidequestor/runtime/yaas-triage/`.

## New Workspace Files

`yaas init` introduces the following workspace-owned layout. The package code
and source runtime are deliberately absent:

```text
.env                         # user-owned; configured separately
.env.example                 # packaged safe template
CLAUDE.md                    # generated package-aware entrypoint
settings.json                # user-owned settings, initially {}
settings.json.example       # packaged wholesale safe template
.yaas/.yaas-version
.yaas/instance.json
.yaas/engine/<version>/OPERATING.md
.yaas/engine/<version>/skills/<managed skill files>
.yaas/engine/current          # symlink to the active engine version
.yaas/rendered-launchd/<job>.plist
logs/<triage,worker,dashboard files>
state/quests/active/<quest>/{meta,context,watch,timeline files}
state/triage/reaction-watermark.json
state/triage/<ledgers,manifests,health and run state>
state/dashboard-token
state/dashboard-url.txt
```

The package currently does not materialize the personal `skills/` directory
with public runtime skills; that root directory is created empty. Managed
skills live under `.yaas/engine/current/skills/` so engine upgrades are
versioned and do not overwrite personal skills.

## Validation and Safety

- Normalized inventory verification: zero missing legacy runtime files, zero
  undocumented differences, and exactly 24 documented amended files. The only
  package-only runtime file is `dispatch/fake-worker.py`.
- Protected legacy tree: no tracked changes. Its unrelated pre-existing
  untracked audit/docs/test files were left untouched.
- Final audit wheel: `sidequestor-0.1.1.dev0`, SHA-256
  `f524e8f511b00e813d9a02d514509392f3644b1d44951545034cd44667468786`.
  Its 129 entries contain 97 runtime and 9 managed-skill entries, with no tests,
  bytecode, or cache paths.
- Installed-wheel package suite: `31 tests, OK` on the developer machine,
  including both localhost dashboard tests. The same suite reports two socket
  skips only inside the managed sandbox where binding is denied.
- Real launchd E2E: production install, three-job UUID identity, authenticated
  dashboard access, selector-free nested-directory stop, old-listener
  termination, restart on a newly selected free port, status, and production
  uninstall all passed in a disposable workspace.
- Legacy baseline: 53 of 54 shell suites passed and all 29 differential tick
  goldens passed. The sole baseline failure is the stale `doc-contracts` check
  requiring checker-authoring literals in `CLAUDE.example.md`; that file now
  deliberately routes source mutation to a separate checkout, while the full
  checker-authoring contract remains bundled in the runtime skill.
- Leak review: no tracked env/state/log/E2E paths, token-like values, Circle
  email addresses, gitlinks, or nested Git paths. Matches were code identifiers,
  empty configuration keys, Keychain commands, and example URLs. `trivy` was
  unavailable locally.
- Signed internal-testing branch: `publish/sidequestor-pip-internal-testing`
  at `f28377a`. It predates the final-audit changes documented here and requires
  republishing before it includes them.
- The original `.git-yaas-v2` tracked tree was not edited, moved, or replaced.
- Production cutover was not performed. The package workspace is running
  side-by-side; the legacy source and its rollback paths remain preserved.
