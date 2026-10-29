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
- A `claude`, `codex`, **or** `cursor-agent` CLI before workers start. Setup can
  offer to install a missing Claude Code or Codex CLI if npm is available, and guides
  you through the selected CLI's login when needed.

Telegram user-session watches require the optional `sidequestor[telegram]` extra. The Slack setup
script checks the command-line tools it needs before it runs, and
`yaas-triage/ops/doctor.sh` checks the rest.

Update checks and the Jira, Notion, and X HTTPS helpers use the operating system's
certificate trust through the installed `truststore` dependency. On macOS this
includes corporate certificates trusted by the system, without pinning a certificate
bundle in `.env`. TLS verification remains enabled. Slack and Telegram use their
existing transports.

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
# Run interactive onboarding; bypassPermissions is the default worker permission mode.
sq --workspace . setup
# Start triage, heartbeat, and the dashboard and print the dashboard URL.
sq --workspace . start
```

Always target the folder explicitly when setting up a new or additional workspace.
`cd` and `sidequestor init .` do not override an existing saved default or an exported
workspace variable. `sq init` prints the exact next setup command for its folder,
including safe quoting for paths with spaces. Use that command or `sq --workspace . setup`
from inside the intended workspace.
Setup saves its selected workspace as the default; initializing an additional
folder alone does not replace an existing default.

The repository also includes `install.sh` for a fresh macOS install. It installs the
PyPI package with uv when needed, creates `~/sidequestor` only when no workspace is
registered, runs `sq setup`, and opens the dashboard. A separate `install-circle.sh.in`
template is rendered for the personal Circle mirror; it supplies non-secret Slack app
identifiers so colleagues do not have to paste an `.env` block. The template itself
contains placeholders and is not the Circle installer.

Nothing of yours is moved or overwritten: `sidequestor init .` only adds `.yaas` metadata alongside your existing files. `sq`, `sidequestor`, and `yaas` are all available aliases. The dashboard workspace label opens the current workspace in Cursor when available, or in macOS’s default IDE/file opener. Set `SIDEQUESTOR_IDE_APP` to prefer another application.

One installation serves every workspace, and `sq` works in any new terminal without activating
anything. To add optional extras, reinstall with them, for example
`uv tool install --force 'sidequestor[telegram]'` or
`pipx install --force 'sidequestor[telegram]'`.

A workspace-local virtualenv still works if you prefer one: `python3 -m venv .venv`,
`source .venv/bin/activate`, then `python -m pip install sidequestor`. Its commands are available
only while that environment is active, or by full path such as `.venv/bin/sq`.

`sq start`, `sq stop`, and `sq dashboard` use your saved default workspace from any terminal directory. The first sole valid workspace is adopted automatically in the existing `~/.config/yaas/instances.json` registry (or the configured config home); no files are moved. With multiple workspaces, run `sq setup --select-workspace` once. Explicit `--workspace PATH` or `--instance ID` overrides the default. The selected workspace path is printed on lifecycle commands. `sq status` shows its path, saved dashboard address, and connection issues. `sq dashboard` opens the running dashboard; `sq dashboard url` prints its URL for scripts.

Targeting precedence is an explicit `--workspace PATH` or `--instance ID`, then
`SIDEQUESTOR_WORKSPACE` (or legacy `YAAS_WORKSPACE`), then the saved default. The
current directory is used only when no environment workspace or registered default
is selected. This also applies to `sq setup`. Combining `--workspace` and `--instance`
is rejected before any action.
Explicitly empty `--workspace` or `--instance` values are also rejected. An unset
shell variable passed as a selector cannot silently fall back to the default.
The default picker offers only registrations whose
folders still have the expected identity, shows their IDs, and rechecks the chosen
identity before saving. Inspect skipped stale/missing registrations with
`sq instances list --all`; refresh a folder with
`sq --workspace /path/to/workspace instances register`.

The dashboard defaults to **http://localhost:8877**. Its port is saved in `.yaas/instance.json`; existing published addresses are retained on upgrade. Starting again reuses running jobs. A port conflict is reported without silently choosing 8878 or 8879. Stop Sidequestor before explicitly changing its port with `sq start --dashboard-port PORT`. Missing AI login or confirmed Claude MCP authentication failures pause affected dispatches without adding quest no-progress backoff; rerun `sq --workspace /path/to/workspace setup` to verify and resume.

Background jobs record the PATH of the terminal that runs `sq start`, with the installation's own
`bin` first, and `sq start` prints the agent CLI they will use. If it reports the CLI as missing,
install it or set `SIDEQUESTOR_PATH` in the workspace `.env` to a PATH that contains it, then run
`sq start` again.

Commands use your saved default workspace. Explicit `--workspace PATH`, `--instance ID`, or `SIDEQUESTOR_WORKSPACE` selects another workspace; the legacy `YAAS_WORKSPACE` name remains accepted. Ordinary starts preserve the workspace’s registered Python runtime. Setup and upgrades can refresh that binding explicitly.

## How do I set up the Slack app?

1. Run `sq setup --manifest > slack-app-manifest.yaml`, then paste that YAML at api.slack.com → Create New App → From an app manifest.
2. Choose the workspace and Install to Workspace; an administrator may need to approve it.
3. Confirm that **Agents → Slack Model Context Protocol (MCP) Server** is enabled. The manifest requests this automatically; enable it there manually only if Slack or workspace policy leaves it off.
4. In **OAuth and Permissions → User Token Scopes**, verify the user scopes listed in the generated manifest. There are no bot scopes. `reactions:read` may need workspace-admin approval; without it reaction monitoring silently finds nothing. `reactions:write` is required or lifecycle transitions fail with `missing_scope`.
5. If you grant a scope later, reinstall the app and run `sq --workspace /path/to/workspace setup` again.
6. In Basic Information, copy the App ID and Client ID into `.env`, then complete OAuth.

For an existing app, generate the updated manifest with `sq setup --manifest`, add the newly
requested user scopes in the app's OAuth settings, reinstall the app, and run `sq --workspace /path/to/workspace setup` again to
re-authorize the token. The manifest and OAuth flow use the same scope list in
`setup/yaas-app-config.json`. It includes `channels:write` and `channels:write.invites`
for public-channel management/invitations; these are not needed to create the private
onboarding interface. Sidequestor's channel-creation helper still creates private channels
only. File uploads need `files:write`; attachment downloads need `files:read`.

The wizard never overwrites real values. It fills only blank or placeholder settings, and `sq setup --instructions` never edits files. The selected backend is `codex` by default; the default Codex model is `gpt-6.1-sol` at `high` effort. Setup checks both the Codex and Claude Code CLIs and offers an npm installation for either missing CLI when Codex or Claude is selected. It checks `cursor-agent` when Cursor is selected. npm installation requires Node.js; the Claude Code npm package requires Node.js 22 or later. Installation is optional, but the selected worker CLI must be available before setup starts jobs. Setup guides CLI login, verifies the selected model, and checks the Slack connection used by workers. Claude’s native Slack MCP authorization is separate from Sidequestor Slack OAuth; setup verifies the supported Sidequestor helper used by workers. Rerun `sq --workspace /path/to/workspace setup` to reconnect after credentials expire. Setup offers a CLI update; startup checks only advise and never install updates. You can select `cursor` to use the authenticated `cursor-agent` CLI; leave `SIDEQUESTOR_CURSOR_MODEL` unset to use Cursor's default model, or set it to pin a model.

## How do I get guided onboarding?

At the end of `sq setup`, choose `slack`, `terminal`, or `later`. Choosing Slack or terminal creates
one onboarding quest for the workspace; `later` postpones it. The quest reviews what you already have, checks your connections, drafts
your AGENTS.md and skills, proposes quests, offers personalized hands-on reaction practice, and checks in until day 30.
Early on, it asks whether dashboard launch buttons should use Codex, Claude Desktop, Cursor,
or Terminal, independently of your background worker harness, and remembers your choice
when you resume or switch onboarding channels. You can keep the current choice or defer.
It asks before each change.

```bash
# Start or resume onboarding in your Slack DM.
sq onboard --slack
# Start or resume onboarding with your coding agent in this terminal.
sq onboard --terminal [--codex|--claude|--cursor]
```

Running either command again resumes the same quest and switches its channel. The onboarding quest
starts in your Slack DM and checks whether your current Slack connection can create a private
channel. If it can, onboarding offers a channel such as `#alex-sidequestor-interface`.
If that permission is absent, onboarding continues in your DM or terminal without asking you to
reauthorize Slack. If you accept the available channel offer, it continues there and sets up a separate
ongoing interface quest, so you can manage Sidequestor in plain language after onboarding
ends. You approve the channel name and any required review in the dashboard; the agent
performs the technical steps. No terminal is needed for routine use.

Text-send permissions stay limited to your verified DM and approved interface channel
(`allow_send_channels`); other actions retain their normal approvals. Slack must be
configured before choosing it. Without Slack, background check-ins are recorded for your next
terminal session. Channel preferences live separately from progress so a worker cannot undo a switch.
The optional reactions practice uses examples researched from your role and recent work, sent as separate messages in your private onboarding conversation. You choose the reactions to try: answer, draft, save, follow a thread, and expand your own message. It uses your configured emoji names and records progress so resuming onboarding does not resend the exercise.

The agent selects a verified, granted and watched channel with `sq onboard --slack-channel CHANNEL_ID`;
this selection does not create channels or grant new permissions. `sq onboard --slack-dm`
returns onboarding to the verified owner DM if the channel becomes unavailable.

## How do I create a private Slack channel?

After upgrading, add `groups:write`, `groups:write.invites`, and `groups:write.topic` to
**User Token Scopes**, reinstall the Slack app, and run `sq --workspace /path/to/workspace setup` again to authorize them.
The generated manifest includes these scopes.

```bash
sq slack-channel create '{"interactive":true,"name":"project-planning"}'
sq slack-channel invite '{"interactive":true,"channel_id":"C...","users":["U...","U..."]}'
sq slack-channel topic '{"interactive":true,"channel_id":"C...","topic":"Project planning"}'
sq slack-channel purpose '{"interactive":true,"channel_id":"C...","purpose":"Coordinate the project"}'
```

Creation always sets `is_private: true`. For automated quest work, replace `interactive`
with `quest_id`; the quest needs `allow_send: true` or an exact claimed approval from
`sq slack-channel approval-spec '{"action":"create","name":"project-planning"}'`.
Each operation is separate, so a failed invitation never causes the channel to be recreated.
Exit 3 means the remote outcome is uncertain or the action succeeded but logging failed:
inspect Slack before retrying. No channel is created by upgrading or running setup.

## What permissions does the worker need?

For unattended work, `sq setup` defaults to `bypassPermissions` for Claude, Codex, and Cursor. Press Enter to accept it if a blank permission setting prompts you, or enter another supported mode. Claude's bypass mode disables its permission checks; Codex's bypass mode disables approvals and its sandbox. Cursor's bypass mode passes `--force --sandbox disabled`, disabling Cursor's command approvals and sandbox. Use unattended backends only in a workspace where that behavior is acceptable. Existing nonempty permission settings are preserved. If a permission setting is removed entirely, the runtime fallback is `acceptEdits` for Claude, `workspace-write` for Codex, and `default` for Cursor. Cursor's `default` mode passes `--trust` and `--approve-mcps` but not `--force` or `--sandbox`, so command approval and sandboxing follow the user's own Cursor CLI configuration (`approvalMode` and `sandbox` in `~/.cursor/cli-config.json`); with stock Cursor settings, unattended shell or network commands may be refused.

```bash
# Allow Claude workers to execute unattended tool actions.
SIDEQUESTOR_CLAUDE_PERMISSION_MODE=bypassPermissions
# Allow Codex workers to execute unattended tool actions.
SIDEQUESTOR_CODEX_PERMISSION_MODE=bypassPermissions
# Allow Cursor workers to execute without command approvals or the sandbox.
SIDEQUESTOR_CURSOR_PERMISSION_MODE=bypassPermissions
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

## How do I configure Notion watches?

Notion polling is opt-in: append `notion` to `SIDEQUESTOR_CHECKER_CONNECTORS` in
your workspace `.env` (for example `slack,email,github,jira,notion`). Create an API-token
connection in Notion, enable **Read content** and **Read comments**, and connect it to the
parent page containing the content to watch. Child pages inherit access. Store its token
in macOS Keychain as service `sidequestor-notion`, account `sidequestor`, or set
`NOTION_TOKEN` in the checker environment. Never put tokens in watch entries or commands.
Optional `NOTION_KEYCHAIN_SERVICE`, `NOTION_KEYCHAIN_ACCOUNT`, and `NOTION_KEYCHAIN_PATH`
select another Keychain item. If no service is configured and the default is missing,
the original `sidequestor-notion-test` item is accepted as a compatibility fallback. No Notion connector or Python dependency is required.

To store or replace the token without it appearing in shell history, run
`sq credentials set-token notion` in a terminal and paste it at the hidden prompt. It
updates the item the checkers already read (honouring the optional selectors above) and
creates `sidequestor-notion` only when none exists. `sq credentials set-token jira` does
the same for the Jira token (service `jira-api-token`, account `yaas`). Neither command
changes any setting names, so existing setups need no changes.

| Watch type | Required target | Coverage |
|---|---|---|
| `notion_page` | `page_id` UUID | Root page properties/content, nested blocks, and comments/replies on the page and blocks. New blocks enroll automatically. |
| `notion_data_source` | `data_source_id` UUID, or single-source `database_id` | Every current and newly discovered item gets the same page/block/comment coverage. |
| `notion_comments` | `block_id` UUID (page IDs are valid) | Comments/replies throughout the selected page/block tree; body edits alone do not trigger it. |

```bash
sq watch QUEST_ID '{"type":"notion_page","page_id":"YOUR-PAGE-UUID","reason":"Spec and discussion changes"}'
sq watch QUEST_ID '{"type":"notion_data_source","database_id":"YOUR-DATABASE-UUID","reason":"Tasks and their discussions"}'
sq watch QUEST_ID '{"type":"notion_comments","block_id":"YOUR-PAGE-UUID","reason":"Discussion only"}'
```

A page with 100 blocks remains **one watch**. Discovery and scanning run in Python,
without LLM tokens; only pending changes dispatch the worker. Comment checks never rely
on page edit timestamps. Comment IDs and content fingerprints detect new comments,
replies and same-minute edits. Repeated changes are coalesced until acknowledged.

**Subpages are opt-in.** `subpage_depth` defaults to `0` (integer 0–100). Set
`include_subpage_content: true` and/or `include_subpage_comments: true` independently
and give a positive depth. `1` includes direct children; `2` also includes grandchildren.
Existing and newly discovered subpages follow the same boundary. A database item's
page starts at depth zero. Ordinary nested blocks do not consume subpage depth.
Child databases, hyperlinks, mentions and relations do not expand the scope; add an
explicit data-source watch for database items.

```json
{"type":"notion_page","page_id":"YOUR-PAGE-UUID","subpage_depth":1,"include_subpage_comments":true,"include_subpage_content":false,"reason":"Root plus direct-child discussions"}
```

**Bounded, resumable scans.** `max_requests` defaults to 8 per invocation (1–50),
`scan_seconds` to 8 (3–30), and `max_entities` to 10000 (1–100000, counting pages,
blocks and comments). Legacy `max_pages` is accepted as a per-invocation request-budget
alias when `max_requests` is absent. Pagination is resumed across invocations, including
comment lists, rather than restarting at a fixed page cap. Breadth-first traversal and
visited-ID tracking prevent loops. The pending inbox has a 10000-resource change limit;
reaching a limit holds further scanning visibly until acknowledged or scope is adjusted.

Scans share a 20-second private API response cache and a connection-level request queue
within the workspace. Rate limits honor `Retry-After`. Requests and CPU still cost time:
a full comment sweep of 100 blocks can require about 100 requests. Larger scopes take
multiple ticks. The dashboard shows scanning/limited/error state, pending tasks, limits
and the last complete scan; incomplete coverage is never presented as a clean full scan. Normal incremental progress returns
a clean result with the delivery cursor unchanged, avoiding stalled-watch backoff;
that result does not mean the tree scan has completed.

**Durable delivery.** Workspace-local `state/notion/collector.sqlite3` stores scan progress,
fingerprints, pending changes and the short-lived cache, with private permissions. Keep
this state when upgrading or backing up; deleting it discards scan/delivery history.
A watch's `last_checked_ts` becomes a local delivery cursor, **not a Notion timestamp**.
Worker acknowledgement advances that cursor; failed/blocked workers retain pending changes.
The worker reads pending targets with the read-only `notion-state.py` helper described in
the dispatch skill. No per-block quest watches are created. Changing depth/comment/content
scope creates a distinct identity; retire and recreate a watch through supported commands
rather than editing watch JSON. Budgets do not change membership identity.

Initial scans baseline older content and conservatively replay the cursor's minute to
avoid losing rounded timestamps. Newly discovered resources after baseline are monitored
automatically, including their available comments. A partial tree scan can deliver a proven
pending-change prefix while traversal continues; the dashboard reports that distinction.

Polling is not an event log. Comments resolved/deleted before their scan, inaccessible
resources, intermediate edits, and ambiguous disappearances cannot be reconstructed.
A missing child is a structure change, not proof of deletion. A 404 means missing content
or missing access. Missing/unsupported descendants become visible coverage gaps while
other branches continue; the collector retries them on later cycles without guessing
that they were deleted. An unavailable selected root still requires configuration repair. Source queries exclude trashed items; a direct
page watch can observe an API-visible trash edit. `include_archived` defaults to `true` for
source archive partitions; set it to `false` to exclude those rows. Server-incomplete source
queries hold visibly and require narrower scope. Wiki data sources need explicit page watches.
No webhooks or OAuth onboarding are required. Production checkers/read bridges never write
Notion content.

For an opt-in live test from a source checkout, run
`python tests/manual/notion_checkers.py --allow-test-writes --page-id PAGE_UUID --database-id DATABASE_UUID`.
This additionally needs Insert/Update content and Insert comments. It creates its own
fixtures in the disposable test parent/database and trashes only those fixtures afterward.

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
Connector edits take effect on the next tick; explicitly exported environment variables
still override `.env`. Creating a disabled watch is rejected unless you deliberately pass
`--allow-dormant` to `sq watch` or `sq new-quest` to stage inactive setup. Retired and disabled
watches are excluded from checker-health warnings, and housekeeping clears their old backoff.

Available X polling types are `x_search`, `x_mentions`, `x_user_posts`, `x_home`, and `x_dm`.
They cover recent search, the authorized account's mentions and home timeline, posts from a
selected user ID, and incoming DMs. X only exposes DM events from the last 30 days. These pollers
detect new items, not later edits, deletions, reactions, list changes, Spaces, or notifications.

`sq x-send` can create posts, replies, threads, DMs, and media uploads; delete the account's posts;
and like, repost, bookmark, follow, mute, or block and reverse those actions. Quest-owned writes
require an active quest with `allow_send: true` or an exact claimed `remote_request` approval.
Dispatched writes also require an `idempotency_key`; an interrupted attempt is held for inspection
instead of retried blindly. Availability and billing still depend on the X app's current access tier.

`sq gdoc-comment` adds verified text-anchored comments through the Google Docs API using the
`gws` CLI. It refuses ambiguous anchor text, requires `allow_send` or an exact claimed approval,
and logs quest-owned writes. Each invocation accepts one exact-case anchor and requires a unique
idempotency key. The CLI must be authenticated with Docs and Drive access, and the author must have
comment access. When the API provides a document revision ID, the writer uses it to guard against
concurrent edits. Comments are limited to 2048
UTF-8 bytes. The response must report
`ALL_SAVED` and quote the requested anchor. The surface is enabled by default; set
`SIDEQUESTOR_GDOC_COMMENTS_ENABLED=0` in the workspace `.env` to disable it. See the installed
`yaas-gdoc-anchored-comments` skill for the payload format.

When upgrading from the browser-based writer, authenticate `gws` with Docs access as well as Drive
access. Remove obsolete `SIDEQUESTOR_GDOC_CDP_URL` and `SIDEQUESTOR_GDOC_CHROME_PROFILE` settings
from the workspace `.env`.

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
# These examples target the initialized workspace in the current directory.
# Print the optional block without changing any files.
sq --workspace . setup --instructions
# Use defaults without OAuth.
sq --workspace . setup --non-interactive
# Use the lower-level launchd operations when needed.
sq --workspace . setup --render-only
sq --workspace . setup status
# Shadow install/uninstall remain available without loading production jobs.
sq --workspace . setup install
sq --workspace . setup uninstall
# Install the production launchd jobs.
sq --workspace . setup --production install
```

## How do I run and inspect it?

```bash
# Start triage, heartbeat, and dashboard jobs for the current workspace.
# Wait for the dashboard and print its selected free loopback URL.
sq start
# Stop every job for the current workspace; the instance ID is optional here.
sq stop
# Pause now for time away; equivalent to sq stop.
sq pause
# Resume when you return; equivalent to sq start.
sq resume
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

An instance ID must still match the identity in its registered folder. If a folder
has been recreated or rekeyed, commands reject its old ID rather than acting on the
new instance. `sq instances list --all` marks these entries `STALE`; they are excluded
from the running list. Run `sq --workspace /path/to/workspace instances register`
to refresh that folder's registration and remove its old IDs. Registration and
rekeying preserve the folder's default-workspace selection.

`sq pause` stops triage, heartbeat, and the dashboard using the same verified lifecycle
as `sq stop`. It can interrupt an active worker; it does not wait for that worker to
finish. Quests, watermarks, history, and approval state are preserved. The managed jobs
remain stopped across reboots until you explicitly start/resume them. The dashboard is
unavailable while stopped. Resume uses the installation running your command, just like
`sq start`; use the workspace's virtualenv command when it has its own installation.
Existing watches and previously approved work continue normally after resuming.

Use `sq --workspace /path/to/workspace pause` or `resume` to select a workspace, or
`sq pause INSTANCE_ID` to pause a registered instance. `resume --dashboard-port PORT`
can select a specific dashboard port. This first version has no scheduled dates or
automatic resumption and is not a policy lock: an explicit `sq start` or `sq tick`
can still run work.

## How do I get briefings?

Open **Briefings**. If the archive is empty, use **Create a morning briefing quest**
or **Create a one-off briefing quest** to open an editable example in the usual quest
creation dialog. Choose the topic, sources, and timing before submitting; opening an
example does not create or schedule anything. You can also ask an existing quest's
agent to save a Markdown briefing with a `# Title` in `state/briefs/`. Files there
appear automatically in the Briefings archive. Saving a local brief does not authorize
posting or emailing it.

## How do I open a workspace or quest in my chosen app?

Set `SIDEQUESTOR_OPEN_IN=codex` (the default), `claude`, `cursor`, or `terminal` in the
workspace's `.env`. This preference is independent of the worker harness and is
reflected on the next dashboard refresh. The quest launch button and the button
beside **New quest**, the alternative inside the **New quest** dialog, and the
settings dialog's edit button all follow it. Onboarding offers this choice early;
you can change it later in `.env`.

Codex and Claude open a new chat in the workspace with a short editable prompt.
The default workspace prompt is “Within this Sidequestor workspace, I'd like to”.
The default creation prompt is “I'd like to create a new quest in this Sidequestor
workspace for .... ”; an existing creation draft, title and non-default priority
are preserved. The quest-level prompt is “Within this Sidequestor workspace, for
quest `"<quest-id>"`, I'd like to”.
Complete the prompt and press Send in the app. Claude Code also asks you to confirm
the folder. Cursor opens the workspace without a prompt. With `terminal`, the quest button retains its persistent session and
the top button opens a terminal at the workspace folder.

The buttons use the documented [Codex chat links](https://learn.chatgpt.com/docs/app/commands#chats)
and [Claude Code desktop links](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link).
They require the respective desktop app; the dashboard cannot confirm that a browser
successfully handed a link to an installed app.

## Where can I see my configuration?

Use the small settings button in the dashboard header. The searchable dialog lists
settings written in your workspace's `.env` first, followed by the remaining
documented settings and their runtime defaults. It shows when an exported process
variable takes precedence. Setup examples can differ from runtime defaults.

Defaults are not all stored in one place. The numeric and switch defaults live
in `tick_state.py`, emoji defaults in `reaction_config.py`, and the settings view
also maintains display copies of other runtime fallbacks in `dashboard_settings.py`.
Those other defaults still live with their runtime consumers; for example, the Codex
launch default also appears in the dashboard server and browser launch fallback.
This duplication is accepted for the current stabilization work. When adding or
changing a variable, follow the [configuration maintenance checklist](docs/architecture-decisions/0006-ACCEPTED-CONFIG-DEFAULT-DUPLICATION.md)
to keep runtime consumers, display defaults, setup values and documentation aligned.

The shipped `env.example` provides the setting catalog and descriptions; setup
examples are not automatically treated as runtime defaults. The view uses the shared
environment loader: exported dashboard process values override `.env`, which overrides
an absent value's runtime fallback. Explicit empty values remain visibly empty because
they can disable a feature or be invalid, rather than always meaning “use the default”.
In the Python loader used by this view, canonical `SIDEQUESTOR_*` names win over
legacy `YAAS_*` names within `.env`, and the first duplicate assignment wins. Avoid
duplicate assignments: sourcing the file in a shell can use the last one instead. Values written in `.env` are listed first, even if
an exported variable overrides them.

Credential values and unrecognized custom values are hidden before being sent to
the browser. The dialog never writes configuration or reads Keychain secrets.

Use its edit button to open the workspace in `SIDEQUESTOR_OPEN_IN` (Codex by
default). Codex and Claude receive an editable request to work on `.env`; Cursor
opens the folder without a prompt. Terminal mode opens a terminal at the workspace.
Save `.env`, then refresh the dialog to see the new values. Loop interval changes
require `sq stop` followed by `sq start`; exported settings and existing terminal
sessions can also require a restart. This preference does not change the worker harness.

## How do I work on one quest in a terminal?

Set `SIDEQUESTOR_OPEN_IN=terminal`, select a quest in the dashboard and choose **Open in terminal**. Sidequestor opens a Terminal
window attached to that quest's own session. The session keeps running after you close the
window, and opening it again joins the same session instead of starting another. Sessions are
named `sq-<instance>-<quest-id>`, so the same quest ID in two workspaces never shares one.

The session starts in the quest's folder with a short summary of the quest and these helpers:

```bash
ask "draft a reply with the docs link"   # start your agent with the quest's context
ask --claude "…"                         # use another installed agent this once
ctx                                      # mission and watches
trail                                    # recent timeline
queue "check again tomorrow"             # same as the dashboard's instruction box
help                                     # show the summary again
```

`ask` starts the agent chosen during `sq setup` (`SIDEQUESTOR_AGENT`, Codex by default) in
interactive mode, so you approve its actions as it works. `queue` asks you to confirm first,
and only accepts that confirmation from an interactive terminal. The same commands work
anywhere as `sq ask QUEST_ID …`, `sq ctx QUEST_ID`, and so on.

To create a quest by conversation instead of the form, choose **Create interactively in
Terminal instead** in the dashboard's **Create a quest** dialog. It opens one session for the
workspace with a `create "<mission>"` helper (also `sq create`), which starts your agent on the
quest-creation skill; the agent asks what to watch and shows you the quest before creating it.

The session is a normal shell running as you. It is focused on the quest, not restricted to
it, and it is not recorded in the quest timeline. Install tmux (`brew install tmux`) for the
best experience; without it Sidequestor uses macOS's built-in `screen`, and without either
each click opens a fresh shell. Set `SIDEQUESTOR_TERMINAL_APP` (for example `iTerm`) to use
another terminal app.

Quest context is stored in `context.md` and rendered as Markdown in the dashboard. Agents are
instructed to use headings for distinct topics, bold labels for short attributes, and flat
lists for fields, rules, and steps when creating or updating context. Existing contexts keep
their saved formatting until an agent or you updates them; an upgrade does not reformat them.

## How do I review proposed actions?

Open a manual review item in the dashboard to read its context and formatted proposal.
Use **Edit** to change the source text, then **Preview** to read it again. Read-only items
offer **View source** instead. Dashboard polling preserves unsaved edits and the selected view.
Approve or save an edit to persist your changes; switching views alone does not save them.

The preview supports basic headings, paragraphs, flat lists, emphasis, code spans, and links.
Code-fenced proposals are shown as literal source. Outbound messages use the source text in
their destination's syntax, so Slack and email formatting may differ from the preview.

## How do I upgrade?

```bash
# From a workspace: upgrade, refresh resources, validate, and restart previously running jobs.
sq upgrade

# From elsewhere, select a workspace for the same full lifecycle.
sq --workspace /path/to/workspace upgrade

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
require a valid receipt to preserve installed extras. Pip-managed PyPI upgrades bypass pip's HTTP
cache so a newly published release is not hidden by an older index response. The installed version
is checked after the installer runs; an unchanged version is reported as a no-op, not an upgrade.
When run from an initialized workspace (or with `--workspace PATH`), it stops production jobs only
when they were previously marked running, upgrades the package, then uses a fresh Python process
to sync resources and run `sq doctor`. Previously running jobs restart only after both checks
succeed. The upgrader preserves each instance's actual published dashboard port and waits up to
60 seconds for that port to be released before restarting. Git installs require confirmation
because they install code with the worker's permissions;
pass `--yes` for a non-interactive run. Use `--pre` to consider PyPI pre-releases or
`--no-restart` to leave previously running jobs stopped. Git sources are limited to HTTPS GitHub
repository URLs and require an explicit `--ref`; a commit SHA is reproducible while a branch can
move.

Outside a workspace, `sq upgrade` upgrades only the package. It does not stop or restart any
running jobs, or sync and validate workspace resources. Select an initialized workspace when you
want the full lifecycle. Versions before 0.1.44 require a workspace even for a package-only
upgrade; from a fresh folder, upgrade once with the installing tool directly, then use `sq upgrade`
normally.

You do not need to check for releases yourself. While a workspace's jobs are running, its
heartbeat job asks PyPI once a day whether a newer version exists. When one does, the dashboard
header shows it, `sq doctor` prints it, and you get one desktop notification per new version.
Each detected stable release also creates one Inbox review item. Approve it to run an
independent background upgrade job after the current tick finishes. It stops the workspace,
upgrades, syncs resources, runs `sq doctor`, restarts previously running jobs, and logs the
outcome in Inbox. On failure, the item returns to review with terminal recovery directions.
If validation leaves the service stopped, a desktop notification also points to recovery;
Sidequestor does not restart a package that failed validation.
This handoff currently requires macOS launchd. Do not run `sq upgrade` inside a dispatched
worker: stopping the service can kill that worker. A separate terminal remains a fallback.
Dismissal is remembered, and installed or superseded versions close their old notices.
Click the dashboard's build/version control to check for updates now (limited to once a minute).
It reports checking, an available release, already current, rate limiting, or failure.
Nothing upgrades on its own. Set `SIDEQUESTOR_UPDATE_CHECK=0` in the workspace `.env` to turn the
check off.

Sidequestor also watches for explicit authentication/configuration failures, repeated checker
errors, and failed worker dispatches. It asks the configured harness for a read-only diagnosis
and a repair proposal for review; it does not apply the repair during diagnosis. Proposals
state whether the fix can run headlessly, needs an interactive terminal/browser session, or
needs further investigation. Shared problems go to Inbox; quest-specific problems stay with
their quest. If the harness cannot launch, Sidequestor writes recovery directions directly.
Suspected packaged-code defects instead produce a sanitized email bug report addressed to
`guangmian.kung@circle.com` for manual review. No report is sent without approval. Reports
include the verified running version and a minimal reproduction, not private Slack content
or credentials. Installed package files and watch watermarks are not edited as a workaround;
an upgrade is a remedy only when a released fix has been verified. Reporting a defect is not
proof of recovery: the health issue remains until a real check succeeds.
The Gmail helpers require exact claimed approval for maintainer-directed mail, including
replies, CCs and plus aliases, even in a send-enabled quest. Real replies resolve their
audience before applying this check; drafts remain unsent and ungated.
Inbox is permanent and is restored automatically, including outstanding review watches.
Reaction actions, source/result links where available, and outcomes are recorded in Inbox.

One uv or pipx installation is shared by every workspace, so `sq upgrade` restarts only the
workspace it runs in and lists any other running workspaces whose long-running jobs
and launchd settings need refreshing. Package-only upgrades list these workspaces too.
Wait for their active workers to finish, then use the printed refresh commands. They
name the upgraded installation's exact Python, avoiding an older workspace-local
`sq` earlier on PATH. A normal start detects changed plists and rewrites/reloads them
on the saved dashboard port; `launchctl kickstart` alone does not rewrite a plist.

The dashboard reports the running package's build identity ahead of inherited
launchd version labels. An existing Python process can still have the old build
loaded until it restarts; a freshly launched process uses the installed build.

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

Reaction defaults use standard names: `robot_face`, `hourglass_flowing_sand`, and `white_check_mark`. Pickup is queued before harness launch; durable status delivery retries Slack failures without repeating completed actions. Work retained for retry or input shows 🕒 (`clock3`, configurable with `SIDEQUESTOR_REACTION_BLOCKED_EMOJI`); exhausted retries create an Inbox review. Conscious no-action outcomes show done. Save reactions remain silent. Existing historical loading-only orphans are not automatically replayed. To retain a custom set, pin `SIDEQUESTOR_REACTION_PROCESS_EMOJI`, `SIDEQUESTOR_REACTION_LOADING_EMOJI`, and `SIDEQUESTOR_REACTION_DONE_EMOJI`.

The configurable `expand` reaction defaults to 🪄 (`magic_wand`). Its first scan leaves pre-existing wand reactions alone, so upgrading does not turn old reactions into edit requests. It expands your own Slack message in place, then carries out its explicit requests or creates a quest for ongoing work. It uses the same processing/done/blocked lifecycle as other action reactions. The edit helper verifies ownership, checks for intervening edits, and retains a separate edit receipt so retries do not expand a completed edit again. Set `SIDEQUESTOR_REACTION_EXPAND_EMOJI` to choose another trigger.

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
