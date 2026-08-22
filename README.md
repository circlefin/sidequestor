# Sidequestor

Sidequestor is a local-first Slack assistant with a per-workspace triage loop.
It packages the battle-tested YAAS v2 runtime behind the `sidequestor` command.

## Install

```bash
mkdir -p ~/sidequestor-install
cd ~/sidequestor-install
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'git+https://github.com/circlefin/sidequestor.git@publish/sidequestor-package-0.1.0'
sidequestor init ./workspace
cd ./workspace
sq setup
```

The virtualenv supplies the `sidequestor` command in the current terminal. Run
`deactivate` when finished. After this branch is merged or released, the
`@publish/sidequestor-package-0.1.0` suffix can be omitted.

Commands automatically use the current directory when it is an initialized
workspace. You can run them from elsewhere by passing `--workspace PATH`, or
set `YAAS_WORKSPACE` for a shell-wide default. For example:

```bash
sidequestor --workspace ~/sidequestor-install/workspace doctor
```

`sq setup` is the complete onboarding wizard. It asks whether to enable the
costless local Slack checker, which worker backend to use (`claude` or
`codex`), the model, reasoning effort, and worker permission mode. It copies
`.env.example` to `.env` when needed, fills only missing or placeholder values,
never overwrites real existing values, optionally runs Slack PKCE OAuth, and
starts the workspace's launchd jobs.

The lifecycle commands are:

```bash
sq start                         # start jobs for the current workspace
sq stop INSTANCE_ID              # stop every job for one instance
sq instances list                # find registered instance IDs
sq --workspace ./workspace doctor
```

Use `sq setup --non-interactive` for defaults without OAuth, or use
`sq setup --render-only|install|status|uninstall` for the lower-level launchd
operations.
The `sq` and `yaas` commands remain compatibility aliases. Existing workspace
state continues to live under `.yaas`, and existing `YAAS_*` environment names
remain accepted. New configuration should use the canonical `SIDEQUESTOR_*`
names shown in `.env.example`.

## License

Apache License 2.0. See `LICENSE`.
