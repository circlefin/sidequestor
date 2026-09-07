# Standalone Git Tree for the Sidequestor Package

**Status:** Published for internal pip testing; lifecycle/configuration corrections committed locally and pending publication

**Date:** 2026-08-23

## Decision

The installable Sidequestor package will be maintained and published from a standalone Git
repository outside `yourself-as-a-service`. That repository will have its own `.git` database and
will not be a worktree, alternate Git directory, or branch of `.git-yaas-v2`.

The standalone tree will continue to push branches to the existing destination repository:
`circlefin/sidequestor`. A separate local source repository does not require a separate GitHub
repository.

The durable local source repository is `/Users/guangmian.kung/sidequestor-pip-package`.
The earlier `/private/tmp/sidequestor-package-publish` repository was the initial migration
staging repository and is not the long-term working copy.

The public package branch will be a package-only tree at its root. It will contain the Python
build metadata, package source, runtime resources, documentation, and OSS/legal files required to
install and understand the distribution. It will not contain the legacy repository's private or
unrelated content.

## Why

`.git-yaas-v2` is a mirror of the battle-tested legacy repository. The package snapshot is a
different tree with a different release boundary. Keeping both in one sidecar Git database makes
source provenance, refs, and publication intent unnecessarily ambiguous.

A standalone Git tree gives the package a clear ownership boundary:

- Legacy production code remains patchable through `.git-yaas-v2`.
- Package development has its own commits, branches, and worktree.
- The publisher cannot accidentally read private working files or legacy review material.
- The package can be installed directly from a branch URL because `pyproject.toml` is at the
  repository root.
- The same Circlefin repository and destination permissions remain in use.

## YAAS to Sidequestor Rebrand

The standalone package will be the first broad-release boundary for the Sidequestor name. The
rebrand should happen before the standalone publication commit, while only four people have
installed the old names. It must be a deliberate compatibility migration, not a global search and
replace against `.git-yaas-v2`.

### Canonical names

| Surface | Canonical name | Compatibility behavior |
|---|---|---|
| Product and repository | Sidequestor / `circlefin/sidequestor` | No change required |
| Python distribution | `sidequestor` | The unpublished `sidequestor-yaas` name is not made canonical |
| CLI | `sidequestor` | `sq` is a short convenience alias; `yaas` remains an alias |
| Python namespace | `sidequestor` | `yaas_triage` remains a thin import/entrypoint shim during transition |
| Packaged runtime paths | `sidequestor` | Existing `yaas-triage` paths remain readable for old workspaces |
| Environment variables | `SIDEQUESTOR_*` | `YAAS_*` remains an accepted runtime fallback |
| Workspace state directory | `.yaas` | Stable YAAS v2 storage contract; no automatic rename or porting |
| New launchd labels | `com.sidequestor.*` | Existing `com.yaas.*` labels are managed only for old installations |
| User-facing docs, logs, and dashboard | Sidequestor | Mention YaaS only as the legacy/runtime alias where needed |
| Generic skill paths | `sidequestor-*` for new public paths where safe | Existing `yaas-*` skill names remain loadable; active workspace skill files are not renamed |

### Accepted packaged-runtime adaptations

The packaged runtime is intentionally not a byte-for-byte copy of every legacy
file. The accepted differences are narrow and preserve legacy behavior while
adapting roots to an installed package: workspace-root resolution for mutable
state, runtime-root resolution for packaged helpers, canonical
`SIDEQUESTOR_*` environment precedence with `YAAS_*` fallback, Sidequestor
user-facing labels, and package-owned setup/OAuth entry points. The complete
file-level list and rationale live in [ADR 0004](0004-STAGE5-FILE-PORT-INVENTORY.md).

`sq stop` defaults to the initialized workspace containing the current
directory, including descendant directories, when no instance identifier is
provided. Explicit `--workspace` and `--instance` selection remains supported.
The same ancestor discovery is used consistently by every workspace-aware
command, including instance maintenance and migration.

### Installed launchd lifecycle

`sq setup` and `sq start` install three persistent production jobs: triage,
heartbeat, and dashboard. The generated labels are UUID-scoped, the manifest
is bound to both the UUID and canonical workspace path, and Python jobs retain
the venv interpreter path instead of resolving through its symlink to a global
Python. This keeps a persistent installation independent of the invoking
shell and source checkout.

The production dashboard starts scanning at port 8877, skips occupied ports,
and publishes the selected loopback URL only after successful bind.
Its header identifies the selected workspace by configured display name and
canonical path; the path truncates visually but remains available as a tooltip,
and the compact mobile layout keeps the name while hiding the path.
`sq start` waits for dashboard readiness and prints the selected URL, so the
normal start path brings up triage, heartbeat, and dashboard together.
`sq dashboard url` retrieves the URL later. `sq dashboard serve` remains a
foreground developer escape hatch rather than a second persistent lifecycle.
`sq stop` unloads the containing workspace's jobs and also cleans up a matching
foreground dashboard controller. It retains the manifest but removes the plists
so the instance stays stopped across reboots; `sq start` recreates them. Full
removal is explicit:
`sq setup --production uninstall` unloads jobs and deletes only the plist paths
recorded for that workspace under `~/Library/LaunchAgents`.

Lifecycle and configuration parity are explicit package guarantees. The
dashboard and health monitor consume the same resolved environment object as
triage, so canonical `SIDEQUESTOR_*` settings and legacy `YAAS_*` aliases
cannot diverge between what is displayed and what `tick.py` uses. `sq stop`
checks the result of each exact-label `launchctl bootout`, retries transient
unloads, verifies that the label is absent, and only then records the manifest
as stopped. A remaining label is reported as an error and leaves the manifest
running for diagnosis or retry.

YaaS remains useful as the historical expansion, `Yourself as a Service`, and as a compatibility
term. It is not the primary product or package brand after this migration.

### Rebrand work sequence

1. Inventory every public and persisted occurrence of `yaas`, `YAAS`, and `yaas-triage` across
   package source, entry points, resource paths, environment loading, launchd templates, skills,
   settings, workspace markers, error messages, logs, README text, and dashboard text.
2. Rename the package distribution, console entry points, Python source namespace, packaged
   runtime directory, and new public skill paths to their Sidequestor forms. Update relative
   imports mechanically and keep the runtime logic unchanged.
3. Add compatibility adapters before deleting old names: a `yaas` CLI alias, `YAAS_*` to
   `SIDEQUESTOR_*` translation with canonical-variable precedence, and a `yaas_triage` import or
   module entrypoint shim for old launchd commands and scripts.
4. Preserve the existing workspace storage contract. The Sidequestor package must open and operate
   `.yaas` workspaces without moving or renaming `instance.json`, `state/quests/active/*`,
   `watch.json`, `meta.json`, `context.md`, `timeline.ndjson`, watermarks, settings, logs, or
   dashboard/checker files. Do not implicitly migrate `.yaas` to another directory while active
   colleagues' quests are running.
5. Render new launchd labels with `com.sidequestor.*` only for newly managed package jobs, while
   detecting and preserving existing `com.yaas.*` labels. Never start both old and new jobs for
   one workspace. Existing jobs must continue to point at the same workspace and state paths.
6. Update `.env.example`, `settings.json.example`, OPERATING instructions,
   skills, README, changelog, dashboard headings, diagnostics, and error messages to say
   Sidequestor. Keep compatibility notes concise and operational.
7. Add tests for both directions: a fresh Sidequestor workspace uses only canonical names, and an
   existing YAAS workspace continues to run through the aliases with its files and storage paths
   unchanged, without losing state or duplicating dispatches.

The rebrand is package-scoped. It must not rename or edit the live `yaas-triage/` tree, active
quest folders, dashboard files, checker files, or persisted workspace schemas in the current
working directory. The standalone repository becomes the canonical place for new public names;
the legacy mirror and its storage contract remain separately patchable rollback surfaces.

## Public Tree

The standalone repository will contain only:

```text
sidequestor-package/
  .gitignore
  CHANGELOG.md
  CONTRIBUTING.md
  LICENSE
  README.md
  SECURITY.md
  pyproject.toml
  src/
    sidequestor/
      ...
    yaas_triage/
      compatibility shim only
```

`src/sidequestor/` is the rebranded package snapshot. Its packaged runtime contains the existing
triage logic and generic skills, but the legacy source tree is not copied into the publication
repository as a second top-level implementation. `src/yaas_triage/` is allowed only as a thin
compatibility shim; it must not become a second implementation.

The publication tree excludes:

- Workspace state, credentials, logs, quests, and personal skills.
- Code reviews and private documentation.
- The legacy top-level `yaas-triage/` tree.
- Tests, test fixtures, compatibility projections, and development-only baselines.
- `.venv`, `build`, `dist`, `*.egg-info`, `__pycache__`, and compiled files.

## Installed-Runtime Import Boundary

The package runtime has two deliberately separate roots:

- `YAAS_WORKSPACE` identifies mutable state such as quests, approvals, logs,
  and watermarks.
- `YAAS_RUNTIME_ROOT`, or the runtime directory derived from a helper's own
  `__file__`, identifies immutable packaged code and manifests.

Directly executable helpers must import sibling runtime modules from the second
root, never from `YAAS_WORKSPACE/yaas-triage`. This was corrected in:

- `runtime/yaas-triage/ledger/approval-helper.py`
- `runtime/yaas-triage/ledger/add-watch.py`
- `runtime/yaas-triage/ledger/checker-health.py`
- `runtime/yaas-triage/checkers/approval.py`
- `runtime/yaas-triage/skills/yaas-quest-creation/new-quest.py`

The bug was found when `sq setup` failed before OAuth with
`ModuleNotFoundError: approval_state`. This is an import-path-only fix; the
underlying approval, checker, quest, and triage logic remains copied from the
battle-tested runtime.

The public `.gitignore` is package-specific and excludes generated Python and virtual-environment
artifacts. It is not copied from the legacy repository wholesale because that file contains rules
for unrelated repository content.

## Test Coverage

The standalone repository now carries the migration suites that were previously only available in
the local package harness:

- `tests/behavior/` covers workspace projections, ledgers, isolated adapters, resources,
  reaction watermarks, rendered jobs, and dashboard behavior.
- `tests/command_surface/` verifies help and execution coverage for every public `sq` command,
  including the `sidequestor` and `yaas_triage` compatibility surfaces.
- `tests/full_workspace/` covers the complete shadow workspace lifecycle, fake dispatch,
  dashboard, loop, reinstall/uninstall, and instance isolation.
- `tests/test_runtime_imports.py` verifies installed helper imports and approval-watch arming
  without a source checkout or `PYTHONPATH` runtime override.
- `tests/test_launchd_lifecycle.py` mocks `launchctl` while covering production install, stop,
  restart, status identity checks, venv retention, dynamic dashboard ports, and uninstall.
- `tests/test_runtime_config.py` verifies that canonical package settings resolve to the same
  effective `YAAS_*` values used by triage and the dashboard, and that launchd lifecycle failures
  are not reported as successful stops.

The local ignored skill `.agents/skills/sidequestor-e2e/SKILL.md` provides a guarded live runbook:
it clones a selected branch into a disposable `e2e/` run, installs it, stops only prior E2E
instances, initializes and starts a workspace, creates a quest, and optionally adds a trigger
reaction to an explicitly supplied self-DM. Live Slack activity requires an explicit opt-in.
Cleanup uses each run's own installed `sq` and the explicit production-uninstall
mode, so it neither depends on a global CLI nor leaves package plists behind.

The 2026-08-23 final audit built and installed `sidequestor-0.1.1.dev0` and
completed the normalized legacy comparison, finding exactly the 24 differences
accepted by ADR 0004. The legacy runner passed 53 of 54 shell suites plus all
29 differential goldens; its one stale source-document check is classified in
ADR 0004 and is not an installed-package behavior regression. A subsequent
disposable E2E run exposed the dashboard configuration-display and unchecked
launchd-stop gaps described above; those corrections are now covered by
focused tests and were published to the internal testing branch at `491d8c1`.
An independent Claude review found and prompted fixes for explicit-instance
precedence over an exported workspace and positional `migrate NAME`
compatibility. Its final tracked-diff review reported no findings and validated
the dashboard workspace identity API and responsive top-bar treatment.

The stable `sidequestor-0.1.4` release candidate was then built as both wheel and
sdist. `twine check` passed for both artifacts, and a clean Python 3.14 environment
installed the wheel and passed `sidequestor --version`, `init`, and `doctor` smoke tests.

## Git History

The legacy runtime baseline currently originates from:

```text
.git-yaas-v2
  main: 18797d2 fix: make worker liveness explicit
    |
    +-- 50b3e91 Package YAAS as an installable distribution
```

The standalone repository will fetch `circlefin/sidequestor:master` only to establish a normal
destination-parent relationship. It will then replace the checked-out tree with the package-only
tree and create a signed delivery commit. The resulting branch is a normal descendant of the
destination `master`, while the local package source remains independent of `.git-yaas-v2`.

The existing delivery branch remains available as rollback history:

```text
publish/yaas-v2-20260822-013157
  29fbecd Publish installable YAAS package
  Source-SHA: 50b3e91
```

The signed internal pip-testing publication is
`publish/sidequestor-pip-internal-testing` at `491d8c1`. Release changes in the
standalone source require a subsequent publication before that branch can
represent this decision in full.

## Publication Tooling

The package-local publisher accepts the standalone repository explicitly through
`--source-repo`. It must be invoked through the local `publish.sh`, never through ad hoc `git
push` commands. Its source-repository mode retains:

- Circlefin remote and destination checks.
- Explicit Circlefin signing identity and SSH key verification.
- Source-commit-only publication.
- Gitlink, nested Git, forbidden-path, and unexpected-binary checks.
- Large-diff acknowledgement.
- GitHub signature verification after push.
- No pull-request creation.

The durable source is
`/Users/guangmian.kung/sidequestor-pip-package`. Branch-only publication uses
`--mode publish-new-branch` and does not open a pull request.

## Migration and Verification Plan

1. Maintain the standalone repository with its own `.git`. Configure only the `circlefin-pub`
   remote pointing to `circlefin/sidequestor`.
2. Copy the validated package snapshot into the repository root. Copy the committed OSS and legal
   files from `.git-yaas-v2` exactly. Add only the package-specific README and `.gitignore`
   adaptations required by the new root layout.
3. Commit the package-only source tree locally. Confirm that no legacy paths, tests, reviews,
   private state, generated artifacts, or nested Git directories are tracked.
4. Build a wheel from the standalone tree. Audit that the wheel contains no tests, caches, or
   symlinks and does contain the license metadata, workspace examples, generic skills, dashboard,
   and packaged runtime.
5. Install the wheel in a fresh virtual environment with normal pip build isolation. Run
   `sidequestor init`, `sidequestor doctor`, an isolated tick, launchd rendering, and the dashboard
   smoke test against a fresh workspace with an isolated `HOME`. Confirm that the fresh workspace
   still uses the stable `.yaas` storage contract.
6. Run the compatibility suite against an existing YAAS-shaped workspace. Verify the `yaas` CLI,
   `YAAS_*` environment aliases, old `yaas_triage` entrypoint, existing quest/checker/dashboard
   files, and old launchd labels all behave safely without moving files or creating duplicate
   workers.
7. Run direct-helper import checks from an installed wheel, including approval setup,
   checker-health loading, approval checking, and quest creation with only `YAAS_WORKSPACE`
   supplied. This must succeed without `PYTHONPATH` pointing at the package runtime.
8. Run the official signed publisher in standalone-source mode to create a new delivery branch.
   Do not open a pull request.
9. Verify the remote commit signature, branch tree, package metadata, and the exact documented
   installation command:

   ```bash
   python3.11 -m venv .venv
   .venv/bin/python -m pip install sidequestor
   .venv/bin/sidequestor init ./sidequestor-workspace
   ```

10. Keep the existing `publish/yaas-v2-20260822-013157` branch and the legacy source branch until
   the new branch has passed installation and live smoke checks. Retire either only as a separate,
   explicit cleanup decision.

## Safety Boundaries

This work must not:

- Modify tracked files in the live top-level worktree.
- Rewrite `.git-yaas-v2/main` or its existing public mirror history.
- Unload, replace, or edit legacy production launchd jobs.
- Delete the existing published package branch.
- Copy `.env`, `settings.json`, state, logs, quests, or personal skills into the public tree.

The package source branch and the standalone publication repository are additive. If publication
fails, the old legacy source and the existing published branch remain usable independently.

## Expected End State

There are two intentionally separate source boundaries plus disposable validation workspaces:

```text
yourself-as-a-service/
  .git-yaas-v2/                 legacy source and production mirror

/Users/guangmian.kung/sidequestor-pip-package/
  .git/                         standalone package Git database
  pyproject.toml
  src/sidequestor/
  src/yaas_triage/              compatibility shim only

sidequestor-pip-package/e2e/
  run-*/                        disposable branch-install validation only
```

Both publication paths may target `circlefin/sidequestor`, but neither local Git database will
own or mutate the other. All fresh and existing workspaces continue using `.yaas` until a future,
separate storage migration is explicitly designed, versioned, and approved.
