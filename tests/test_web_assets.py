"""首页与静态资源跨更新不能混用浏览器里缓存的旧脚本。"""

from __future__ import annotations

import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.web import pages


class WebAssetCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.static = Path(temporary.name).resolve()
        self.script = self.static / 'js/workspace.js'
        self.script.parent.mkdir()
        self.script.write_text('const overlayTop = null;', encoding='utf-8')
        (self.static / 'css').mkdir()
        (self.static / 'css/base.css').write_text('body { color: black; }', encoding='utf-8')
        (self.static / 'favicon.svg').write_text('<svg/>', encoding='utf-8')
        self.index = self.static / 'index.html'
        self.index.write_text('''<!doctype html><html><head>
<link rel="icon" href="/static/favicon.svg">
<link rel="stylesheet" href="/static/css/base.css">
<script src="/static/js/workspace.js"></script>
<script src="https://example.com/external.js"></script>
</head><body>我的订阅 · 每日日报</body></html>''', encoding='utf-8')
        static_patch = patch.object(pages, '_STATIC', self.static)
        static_patch.start()
        self.addCleanup(static_patch.stop)
        app = FastAPI()
        app.include_router(pages.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def asset_urls(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        return re.findall(r'(?:src|href)=["\'](/static/[^"\']+)', response.text)

    def script_url(self):
        return next(url for url in self.asset_urls() if '/js/workspace.js' in url)

    def test_index_and_assets_require_cache_revalidation(self):
        for url in ['/', *self.asset_urls()]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('cache-control'), 'no-cache')

    def test_local_scripts_styles_and_icons_have_content_versions(self):
        urls = self.asset_urls()
        self.assertEqual(len(urls), 3)
        for url in urls:
            self.assertRegex(url, r'\?v=[a-f0-9]{16}$')
        response = self.client.get('/')
        self.assertIn('src="https://example.com/external.js"', response.text)
        self.assertIn('我的订阅 · 每日日报', response.text)

    def test_unchanged_content_keeps_version_even_after_touch(self):
        before = self.script_url()
        stat = self.script.stat()
        os.utime(self.script, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertEqual(self.script_url(), before)

    def test_content_change_busts_old_url_even_with_identical_size_and_mtime(self):
        before = self.script_url()
        stat = self.script.stat()
        self.script.write_text('const overlayTop = true;', encoding='utf-8')
        os.utime(self.script, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.script.stat().st_size, stat.st_size)
        after = self.script_url()
        self.assertNotEqual(after, before)
        self.assertEqual(self.client.get(after).text, 'const overlayTop = true;')

    def test_removed_scheduler_reference_cannot_reuse_cached_workspace_url(self):
        self.script.write_text('const closers = {sched: closeSchedModal};', encoding='utf-8')
        stale_url = self.script_url()
        self.script.write_text('const closers = {};', encoding='utf-8')
        fresh_url = self.script_url()
        self.assertNotEqual(stale_url, fresh_url)
        self.assertNotIn('closeSchedModal', self.client.get(fresh_url).text)

    def test_versioned_static_route_preserves_path_boundary(self):
        outside = self.static.parent / (self.static.name + '-outside.js')
        outside.write_text('private', encoding='utf-8')
        self.addCleanup(outside.unlink)
        (self.static / 'js/outside.js').symlink_to(outside)
        for url in ['/static/js/outside.js?v=old', '/static/missing.js?v=old',
                    '/static/js/%2e%2e/%2e%2e/outside.js?v=old']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_real_index_versions_every_local_reference(self):
        real_static = Path(__file__).resolve().parents[1] / 'kb/static'
        with patch.object(pages, '_STATIC', real_static):
            urls = self.asset_urls()
            self.assertGreater(len(urls), 30)
            for url in urls:
                self.assertRegex(url, r'\?v=[a-f0-9]{16}$')
                self.assertEqual(self.client.get(url).status_code, 200)


if __name__ == '__main__':
    unittest.main()
