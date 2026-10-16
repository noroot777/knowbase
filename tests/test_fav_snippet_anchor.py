# -*- coding: utf-8 -*-
"""采集 → 核验 → 落库:采集那一刻算出来的事实不许在半路丢掉。

判据本体是 `tests/snip_verify_contract_test.mjs`(node 的测试跑器),这一头只负责
把它接进全仓那一次 `unittest discover` —— 和 `tests/test_favorites.py` 里那条前端
状态回归同一个办法。**为什么不写成 Python**:要验的那两个函数
(`snip_text.js` 的 `snipSeek`、`snip.js` 的 `snipVerified`)就在前端那一份实现里,
照着抄一份 Python 版的话,验的就不再是产品里跑的那一份了 —— 而这条链出事的形态
恰恰是"两头各算一遍、算出来的数不一样"。

**和那几份浏览器探针的分工**:探针验的是用户点下去之后屏幕上和库里发生了什么
(`tests/snip_probe_ident.mjs` 第 1f 格就是这条的 UI 后果),这一份验的是同一件事
在函数边界上的契约 —— 它不用起浏览器、不用摆盘,所以每一格都能把"反过来那一边"
摆到位(同版照旧放行、尾部续写照旧放行、切片的未知不当成已证明)。
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "需要 node 执行共享核验契约回归")
class SnippetVerifyContractTests(unittest.TestCase):
    def test_collected_facts_survive_into_verification(self):
        probe = Path(__file__).with_name("snip_verify_contract_test.mjs")
        done = subprocess.run([shutil.which("node"), "--test", str(probe)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
