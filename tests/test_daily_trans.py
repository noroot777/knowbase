# -*- coding: utf-8 -*-
"""日报出处的本地译文(kb/daily_trans.py):哪几条认、哪几条不认。

界面那一侧在 tests/test_daily_zh_browser.py(真浏览器)。

这里钉的是判据本身。它只有一条 —— vault md 的 frontmatter 里 `translated_from`
非空,那一行是 `kb.feeds.sync.export` 落盘时写的。**两个方向都要钉**:认得出译文
的那半对了,"不是译文的一个字都不多"那半也得对 —— 后者错了就是在界面上说假话
(用户点开「译」,读到的还是英文)。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily_trans, store
from kb.web import daily_api


class DailyTranslationLookupTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(store, 'connect', lambda *a, **k: connect(db)),
                  patch.object(daily_trans, 'VAULT_ROOT', self.vault)):
            p.start(); self.addCleanup(p.stop)

    def doc(self, path, url, *, source='rss', title='标题', meta='', body='正文',
            on_disk=True):
        """库里一行 + vault 里一个文件(`on_disk=False` 时只有库里那一行)。"""
        self.conn.execute(
            'INSERT INTO documents(path,source,title,account,published,url)'
            ' VALUES(?,?,?,?,?,?)', (path, source, title, '某号', '2026-10-17 09:00', url))
        self.conn.commit()
        if on_disk:
            f = self.vault / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f'---\nsource: {source}\ntitle: {title}\nurl: {url}\n{meta}---\n\n{body}\n',
                         encoding='utf-8')

    def test_a_translated_article_is_offered_with_its_original_title(self):
        self.doc('source/rss/zh.md', 'https://e.org/en', title='译过来的标题',
                 meta='lang: zh\ntranslated_from: en\noriginal_title: The English Title\n')
        got = daily_trans.translations(['https://e.org/en'])
        self.assertEqual({'https://e.org/en': {
            'path': 'source/rss/zh.md', 'title': '译过来的标题',
            'original_title': 'The English Title'}}, got)

    def test_an_article_that_was_never_translated_is_not_offered(self):
        # 反过来钉一边:库里有这一篇,但它本来就是中文 —— 一个字都不该多。
        self.doc('source/gzh/a.md', 'https://e.org/a', source='gzh', title='中文原文')
        self.assertEqual({}, daily_trans.translations(['https://e.org/a']))

    def test_a_translation_without_a_translated_title_still_counts(self):
        # 只有一行标题的短动态译完 `body_zh` 仍是空串,标题没跟着译时
        # `original_title` 压根不写(见 sync.export)。那一档照样是译文。
        self.doc('source/social/x.md', 'https://e.org/x', source='social',
                 meta='lang: zh\ntranslated_from: en\n')
        got = daily_trans.translations(['https://e.org/x'])
        self.assertEqual('', got['https://e.org/x']['original_title'])

    def test_a_url_the_library_never_saw_is_simply_absent(self):
        self.doc('source/rss/zh.md', 'https://e.org/en',
                 meta='lang: zh\ntranslated_from: en\n')
        self.assertEqual([], list(daily_trans.translations(['https://e.org/other'])))

    def test_a_row_whose_file_is_gone_is_treated_as_no_translation(self):
        # 索引里有行、盘上读不出来:判不出来一律当成没有 —— 少一个入口而已,
        # 而抛出去的话整份日报的入口一个都补不上(那一次请求 500)。
        self.doc('source/rss/missing.md', 'https://e.org/missing', on_disk=False)
        self.assertEqual({}, daily_trans.translations(['https://e.org/missing']))

    def test_a_derived_entry_never_stands_in_for_the_article(self):
        # 条目化产物和原文**共享同一个 url**(见 store.promo_exclude_sql 那一带)。
        # 它是提取出来的条目、不是文章本身,日报素材也不是从它那儿取的。
        self.doc('source/dr-gzh-projects/e.md', 'https://e.org/en',
                 source='dr-gzh-projects', title='某工具:一句话',
                 meta='lang: zh\ntranslated_from: en\n')
        self.assertEqual({}, daily_trans.translations(['https://e.org/en']))

    def test_the_translated_copy_wins_when_one_url_has_several_articles(self):
        # 同一篇被两条订阅各抓一次:只看排在前面那一篇的话,它恰好没译,这个 url
        # 就被判成"本地没有译文" —— 而译文明明在库里。
        self.doc('source/rss/aaa.md', 'https://e.org/dup', title='英文那一份')
        self.doc('source/rss/zzz.md', 'https://e.org/dup', title='中文那一份',
                 meta='lang: zh\ntranslated_from: en\n')
        got = daily_trans.translations(['https://e.org/dup'])
        self.assertEqual('source/rss/zzz.md', got['https://e.org/dup']['path'])

    def test_a_percent_encoded_url_still_finds_its_article(self):
        # 页面上那个 href 是 marked 用 `encodeURI` 写出来的,库里存的是原样 ——
        # 两边只差一层 percent-encoding 时也得对得上,否则带非 ASCII 的出处
        # 永远拿不到入口。
        self.doc('source/rss/cn.md', 'https://e.org/中文/path',
                 meta='lang: zh\ntranslated_from: en\n')
        got = daily_trans.translations(['https://e.org/%E4%B8%AD%E6%96%87/path'])
        self.assertEqual('source/rss/cn.md',
                         got['https://e.org/%E4%B8%AD%E6%96%87/path']['path'])

    def test_two_urls_that_only_differ_in_escaping_are_not_the_same_article(self):
        """`?q=a%26id%3Db`(一个参数)和 `?q=a&id=b`(两个参数)是两篇不同的文章。

        整条 url 解码之后它们是同一个键 —— 那条路会让点「译」打开**另一篇**,而
        抽屉顶上那个「原文」还指着你点的那一篇(2026-10-17 审查用隔离库 + 真浏览器
        实测)。**排在前面的故意是错的那一篇**(`ORDER BY path`:`aaa-` 在 `zzz-`
        之前),挑错了当场看得见。
        """
        from kb.daily_trans import _as_href
        wanted = 'https://example.org/条目?q=a%26id%3Db'
        other = 'https://example.org/条目?q=a&id=b'
        self.doc('source/rss/zzz-wanted.md', wanted, title='要的那一篇',
                 meta='lang: zh\ntranslated_from: en\n')
        self.doc('source/rss/aaa-other.md', other, title='另一篇',
                 meta='lang: zh\ntranslated_from: en\n')
        # 页面上那两个 href(marked 把中文路径编码掉,所以两条都走宽松匹配)。
        href_w, href_o = _as_href(wanted), _as_href(other)
        self.assertNotEqual(href_w, href_o)
        self.assertEqual('source/rss/zzz-wanted.md',
                         daily_trans.translations([href_w])[href_w]['path'])
        self.assertEqual('source/rss/aaa-other.md',
                         daily_trans.translations([href_o])[href_o]['path'])

    def test_both_of_them_survive_one_batch_in_either_order(self):
        """同一批里问两条,两条都要有结果 —— 哪个顺序都一样。

        「规范化键 → 请求地址」的单值映射会让后写进去的那条把前一条挤掉:同批问
        两条只回来一条,颠倒顺序消失的换成另一条(同上,审查实测)。
        """
        from kb.daily_trans import _as_href
        wanted = 'https://example.org/条目?q=a%26id%3Db'
        other = 'https://example.org/条目?q=a&id=b'
        self.doc('source/rss/zzz-wanted.md', wanted, title='要的那一篇',
                 meta='lang: zh\ntranslated_from: en\n')
        self.doc('source/rss/aaa-other.md', other, title='另一篇',
                 meta='lang: zh\ntranslated_from: en\n')
        href_w, href_o = _as_href(wanted), _as_href(other)
        for batch in ([href_w, href_o], [href_o, href_w]):
            with self.subTest(order=batch[0]):
                got = daily_trans.translations(batch)
                self.assertEqual({href_w: 'source/rss/zzz-wanted.md',
                                  href_o: 'source/rss/aaa-other.md'},
                                 {u: d['path'] for u, d in got.items()})

    def test_the_answer_is_keyed_by_the_url_that_was_asked_about(self):
        # 回包的键必须是调用方传进来的那个字符串:换成库里那一份,上面那条
        # 宽松匹配的结果前端就对不上号(页面上那个 href 才是它手里的键)。
        self.doc('source/rss/cn.md', 'https://e.org/中文/path',
                 meta='lang: zh\ntranslated_from: en\n')
        asked = 'https://e.org/%E4%B8%AD%E6%96%87/path'
        self.assertEqual([asked], list(daily_trans.translations([asked])))

    def test_nothing_to_ask_about_is_an_empty_answer(self):
        for urls in ([], None, ['', '   ']):
            self.assertEqual({}, daily_trans.translations(urls))

    def test_too_many_urls_are_cut_off_rather_than_refused(self):
        # 上限是防着调用方乱传,不是业务分档:截断只少几个入口,而拒绝整批
        # 会让一份正常日报一个入口都没有。
        self.doc('source/rss/zh.md', 'https://e.org/en',
                 meta='lang: zh\ntranslated_from: en\n')
        flood = [f'https://e.org/{i}' for i in range(daily_trans.MAX_URLS)]
        # 排在上限之外的那个问不到……
        self.assertEqual({}, daily_trans.translations([*flood, 'https://e.org/en']))
        # ……而排在上限之内的照旧答得出来(截断丢的是后面那截,不是整批拒绝)。
        self.assertEqual(['https://e.org/en'],
                         list(daily_trans.translations(['https://e.org/en', *flood])))
        # 同一个地址重复问不占额度(正文里同一个编号可以引用好多次)。
        self.assertEqual(
            1, len(daily_trans.translations(['https://e.org/en'] * (daily_trans.MAX_URLS + 5))))

    def test_the_endpoint_hands_the_panel_what_it_needs(self):
        self.doc('source/rss/zh.md', 'https://e.org/en',
                 meta='lang: zh\ntranslated_from: en\n')
        app = FastAPI(); app.include_router(daily_api.router)
        client = TestClient(app)
        r = client.post('/api/daily/translations',
                        json={'urls': ['https://e.org/en', 'https://e.org/nope']})
        self.assertEqual(200, r.status_code)
        got = r.json()
        self.assertEqual(['https://e.org/en'], list(got['docs']))
        # vault 名是 obsidian:// 深链拼得出来的前提,少了它抽屉头上那个入口是个空 #。
        self.assertTrue(got['vault'])
        # 一条 url 都不给也要照常回答(面板上那一份日报可能一个外链都没有)。
        self.assertEqual({'docs': {}, 'vault': got['vault']},
                         client.post('/api/daily/translations', json={}).json())


if __name__ == '__main__':
    unittest.main()
