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
sidequestor --workspace ./workspace doctor
```

The virtualenv supplies the `sidequestor` command in the current terminal. Run
`deactivate` when finished. After this branch is merged or released, the
`@publish/sidequestor-package-0.1.0` suffix can be omitted.

The `sq` and `yaas` commands remain compatibility aliases. Existing workspace
state continues to live under `.yaas`, and existing `YAAS_*` environment names
remain accepted. New configuration should use the canonical `SIDEQUESTOR_*`
names shown in `.env.example`.

## License

Apache License 2.0. See `LICENSE`.
