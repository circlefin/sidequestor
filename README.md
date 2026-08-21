# Sidequestor

Sidequestor is a local-first Slack assistant with a per-workspace triage loop.
It packages the battle-tested YAAS v2 runtime behind the `sidequestor` command.

## Install

```bash
python -m pip install 'git+https://github.com/circlefin/sidequestor.git'
sidequestor init ~/sidequestor-workspace
sidequestor --workspace ~/sidequestor-workspace doctor
```

The `sq` and `yaas` commands remain compatibility aliases. Existing workspace
state continues to live under `.yaas`, and existing `YAAS_*` environment names
remain accepted. New configuration should use the canonical `SIDEQUESTOR_*`
names shown in `.env.example`.

## License

Apache License 2.0. See `LICENSE`.
