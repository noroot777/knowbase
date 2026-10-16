# -*- coding: utf-8 -*-
"""摊了文档正文的那几处,报出来的「我摊的是哪一版」必须和**它摊的那份语料同源**。

片段收藏那一头拿这个数证明「屏幕上这一份就是盘上这一份」,证明不了就不采信弱档
(一侧上下文失配、裸引文那两档)。所以这个数**不能是「响应那一刻读一遍盘」算出来
的** —— 那个数证明的是刚读到的文件,而面板摊的往往是另一份语料:

- 订阅条目面板摊的是 feeds.db 里那一行(`body`/`body_zh`),回跳读的是导出的那一篇
  md。先改稿、之后才铺这张列表的话,这一行旧正文会带着**新文件**的版本号,核对那
  一步必然判「相等」,弱档重新放行 —— 审查实测点回去高亮的是改稿新写的那一节。
  这里的口径:按那一行内容算出「它导出成 md 之后正文该是什么指纹」
  (`exported.item_body_md` → `scanner.body_version_of`)。
- 搜索卡摊的是**建索引那一刻**切下来的 `best_chunk`。改稿之后没重建索引,卡片上躺
  的就是一截旧正文 —— 口径:只有建索引时那份文件和此刻盘上这一份逐字节相等才报
  (`web.common.indexed_doc_version`),否则空串 = 「说不出」。

两头的端到端(接口真发出这个数、前端真拿它核对、拒绝之后退到哪一档)在
`tests/snip_probe_ident.mjs` 第 1d / 1d-bis / 1e 格;这一份只钉那两个共用口径本身。
事故形态和回归清单在 `docs/decisions/fav-snippets.md`。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import maintenance                                     # noqa: E402
from kb.feeds import registry as reg                           # noqa: E402
from kb.feeds import sync                                      # noqa: E402
from kb.feeds.exported import item_body_md                     # noqa: E402
from kb.scanner import (body_version, body_version_of,         # noqa: E402
                        file_hash, read_md_text)
from kb.web import common as web_common                        # noqa: E402


class SubsRowVersionTests(unittest.TestCase):
    """订阅条目行报出来的那个数 = 这一行内容导出成 md 之后的正文版本号。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这几条会去 flock 生产
        # 那一个,被 kb-web 正在跑的一轮索引(整趟持锁)挂住(见 kb.maintenance)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.vault)):
            p.start(); self.addCleanup(p.stop)
        self.source = reg.save_subscription('rss', {'name': 'Blog',
                                                    'url': 'https://e.org/f.xml'})
        reg.put_items(self.source, [{'url': 'https://e.org/post-1',
                                     'title': '一篇订阅内容',
                                     'body': '原处这一段先说:这一句是要收藏的那一句。',
                                     'published': '2026-10-01 09:00'}])
        sync.export('rss', hold_untranslated=False)
        self.row = reg.items('rss')['items'][0]
        self.path = self.vault / self.row['path']

    def shown(self):
        """面板那一行会报出来的数(口径同 `kb/web/intake_api.py` 的 `list_items`)。"""
        row = reg.items('rss')['items'][0]
        return body_version_of(item_body_md(row))

    def on_disk(self):
        """`/api/doc` 会报出来的数(回跳那一头核对的就是它)。"""
        return body_version(read_md_text(self.path))

    def test_a_just_exported_row_reports_the_number_api_doc_reports(self):
        """导出完没人动过:两个数必须相等 —— 不然订阅正文的收藏整类退回站外原文。"""
        self.assertEqual(self.on_disk(), self.shown())

    def test_a_disk_edit_after_the_export_makes_them_differ(self):
        """**盘上那一篇被改过之后两个数就不等了**,哪怕这一行内容一个字没变。

        这正是要拦的那个窗口:先改稿、之后才铺列表,面板上还是原来那一段。
        读盘算版本号的那一版在这里必然判「相等」(两边都是新文件),于是弱档
        重新放行、点回去高亮改稿新写的那一节。
        """
        self.path.write_text(
            self.path.read_text(encoding='utf-8')
            .replace('这一句是要收藏的那一句。',
                     '另一节后来也写了一句:这一句是要收藏的那一句。'),
            encoding='utf-8')
        self.assertNotEqual(self.on_disk(), self.shown())

    def test_a_translation_written_back_without_a_re_export_makes_them_differ(self):
        """译文写回了、还没重导出:面板摊的是新译文,盘上那一篇还是旧的。

        反面的旧判据(`exported_hash === content_hash`)在这里判不出来 —— 它是
        **原文**的哈希,重译一遍一个字节都不变。
        """
        with reg.db() as c:
            c.execute("UPDATE feed_items SET title_zh=?, body_zh=? WHERE key=?",
                      ('译过的标题', '译过的正文:这一句是要收藏的那一句。',
                       self.row['key']))
        self.assertNotEqual(self.on_disk(), self.shown())
        # 重导出之后又相等 —— 拦的是「两份语料分了岔」,不是「译过的条目一律不认」。
        # 导出队列是 `exported_hash<>content_hash`,所以这儿照 `translate._save`
        # 的样子先把它清掉(上面那一句故意没清:那正是「两个哈希相等、盘上那篇
        # 却已经是另一份」的窗口)。
        with reg.db() as c:
            c.execute("UPDATE feed_items SET exported_hash='' WHERE key=?",
                      (self.row['key'],))
        sync.export('rss', hold_untranslated=False)
        self.assertEqual(self.on_disk(), self.shown())

    def test_a_frontmatter_only_change_keeps_them_equal(self):
        """反过来钉一边:**只动 frontmatter 不算换了语料。** 提取回写(补 tags、
        写 extracted_at)天天在动它,算进去的话那一刻起所有收藏都退回站外原文。
        """
        text = self.path.read_text(encoding='utf-8')
        self.path.write_text(
            text.replace('\n---\n\n', '\ntags:\n- topic/x\n---\n\n', 1),
            encoding='utf-8')
        self.assertIn('topic/x', self.path.read_text(encoding='utf-8'))
        self.assertEqual(self.on_disk(), self.shown())


class IndexedHitVersionTests(unittest.TestCase):
    """搜索命中那个数:只在「索引扫过的就是此刻盘上这一份」时才报。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.vault = Path(temp.name)
        p = patch.object(web_common, 'VAULT_ROOT', self.vault)
        p.start(); self.addCleanup(p.stop)
        self.rel = 'source/rss/post.md'
        self.full = self.vault / self.rel
        self.full.parent.mkdir(parents=True, exist_ok=True)
        self.text = '---\ntitle: 一篇文章\n---\n\n正文这一段是建索引时切下来的那一截。\n'
        self.full.write_text(self.text, encoding='utf-8')

    def test_it_is_api_docs_number_when_the_file_is_still_the_indexed_one(self):
        self.assertEqual(body_version(self.text),
                         web_common.indexed_doc_version(self.rel, file_hash(self.text)))

    def test_a_stale_index_comes_back_blank_instead_of_the_current_file(self):
        """**索引落后时一律空串。** 报「盘上此刻那一版」的话,卡片上那一截旧正文
        就带着新文件的版本号 —— 核对那一步必然判「相等」,弱档重新放行,审查实测
        点回去高亮的是改稿新写的那一节。
        """
        indexed = file_hash(self.text)
        self.full.write_text(self.text.replace('正文这一段', '改稿之后这一段'),
                             encoding='utf-8')
        self.assertEqual('', web_common.indexed_doc_version(self.rel, indexed))

    def test_no_indexed_hash_or_no_path_comes_back_blank(self):
        """索引里压根没有这一行(空哈希)、或者没有 path:同样是「说不出」。"""
        self.assertEqual('', web_common.indexed_doc_version(self.rel, ''))
        self.assertEqual('', web_common.indexed_doc_version('', file_hash(self.text)))

    def test_unreadable_or_missing_comes_back_blank_instead_of_raising(self):
        """**读不出来只能回空串,不许抛。** 一页列着几十条,其中一篇刚移走、还没
        重建索引是常态 —— 抛出去就是整页列不出来。
        """
        self.assertEqual('', web_common.indexed_doc_version(
            'source/rss/gone.md', file_hash(self.text)))
        # 不是 FileNotFound 的那一类 OSError(权限、网盘没挂上、IO 错都算):
        # 同名目录这一种在任何用户下都稳定可复现
        self.full.unlink()
        self.full.mkdir()
        self.assertEqual('', web_common.indexed_doc_version(self.rel,
                                                            file_hash(self.text)))


if __name__ == '__main__':
    unittest.main()
