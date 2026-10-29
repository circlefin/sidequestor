# Accepted duplication of configuration defaults

**Status:** Accepted; retain the current implementation and maintain the copies together

**Date:** 2026-10-29

## Decision

Keep runtime defaults with their existing consumers. Keep the dashboard's imported
and manually mirrored defaults rather than introducing a central configuration
registry during the current stabilization work. Centralization is a separate
architectural change that needs its own review and regression coverage.

Accepting duplication does not make the dashboard table authoritative. Runtime
consumers determine actual behavior. A display mismatch should be tracked and
corrected without assuming that the worker is using the displayed value.

## Where the values come from

| Location | Responsibility |
|---|---|
| Runtime Python, shell and browser consumers | Actual fallback, validation, and handling of unset or empty values |
| `src/sidequestor/runtime/yaas-triage/tick_state.py` | Shared numeric and boolean defaults imported by the settings view |
| `src/sidequestor/runtime/yaas-triage/reaction_config.py` | Shared emoji defaults and adaptive emoji resolution |
| `src/sidequestor/runtime/yaas-triage/dashboard_settings.py` | Display metadata: imports shared defaults and mirrors other consumers in `FALLBACKS` |
| `src/sidequestor/package_data/env.example` | Shipped setting names, descriptions and setup examples; materialized as the workspace `.env.example` |
| `src/sidequestor/setup.py` and other provisioning code | Values explicitly written during setup, which may differ from runtime fallbacks |

The dashboard uses the shared Python environment loader. Process environment
values take precedence over `.env`; absent values use runtime fallbacks. Explicit
empty values stay visible as empty because consumers can treat them as a default,
a disabled feature, or an invalid value. Within the Python loader, canonical
`SIDEQUESTOR_*` names beat legacy `YAAS_*` names in the file and the first duplicate
assignment wins. Sourcing a file in a shell can use the last duplicate instead.
Avoid duplicate assignments.

## Checklist for adding or changing a setting

1. Find every runtime consumer and establish the actual unset fallback, empty-value
   behavior, accepted values and units. Check aliases and any browser or shell
   fallback as well as Python readers. Runtime behavior is the reference.
2. Update the dashboard metadata in the same change. For numeric, boolean or emoji
   settings already imported from shared definitions, verify that the import gives
   the new value. For other fixed fallbacks, add or update the canonical key in
   `dashboard_settings.FALLBACKS`. Do not copy a setup/example value into this table
   when the runtime fallback differs. Use “Not set” only when there genuinely is no
   fixed fallback, such as a value delegated to the agent CLI/account.
3. Update the shipped `package_data/env.example`, relevant setup/provisioning code,
   and user/operations documentation. Explain intentional differences between
   setup values and runtime fallbacks. Preserve existing user overrides during
   setup and upgrade. Verify that resource synchronization carries the updated
   template into the workspace's `.env.example`.
4. Check how the setting appears in the dashboard: unset, explicitly configured,
   explicitly empty, and overridden by a process variable. Include namespace-alias
   precedence where applicable. Credential settings must remain redacted before
   reaching the browser; listing settings must not retrieve Keychain secrets.
5. Add or update focused regression coverage for both the runtime reader and the
   displayed default. Compare with the actual consumer/shared definition where
   possible, rather than repeating a literal only in the UI test. Verify that a
   display change does not change worker behavior. Run the normal regression
   checks appropriate to the affected consumers.

Review runtime consumers, display mirrors, template, setup and documentation
as one configuration change. Passing the existing suite alone does not prove that
all manually mirrored defaults agree: complete drift detection is not implemented.

## Example: Cursor permission defaults

`SIDEQUESTOR_CURSOR_PERMISSION_MODE` illustrates the distinction:

- The Cursor dispatcher falls back to `default` when the variable is unset.
- Setup and the shipped example write `bypassPermissions` for a new configuration.
- At commit `867c536`, the dashboard incorrectly showed “Not set” because its
  display fallback was missing. The display entry now mirrors the runtime value
  `default`.

The runtime/setup difference is intentional. Correcting the display entry does
not change effective permissions or remove the accepted duplication. This example
shows why every new variable needs both runtime and display-default coverage.
