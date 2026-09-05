# Sidequestor

**Your side quests, handled.** Sidequestor is a local-first Slack sidekick that keeps small
missions moving while you get on with the main storyline. Point it at a workspace, pick your
agent, and it takes the next useful step — then leaves every file, log and decision on disk so
you can always see exactly what it did.

- 🎯 **Quests, not chores** — you describe the mission once; the triage loop watches for what changes.
- 🏠 **Local-first** — your machine, your Slack token, your files. Nothing to host.
- 🔍 **Nothing hidden** — every dispatch leaves a transcript, a timeline entry and a run log.
- 🤖 **Your agent, your call** — `codex` out of the box, or `claude` and `cursor` if you prefer.

## Before you start

A short checklist, so nothing surprises you halfway through:

- macOS with launchd (that is what runs the loop) and Python ≥ 3.11.
- The command-line tools `jq`, `curl`, `openssl`, `security` and `open`.
- An already-authenticated `claude`, `codex`, **or** `cursor-agent` CLI. Sidequestor never logs in for you.

Telegram user-session watches require the optional `sidequestor[telegram]` extra. The Slack setup
script checks the command-line tools it needs before it runs, and
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
# Run interactive onboarding; choose bypassPermissions when using the Codex backend.
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

The wizard never overwrites real values. It fills only blank or placeholder settings, and `sq setup --instructions` never edits files. The selected backend is `codex` by default; the default Codex model is `gpt-5.6-luna` at `high` effort. You can select `cursor` to use the authenticated `cursor-agent` CLI; leave `SIDEQUESTOR_CURSOR_MODEL` unset to use Cursor's default model, or set it to pin a model.

## What permissions does the worker need?

For unattended Codex work, choose `bypassPermissions` at the Worker permission mode prompt. This lets Codex execute filesystem, shell, and network/MCP operations without per-action approval. Cursor uses its own CLI execution mode and Sidequestor auto-approves its MCP calls; Claude uses its native permission mode. Use unattended backends only in a workspace where that behavior is acceptable.

```bash
# Allow Claude workers to execute unattended tool actions.
SIDEQUESTOR_CLAUDE_PERMISSION_MODE=bypassPermissions
# Allow Codex workers to execute unattended tool actions.
SIDEQUESTOR_CODEX_PERMISSION_MODE=bypassPermissions
```

The optional instruction block targets `CLAUDE.md` for Claude and `AGENTS.md` for every other backend. Neither file is ever created or edited by Sidequestor.

## How do I authorize Telegram or X watchers?

Run these commands from the virtualenv where Sidequestor is installed. If `sq` reports
`command not found`, activate that environment first (`source .venv/bin/activate`) or invoke it
by its full path (`.venv/bin/sq`). Installing Sidequestor does not add `sq` globally. Run from an
initialized Sidequestor workspace, or select one explicitly with `--workspace PATH`.

Telegram watches use your own Telegram user through the official MTProto API. Create an API
application at `my.telegram.org`, then authorize once; the API hash and serialized user session
are stored together in macOS Keychain and no SQLite session file is created.

```bash
python -m pip install 'sidequestor[telegram]'
sq telegram-auth authorize API_ID
sq telegram-auth status
sq telegram-send --peer @chat --message "hello" --quest-id quest-id
```

The authorization command securely prompts for the phone number and API hash, then Telegram asks
for the login code and, when enabled, the account's 2FA password. None of those values are placed
in the command line. `telegram-send` uses that same authorized user session. It logs drafted
`message_text` automatically when `--quest-id` is provided. Despite its compatibility name,
`telegram-send` only saves a native Telegram cloud draft with `SaveDraftRequest`; it never delivers
the message to the recipient. The draft synchronizes to the authorized account's Telegram clients.
Saving a new draft replaces the account's existing cloud draft in that dialog.

X watches use an app bearer token from the X Developer Console. The prompt stores it in Keychain;
watch entries may select another named credential with `credential_id`.

```bash
sq x-auth install-app
sq x-auth status
```

External direct checkers are enabled by connector. New workspaces default to Slack, email, GitHub,
and Jira; Telegram and X remain dormant until explicitly added in the workspace `.env`:

```bash
SIDEQUESTOR_CHECKER_CONNECTORS=slack,email,github,jira,telegram,x
```

Disabling a connector does not delete its watches or advance their watermarks. The older
`SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0` setting remains an additional Slack-only kill switch.

Available direct-polling types are `telegram_chat`, `telegram_search`, and `x_search`. Use an X
query such as `@handle` for mentions or `from:handle` for an author's posts. They detect new
messages/posts; Telegram edits, deletions, reactions, Secret Chats, and X DMs/home timelines are
intentionally outside this no-journal implementation.

To test without permitting a worker to send anything, initialize a disposable workspace, enable
the connectors, create a quest in its dashboard, add a watch, then use `tick --dry-run`:

```bash
WS=/tmp/sidequestor-connector-test
python -m venv "$WS-venv"
"$WS-venv/bin/pip" install -e '.[telegram]'
"$WS-venv/bin/sq" init "$WS" --name connector-test
cp "$WS/.env.example" "$WS/.env"
chmod 600 "$WS/.env"

# Edit $WS/.env: add telegram,x to SIDEQUESTOR_CHECKER_CONNECTORS.
"$WS-venv/bin/sq" --workspace "$WS" telegram-auth authorize API_ID e2e
"$WS-venv/bin/sq" --workspace "$WS" x-auth install-app e2e

# Run this in a second terminal. It serves the test UI without starting background triage.
"$WS-venv/bin/sq" --workspace "$WS" dashboard serve 0
```

Create a quest through that dashboard, then add narrowly scoped watches. Add each watch before
posting its unique test marker so the initial watermark cannot ingest older history:

```bash
"$WS-venv/bin/sq" --workspace "$WS" watch QUEST_ID \
  '{"type":"telegram_chat","credential_id":"e2e","peer":"YOUR_TELEGRAM_USER_ID","include_outgoing":true,"filter_keywords":["sq-e2e-unique"]}'
"$WS-venv/bin/sq" --workspace "$WS" watch QUEST_ID \
  '{"type":"telegram_search","credential_id":"e2e","peer":"YOUR_TELEGRAM_USER_ID","query":"sq-e2e-unique","include_outgoing":true}'
"$WS-venv/bin/sq" --workspace "$WS" watch QUEST_ID \
  '{"type":"x_search","credential_id":"e2e","query":"from:YOUR_TEST_ACCOUNT sq-e2e-unique"}'

# Post/send the unique markers, wait 30 seconds for search indexes, then inspect detection.
"$WS-venv/bin/sq" --workspace "$WS" tick --dry-run
```

The dry run should log `DIRTY` without dispatching. A second dry run must rediscover the same
items because dirty watermarks are committed only after successful acknowledgement. Use
`tick --isolated --fake-worker` to acknowledge them safely, then confirm a final dry run is clean.

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
