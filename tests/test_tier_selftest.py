# -*- coding: utf-8 -*-
"""测试设施自测那一组什么时候可以不跑(tests/tier_selftest.py)。

口径各钉两面:

- **名单里只许有不碰产品代码的文件**(结构守卫,AST 判 import):一旦某个自测
  文件开始 import `kb`,产品改动就能改变它的结论,「指纹没变就不跑」随即失效。
  反过来钉:名单里每个名字都真有其文件。
- **指纹圈得住设施、圈不进别的测试**:headless.py / fixture_life.py /
  probe_shell.mjs 这类必须在里面;别的 test_*.py 不许被顺进来 —— 顺进来的话指纹
  随每次产品测试改动都变,这一组就永远照跑。
- **证要对得上、没过期才跳**:没证、证坏了、指纹变了、过期了、设了
  `KB_SELFTEST=always`,一律照跑;反过来钉:刚落的证当场就能让下一轮跳过。
- **改了一份设施文件,指纹就变**;不改就不变。
"""

import ast
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import tier_selftest as st


class RosterTests(unittest.TestCase):

    def test_every_listed_module_is_a_real_file(self):
        for module in st.SELFTEST_MODULES:
            self.assertTrue(st.module_file(module).is_file(), module)

    def test_listed_modules_never_import_product_code(self):
        for module in st.SELFTEST_MODULES:
            tree = ast.parse(st.module_file(module).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    self.assertFalse(name == "kb" or name.startswith("kb."),
                                     f"{module} import 了 {name}:产品改动能改变它的结论,"
                                     "不许留在可跳过的名单里")


class ClosureTests(unittest.TestCase):

    def test_the_infrastructure_is_inside(self):
        names = {p.name for p in st.closure()}
        for must in ("__init__.py", "headless.py", "fixture_life.py",
                     "probe_shell.mjs", "serve_workspace.py"):
            self.assertIn(must, names)

    def test_other_test_files_stay_out(self):
        own = {st.module_file(m).name for m in st.SELFTEST_MODULES}
        strays = {p.name for p in st.closure()
                  if p.name.startswith("test_") and p.name not in own}
        self.assertEqual(set(), strays)


class FingerprintTests(unittest.TestCase):

    def test_editing_an_infrastructure_file_changes_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tests").mkdir()
            infra = root / "tests" / "fixture_life.py"
            infra.write_text("A = 1\n", encoding="utf-8")
            with mock.patch.object(st, "ROOT", root), \
                    mock.patch.object(st, "closure", lambda: [infra]):
                first = st.fingerprint()
                self.assertEqual(first, st.fingerprint(), "不改就不变")
                infra.write_text("A = 2\n", encoding="utf-8")
                self.assertNotEqual(first, st.fingerprint(), "改了一份设施文件,指纹必须变")


class DecideTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.td.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(st.FORCE_ENV, None)
        fp = mock.patch.object(st, "fingerprint", lambda: "fp-now")
        fp.start()
        self.addCleanup(fp.stop)

    def test_no_record_means_run(self):
        self.assertFalse(st.decide()[0])

    def test_a_fresh_record_lets_the_next_round_skip(self):
        st.record_pass()
        skip, why = st.decide()
        self.assertTrue(skip, why)
        self.assertIn(st.FORCE_ENV, why, "跳过时要说清怎么强制跑")

    def test_a_changed_fingerprint_means_run(self):
        st.record_pass()
        with mock.patch.object(st, "fingerprint", lambda: "fp-other"):
            self.assertFalse(st.decide()[0])

    def test_an_expired_record_means_run(self):
        st.record_pass(now=time.time() - st.MAX_AGE_SECONDS - 60)
        self.assertFalse(st.decide()[0])

    def test_a_record_from_the_future_means_run(self):
        st.record_pass(now=time.time() + 3600)
        self.assertFalse(st.decide()[0])

    def test_force_means_run(self):
        st.record_pass()
        os.environ[st.FORCE_ENV] = "always"
        self.assertFalse(st.decide()[0])

    def test_a_corrupt_record_means_run(self):
        path = st.cache_path()
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertFalse(st.decide()[0])


if __name__ == "__main__":
    unittest.main()
