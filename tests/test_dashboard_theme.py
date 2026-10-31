from __future__ import annotations

import http.client
import importlib.util
import json
import re
import shutil
import subprocess
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch

from sidequestor.resources import sync_resources
from sidequestor.workspace import init_workspace

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / 'src/sidequestor/runtime/yaas-triage'
sys.path.insert(0, str(TRIAGE))
import dashboard_theme as theme
sys.path.remove(str(TRIAGE))


class ThemeFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = self.root / 'theme/assets'
        self.assets.mkdir(parents=True)

    def read(self, path='/theme/custom.css'):
        return theme.read_theme(self.root, path)

    def test_live_edits_and_filesystem_reset(self):
        self.assertEqual(self.read(), (200, 'text/css; charset=utf-8', b''))
        css = self.root / 'theme/custom.css'
        for content in (b'body{color:red}', b'body{color:blue}', b''):
            css.write_bytes(content)
            self.assertEqual(self.read()[2], content)
        css.unlink()
        self.assets.rmdir()
        css.parent.rmdir()
        self.assertEqual(self.read()[2], b'')
        self.assertFalse(css.parent.exists())

    def test_assets_and_isolation(self):
        (self.assets / 'wood.PNG').write_bytes(b'local image')
        self.assertEqual(self.read('/theme/assets/wood.PNG'), (200, 'image/png', b'local image'))
        (self.assets / 'wood.PNG').unlink()
        self.assertEqual(self.read('/theme/assets/wood.PNG')[0], 404)
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(theme.read_theme(Path(other), '/theme/assets/wood.PNG')[0], 404)
        for path in ('../secret.css', '%2e%2e/secret.css', 'assets/%2fsecret.css',
                     'assets/%252e%252e/secret.css', 'assets/.env', 'assets/a\\b.css',
                     'assets/a%00.css', 'assets/a.svg', 'assets/a.html', 'assets/a.js',
                     'assets/a.png.html', 'assets/', 'other.css'):
            with self.subTest(path=path):
                self.assertEqual(self.read('/theme/' + path)[0], 404)

    def test_symlinks_and_nonregular_files_never_open(self):
        secret = self.root / 'secret.css'
        secret.write_bytes(b'secret')
        (self.assets / 'secret.css').symlink_to(secret)
        (self.assets / 'dir').symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.read('/theme/assets/secret.css')[0], 404)
        self.assertEqual(self.read('/theme/assets/dir/secret.css')[0], 404)
        os.mkfifo(self.assets / 'pipe.css')
        self.assertEqual(self.read('/theme/assets/pipe.css')[0], 404)
        (self.root / 'theme/custom.css').mkdir()
        with self.assertLogs('dashboard_theme', level='WARNING'):
            self.assertEqual(self.read()[2], b'')
        (self.root / 'theme').rename(self.root / 'saved')
        (self.root / 'theme').symlink_to(self.root / 'saved', target_is_directory=True)
        self.assertEqual(self.read('/theme/assets/secret.css')[0], 404)

    def test_limits_and_unreadable_css_discard_entire_file(self):
        (self.root / 'theme/custom.css').write_bytes(b'x' * 9)
        with patch.object(theme, 'CSS_LIMIT', 8), self.assertLogs('dashboard_theme', level='WARNING'):
            self.assertEqual(self.read()[2], b'')
        (self.assets / 'a.png').write_bytes(b'x' * 9)
        with patch.object(theme, 'ASSET_LIMIT', 8):
            self.assertEqual(self.read('/theme/assets/a.png')[0], 413)
        with patch.object(theme.os, 'open', side_effect=PermissionError('unreadable')), \
                self.assertLogs('dashboard_theme', level='WARNING'):
            self.assertEqual(self.read()[2], b'')

    def test_sync_preserves_theme_and_discovers_skill(self):
        with patch.dict(os.environ, {'YAAS_CONFIG_HOME': str(self.root / 'config')}):
            ws = init_workspace(self.root / 'workspace')
            self.assertFalse((ws.root / 'theme').exists())
            css = ws.root / 'theme/custom.css'
            css.parent.mkdir()
            css.write_bytes(b'/* personal */\nbody{color:gold}')
            sync_resources(ws)
            self.assertEqual(css.read_bytes(), b'/* personal */\nbody{color:gold}')
            for location in ('skills', '.agents/skills', '.claude/skills'):
                skill = ws.root / location / 'yaas-dashboard-theme'
                self.assertTrue((skill / 'SKILL.md').is_file())
                self.assertTrue((skill / 'references/theming.md').is_file())
            css.unlink()
            sync_resources(ws)
            self.assertEqual(list(css.parent.iterdir()), [])
            css.parent.rmdir()
            sync_resources(ws)
            self.assertFalse(css.parent.exists())

    def test_all_allowed_assets_preserve_binary_bytes_and_mime(self):
        expected = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
                    '.woff': 'font/woff', '.woff2': 'font/woff2', '.css': 'text/css; charset=utf-8'}
        folder = self.assets / 'nested'
        folder.mkdir()
        for extension, mime in expected.items():
            with self.subTest(extension=extension):
                data = b'GIF89a\x00\xff\x80' if extension != '.css' else b'@import "../base.css";'
                name = 'sample' + extension.upper()
                (folder / name).write_bytes(data)
                self.assertEqual(self.read('/theme/assets/nested/' + name), (200, mime, data))

    def test_entry_symlinks_and_invalid_encodings(self):
        secret = self.root / 'secret.css'
        secret.write_bytes(b'secret')
        (self.root / 'theme/custom.css').symlink_to(secret)
        with self.assertLogs('dashboard_theme', level='WARNING'):
            self.assertEqual(self.read()[2], b'')
        for path in ('/theme/assets/%ff.css', '/theme/assets/%2Fa.css', '/theme/assets/%5Ca.css'):
            self.assertEqual(self.read(path)[0], 404)

    def test_branding_is_bounded_local_config_with_reset(self):
        config = self.root / 'theme/branding.json'
        self.assertEqual(theme.read_branding(self.root), {})
        config.write_text(json.dumps({'logo': 'assets/Crest 10.PNG', 'activity': {
            'images': ['assets/flower.gif', 'assets/flower.gif', 'https://example.com/x.gif',
                       'assets/../secret.png', 'assets/star.webp'],
            'height': 900, 'gap': -10, 'speed': float('inf'), 'direction': 'left'},
            'script': 'ignored'}))
        result = theme.read_branding(self.root)
        self.assertEqual(result, {'logo': '/theme/assets/Crest%2010.PNG', 'activity': {
            'images': ['/theme/assets/flower.gif', '/theme/assets/star.webp'],
            'height': 96, 'gap': 0, 'speed': 32, 'direction': 'left'}})
        config.unlink()
        self.assertEqual(theme.read_branding(self.root), {})
        for raw in ('broken json', '[]', '{"logo":"assets/a.svg"}', '{"logo":"assets/a.png?x=1"}'):
            config.write_text(raw)
            self.assertEqual(theme.read_branding(self.root), {})
        with patch.object(theme, 'BRANDING_LIMIT', 2):
            self.assertEqual(theme.read_branding(self.root), {})
        config.write_text('[' * 2000 + ']' * 2000)
        self.assertEqual(theme.read_branding(self.root), {})
        config.unlink()
        outside = self.root / 'outside.json'
        outside.write_text('{"logo":"assets/hidden.png"}')
        config.symlink_to(outside)
        self.assertEqual(theme.read_branding(self.root), {})

    def test_sync_preserves_binary_assets_and_branding(self):
        with patch.dict(os.environ, {'YAAS_CONFIG_HOME': str(self.root / 'config')}):
            ws = init_workspace(self.root / 'workspace')
            (ws.root / 'theme/assets').mkdir(parents=True)
            expected = {'theme/assets/flower.gif': b'GIF89a\x00\xff',
                        'theme/branding.json': b'{"logo":"assets/flower.gif"}'}
            for name, data in expected.items():
                (ws.root / name).write_bytes(data)
            sync_resources(ws)
            for name, data in expected.items():
                self.assertEqual((ws.root / name).read_bytes(), data)

    @unittest.skipUnless(shutil.which('node'), 'Node.js needed for train behavior checks')
    def test_activity_train_behavior(self):
        result = subprocess.run(['node', str(ROOT / 'tests/dashboard_theme_branding.cjs')],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ThemeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with patch.dict(os.environ, {'YAAS_WORKSPACE': str(self.root), 'YAAS_RUNTIME_ROOT': str(TRIAGE.parent)}), \
                patch.object(sys, 'argv', ['dashboard-server.py']), \
                patch.object(sys, 'path', [str(TRIAGE), *sys.path]):
            spec = importlib.util.spec_from_file_location('theme_http_test', TRIAGE / 'ops/dashboard-server.py')
            self.server_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.server_module)
        try:
            server = HTTPServer(('127.0.0.1', 0), self.server_module.Handler)
        except PermissionError:
            self.skipTest('localhost binding forbidden')
        self.server = server
        self.server_module.ALLOWED_HOSTS = {f'127.0.0.1:{server.server_port}'}
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.cookie = None

    def shutdown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, method='GET', host=None, extra_headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Cookie': self.cookie} if self.cookie else {}
        headers.update(extra_headers or {})
        if host:
            headers['Host'] = host
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def test_quest_named_visualization_uses_normal_detail_route(self):
        status, headers, _ = self.request('/')
        self.cookie = headers['Set-Cookie'].split(';')[0]
        with patch.object(self.server_module, 'build_quest_detail', return_value={'id':'visualization'}) as detail:
            status, _, body = self.request('/api/quest/visualization')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['id'], 'visualization')
        detail.assert_called_once_with('visualization')

    def test_bootstrap_cookie_theme_headers_auth_and_read_only(self):
        self.assertEqual(self.request('/theme/custom.css')[0], 403)
        status, headers, html = self.request('/')
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn("font-src 'self'", headers['Content-Security-Policy'])
        head = html.decode().split('</head>')[0]
        self.assertTrue(head.rstrip().endswith('<link rel="stylesheet" href="/theme/custom.css">'))
        self.assertEqual(self.request('/theme/custom.css', host='evil.invalid')[0], 403)
        status, headers, body = self.request('/theme/custom.css')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(headers['Content-Type'], 'text/css; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        css = self.root / 'theme/custom.css'
        css.parent.mkdir()
        css.write_bytes(b'body{color:gold}')
        self.assertEqual(self.request('/theme/custom.css')[2], css.read_bytes())
        status, headers, body = self.request('/theme/custom.css', 'HEAD')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(int(headers['Content-Length']), css.stat().st_size)
        self.assertEqual(self.request('/theme/custom.css', 'POST')[0], 405)
        self.assertEqual(self.request('/theme/assets/../custom.css')[0], 404)
        self.assertFalse((self.root / 'state/pending-approvals.json').exists())

    def test_assets_branding_and_module_http_contract(self):
        self.assertEqual(self.request('/api/theme')[0], 403)
        self.assertEqual(self.request('/theme/assets/a.gif')[0], 403)
        self.assertEqual(self.request('/dashboard-assets/theme-branding.js')[0], 403)
        _, headers, html = self.request('/')
        self.cookie = headers['Set-Cookie'].split(';')[0]
        nonce = re.search(r"script-src 'nonce-([^']+)'", headers['Content-Security-Policy']).group(1)
        self.assertIn(f'nonce="{nonce}" src="/dashboard-assets/theme-branding.js"', html.decode())
        self.assertNotIn('__DASHBOARD_NONCE__', html.decode())
        for path, mime in [('/dashboard-assets/theme-branding.js', 'text/javascript; charset=utf-8'),
                           ('/dashboard-assets/theme-branding.css', 'text/css; charset=utf-8')]:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], mime)
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertTrue(body)
        self.assertEqual(json.loads(self.request('/api/theme')[2]), {})
        assets = self.root / 'theme/assets'
        assets.mkdir(parents=True)
        picture = assets / 'a.gif'
        picture.write_bytes(b'GIF89a\x00\xff')
        (self.root / 'theme/branding.json').write_text('{"logo":"assets/a.gif"}')
        self.assertEqual(json.loads(self.request('/api/theme')[2]), {'logo': '/theme/assets/a.gif'})
        status, headers, body = self.request('/theme/assets/a.gif')
        self.assertEqual((status, body), (200, picture.read_bytes()))
        self.assertEqual(headers['Content-Type'], 'image/gif')
        self.assertEqual(headers['Cache-Control'], 'private, no-cache')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.request('/theme/assets/a.gif', 'HEAD')[2], b'')
        with patch.object(theme, 'ASSET_LIMIT', 2):
            self.assertEqual(self.request('/theme/assets/a.gif')[0], 413)
        picture.unlink()
        self.assertEqual(self.request('/theme/assets/a.gif')[0], 404)
        css = self.root / 'theme/custom.css'
        css.write_text('body{color:red}')
        self.assertEqual(self.request('/theme/custom.css')[2], css.read_bytes())
        css.unlink()
        (self.root / 'theme/branding.json').unlink()
        assets.rmdir()
        css.parent.rmdir()
        self.assertEqual(self.request('/theme/custom.css')[2], b'')
        self.assertEqual(json.loads(self.request('/api/theme')[2]), {})

    def test_asset_revalidation_reuses_bytes_but_sees_edits_and_deletion(self):
        _, headers, _ = self.request('/')
        self.cookie = headers['Set-Cookie'].split(';')[0]
        assets = self.root / 'theme/assets'
        assets.mkdir(parents=True)
        image = assets / 'a.gif'
        image.write_bytes(b'GIF89a-first')
        status, headers, body = self.request('/theme/assets/a.gif')
        self.assertEqual((status, body), (200, b'GIF89a-first'))
        self.assertEqual(headers['Cache-Control'], 'private, no-cache')
        etag = headers['ETag']
        status, headers, body = self.request('/theme/assets/a.gif', extra_headers={'If-None-Match': etag})
        self.assertEqual((status, body), (304, b''))
        self.assertEqual(headers['ETag'], etag)
        image.write_bytes(b'GIF89a-second')
        status, headers, body = self.request('/theme/assets/a.gif', extra_headers={'If-None-Match': etag})
        self.assertEqual((status, body), (200, b'GIF89a-second'))
        self.assertNotEqual(headers['ETag'], etag)
        image.unlink()
        status, headers, body = self.request('/theme/assets/a.gif', extra_headers={'If-None-Match': etag})
        self.assertEqual(status, 404)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', headers)
        css = self.root / 'theme/custom.css'
        css.write_bytes(b'body{color:gold}')
        status, headers, body = self.request('/theme/custom.css', extra_headers={'If-None-Match': etag})
        self.assertEqual((status, body), (200, css.read_bytes()))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', headers)
