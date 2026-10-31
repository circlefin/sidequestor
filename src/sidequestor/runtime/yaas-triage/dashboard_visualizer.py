"""Bounded, read-only evidence projection for the optional quest replay."""
from __future__ import annotations
from collections import deque, Counter
from functools import lru_cache
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import re

MAX_BYTES = 20 * 1024 * 1024
MAX_EVENTS = 5000
MAX_RUN_BYTES = 80 * 1024 * 1024
MAX_LINE = 256 * 1024
EVENT_FIELDS = ('id', 'event_id', 'ts', 'timestamp', 'event', 'note', 'reason',
                'action_type', 'watch_id', 'approval_id', 'dispatch_id',
                'parent_event_id', 'source_event_id', 'caused_by', 'item_id',
                'channel_id', 'channel_name', 'thread_ts', 'message_ts', 'response_ts',
                'repo', 'issue', 'pr', 'doc_id', 'file', 'path', 'file_id',
                'source_file_id', 'target_file_id', 'message_id', 'thread_id',
                'link_url', 'slack_url', 'permalink', 'source_ts', 'watch_type')

EVENT_FIELDS += tuple("created_ts type parent_event_ids url html_url pr_url issue_url ticket_url doc_url source_url target_url repo_url webViewLink jira jira_key issue_key document_id new_file_id clone_file_id copy_file_id source_file_ids new_file_ids prs related_pr issue_numbers title resource_title issue_title pr_title ticket_title doc_title document_title source_title target_title thread_title subject source_channel_id target_channel_id target_channel_name slack_channel_id channel repository".split())
EVENT_FIELDS += tuple("links succeeded status kind resource_type threadId messageId gmail_web_thread_id block_title database_title data_source_title gmail_id message_url ticket summary brief detail finding system_event_key provider surface service account account_email gmail_account mail_account gmail_thread_id gmail_message_id peer chat_id telegram_chat_id chat_title peer_title username conversation_id participant_id post_id tweet_id post_title page_id block_id data_source_id database_id page_title coda_doc_id query search jql cron tz next_fire_ts filter_keywords filter_sender_ids filter_user_ids from_user include_outgoing exclude_replies exclude_reposts user_id issue_id health_issue_id system_key retired_at source target resource resources result watch_identity".split())
COMPOUND_FIELDS = {'links', 'source', 'target', 'resource', 'resources', 'result', 'watch_identity', 'filter_keywords', 'filter_sender_ids', 'filter_user_ids', 'parent_event_ids', 'pr', 'prs', 'related_pr', 'issue', 'issue_numbers', 'jira', 'source_file_ids', 'new_file_ids'}


def _instant(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) or re.fullmatch(r'\d+(?:\.\d+)?', str(value)):
            result = float(value) / (1000 if float(value) > 1e12 else 1)
            return result if math.isfinite(result) and abs(result) < 8.64e12 else None
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _skip(coverage):
    if coverage is not None:
        coverage['truncated'] = True
        coverage['skipped_files'] = coverage.get('skipped_files', 0) + 1


def _allowed(path, root, coverage):
    if root is not None:
        try:
            path.resolve().relative_to(root.resolve())
        except (ValueError, OSError, RuntimeError):
            _skip(coverage)
            return False
    return True


def _records(path, coverage, budget=None, contains=(), root=None):
    """Read a snapshot tail, not a growing/unbounded stream. No writes or locks."""
    if not _allowed(path, root, coverage):
        return
    try:
        with path.open('rb') as source:
            end = source.seek(0, 2)
            available = min(MAX_BYTES, max(0, budget[0])) if budget is not None else MAX_BYTES
            if available <= 0:
                coverage['truncated'] = True
                return
            start = max(0, end - available)
            partial = False
            if start:
                source.seek(start - 1)
                partial = source.read(1) != b'\n'
            source.seek(start)
            remaining = end - start
            if budget is not None:
                budget[0] -= remaining
            if partial:
                line = source.readline(min(remaining, MAX_LINE + 1))
                remaining -= len(line)
                # Discard the whole partial first record, including oversized tails.
                while remaining and line and not line.endswith(b'\n'):
                    line = source.readline(min(remaining, MAX_LINE + 1)); remaining -= len(line)
                coverage['truncated'] = True
            if start:
                coverage['truncated'] = True
            while remaining:
                line = source.readline(min(remaining, MAX_LINE + 1)); remaining -= len(line)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    while remaining and not line.endswith(b'\n'):
                        line = source.readline(min(remaining, MAX_LINE + 1)); remaining -= len(line)
                    coverage['truncated'] = True
                    continue
                if contains and any(needle not in line for needle in contains):
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _small(path, budget=None, coverage=None, root=None):
    if not _allowed(path, root, coverage):
        return {}
    try:
        limit = min(1024 * 1024, budget[0]) if budget is not None else 1024 * 1024
        if limit <= 0:
            _skip(coverage)
            return {}
        with path.open('rb') as source:
            raw = source.read(limit + 1)
        if budget is not None:
            budget[0] -= len(raw)
        if len(raw) > limit:
            _skip(coverage)
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            _skip(coverage)
            return {}
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        _skip(coverage)
        return {}


@lru_cache(maxsize=1)
def _watch_types():
    result = {}
    for path in sorted((Path(__file__).parent / 'checkers').glob('*.watch.json')):
        shape = _small(path)
        identity = shape.get('identity', [])
        fields = {k for k in identity if isinstance(k, str)} if isinstance(identity, list) else set()
        required = shape.get('required', [])
        for group in required if isinstance(required, list) else []:
            if isinstance(group, list):
                fields.update(k for k in group if isinstance(k, str))
        fields = {k for k in fields if isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', k)
                  and not re.search(r'token|secret|password|api_key|cookie|authorization', k, re.I)}
        result[path.name.removesuffix('.watch.json')] = {'fields': sorted(fields)}
    return result


def _clean(row):
    fields = set(EVENT_FIELDS)
    for shape in _watch_types().values():
        fields.update(shape['fields'])
    shortened = False
    def bounded(value, depth=0):
        nonlocal shortened
        if isinstance(value, str):
            if len(value) > 1500:
                shortened = True
            value = re.sub(r'(https?://)[^/\s@]+@', r'\1[credentials]@', value[:1500])
            return re.sub(r'(?i)([?&](?:access_token|token|api_key|key|auth|authorization|signature|sig|password|secret)=)[^&#\s]*', r'\1[redacted]', value)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value if math.isfinite(value) else None
        if depth < 3 and isinstance(value, list):
            if len(value) > 20:
                shortened = True
            return [bounded(v, depth + 1) for v in value[:20] if isinstance(v, (str, int, float, dict))]
        if depth < 3 and isinstance(value, dict):
            return {k: bounded(v, depth + 1) for k, v in value.items()
                    if k in fields or k in {'number', 'name'}
                    if isinstance(v, (str, int, float, bool)) or k in COMPOUND_FIELDS}
        return None
    result = {key: bounded(row[key]) for key in fields
            if isinstance(row.get(key), (str, int, float, bool))
            or ((key in COMPOUND_FIELDS or key.startswith('filter_')) and isinstance(row.get(key), (list, dict)))}
    if shortened:
        result['projection_truncated'] = True
    return result


def build_visualization(workspace: Path, quest_id: str, detail: dict | None = None) -> dict | None:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', quest_id) or quest_id in {'.', '..'}:
        return None
    base = workspace / 'state/quests/active'
    folder = base / quest_id
    try:
        folder.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    if not folder.is_dir():
        return None
    coverage = {'truncated': False, 'timeline': 0, 'triggers': 0, 'dispatches': 0, 'receipts': 0, 'skipped_files': 0}
    if detail is None:
        detail = {'meta': _small(folder / 'meta.json', coverage=coverage, root=workspace), 'watches': _small(folder / 'watch.json', coverage=coverage, root=workspace).get('watches', [])}
    timeline = deque(maxlen=MAX_EVENTS)
    timeline_budget = [MAX_BYTES]
    # Read the current tail first to reserve its budget, then restore archive-first
    # ordering. Rotation can briefly duplicate rows, so deduplicate exact records.
    current = list(_records(folder / 'timeline.ndjson', coverage, timeline_budget, root=workspace))
    archived = _records(folder / 'timeline.archive.ndjson', coverage, timeline_budget, root=workspace)
    archive_counts = Counter()
    coverage['timeline_archived'] = 0
    for origin, rows in [('quest timeline archive', archived), ('quest timeline', current)]:
        for index, row in enumerate(rows):
            identity = json.dumps(row, sort_keys=True, separators=(',', ':'))
            if origin == 'quest timeline' and archive_counts[identity] > 0:
                archive_counts[identity] -= 1
                continue
            if origin.endswith('archive'):
                archive_counts[identity] += 1
            coverage['timeline'] += 1
            if origin.endswith('archive'):
                coverage['timeline_archived'] += 1
            item = _clean(row)
            item['event_id'] = str(row.get('event_id') or row.get('id') or f'{origin}:{index}')
            item['evidence_source'] = origin
            timeline.append(item)
    if coverage['timeline'] > MAX_EVENTS:
        coverage['truncated'] = True
    events = list(timeline)
    dispatches = []
    # The old view omitted this source: fired watches are recorded here, not
    # necessarily in the quest timeline. Keep only this quest's rows.
    run_paths = [p for p in (workspace / 'state').glob('run-log*.ndjson')
                 if p.name == 'run-log.ndjson' or re.fullmatch(r'run-log-archive-\d{4}-\d{2}\.ndjson', p.name)]
    if len(run_paths) > 73:
        coverage['truncated'] = True
    run_budget = [MAX_RUN_BYTES]
    coverage['run_log_files'] = 0
    def run_records():
        for path in sorted(run_paths, key=lambda p: p.name, reverse=True)[:73]:
            if run_budget[0] <= 0:
                coverage['truncated'] = True
                break
            coverage['run_log_files'] += 1
            yield from _records(path, coverage, run_budget, (b'\"gate_dispatch\"', quest_id.encode()), root=workspace)
    seen_dispatches = set()
    for row in run_records():
        if row.get('event') != 'gate_dispatch':
            continue
        dirty = row.get('dirty_watches')
        if not isinstance(dirty, list):
            continue
        mine = [w for w in dirty if isinstance(w, dict) and w.get('quest_id') == quest_id]
        if not mine:
            continue
        identity = json.dumps(sorted((str(w.get('watch_id')), str(w.get('type')), str(w.get('advance_to'))) for w in mine), separators=(',', ':'))
        session_id = 'dispatch:' + str(row.get('ts')) + ':' + hashlib.sha256(identity.encode()).hexdigest()[:12]
        if session_id in seen_dispatches:
            continue
        seen_dispatches.add(session_id)
        start = _instant(row.get('ts'))
        sources = []
        for wi, watch in enumerate(mine):
            source_id = f'{session_id}:trigger:{wi}'
            sources.append(source_id)
            events.append({'event_id': source_id, 'ts': row.get('ts'), 'event': 'watch_fired',
                           'watch_id': str(watch.get('watch_id') or ''), 'watch_type': str(watch.get('type') or ''),
                           'dispatch_id': session_id, 'source_ts': watch.get('advance_to'),
                           'evidence_source': 'triage run log', 'note': 'Checker reported activity and selected this watch for dispatch.'})
            coverage['triggers'] += 1
        events.append({'event_id': session_id, 'ts': row.get('ts'), 'event': 'dispatch_started',
                       'dispatch_id': session_id, 'parent_event_ids': sources,
                       'evidence_source': 'triage run log', 'note': 'Recorded watch selection for this dispatch; individual worker actions may not carry a source ID.'})
        dispatches.append((start, session_id))
        coverage['dispatches'] += 1
    dispatches.sort(key=lambda d: d[0] if d[0] is not None else float('-inf'))
    # Temporal dispatch membership is an association, never a fabricated cause.
    # Keep it separate and bounded: no matching across long idle gaps.
    import bisect
    starts = [d[0] or float('-inf') for d in dispatches]
    for event in timeline:
        when = _instant(event.get('ts') or event.get('timestamp'))
        if when is None:
            continue
        i = bisect.bisect_right(starts, when) - 1
        if i >= 0 and 0 <= when - starts[i] <= 3600 and not event.get('dispatch_id'):
            event['associated_dispatch_id'] = dispatches[i][1]
            event['dispatch_association'] = 'Occurred within one hour after recorded dispatch; timing association, not proof of causality.'
    # Dispatch receipts are additional explicit evidence when retained. They are
    # optional; older quests can still recover triggers from the run log above.
    receipt_paths = list((workspace / 'state/triage').glob('dispatch-*.json'))
    if len(receipt_paths) > 3000:
        coverage['truncated'] = True
    known_watch_ids = {str(w.get('watch_id')) for w in detail.get('watches', [])
                       if isinstance(w, dict)} if isinstance(detail.get('watches'), list) else set()
    receipt_budget = [MAX_BYTES]
    for path in sorted(receipt_paths, key=lambda p: p.name, reverse=True)[:3000]:
        if receipt_budget[0] <= 0:
            coverage['truncated'] = True
            break
        receipt = _small(path, receipt_budget, coverage=coverage, root=workspace)
        if receipt.get('target') != quest_id:
            continue
        items = receipt.get('items') or []
        if not isinstance(items, list):
            _skip(coverage)
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get('item_id') or '')
            watch_id = item_id if (item_id in known_watch_ids or re.fullmatch(r'watch-[0-9a-f]+(?:-\d+)?', item_id)) else ''
            events.append({'event_id': f'receipt:{receipt.get("run_id", path.stem)}:{i}',
                           'ts': item.get('acked_utc') or receipt.get('created_utc'), 'event': 'dispatch_receipt',
                           'watch_id': watch_id, 'item_id': item_id, 'watch_type': str(item.get('type') or ''),
                           'dispatch_id': str(receipt.get('run_id') or ''), 'note': str(item.get('note') or '')[:1500],
                           'outcome': str(item.get('status') or ''), 'evidence_source': 'dispatch receipt'})
            coverage['receipts'] += 1
    events.sort(key=lambda e: _instant(e.get('ts') or e.get('timestamp')) or float('-inf'))
    if len(events) > MAX_EVENTS:
        coverage['truncated'] = True
        events = events[-MAX_EVENTS:]
    watches = detail.get('watches') if isinstance(detail.get('watches'), list) else []
    if len(watches) > 1000:
        coverage['truncated'] = True
    meta = detail.get('meta') if isinstance(detail.get('meta'), dict) else {}
    historical_watches = []
    snapshot_by_id = {}
    retained_ids = {e.get('watch_id') for e in events if e.get('watch_id')}
    snapshot_budget = [4 * 1024 * 1024]
    snapshots = sorted((p for p in folder.glob('watch.json.bak-*')
                        if re.fullmatch(r'watch\.json\.bak-\d{14}', p.name) and not p.is_symlink()), reverse=True)
    if len(snapshots) > 20:
        coverage['truncated'] = True
    for path in snapshots[:20]:
        if snapshot_budget[0] <= 0:
            coverage['truncated'] = True
            break
        try:
            observed = datetime.strptime(path.name[-14:], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
        snapshot = _small(path, snapshot_budget, coverage=coverage, root=workspace)
        saved = snapshot.get('watches', [])
        for index, watch in enumerate(saved[:1000] if isinstance(saved, list) else []):
            if not isinstance(watch, dict):
                continue
            item = _clean(watch)
            wid = watch.get('watch_id')
            if not wid:
                # Older snapshots predate stable IDs. Recover an identity only
                # when the engine's deterministic migration hash matches a log.
                identity = {k: v for k, v in watch.items() if k not in ('last_checked_ts', 'watch_id')}
                canonical = json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                candidate = 'watch-' + hashlib.sha256(f'{quest_id}\0{index}\0{canonical}'.encode()).hexdigest()[:16]
                fields = _watch_types().get(watch.get('type'), {}).get('fields', [])
                logical = {'type': watch.get('type'), **{k: watch[k] for k in fields if k in watch}} if fields else identity
                stable = hashlib.sha256(json.dumps(logical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
                wid = candidate if candidate in retained_ids else 'snapshot:' + stable
            item.update(watch_id=wid, historical=True, observed_at=observed,
                        watch_evidence='Retained watch snapshot; exact start and retirement may be unknown.')
            if wid in snapshot_by_id:
                previous = snapshot_by_id[wid]
                previous['observed_at'] = min(previous['observed_at'], observed)
                continue
            snapshot_by_id[wid] = item
            historical_watches.append(item)
            if len(historical_watches) >= 1000:
                coverage['truncated'] = True
                break
        if len(historical_watches) >= 1000:
            break
    return {'api_version': 2, 'id': quest_id, 'title': meta.get('title') or quest_id,
            'watch_types': _watch_types(), 'created_at': meta.get('created') or meta.get('created_at'), 'historical_watches': historical_watches,
            'watches': [_clean(w) for w in watches if isinstance(w, dict)][:1000], 'events': events,
            'coverage': coverage, 'truncated': coverage['truncated'], 'scanned_events': coverage['timeline'],
            'event_limit': MAX_EVENTS,
            'causality_note': 'Solid paths are recorded links. Dotted paths are resource or dispatch-time associations. Missing historical sources do not mean an action had no trigger.'}
