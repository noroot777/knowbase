"""扫描时每篇 frontmatter 只解析一次,而读出来的 Doc 跟逐字段重新解析的那份一模一样。

同步中心的待处理数要扫两遍全库。原来一篇 md 的 YAML 要解析三遍(`_doc_from_file`
的校验、`doc_from_text`、`body_version`),全库近三万篇时一次查询超过前端 60 秒的
超时,面板永远停在「读取失败：连接超时」。现在校验那一遍的结果直接交给
`doc_from_text`,正文版本号按已经拆好的正文算。

判据落在两头:**结果** —— 走快路径扫出来的 Doc 和 `doc_from_text(text)` 那条慢路径
逐字段相等(正文版本号也和 `body_version(text)` 相等);**次数** —— 每篇只调一次
YAML。反过来钉一边:读坏的、不是映射的 frontmatter 照旧被拒,不因为少解析一遍就
放进索引。
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from kb import scanner
from kb.config import Source


TEXTS = {
    'plain': '没有 frontmatter 的正文。\n',
    'empty': '---\n\n---\n只有空 frontmatter。\n',
    'full': ('---\ntitle: 标题\naccount: 某号\npublished: 2026-11-04\n'
             'url: https://example.org/a\ntags: [甲, 乙]\n---\n'
             '正文提到 [[页面|别名]] 和 [[另一页]]。\n'
             '\n<!-- kb-graph -->\n图谱尾区\n'),
    'scalar-tag': '---\ntitle: 单标签\ntags: 只一个\nsource: dr-gzh\n---\n正文\n',
    'channel': '---\nchannel: 频道\nyoutube: https://youtu.be/x\n---',
}


class ScanParseOnceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kb-scan-parse-once-')
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / 'vault'
        self.src = Source('rss', self.vault / 'source' / 'rss')
        self.src.root.mkdir(parents=True)
        patcher = mock.patch.object(scanner, 'VAULT_ROOT', self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, text: str) -> Path:
        path = self.src.root / f'{name}.md'
        path.write_text(text, encoding='utf-8')
        return path

    def test_fast_path_doc_equals_full_reparse(self):
        for name, text in TEXTS.items():
            with self.subTest(name=name):
                path = self.write(name, text)
                got = scanner._doc_from_file(path, self.src)
                want = scanner.doc_from_text(text, path, self.src)
                fields = lambda d: {k: v for k, v in dataclasses.asdict(d).items()
                                    if k != 'scan_evidence'}
                self.assertEqual(fields(want), fields(got))
                self.assertEqual(scanner.body_version(text), got.body_version)

    def test_yaml_is_loaded_once_per_file(self):
        for name, text in TEXTS.items():
            self.write(name, text)
        real = scanner.load_frontmatter_yaml
        with mock.patch.object(scanner, 'load_frontmatter_yaml',
                               side_effect=real) as load:
            report = scanner.ScanReport()
            with mock.patch.object(scanner, 'SOURCES', [self.src]):
                docs = list(scanner.scan_all(report=report))
        self.assertEqual(len(TEXTS), len(docs))
        self.assertEqual([], report.errors)
        with_fm = sum(1 for t in TEXTS.values() if t.startswith('---\n'))
        self.assertEqual(with_fm, load.call_count)

    def test_broken_or_non_mapping_frontmatter_is_still_rejected(self):
        broken = self.write('broken', '---\ntitle: [没闭合\n---\n正文\n')
        with self.assertRaises(yaml.YAMLError):
            scanner._doc_from_file(broken, self.src)
        listed = self.write('listed', '---\n- 一\n- 二\n---\n正文\n')
        with self.assertRaisesRegex(ValueError, '不是映射'):
            scanner._doc_from_file(listed, self.src)


if __name__ == '__main__':
    unittest.main()
