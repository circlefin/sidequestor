"""Explicit dashboard changes to a single quest's send policy."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re



class PermissionConflict(ValueError):
    pass


def set_send_permission(workspace: Path, quest_id: str, payload: dict) -> dict:
    """Hold the same lock as triage; never change authority during a dispatch.

    Compare-and-set prevents a stale tab from overwriting a newer decision. The
    audit entry shares the atomic metadata replacement, so it cannot be lost
    independently of the permission change. Existing approvals/channel exceptions
    are deliberately untouched.
    """
    from sidequestor.workspace import _write_json, file_lock

    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}', quest_id) or quest_id in {'.', '..'}:
        raise ValueError('invalid quest id')
    if type(payload.get('allow_send')) is not bool or type(payload.get('expected_allow_send')) is not bool:
        raise ValueError('allow_send and expected_allow_send must be booleans')
    requested = payload['allow_send']
    if requested and payload.get('confirm_send') is not True:
        raise ValueError('confirm_send must be true to enable sending')
    base = (workspace / 'state/quests/active').resolve()
    folder = base / quest_id
    if folder.is_symlink() or folder.resolve().parent != base:
        raise ValueError('invalid quest path')
    path = folder / 'meta.json'
    if path.is_symlink():
        raise ValueError('quest metadata must not be a symlink')
    try:
        with file_lock(workspace / 'logs/triage.lock', blocking=False):
            meta = json.loads(path.read_text())
            if not isinstance(meta, dict) or meta.get('status', 'active') != 'active':
                raise ValueError('only active quests can change send permission')
            previous = meta.get('allow_send', False)
            if type(previous) is not bool:
                raise ValueError('quest send permission is invalid')
            if previous != payload['expected_allow_send']:
                raise PermissionConflict('Send permission changed in another session. Refresh and try again.')
            if requested != previous:
                history = meta.get('send_permission_changes', [])
                if not isinstance(history, list):
                    raise ValueError('quest permission history is invalid')
                meta['allow_send'] = requested
                meta['send_permission_changes'] = [*history, {
                    'at': datetime.now(timezone.utc).isoformat(), 'source': 'dashboard',
                    'previous': previous, 'allow_send': requested,
                }]
                _write_json(path, meta)
            return {'ok': True, 'quest_id': quest_id, 'allow_send': requested,
                    'allow_send_channels': meta.get('allow_send_channels', [])}
    except BlockingIOError as exc:
        raise PermissionConflict('Triage is checking or dispatching. Try again when it is idle.') from exc
