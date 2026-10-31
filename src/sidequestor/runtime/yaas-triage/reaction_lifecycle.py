"""Durable, runtime-owned reaction status delivery. Never repeats business actions.

Workers acknowledge outcomes; this outbox delivers the corresponding Slack emoji.
It survives pending-queue pruning, process crashes and transport/auth failures.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import copy
from datetime import datetime

from inbox import write_json, try_log_inbox
from reaction_config import load_reaction_emojis

HERE = Path(__file__).resolve().parent


def read(path, default):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


class Lifecycle:
    def __init__(self, root, env=None):
        self.root = Path(root)
        self.env = dict(os.environ if env is None else env, SIDEQUESTOR_WORKSPACE=str(root))
        self.path = self.root / 'state/triage/reaction-lifecycle.json'
        self.emojis = load_reaction_emojis(self.env)

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix('.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Never erase an unreadable outbox and lose a completed-action receipt.
            data = json.loads(self.path.read_text()) if self.path.exists() else {'items': {}}
            if not isinstance(data, dict) or not isinstance(data.get('items'), dict):
                raise ValueError('invalid reaction lifecycle outbox')
            if any(not isinstance(record, dict) for record in data['items'].values()):
                raise ValueError('invalid reaction lifecycle record')
            before = copy.deepcopy(data)
            yield data['items']
            if data != before or not self.path.exists():
                write_json(self.path, data)
                with open(self.path, 'rb') as committed:
                    os.fsync(committed.fileno())
                directory = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    def request(self, item, desired, *, final=False, at=None, reason=''):
        iid = item['item_id']
        emoji, _, ts = iid.partition(':')
        silent = emoji == self.emojis['save']
        if not re.fullmatch(r'\d+\.\d+', ts) or desired not in ('loading', 'done', 'blocked'):
            raise ValueError('invalid reaction status request')
        sources = read(self.root / 'state/reaction-sources.json', {})
        source = sources.get(iid, {})
        channel = item.get('channel_id') or (source.get('channel_id') if isinstance(source, dict) else None)
        if channel and not re.fullmatch(r'[CDG][A-Z0-9]+', channel):
            raise ValueError('invalid reaction channel')
        stamp = time.time() if at is None else at
        with self.locked() as records:
            old = records.get(iid, {})
            if stamp < old.get('at', 0) or (old.get('final') and not final):
                return
            channel = channel or old.get('channel_id')
            if old.get('channel_id') and channel != old['channel_id']:
                raise ValueError('reaction source changed')
            if (old.get('desired'), old.get('final'), old.get('channel_id')) == (desired, final, channel):
                return
            records[iid] = dict(old, item_id=iid, channel_id=channel, message_ts=ts,
                               silent=silent,
                               desired=desired, final=final, at=stamp, reason=reason[:1000],
                               revision=old.get('revision', 0) + 1, confirmed=False, next_retry=0)

    def acknowledge(self, item):
        status = item.get('status')
        if status in ('handled', 'nothing_to_do', 'blocked'):
            at = datetime.fromisoformat(item['acked_utc'].replace('Z', '+00:00')).timestamp()
            self.request(item, 'blocked' if status == 'blocked' else 'done',
                         final=status != 'blocked', at=at, reason=item.get('note', ''))

    def recover(self):
        # Ack is the business receipt. Rebuild status requests even if a worker
        # died immediately after its manifest was fsynced, before outbox enqueue.
        for path in sorted(self.path.parent.glob('dispatch-*.json')):
            manifest = read(path, {})
            if manifest.get('kind') != 'reactions':
                continue
            items = manifest.get('items')
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get('acked_utc'):
                    try:
                        self.acknowledge(item)
                    except (ValueError, KeyError, AttributeError, TypeError):
                        continue  # One malformed receipt must not starve others.
        with self.locked() as records:
            reviews = [dict(r) for r in records.values() if r.get('review_pending')]
        for record in reviews:
            try:
                self.review(record, record.get('reason', 'Reaction needs recovery'))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass  # Durable review_pending prevents business redispatch.
        approvals = read(self.root / 'state/pending-approvals.json', {}).get('items')
        for approval in approvals if isinstance(approvals, list) else []:
            if not isinstance(approval, dict):
                continue
            target = approval.get('target', {})
            if not isinstance(target, dict):
                continue
            if target.get('action') == 'reaction_recovery' and approval.get('status') == 'executed':
                iid = str(target.get('system_key', '')).removeprefix('reaction:')
                try:
                    self.request({'item_id': iid, 'channel_id': target.get('channel_id')},
                                 'done', final=True, reason='Reviewed recovery verified complete')
                except ValueError:
                    continue

    def prune_pending(self):
        """Called under the tick lock; held reviews retry through recover, not workers."""
        path = self.path.parent / 'pending_reactions.json'
        if not path.exists():
            return
        pending = json.loads(path.read_text())
        with self.locked() as records:
            remaining = {emoji: [ts for ts in timestamps if not (
                records.get(f'{emoji}:{ts}', {}).get('final') or
                records.get(f'{emoji}:{ts}', {}).get('review_pending'))]
                for emoji, timestamps in pending.items()}
        remaining = {emoji: timestamps for emoji, timestamps in remaining.items() if timestamps}
        if remaining != pending:
            if remaining:
                write_json(path, remaining)
            else:
                path.unlink(missing_ok=True)

    def flush(self, limit=12):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix('.delivery.lock'), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for _ in range(limit):
                with self.locked() as records:
                    ready = [dict(r) for r in records.values() if not r.get('confirmed')
                             and not r.get('silent')
                             and r.get('channel_id') and r.get('next_retry', 0) <= time.time()]
                if not ready:
                    return
                record = min(ready, key=lambda r: (r.get('desired') == 'loading', r.get('at', 0)))
                # Different triggers on one message share a visible lifecycle.
                with self.locked() as records:
                    group = [r for r in records.values() if r.get('channel_id') == record['channel_id']
                             and not r.get('silent')
                             and r['message_ts'] == record['message_ts']]
                    revisions = {r['item_id']: r['revision'] for r in group}
                    desired = 'loading' if any(r['desired'] == 'loading' for r in group) else (
                        'blocked' if any(r['desired'] == 'blocked' for r in group) else 'done')
                try:
                    # Only old, tracked blocked deliveries need the former default
                    # removed. New transitions must not pay an extra API call.
                    retire_warning = (not self.env.get('SIDEQUESTOR_REACTION_BLOCKED_EMOJI')
                                      and not self.env.get('YAAS_REACTION_BLOCKED_EMOJI')
                                      and 'warning' not in self.emojis.values()
                                      and any(r.get('delivered') == 'blocked' and not r.get('delivered_emoji')
                                              for r in group))
                    options = {'retire_warning': True} if retire_warning else {}
                    retired = sorted({r['delivered_emoji'] for r in group
                                      if r.get('delivered_emoji') and r['delivered_emoji'] not in self.emojis.values()})
                    if retired:
                        options['retire_emojis'] = retired
                    success = self.deliver(record['channel_id'], record['message_ts'], desired, **options)
                except (OSError, subprocess.SubprocessError):
                    success = False
                with self.locked() as records:
                    current = records.get(record['item_id'], {})
                    if current.get('revision') != record['revision']:
                        continue  # A newer ack arrived during delivery; don't lose it.
                    failures = 0 if success else current.get('failures', 0) + 1
                    current.update(confirmed=success, failures=failures,
                                   next_retry=time.time() + min(300, 5 * 2 ** min(failures, 6)))
                    if success:
                        for iid, revision in revisions.items():
                            sibling = records.get(iid, {})
                            if sibling.get('revision') == revision:
                                sibling.update(confirmed=True, delivered=desired,
                                               delivered_emoji=self.emojis[desired])
                if success:
                    try_log_inbox(self.root, f"reaction-status:{record['item_id']}:{record['revision']}", {
                        'event': 'reaction_started' if desired == 'loading' else 'reaction_finished',
                        'channel_id': record['channel_id'], 'message_ts': record['message_ts'],
                        'status': desired, 'succeeded': True, 'note': f'Reaction status confirmed: {desired}'})

    def deliver(self, channel, ts, state, *, retire_warning=False, retire_emojis=()):
        arguments = [sys.executable, str(HERE / 'surfaces/react-lifecycle.py'),
                     'advance', channel, ts, state]
        if retire_warning:
            arguments.append('--retire-warning')
        for emoji in retire_emojis:
            arguments.extend(['--retire-emoji', emoji])
        cp = subprocess.run(arguments, env=self.env,
                            capture_output=True, timeout=200)
        return cp.returncode == 0

    def review(self, item, reason):
        self.request(item, 'blocked', reason=reason)
        with self.locked() as records:
            record = dict(records.get(item['item_id'], {}))
            if record:
                records[item['item_id']]['review_pending'] = True
        record.setdefault('message_ts', item['item_id'].partition(':')[2])
        helper = HERE / 'ledger/approval-helper.py'
        subprocess.run([sys.executable, str(helper), 'ensure-inbox'], env=self.env,
                       capture_output=True, check=True)
        target = {'action': 'reaction_recovery', 'system_key': 'reaction:' + item['item_id'],
                  'repair_mode': 'uncertain', 'message_ts': record['message_ts']}
        if record.get('channel_id'):
            target['channel_id'] = record['channel_id']
        subprocess.run([sys.executable, str(helper), 'write', json.dumps({
            'quest_id': 'quest-inbox', 'source': 'reaction-lifecycle', 'action_type': 'remote_request',
            'target': target, 'message_text': 'Investigate this blocked reaction and reconcile any action already taken before retrying. '
            'Do not blindly repeat a Slack send or other external action. Restore the missing capability, clarify scope, '
            'or arrange an explicitly tracked handoff. Carry out only the reviewed recovery, verify it, then mark '
            'this approval done. This does not automatically replay the original reaction.\n\n' + reason[:1000] +
            ('\nFor expand recovery, read yaas-reactions/SKILL.md and use surfaces/slack-expand.py '
             'with this claimed approval_id. Inspect first; use only the reviewed retry or '
             'resolve outcome. Never edit through a native tool or repeat completed actions.'
             if item['item_id'].partition(':')[0] == self.emojis['expand'] else ''),
            'context': 'Reaction processing stopped without completion; it is not still running.',
            'risk_reason': 'Blocked reaction needs operator review'} )], env=self.env, capture_output=True, check=True)
        # Only stop retries once an actual review item is durable.
        self.request(item, 'blocked', final=True, reason=reason)
        with self.locked() as records:
            if item['item_id'] in records:
                records[item['item_id']]['review_pending'] = False


def kick(root, env):
    # Best effort launch; the durable outbox is retried by the next online tick.
    records = read(Path(root) / 'state/triage/reaction-lifecycle.json', {}).get('items', {})
    if not isinstance(records, dict) or not any(
            isinstance(r, dict) and r.get('channel_id') and not r.get('confirmed')
            and not r.get('silent')
            and r.get('next_retry', 0) <= time.time() for r in records.values()):
        return
    try:
        process = subprocess.Popen([sys.executable, str(HERE / 'reaction_lifecycle.py'), 'flush', str(root)],
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        import threading
        threading.Thread(target=process.wait, daemon=True).start()
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['flush'])
    parser.add_argument('workspace')
    args = parser.parse_args()
    lifecycle = Lifecycle(args.workspace)
    try:
        lifecycle.flush()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Reaction status reconciliation failed: {type(exc).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
