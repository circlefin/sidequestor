"""Read-only, workspace-owned dashboard theme assets (theme API v1)."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote, unquote

CSS_LIMIT = 1024 * 1024
ASSET_LIMIT = 10 * 1024 * 1024
MIME_TYPES = {
    '.css': 'text/css; charset=utf-8', '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
    '.webp': 'image/webp', '.avif': 'image/avif',
    '.woff': 'font/woff', '.woff2': 'font/woff2',
}


def read_theme(workspace: Path, url_path: str) -> tuple[int, str, bytes]:
    """Open each component relative to a pinned directory fd; never follow links.

    No writes, state access, directory listing, or unbounded file reads. Invalid
    paths are always errors; only unavailable entry CSS falls back to empty CSS.
    """
    entry = url_path == '/theme/custom.css'
    parts = url_path.removeprefix('/theme/').split('/')
    if (not url_path.startswith('/theme/')
            or re.search(r'%(?:2f|5c)', url_path, re.I)):
        return 404, 'text/plain', b''
    try:
        parts = [unquote(part, errors='strict') for part in parts]
    except UnicodeError:
        return 404, 'text/plain', b''
    if (any(not p or p.startswith('.') or any(c in p for c in '/\\\x00%') for p in parts)
            or not (entry or (len(parts) >= 2 and parts[0] == 'assets'))):
        return 404, 'text/plain', b''
    content_type = MIME_TYPES.get(Path(parts[-1]).suffix.lower())
    if content_type is None:
        return 404, 'text/plain', b''
    limit = CSS_LIMIT if parts[-1].lower().endswith('.css') else ASSET_LIMIT
    try:
        return 200, content_type, _read_file(workspace, parts, limit)
    except (OSError, OverflowError) as exc:
        if entry:
            if not isinstance(exc, FileNotFoundError):
                logging.getLogger(__name__).warning('Ignoring theme/custom.css: %s', exc)
            return 200, MIME_TYPES['.css'], b''
        return (413 if isinstance(exc, OverflowError) else 404), 'text/plain', b''


def _read_file(workspace: Path, parts: list[str], limit: int) -> bytes:
    """Open relative to pinned directory descriptors without following symlinks."""
    descriptors = []
    try:
        parent = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(parent)
        for component in ['theme', *parts[:-1]]:
            parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError('theme assets must be regular files')
        if info.st_size > limit:
            raise OverflowError('theme asset exceeds size limit')
        with os.fdopen(os.dup(fd), 'rb') as source:
            body = source.read(limit + 1)
        if len(body) > limit:
            raise OverflowError('theme asset exceeds size limit')
        return body
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif'}
BRANDING_LIMIT = 32 * 1024


def _image_url(value) -> str | None:
    # Paths in JSON are filesystem-relative, not encoded URLs. Reject traversal,
    # query strings and schemes before producing a URL with escaped segments.
    if not isinstance(value, str) or len(value) > 512:
        return None
    parts = value.split('/')
    if (len(parts) < 2 or parts[0] != 'assets'
            or any(not p or p.startswith('.') or any(ord(c) < 32 or ord(c) == 127 for c in p)
                   or any(c in p for c in ('\\', '%', '?', ':', '#')) for p in parts)
            or Path(parts[-1]).suffix.lower() not in IMAGE_SUFFIXES):
        return None
    return '/theme/' + '/'.join(quote(p, safe='') for p in parts)


def _number(value, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return round(max(low, min(high, value)))


def read_branding(workspace: Path) -> dict:
    """Return a bounded presentation-only config; missing/bad config means defaults."""
    try:
        value = json.loads(_read_file(workspace, ['branding.json'], BRANDING_LIMIT))
    except (OSError, OverflowError, ValueError, UnicodeError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    logo = _image_url(value.get('logo'))
    if logo:
        result['logo'] = logo
    activity = value.get('activity')
    if isinstance(activity, dict):
        candidates = activity.get('images')
        images = []
        if isinstance(candidates, list):
            for candidate in candidates[:32]:
                url = _image_url(candidate)
                if url and url not in images:
                    images.append(url)
        if images:
            result['activity'] = {
                'images': images,
                'height': _number(activity.get('height'), 28, 12, 96),
                'gap': _number(activity.get('gap'), 16, 0, 96),
                'speed': _number(activity.get('speed'), 32, 4, 160),
                'direction': 'left' if activity.get('direction') == 'left' else 'right',
            }
    return result
