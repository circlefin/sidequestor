# Sidequestor

**Your side quests, handled.** Sidequestor is a local-first Slack sidekick that keeps small
missions moving while you get on with the main storyline. Point it at a workspace, pick your
agent, and it takes the next useful step — then leaves every file, log and decision on disk so
you can always see exactly what it did.

- 🎯 **Quests, not chores** — you describe the mission once; the triage loop watches for what changes.
- 🏠 **Local-first** — your machine, your Slack token, your files. Nothing to host.
- 🔍 **Nothing hidden** — every dispatch leaves a transcript, a timeline entry and a run log.
- 🤖 **Your agent, your call** — `codex` out of the box, `claude` if you prefer.

## Before you start

A short checklist, so nothing surprises you halfway through:

- macOS with launchd (that is what runs the loop) and Python ≥ 3.11.
- The command-line tools `jq`, `curl`, `openssl`, `security` and `open`.
- An already-authenticated `claude` **or** `codex` CLI. Sidequestor never logs in for you.

Sidequestor installs zero Python dependencies, so pip cannot check the list above for you.
The Slack setup script checks the tools it needs before it runs, and
`yaas-triage/ops/doctor.sh` checks the rest.

## How do I install it?

```bash
# Change to an existing project or workspace directory.
cd ~/path/to/existing-workspace
# Optional instead: create a new directory and enter it.
# mkdir -p ~/new-sidequestor-workspace
# cd ~/new-sidequestor-workspace
# Create an isolated Python environment inside that workspace.
python3 -m venv .venv
# Make the Sidequestor commands available in this terminal.
source .venv/bin/activate
# Install the latest stable package from PyPI.
python -m pip install sidequestor
# Add Sidequestor metadata and configuration to this directory.
sidequestor init .
# Save the ready-to-paste Slack YAML manifest.
sq setup --manifest > slack-app-manifest.yaml
# Run interactive onboarding; choose bypassPermissions for the worker backend.
sq setup
# Start triage, heartbeat, and the dashboard and print the dashboard URL.
sq start
```

Nothing of yours is moved or overwritten: `sidequestor init .` only adds `.yaas` metadata alongside your existing files. `sq`, `sidequestor`, and `yaas` are all available aliases. The dashboard workspace label opens the current workspace in Cursor when available, or in macOS’s default IDE/file opener. Set `SIDEQUESTOR_IDE_APP` to prefer another application.

The virtualenv supplies the commands, so `deactivate` puts the shell back the way you found it.

Commands use the current directory when it is an initialized workspace. From elsewhere, pass `--workspace PATH` or set `SIDEQUESTOR_WORKSPACE`; the legacy `YAAS_WORKSPACE` name remains accepted.

## How do I set up the Slack app?

1. Run `sq setup --manifest > slack-app-manifest.yaml`, then paste that YAML at api.slack.com → Create New App → From an app manifest.
2. Choose the workspace and Install to Workspace; an administrator may need to approve it.
3. Confirm that **Agents → Slack Model Context Protocol (MCP) Server** is enabled. The manifest requests this automatically; enable it there manually only if Slack or workspace policy leaves it off.
4. In **OAuth and Permissions → User Token Scopes**, verify the 18 requested user scopes. There are no bot scopes. `reactions:read` may need workspace-admin approval; without it reaction monitoring silently finds nothing. `reactions:write` is required or lifecycle transitions fail with `missing_scope`.
5. If you grant a scope later, reinstall the app and run `sq setup` again.
6. In Basic Information, copy the App ID and Client ID into `.env`, then complete OAuth.

The wizard never overwrites real values. It fills only blank or placeholder settings, and `sq setup --instructions` never edits files. The selected backend is `codex` by default; the default Codex model is `gpt-5.6-luna` at `high` effort.

## What permissions does the worker need?

For unattended work, choose `bypassPermissions` at the Worker permission mode prompt. This lets the selected agent execute filesystem, shell, and network/MCP operations without per-action approval, so use it only in a workspace where that behavior is acceptable.

```bash
# Allow Claude workers to execute unattended tool actions.
SIDEQUESTOR_CLAUDE_PERMISSION_MODE=bypassPermissions
# Allow Codex workers to execute unattended tool actions.
SIDEQUESTOR_CODEX_PERMISSION_MODE=bypassPermissions
```

The optional instruction block targets `CLAUDE.md` for Claude and `AGENTS.md` for every other backend. Neither file is ever created or edited by Sidequestor.

```bash
# Print the optional block without changing any files.
sq setup --instructions
# Use defaults without OAuth.
sq setup --non-interactive
# Use the lower-level launchd operations when needed.
sq setup --render-only|install|status|uninstall
# Install the production launchd jobs.
sq setup --production install
```

## How do I run and inspect it?

```bash
# Start triage, heartbeat, and dashboard jobs for the current workspace.
# Wait for the dashboard and print its selected free loopback URL.
sq start
# Stop every job for the current workspace; the instance ID is optional here.
sq stop
# List currently running Sidequestor instances and their exact workspaces.
sq instances list
# Include stopped or historical registered workspaces as well.
sq instances list --all
# Validate the current workspace and print its build identity.
sq doctor
# Look up the dashboard URL later without restarting anything.
sq dashboard url
# From another directory, stop one registered workspace explicitly.
sq stop INSTANCE_ID
# Print the installed package build identity.
sq --version
```

## How do I upgrade?

```bash
# Upgrade to the latest stable PyPI release, refresh resources, validate, and restart.
sq upgrade

# Or install one explicit branch, tag, or commit from GitHub.
sq upgrade --source https://github.com/OWNER/sidequestor.git --ref BRANCH
```

`sq upgrade` uses the same Python environment as the running `sq` command. It stops production
jobs only when they were previously marked running, invokes pip, then uses a fresh Python process
to sync resources and run `sq doctor`. Previously running jobs restart only after both checks
succeed. Git installs require confirmation because they install code with the worker's permissions;
pass `--yes` for a non-interactive run. Use `--pre` to consider PyPI pre-releases or
`--no-restart` to leave previously running jobs stopped. Git sources are limited to HTTPS GitHub
repository URLs and require an explicit `--ref`; a commit SHA is reproducible while a branch can
move.

If the installed command itself is broken, the equivalent recovery sequence remains
`python -m pip install --upgrade sidequestor`, `sq sync-resources`, `sq doctor`, and `sq start`.

`.env`, `settings.json`, `state/`, `logs/`, and your personal `skills/` survive. `.yaas/engine/current/` is wiped and rebuilt every sync; hand-edits there are intentionally lost. An existing `.env` never gains newly added knobs because `provision_env` fills only placeholders, so diff it against `.env.example` after upgrading. Plists embed the venv’s absolute interpreter path: upgrading in place is fine, but recreating the venv means running `sq setup` again.

Reaction defaults are now standard Unicode names: `robot_face`, `hourglass_flowing_sand`, and `white_check_mark`. Items already queued under an old emoji in `state/triage/pending_reactions.json` are not picked up again, and a message already wearing the old loading emoji keeps it. To keep the old set, pin it with `SIDEQUESTOR_REACTION_PROCESS_EMOJI`, `SIDEQUESTOR_REACTION_LOADING_EMOJI`, and `SIDEQUESTOR_REACTION_DONE_EMOJI`.

## Something looks wrong. Now what?

Start with `sq doctor` — it is the cheapest question you can ask. Include its build line when reporting a problem. `sq start` prints the dashboard URL and records it in `state/dashboard-url.txt`; `sq dashboard url` retrieves it later. `sq dashboard serve` remains available as a foreground developer escape hatch, but the normal persistent lifecycle is `sq start` and `sq stop`. Job errors are in `logs/package-*.err.log`.

## How do I test a checkout?

```bash
# Run the complete unittest suite from the repository checkout.
python -m unittest discover -s tests -p 'test_*.py'
```

For a disposable branch-install test, read `.agents/skills/sidequestor-e2e/SKILL.md`. Before publishing package changes, run `.agents/skills/regression-check/SKILL.md`, then use `.agents/skills/publish-yaas-to-circlefin/SKILL.md` for the signed branch publication. Neither skill modifies `.git-yaas-v2`.

## License

Apache License 2.0. See `LICENSE`.
