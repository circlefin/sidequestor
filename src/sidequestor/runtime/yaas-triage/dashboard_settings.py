"""Read-only, redacted dashboard view of documented settings and dotenv overrides."""

import os
import re
from pathlib import Path

from tick_state import BOOLEAN_KNOBS, NUMERIC_KNOBS, DEFAULT_CHECKER_CONNECTORS, load_environment
from reaction_config import EMOJI_SETTINGS, load_reaction_emojis

# Accepted duplication: these literals mirror runtime consumers; they do not configure them.
# When adding/changing settings, keep the consumer, display mirror, env.example and setup
# documentation/tests in sync. Shared defaults imported below need no additional literal.
# Maintenance checklist: docs/architecture-decisions/0006-ACCEPTED-CONFIG-DEFAULT-DUPLICATION.md
# Runtime fallbacks, not the example values that setup may write into a new .env.
# Gate/emoji defaults below come directly from their owners rather than copies.
FALLBACKS = {
    'SIDEQUESTOR_OPEN_IN': 'codex', 'SIDEQUESTOR_IDE_APP': 'Cursor',
    'SIDEQUESTOR_TERMINAL_APP': 'Terminal', 'SIDEQUESTOR_AGENT': 'codex',
    'SIDEQUESTOR_CHECKER_CONNECTORS': ','.join(DEFAULT_CHECKER_CONNECTORS),
    'SIDEQUESTOR_GDOC_COMMENTS_ENABLED': '1', 'SIDEQUESTOR_UPDATE_CHECK': '1',
    'SIDEQUESTOR_MAX_SPEND_1H': '40', 'SIDEQUESTOR_MAX_SPEND_24H': '250',
    'SIDEQUESTOR_MAX_DISPATCH_6H': '250', 'SIDEQUESTOR_TRIAGE_INTERVAL': '60',
    'SIDEQUESTOR_HEARTBEAT_INTERVAL': '300', 'SIDEQUESTOR_WORKER_TIMEOUT': '1800',
    'SIDEQUESTOR_WORKER_STALL_SECONDS': '900', 'SIDEQUESTOR_WORKER_HEARTBEAT_SECONDS': '15',
    'SIDEQUESTOR_HEALTH_STALL_MIN': '10', 'SIDEQUESTOR_HEALTH_HUNG_MIN': '75',
    'SIDEQUESTOR_HEALTH_FAIL_STREAK': '5', 'SIDEQUESTOR_APPROVAL_LEASE_MIN': '45',
    'SIDEQUESTOR_HEALTH_APPROVAL_STUCK_MIN': '45', 'SIDEQUESTOR_HEALTH_EVENT_LOOKBACK_MIN': '60',
    'SIDEQUESTOR_HEALTH_COOLDOWN_MIN': '360', 'SIDEQUESTOR_RETIRE_EPHEMERAL_HOURS': '168',
    'SIDEQUESTOR_CLAUDE_MODEL': 'opus', 'SIDEQUESTOR_CLAUDE_PERMISSION_MODE': 'acceptEdits',
    'SIDEQUESTOR_CODEX_PERMISSION_MODE': 'workspace-write',
    'SIDEQUESTOR_CURSOR_PERMISSION_MODE': 'default',
    'NOTION_KEYCHAIN_SERVICE': 'sidequestor-notion', 'NOTION_KEYCHAIN_ACCOUNT': 'sidequestor',
}
for _key, _value in {**NUMERIC_KNOBS, **BOOLEAN_KNOBS}.items():
    FALLBACKS[_key.replace('YAAS_', 'SIDEQUESTOR_', 1)] = str(_value)
for _key, _value in EMOJI_SETTINGS.values():
    FALLBACKS[_key.replace('YAAS_', 'SIDEQUESTOR_', 1)] = _value

_ASSIGNMENT = re.compile(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')
_SECRET = re.compile(r'(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIALS?|SESSION)(?:_|$)', re.I)


def canonical(key):
    return key.replace('YAAS_', 'SIDEQUESTOR_', 1) if key.startswith('YAAS_') else key


def _catalog(template):
    """Descriptions from the shipped example; values are never assumed to be defaults."""
    catalog, comments, previous = {}, [], None
    for raw in template.read_text().splitlines():
        line = raw.strip()
        candidate = line[1:].strip() if line.startswith('#') else line
        match = _ASSIGNMENT.fullmatch(candidate)
        if match:
            key = canonical(match[1])
            description = ' '.join(comments) or previous or 'Optional workspace setting.'
            catalog.setdefault(key, description[:1200])
            previous, comments = description, []
            continue
        if not line:
            comments, previous = [], None
        elif line.startswith('#'):
            text = candidate.strip('─═ ').strip()
            if any(mark in candidate for mark in '─═') or text.isupper():
                comments, previous = [], None
            elif text:
                comments.append(text)
    catalog.setdefault("SIDEQUESTOR_WORKER_PERMISSION_MODE", "Deprecated fallback for Claude permissions; prefer SIDEQUESTOR_CLAUDE_PERMISSION_MODE.")
    catalog.setdefault("NOTION_TOKEN", "Optional token injected by a secret manager; prefer macOS Keychain and never commit it.")
    for key in FALLBACKS:
        catalog.setdefault(key, 'Optional workspace setting.')
    return catalog


def _dotenv_entries(path):
    entries = {}
    for raw in path.read_text().splitlines():
        match = _ASSIGNMENT.fullmatch(raw.strip())
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        entries.setdefault(key, value)
    return entries


def _display(value, hidden):
    if value is None:
        return 'Not set'
    if value == '':
        return 'Empty'
    if hidden:
        return 'Set (hidden)'
    # URLs sometimes carry credentials despite an otherwise public setting name.
    if re.search(r'://[^/\s]*@|://[^\s]*[?#]', str(value), re.I):
        return 'Set (hidden: URL credentials or parameters)'
    return str(value)


def build_settings(workspace, template, environ=None):
    workspace = Path(workspace)
    inherited = dict(os.environ if environ is None else environ)
    catalog = _catalog(Path(template))
    error = None
    try:
        entries = _dotenv_entries(workspace / '.env')
    except FileNotFoundError:
        entries = {}
    except (OSError, UnicodeError):
        entries, error = {}, 'Could not read .env. Check its file permissions and text encoding.'
    effective = load_environment(workspace, inherited)
    inherited_keys = {canonical(k) for k in inherited}
    local = {}
    for key, value in entries.items():
        name = canonical(key)
        if name not in local or key == name:
            local[name] = (key, value)
    try:
        emojis = load_reaction_emojis(effective)
    except ValueError:
        emojis = {}
        error = error or 'Reaction emoji settings are invalid. Check that names are valid and unique.'
    emoji_roles = {canonical(var): role for role, (var, _) in EMOJI_SETTINGS.items()}
    keys = set(catalog) | set(local)
    rows = []
    for key in sorted(keys):
        hidden = bool(_SECRET.search(key)) or key not in catalog
        default = FALLBACKS.get(key)
        raw = effective.get(key)
        # Preserve explicit empty values: some disable features or are invalid.
        # Showing a default in their place would claim behavior the runtime does not have.
        specified = raw is not None
        source = 'environment' if key in inherited_keys else 'dotenv' if key in local else 'default'
        raw = raw if specified else default
        if not specified and key in emoji_roles and emoji_roles[key] in emojis:
            raw = emojis[emoji_roles[key]]
        if not specified and key == 'SIDEQUESTOR_CLAUDE_PERMISSION_MODE':
            legacy = effective.get('SIDEQUESTOR_WORKER_PERMISSION_MODE')
            if legacy:
                raw = legacy
                source = 'environment' if 'SIDEQUESTOR_WORKER_PERMISSION_MODE' in inherited_keys else 'dotenv'
        configured_key, configured = local.get(key, (None, None))
        desc = catalog.get(key, 'Additional .env variable; its value is hidden because it is not in the documented settings catalog.')
        if key == 'SIDEQUESTOR_CHECKER_CONNECTORS':
            desc += ' An explicit empty value disables all external checkers.'
        if key == 'SIDEQUESTOR_GDOC_COMMENTS_ENABLED':
            desc += ' An empty value is invalid; use 0 or 1.'
        if key == 'SIDEQUESTOR_OPEN_IN' and specified and str(raw).strip().lower() not in {'terminal', 'codex', 'claude', 'cursor'}:
            desc += ' This value is empty or unrecognized; the dashboard falls back to codex.'
        if key in emoji_roles:
            desc += ' Unset markers may adapt to keep all configured emojis unique.'
        if key.endswith(('_MODEL', '_EFFORT')) and default is None:
            desc += ' When unset, the agent CLI/account chooses its default; setup may write an explicit value.'
        if key in {'SIDEQUESTOR_CLAUDE_PERMISSION_MODE', 'SIDEQUESTOR_CODEX_PERMISSION_MODE', 'SIDEQUESTOR_CLAUDE_MODEL'}:
            desc += ' The default shown is the runtime fallback; sq setup can write a different explicit value.'
        rows.append({'key': key, 'value': _display(raw, hidden),
                     'default': _display(default, hidden), 'source': source,
                     'in_dotenv': key in local, 'dotenv_key': configured_key,
                     'dotenv_value': _display(configured, hidden), 'hidden': hidden,
                     'using_default': not specified, 'description': desc})
    return {'env_path': str(workspace / '.env'), 'error': error,
            'overrides': [row for row in rows if row['in_dotenv']],
            'defaults': [row for row in rows if not row['in_dotenv']]}
