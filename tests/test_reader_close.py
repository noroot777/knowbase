"""把阅读器关闭契约接入统一验收回归，不需要浏览器或外网。"""

import html
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote


@unittest.skipUnless(shutil.which("node"), "需要 node 执行阅读器关闭契约回归")
class ReaderCloseTests(unittest.TestCase):
    def test_reader_close_contract(self):
        probe = Path(__file__).with_name("reader_close_test.mjs")
        result = subprocess.run([shutil.which("node"), "--test", str(probe)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class ReaderPageIdentityTests(unittest.TestCase):
    def test_markdown_identity_is_escaped_in_the_server_template(self):
        from kb.web import pages

        class RootParser(HTMLParser):
            attributes = None

            def handle_starttag(self, tag, attrs):
                if tag == "html":
                    self.attributes = dict(attrs)

        path = 'notes/中文"<&.md'
        rendered = pages._DOC_TMPL.format(
            title="Identity", meta="", md_json='"body"',
            path_json=pages._script_json(path), path_attr=html.escape(path),
            version_json='"version"', literal_js='throw new Error("render failed");',
            links_js=pages._DOC_LINKS_JS, snip_js="",
        )
        parser = RootParser()
        parser.feed(rendered)
        self.assertEqual(path, parser.attributes["data-reader-path"])
        self.assertLess(rendered.index("data-reader-path="), rendered.index("<script"))
        self.assertNotIn("dataset.readerPath =", pages._DOC_LINKS_JS)

    def test_raw_file_identity_is_declared_for_get_and_head(self):
        from kb.web import pages

        path = '中文&"<raw>.txt'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / path).write_text("raw file", encoding="utf-8")
            with patch.object(pages, "VAULT_ROOT", root):
                response = pages.doc_page(path)
        self.assertEqual(quote(path, safe=""), response.headers["X-KB-Reader-Path"])
        methods = set().union(*(route.methods for route in pages.router.routes
                                if route.path == "/doc/{doc_path:path}"))
        self.assertTrue({"GET", "HEAD"}.issubset(methods))


if __name__ == "__main__":
    unittest.main()
