# Sidequestor

Sidequestor is a local-first Slack assistant with a per-workspace triage loop.
It packages the battle-tested YAAS v2 runtime behind the `sidequestor` command.

## Install

```bash
# Most users: change to an existing project/workspace directory.
cd ~/path/to/existing-workspace
# Optional instead: create a new directory and enter it.
# mkdir -p ~/new-sidequestor-workspace
# cd ~/new-sidequestor-workspace
# Create an isolated Python environment inside that workspace.
python3 -m venv .venv
# Make the Sidequestor commands available in this terminal.
source .venv/bin/activate
# Install the package from the internal testing publication branch.
python -m pip install 'git+https://github.com/circlefin/sidequestor.git@publish/sidequestor-package-0.1.0'
# Add Sidequestor's .yaas metadata and configuration to this directory.
sidequestor init .
# If enabling local Slack integration, print the manifest to paste into Slack.
# sq setup --manifest
# Run interactive onboarding; choose `bypassPermissions` for the worker backend.
sq setup
```

The existing directory does not need Sidequestor metadata beforehand;
`sidequestor init .` adds it in place and preserves existing files. The
dashboard workspace label opens the current workspace in Cursor when available,
or in macOS's default IDE/file opener; set `SIDEQUESTOR_IDE_APP` to prefer a
different installed application.

The virtualenv supplies the `sidequestor` command in the current terminal. Run
`deactivate` when finished. After this branch is merged or released, the
`@publish/sidequestor-package-0.1.0` suffix can be omitted.

### Worker Permissions

For Sidequestor to be useful as an unattended triage worker, the selected agent
must be able to run its tools without waiting for an interactive approval. During
`sq setup`, manually choose `bypassPermissions` for the backend you use. The
installer does not set this automatically so you explicitly acknowledge the
tradeoff: this allows the agent to execute filesystem, shell, and network/MCP
operations without per-action approval. Use it only in a workspace where that
behavior is acceptable.

If you configure the workspace non-interactively, set the values yourself in
`.env` before starting the jobs:

```bash
# Allow Claude workers to execute unattended tool actions.
SIDEQUESTOR_CLAUDE_PERMISSION_MODE=bypassPermissions
# Allow Codex workers to execute unattended tool actions.
SIDEQUESTOR_CODEX_PERMISSION_MODE=bypassPermissions
```

Commands automatically use the current directory when it is an initialized
workspace. You can run them from elsewhere by passing `--workspace PATH`, or
set `SIDEQUESTOR_WORKSPACE` for a shell-wide default. The legacy
`YAAS_WORKSPACE` name remains accepted. For example:

```bash
# Validate the workspace selected by an explicit path.
sidequestor --workspace ~/path/to/existing-workspace doctor
```

`sq setup` is the complete onboarding wizard. It asks whether to enable the
costless local Slack checker, which worker backend to use (`claude` or
`codex`), the model, reasoning effort, and worker permission mode. It copies
`.env.example` to `.env` when needed, fills only missing or placeholder values,
never overwrites real existing values, optionally runs Slack PKCE OAuth, and
starts the workspace's launchd jobs.

The lifecycle commands are:

```bash
# Start triage, heartbeat, and dashboard jobs for the current workspace.
sq start
# Stop every job for the current workspace; the instance ID is optional here.
sq stop
# List currently running Sidequestor instances and their exact workspaces.
sq instances list
# Include stopped or historical registered workspaces as well.
sq instances list --all
# Validate the current workspace from inside it.
sq doctor
# From another directory, stop one registered workspace explicitly.
sq stop INSTANCE_ID
```

Use `sq setup --non-interactive` for defaults without OAuth, or use
`sq setup --render-only|install|status|uninstall` for the lower-level launchd
operations.
The `sq` and `yaas` commands remain compatibility aliases. Existing workspace
state continues to live under `.yaas`, and existing `YAAS_*` environment names
remain accepted. New configuration should use the canonical `SIDEQUESTOR_*`
names shown in `.env.example`.

## Test

From a checkout with its development virtualenv activated:

```bash
# Run the complete unittest suite from the repository checkout.
python -m unittest discover -s tests -p 'test_*.py'
```

This runs setup, installed-runtime imports, behavior, public command-surface,
and full-workspace lifecycle tests. The dashboard tests require localhost socket
binding; they are skipped only in restricted sandboxes that prohibit it.

For a disposable branch-install test, read the local ignored runbook at
`.agents/skills/sidequestor-e2e/SKILL.md`. It requires explicit opt-in before
performing any live Slack reaction.

Before publishing package changes, run the local ignored
`.agents/skills/regression-check/SKILL.md`, then use
`.agents/skills/publish-yaas-to-circlefin/SKILL.md` for the signed branch
publication. Neither skill modifies `.git-yaas-v2`.

## License

Apache License 2.0. See `LICENSE`.
