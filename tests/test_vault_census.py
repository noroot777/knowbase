"""待处理数的 vault 清点:没变过的文件不重读,变过的一定重读,结果和 `scan_all` 一致。

同步中心每次打开都要一份待处理数。原来每问一次就把全库读一遍(一次查询还读两遍),
`vault_census` 改成按文件 stat 签名记住上一次的结果。判据落在两头:**省下来的** ——
第二次清点一篇都不读;**没漏掉的** —— 改了内容(连同把 mtime 拨回原值)、新增、删除、
读坏又修好,下一次清点都如实反映,且始终和 `scan_all` 读出来的那份相等。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kb import scanner, vault_census
from kb.config import Source


def fm(title: str, source: str = '') -> str:
    extra = f'source: {source}\n' if source else ''
    return f'---\ntitle: {title}\n{extra}---\n{title} 的正文。\n'


class VaultCensusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kb-vault-census-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'vault'
        self.sources = [Source(name, self.vault / 'source' / name) for name in ('rss', 'dr-gzh')]
        for source in self.sources:
            source.root.mkdir(parents=True)
        for patcher in (mock.patch.object(scanner, 'VAULT_ROOT', self.vault),
                        mock.patch.object(scanner, 'SOURCES', self.sources)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(vault_census._memo.clear)

    def write(self, rel: str, text: str) -> Path:
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def census(self):
        """清点一次,同时数出这一次真去读了几篇。"""
        real = scanner._doc_from_file
        with mock.patch.object(scanner, '_doc_from_file', side_effect=real) as read:
            got = sorted((e.path, e.source, e.content_hash) for e in vault_census.census())
        return got, read.call_count

    def full_scan(self):
        return sorted((d.path, d.source, d.content_hash) for d in scanner.scan_all())

    def test_second_census_reads_nothing_and_matches_full_scan(self):
        self.write('source/rss/a.md', fm('甲'))
        self.write('source/dr-gzh/b.md', fm('乙'))
        self.write('source/rss/c.md', fm('丙', source='wechat'))
        first, reads = self.census()
        self.assertEqual((self.full_scan(), 3), (first, reads))
        second, reads = self.census()
        self.assertEqual((first, 0), (second, reads))

    def test_every_change_is_seen_on_the_next_census(self):
        self.write('source/rss/same.md', fm('不动'))
        edited = self.write('source/rss/edited.md', fm('原稿'))
        gone = self.write('source/rss/gone.md', fm('要删'))
        first = {p: h for p, _s, h in self.census()[0]}
        # 等长改写,再把 mtime 拨回原值:只剩 ctime 能说明它变了
        before = edited.stat()
        edited.write_text(fm('改稿'), encoding='utf-8')
        os.utime(edited, ns=(before.st_atime_ns, before.st_mtime_ns))
        gone.unlink()
        self.write('source/dr-gzh/new.md', fm('新来'))
        got, reads = self.census()
        self.assertEqual(self.full_scan(), got)
        self.assertEqual(2, reads)                     # 改过的那篇 + 新来的那篇
        now = {p: h for p, _s, h in got}
        self.assertNotIn('source/rss/gone.md', now)
        self.assertNotEqual(first['source/rss/edited.md'], now['source/rss/edited.md'])
        self.assertEqual(first['source/rss/same.md'], now['source/rss/same.md'])

    def test_unreadable_file_is_absent_and_not_remembered(self):
        broken = self.write('source/rss/broken.md', '---\ntitle: [没闭合\n---\n正文\n')
        got, reads = self.census()
        self.assertEqual(([], 1), (got, reads))
        # 文件没动,下一次照样重读(读坏的那篇不记住)
        got, reads = self.census()
        self.assertEqual(([], 1), (got, reads))
        broken.write_text(fm('修好'), encoding='utf-8')
        got, _reads = self.census()
        self.assertEqual(self.full_scan(), got)
        self.assertEqual(1, len(got))


if __name__ == '__main__':
    unittest.main()
