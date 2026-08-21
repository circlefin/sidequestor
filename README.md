# Sidequestor YaaS

Sidequestor is a local, Slack-centric triage loop for an existing Claude, Codex, or Cursor
installation. The loop polls inexpensive local checkers and wakes the configured agent only when
a watched quest or reaction has new activity.

## Install

This branch is intentionally a Python package at its repository root. From a new directory:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install \
  "git+https://github.com/circlefin/sidequestor.git@<branch>"
.venv/bin/yaas init ./yaas-workspace
cp ./yaas-workspace/.env.example ./yaas-workspace/.env
```

Replace `<branch>` with the published branch name. The corresponding GitHub tree URL is:

```text
https://github.com/circlefin/sidequestor/tree/<branch>
```

YAAS requires Python 3.11 or newer and is designed for macOS. Configure `.env` before enabling
background jobs. Slack polling is optional; set `YAAS_SLACK_CHECKERS_ENABLED=0` when it is not
configured.

## Run

```bash
.venv/bin/yaas --workspace ./yaas-workspace doctor
.venv/bin/yaas --workspace ./yaas-workspace tick --isolated
.venv/bin/yaas --workspace ./yaas-workspace dashboard serve 0
```

For a persistent local loop, use `yaas --workspace ./yaas-workspace loop`. To render or install
workspace-scoped launchd jobs, use `yaas --workspace ./yaas-workspace setup --render-only` or
`yaas --workspace ./yaas-workspace setup install`.

The package owns the engine under `.yaas/engine/current`. `yaas init` owns workspace state under
`.yaas`, `state`, `logs`, `skills`, `.env`, and `CLAUDE.md`; it does not copy private state or
credentials from the publishing repository.

## Safety

The package contains the battle-tested triage runtime and its generic YAAS skills. Slack sends and
agent dispatch remain governed by the workspace configuration and the quest approval protocol.
Review `.env.example` and the generated workspace instructions before starting background jobs.

## License

Released under the Apache License, Version 2.0. See [LICENSE](LICENSE).
