# -*- coding: utf-8 -*-
"""改 md frontmatter 不许就地截断原文(`kb/extract_writeback.replace_md`)。

被测的那件事:`Path.write_text` 是 `open(mode="w")` —— 它**先把原文件截成 0
字节**,再写新内容。那中间进程没了(SIGKILL、OOM、掉电),磁盘上剩的是个空
文件,而整条交付的兜底认不出这件事:`promo_tag_pending` 里存的是**目标标签**,
不是原文。实测过的末态是空文件 + 重启后 `sync-tags` 修不回来(空文件压根没有
frontmatter) + 再索引一次把空正文收进库,`doc_body` 从此是空串,阅读页上一片
空白 —— 正文永久没了。**维护锁防不了这一档**:它挡的是别的协作进程插进来,
挡不住本进程在打开写模式之后没了。

所以 tag 回写走原子替换:同目录临时文件 → `flush` + `fsync` → `os.replace`。
末态只剩两个 —— **要么还是原来那一份完整的,要么已经是新的那一份完整的**。

这里用真实 `SIGKILL` 钉它:子进程跑到 `os.replace` 那一瞬间(临时文件已经写
完、目标文件还没被动过)给自己 -9,父进程检查磁盘上剩下什么。正反都钉:不注入
中断时两种变换都要正常改完,而且**残留的临时文件不许被索引当成一篇文档**。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kb import config, extract, scanner                        # noqa: E402
from tests.promo_delivery_fixture import OLD_BODY, PATH, _Vault   # noqa: E402

# 子进程:跑一次真实的 tag 变换,在 `os.replace` 那一瞬间给自己 -9。
# 注入点挑 replace 是因为那正是旧写法最危险的那一点 —— `write_text` 走到这里
# 时原文件已经被截断了,而原子替换走到这里时原文件一个字节都还没动。
_KILL_AT_REPLACE = r'''
import os, signal, sys
sys.path.insert(0, {root!r})
from pathlib import Path
from unittest import mock
from kb import extract

with mock.patch.object(extract, "VAULT_ROOT", Path({tmp!r}) / "vault"):
    def die(*a, **kw):
        sys.stdout.write("about-to-replace\n")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    with mock.patch.object(os, "replace", die):
        {call}
'''


class KilledMidWriteTest(_Vault):
    """写到一半被 SIGKILL —— 整篇原文必须还在。"""

    def _change_then_die(self, call: str):
        root = Path(__file__).resolve().parent.parent
        code = _KILL_AT_REPLACE.format(root=str(root), tmp=str(self.root),
                                       call=call)
        return subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=120)

    def _killed(self, call: str) -> bytes:
        """注入一次真实中断,返回磁盘上剩下的那份字节。"""
        before = self.md.read_bytes()
        proc = self._change_then_die(call)
        self.assertIn("about-to-replace", proc.stdout,
                      f"子进程没跑到注入点:{proc.stderr[-800:]}")
        self.assertEqual(-9, proc.returncode,
                         f"子进程不是被 SIGKILL 结束的:{proc.stderr[-800:]}")
        self.assertEqual(before, self.md.read_bytes(),
                         "原文被改动了 —— 替换那一步不该在中断前碰目标文件")
        return self.md.read_bytes()

    def test_a_kill_while_adding_a_tag_keeps_the_whole_article(self):
        """加一行 tag 的路上被 -9:正文一个字节不少,那行 tag 没加上。

        **后果按「这一篇还读得到吗」钉**,不验「临时文件清掉没有」:
        `write_text` 那一版在这里是 0 字节,正文永久没了。
        """
        self.seed("hard", OLD_BODY, flagged=False)
        left = self._killed(
            f'extract.writeback_tags({PATH!r}, [extract.PROMO_TAG])')
        self.assertIn(OLD_BODY.encode(), left, "正文没了")
        self.assertNotEqual(b"", left)
        self.assertFalse(extract.promo_tag_in_md(PATH), "那行 tag 居然加上了")

    def test_a_kill_while_removing_a_tag_keeps_the_whole_article(self):
        """对称的那一边:摘一行 tag 的路上被 -9,同样一个字节不少。

        这一边的旧末态更阴:空文件「确实没有」那个 tag,于是重启后的修复会把
        待办**清零**,再没有人回来看它一眼。
        """
        self.seed("none", OLD_BODY, flagged=True)
        left = self._killed(
            f'extract.remove_writeback_tag({PATH!r}, extract.PROMO_TAG)')
        self.assertIn(OLD_BODY.encode(), left, "正文没了")
        self.assertTrue(extract.promo_tag_in_md(PATH), "那行 tag 居然摘掉了")

    def test_the_leftover_temp_file_is_not_indexed_as_a_document(self):
        """中断留下的临时文件不许被索引当成一篇文档。

        原子替换必然在同目录留下残骸(SIGKILL 捕获不到,清不了)。它要是叫
        `xxx.md`,下一轮索引就会把半份内容收成一篇新文章 —— 用一个真实的
        `rglob("*.md")` 枚举钉住这一条,而不是断言文件名长什么样。
        """
        self.seed("hard", OLD_BODY, flagged=False)
        self._killed(f'extract.writeback_tags({PATH!r}, [extract.PROMO_TAG])')
        self.assertTrue(
            any(p.name.endswith(".tmp") for p in self.md.parent.iterdir()),
            "夹具没留下残骸 —— 这条用例什么也没验到")
        src = config.Source(name="dr-gzh", root=self.vault / "source/dr-gzh")
        with mock.patch.object(scanner, "VAULT_ROOT", self.vault):
            found = sorted(p.name for p in src.root.rglob("*.md"))
        self.assertEqual(["ad.md"], found)


class NormalWriteTest(_Vault):
    """反过来钉一边:没人打断时两种变换都要正常改完。"""

    def test_adding_and_removing_a_tag_still_work(self):
        """少了这条,把 `replace_md` 写成「什么都不做」也照样全绿。"""
        self.seed("hard", OLD_BODY, flagged=False)
        self.assertTrue(extract.writeback_tags(PATH, [extract.PROMO_TAG]))
        self.assertTrue(extract.promo_tag_in_md(PATH))
        self.assertIn(OLD_BODY, self.md.read_text(encoding="utf-8"))
        self.assertTrue(extract.remove_writeback_tag(PATH, extract.PROMO_TAG))
        self.assertFalse(extract.promo_tag_in_md(PATH))
        self.assertIn(OLD_BODY, self.md.read_text(encoding="utf-8"))

    def test_the_target_file_is_untouched_until_the_very_last_step(self):
        """**不变量本身**:走到替换那一步之前,目标文件还是完整的旧内容。

        SIGKILL 那三条钉的是某一个瞬间的末态;这一条钉的是整段 ——
        `write_text` 那一版在这里压根不会有「替换那一步」(钩子一次都不被调到),
        而它在第一个字节落盘之前就已经把原文截没了。
        """
        self.seed("hard", OLD_BODY, flagged=False)
        before = self.md.read_bytes()
        seen: list[bytes] = []
        real = os.replace

        def spy(src, dst):
            seen.append(Path(dst).read_bytes())
            return real(src, dst)

        with mock.patch.object(os, "replace", spy):
            extract.writeback_tags(PATH, [extract.PROMO_TAG])
        self.assertEqual([before], seen,
                         "替换之前目标文件就被动过了(或者压根没走原子替换)")
        self.assertTrue(extract.promo_tag_in_md(PATH))

    def test_the_file_keeps_its_permission_bits(self):
        """替换之后留下的是临时文件那个 inode —— 权限位要沿用原来那份。

        `mkstemp` 给的是 0600:不钉这一条的话,一次 tag 回写就能把 vault 里那
        篇 md 变成只有我们自己读得了,而编辑器和同步工具都还在用它。
        """
        self.seed("hard", OLD_BODY, flagged=False)
        self.md.chmod(0o644)
        extract.writeback_tags(PATH, [extract.PROMO_TAG])
        self.assertEqual(0o644, self.md.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
