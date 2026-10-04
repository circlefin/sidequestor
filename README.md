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

- macOS with launchd (that is what runs the loop).
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) to install the `sq` command
  (`brew install uv` or `brew install pipx`). uv also downloads Python ≥ 3.11 when the Mac does
  not have it.
- The command-line tools `jq`, `curl`, `openssl`, `security` and `open`.
- An already-authenticated `claude`, `codex`, **or** `cursor-agent` CLI. Sidequestor never logs in for you.

Telegram user-session watches require the optional `sidequestor[telegram]` extra. The Slack setup
script checks the command-line tools it needs before it runs, and
`yaas-triage/ops/doctor.sh` checks the rest.

## How do I install it?

```bash
# Install the sq command once for your user (or: pipx install sidequestor).
uv tool install sidequestor
# Add the tool's command directory to PATH if the installer asks you to, then open a new terminal.
uv tool update-shell
# Change to an existing project or workspace directory.
cd ~/path/to/existing-workspace
# Optional instead: create a new directory and enter it.
# mkdir -p ~/new-sidequestor-workspace
# cd ~/new-sidequestor-workspace
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

One installation serves every workspace, and `sq` works in any new terminal without activating
anything. To add optional extras, reinstall with them, for example
`uv tool install --force 'sidequestor[telegram]'` or
`pipx install --force 'sidequestor[telegram]'`.

A workspace-local virtualenv still works if you prefer one: `python3 -m venv .venv`,
`source .venv/bin/activate`, then `python -m pip install sidequestor`. Its commands are available
only while that environment is active, or by full path such as `.venv/bin/sq`.

Background jobs record the PATH of the terminal that runs `sq start`, with the installation's own
`bin` first, and `sq start` prints the agent CLI they will use. If it reports the CLI as missing,
install it or set `SIDEQUESTOR_PATH` in the workspace `.env` to a PATH that contains it, then run
`sq start` again.

Commands use the current directory when it is an initialized workspace. From elsewhere, pass `--workspace PATH` or set `SIDEQUESTOR_WORKSPACE`; the legacy `YAAS_WORKSPACE` name remains accepted.

## How do I set up the Slack app?

1. Run `sq setup --manifest > slack-app-manifest.yaml`, then paste that YAML at api.slack.com → Create New App → From an app manifest.
2. Choose the workspace and Install to Workspace; an administrator may need to approve it.
3. Confirm that **Agents → Slack Model Context Protocol (MCP) Server** is enabled. The manifest requests this automatically; enable it there manually only if Slack or workspace policy leaves it off.
4. In **OAuth and Permissions → User Token Scopes**, verify the 31 requested user scopes. There are no bot scopes. `reactions:read` may need workspace-admin approval; without it reaction monitoring silently finds nothing. `reactions:write` is required or lifecycle transitions fail with `missing_scope`.
5. If you grant a scope later, reinstall the app and run `sq setup` again.
6. In Basic Information, copy the App ID and Client ID into `.env`, then complete OAuth.

For an existing app, generate the updated manifest with `sq setup --manifest`, add the newly
requested user scopes in the app's OAuth settings, reinstall the app, and run `sq setup` again to
re-authorize the token. File uploads need `files:write`; attachment downloads need `files:read`.

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

## How do I use Slack files?

`sq slack-file fetch '{"channel_id":"C...","ts":"<message ts>"}'` downloads the images and PDFs
attached to one Slack message into a private temporary directory and prints their local paths.
Open those paths to inspect the attachments. You can also fetch one attachment with
`{"file_id":"F..."}`. Downloads are capped at 20 MiB per file by default, and the command
reports files it skips.

`sq slack-file send '{"quest_id":"<active quest>","channel_id":"C...","file":"/path/to/image.png","initial_comment":"Here is the chart"}'`
posts a local file. A quest needs `allow_send: true` or a claimed approval for this exact file,
comment, and destination. Run `sq slack-file approval-spec '<same JSON>'` to prepare that approval.
For an explicitly requested interactive upload without a quest, use
`sq slack-file send '{"interactive":true,"channel_id":"D...","file":"/path/to/image.png"}'`.
This mode is unavailable inside a dispatch; that check reads an environment variable, so treat
it as a guard against mistakes rather than a security boundary. The packaged `yaas-slack-file` skill is linked into
`skills/`, `.agents/skills/`, and `.claude/skills/` when workspace resources sync.
For a reply, include `"thread_ts":"<parent ts>"`. If sending exits 3, the file may already be
posted; inspect the Slack conversation before trying again.

## How do I authorize Telegram or X watchers?

Run from an initialized Sidequestor workspace, or select one explicitly with `--workspace PATH`.
If `sq` reports `command not found` with a uv or pipx install, run `uv tool update-shell` or
`pipx ensurepath` and open a new terminal; with a workspace virtualenv, activate it first.

Telegram watches use your own Telegram user through the official MTProto API. Create an API
application at `my.telegram.org`, then authorize once; the API hash and serialized user session
are stored together in macOS Keychain and no SQLite session file is created.
You may put the non-secret app identifier in the workspace `.env` as `TELEGRAM_API_ID` and
omit `API_ID` from the authorization command.

```bash
uv tool install --force 'sidequestor[telegram]'  # or pipx install --force, or pip install in a venv
sq telegram-auth authorize API_ID  # or omit API_ID when TELEGRAM_API_ID is in .env
sq telegram-auth status
sq telegram-send --peer @chat --message "hello" --quest-id quest-id
sq telegram-send --peer @chat --message "hello" --send --quest-id quest-id --idempotency-key unique-key
```

The authorization command securely prompts for the phone number and API hash, then Telegram asks
for the login code and, when enabled, the account's 2FA password. None of those values are placed
in the command line. `telegram-send` uses that same authorized user session and logs `message_text`
automatically when `--quest-id` is provided. It saves a native Telegram cloud draft by default;
the draft synchronizes to the authorized account's Telegram clients, and a new draft replaces the
existing draft in that dialog. Pass `--send` to deliver instead. Dispatched sends require an active
quest with `allow_send: true` or an exact claimed approval, plus an `idempotency_key`; an
interrupted send is held for inspection instead of retried blindly.

X uses OAuth 2.0 Authorization Code with PKCE to act as the account that approves access. Create
an X Developer App configured as a public/native OAuth 2.0 client, enable the callback
`http://127.0.0.1:8765/callback`, then authorize it. Sidequestor stores the user access and rotating
refresh tokens in Keychain; it does not accept an app-only bearer token.

```bash
sq x-auth authorize YOUR_OAUTH2_CLIENT_ID
sq x-auth status
sq x-auth revoke
```

External direct checkers are enabled by connector. New workspaces default to Slack, email, GitHub,
and Jira; Telegram and X remain dormant until explicitly added in the workspace `.env`:

```bash
SIDEQUESTOR_CHECKER_CONNECTORS=slack,email,github,jira,telegram,x
```

Disabling a connector does not delete its watches or advance their watermarks. The older
`SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0` setting remains an additional Slack-only kill switch.

Available X polling types are `x_search`, `x_mentions`, `x_user_posts`, `x_home`, and `x_dm`.
They cover recent search, the authorized account's mentions and home timeline, posts from a
selected user ID, and incoming DMs. X only exposes DM events from the last 30 days. These pollers
detect new items, not later edits, deletions, reactions, list changes, Spaces, or notifications.

`sq x-send` can create posts, replies, threads, DMs, and media uploads; delete the account's posts;
and like, repost, bookmark, follow, mute, or block and reverse those actions. Quest-owned writes
require an active quest with `allow_send: true` or an exact claimed `remote_request` approval.
Dispatched writes also require an `idempotency_key`; an interrupted attempt is held for inspection
instead of retried blindly. Availability and billing still depend on the X app's current access tier.

`sq gdoc-comment` adds verified text-anchored comments through the Google Docs editor. It uses a
dedicated authenticated Chrome profile, refuses ambiguous anchor text, requires `allow_send` or an
exact claimed approval, and logs quest-owned writes. Each invocation accepts one exact-case anchor
and always requires an independently unique idempotency key. Google Chrome and the `gws` CLI must
be installed, and `gws` must be authenticated with Drive access for capability checks and
post-write verification. The surface is enabled by default; set
`SIDEQUESTOR_GDOC_COMMENTS_ENABLED=0` in the workspace `.env` to disable it. See the installed
`yaas-gdoc-anchored-comments` skill for one-time Chrome authentication and the payload format.

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
# Add TELEGRAM_API_ID to $WS/.env, then omit the API_ID here.
"$WS-venv/bin/sq" --workspace "$WS" telegram-auth authorize
"$WS-venv/bin/sq" --workspace "$WS" x-auth authorize YOUR_OAUTH2_CLIENT_ID e2e

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

`sq upgrade` upgrades the installation running the `sq` command. It detects whether uv, pipx, or
pip in a virtualenv owns that installation and uses the same tool (`uv tool upgrade`,
`pipx upgrade`, or `pip install --upgrade`), keeping any installed extras. If it cannot tell, it
stops before touching anything. `--installer pip` covers pip installs it cannot detect, such as a
venv under a `venvs/` directory or a pyenv, conda, or `--user` interpreter, unless a uv or pipx
receipt or tool directory claims the environment. `--installer uv` or `pipx` can recover from a
missing receipt only when that manager confirms it owns the running environment; Git upgrades
require a valid receipt to preserve installed extras. It stops production
jobs only when they were previously marked running, upgrades the package, then uses a fresh Python process
to sync resources and run `sq doctor`. Previously running jobs restart only after both checks
succeed. The upgrader preserves each instance's actual published dashboard port and waits up to
60 seconds for that port to be released before restarting. Git installs require confirmation
because they install code with the worker's permissions;
pass `--yes` for a non-interactive run. Use `--pre` to consider PyPI pre-releases or
`--no-restart` to leave previously running jobs stopped. Git sources are limited to HTTPS GitHub
repository URLs and require an explicit `--ref`; a commit SHA is reproducible while a branch can
move.

One uv or pipx installation is shared by every workspace, so `sq upgrade` restarts only the
workspace it runs in and lists any other running workspaces that need `sq --workspace PATH start`
to pick up the new version.

The first upgrade to the stable per-user Slack Keychain helper can show one macOS password prompt.
Choose **Always Allow** so background refreshes can use that same helper identity without prompting
again. Run `sq credentials status` to inspect the migration without opening Keychain, or
`sq credentials repair-keychain` in an interactive terminal to retry it. A failed or cancelled
migration leaves the selected instance stopped; after repair, start it normally with `sq start`.

If the installed command itself is broken, upgrade with the installing tool directly
(`uv tool upgrade sidequestor`, `pipx upgrade sidequestor`, or
`python -m pip install --upgrade sidequestor` in the virtualenv), then run `sq sync-resources`,
`sq doctor`, and `sq start`.

`.env`, `settings.json`, `state/`, `logs/`, and your personal `skills/` survive. `.yaas/engine/current/` is wiped and rebuilt every sync; hand-edits there are intentionally lost. An existing `.env` never gains newly added knobs because `provision_env` fills only placeholders, so diff it against `.env.example` after upgrading. Plists embed the installation’s absolute interpreter path: upgrading in place is fine, but recreating the environment (for example `uv tool install --force` with a different Python, or a new venv) means running `sq start` again.

Reaction defaults are now standard Unicode names: `robot_face`, `hourglass_flowing_sand`, and `white_check_mark`. Items already queued under an old emoji in `state/triage/pending_reactions.json` are not picked up again, and a message already wearing the old loading emoji keeps it. To keep the old set, pin it with `SIDEQUESTOR_REACTION_PROCESS_EMOJI`, `SIDEQUESTOR_REACTION_LOADING_EMOJI`, and `SIDEQUESTOR_REACTION_DONE_EMOJI`.

## Something looks wrong. Now what?

Start with `sq doctor` — it is the cheapest question you can ask. Include its build line when reporting a problem. `sq start` prints the dashboard URL and records it in `state/dashboard-url.txt`; `sq dashboard url` retrieves it later. `sq dashboard serve` remains available as a foreground developer escape hatch, but the normal persistent lifecycle is `sq start` and `sq stop`. Job errors are in `logs/package-*.err.log`.

In the dashboard, **Needs a fix** means a watch has a configuration or access problem and shows a
suggested action. **Investigate** means a checker or worker is backing off and will retry; check
the recorded cause if it persists. **Retrying** means a temporary read interruption or rate limit
will be retried automatically. A healthy long worker run shows **Dispatching**, not **Triage
stale**. **Worker stalled**, **Dispatch overdue**, or **Tick overdue** mean the run needs attention:
check `logs/worker-latest.log` and `logs/triage.log`, then run `sq doctor`. The health monitor waits
for a second sample before sending a macOS notification for transient worker faults or a long gap
between workers. A temporary watcher watermark hold clears from the quest's current blockers
after that watch successfully checks again; a separate unresolved quest blocker stays visible.

## How do I test a checkout?

```bash
# Run the complete unittest suite from the repository checkout.
python -m unittest discover -s tests -p 'test_*.py'
```

For a disposable branch-install test, read `.agents/skills/sidequestor-e2e/SKILL.md`. Before publishing package changes, run `.agents/skills/regression-check/SKILL.md`. Use `.agents/skills/publish-sidequestor-to-pypi/SKILL.md` for a PyPI release, or `.agents/skills/publish-yaas-to-circlefin/SKILL.md` for the signed Circlefin branch publication. Neither publication skill modifies `.git-yaas-v2`.

## License

Apache License 2.0. See `LICENSE`.
