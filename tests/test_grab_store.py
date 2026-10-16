# -*- coding: utf-8 -*-
"""手动收录的**入库与端点**那一半:保存、查重与完成事实、crawl 空转、整条链的
后果,以及 `/api/grab` 那一层。

抓取那一半在 `tests/test_grab.py`,夹具共用 `tests/grab_fixtures.py`。
判据一律先断言后果(见夹具模块的说明)。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import yaml

from kb import config, pipeline
from kb.collectors import FetchScope
from kb.feeds import grab, registry as reg, sync
from kb.source_types import feed_types
from tests.grab_fixtures import (BODY, SOFT_WALL, GrabCase, PAGE, TWEET, html_resp,
                                 page_html)


class SaveTests(GrabCase):
    def test_one_bucket_per_site_with_extraction_on_and_refetch_off(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        buckets = reg.subscriptions(grab.KIND)
        self.assertEqual([b['name'] for b in buckets], ['example.org'])
        self.assertEqual(buckets[0]['url'], 'https://example.org/')
        self.assertEqual(buckets[0]['extract'], 1)   # 手动挑的内容值得买一次提取
        # `fulltext` 必须是 0:那个开关会把条目扣在导出闸门外等「去原文补正文」,
        # 而正文此刻已经在手里了。
        self.assertEqual(buckets[0]['fulltext'], 0)
        self.assertEqual(buckets[0]['enabled'], 1)

    def test_the_same_site_reuses_its_bucket_and_x_gets_a_readable_name(self):
        for url in ('https://example.org/a', 'https://www.example.org/b'):
            grab.save({'url': url, 'title': 'A', 'body': BODY, 'author': 'x',
                       'published': '', 'content_kind': 'content'})
        grab.save({'url': TWEET, 'title': 'T', 'body': '推文', 'author': 'x',
                   'published': '', 'content_kind': 'post'})
        self.assertEqual(sorted(b['name'] for b in reg.subscriptions(grab.KIND)),
                         ['X / Twitter', 'example.org'])

    def test_storing_the_same_link_twice_changes_nothing(self):
        entry = {'url': PAGE, 'title': '原标题', 'body': '原正文', 'author': 'x',
                 'published': '', 'content_kind': 'content'}
        self.assertEqual(grab.save(entry)['added'], 1)
        again = grab.save({**entry, 'title': '新标题', 'body': '新正文'})
        # **0 不是失败,正是想要的末态**:库里那一份一个字节都没动。
        self.assertEqual(again['added'], 0)
        rows = self.items()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['title'], rows[0]['body']), ('原标题', '原正文'))

    def test_an_empty_body_never_reaches_the_insert(self):
        with self.assertRaises(grab.GrabError):
            grab.save({'url': PAGE, 'title': 'A', 'body': '', 'author': 'x',
                       'published': '', 'content_kind': 'content'})
        self.assertEqual(self.items(), [])

    def test_known_separates_staged_from_searchable(self):
        """**「feed_items 里有这一行」不等于「知识库里搜得到」。**

        导出没跑(占不到管道、服务刚重启)、或者索引自己失败时,这一条在库里但
        搜不到。两件事混成一句「已经在知识库里了」就是谎报:用户搜不到,而再投
        一次只会再得到同一句话,后半条链永远没人补。
        """
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.assertFalse(grab.known(PAGE)['done'])          # 刚入库:还没导出
        self.mark_exported(PAGE, 'source/inbox/abc.md')
        self.assertFalse(grab.known(PAGE)['done'])          # 导出了,索引还没有
        self.index_doc('source/inbox/abc.md')
        self.assertTrue(grab.known(PAGE)['done'])           # 这才是走完整条链
        # 索引里是**另一篇**时不算(路径要对上,不是「库里有东西」就算)
        self.mark_exported(PAGE, 'source/inbox/other.md')
        self.assertFalse(grab.known(PAGE)['done'])

    def test_done_follows_the_current_version_not_a_past_one(self):
        """**「以前走完过」不算完成事实。**

        正文补抓回来、一轮翻译回写,都会把 `feed_items.content_hash` 推新 ——
        那时 vault 里还是上一版、索引里也是上一版。按「`documents` 里有这条路径」
        答的话这一条被当成走完了:面板说「已经在知识库里了」,而用户搜到的是旧
        内容,后半条链再也没人补(2026-10-16 第二轮审查)。判据因此是**版本**:
        导出的是现在这一版,而且 vault 里当前那一份进了索引。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        self.assertTrue(grab.known(PAGE)['done'])
        # ① 库里那一行换了版(补抓回来的新正文),vault 和索引都还是上一版
        with reg.db() as c:
            c.execute("UPDATE feed_items SET content_hash='v2' WHERE url=?", (PAGE,))
        self.assertFalse(grab.known(PAGE)['done'])
        # ② 导出跟上了:vault 里已是新正文,而索引里记着的还是上一版的 hash
        self.export_vault(path, '# A\n\n新正文')
        self.mark_exported(PAGE, path)
        self.assertFalse(grab.known(PAGE)['done'])
        # ③ 索引也跟上了,这才算走完
        self.index_doc(path, '# A\n\n新正文')
        self.assertTrue(grab.known(PAGE)['done'])
        # ④ vault 里那一篇没了(清理脚本删过/导出其实没落):照旧不算走完
        (config.VAULT_ROOT / path).unlink()
        self.assertFalse(grab.known(PAGE)['done'])

    def test_a_missing_file_is_made_exportable_again(self):
        """**「补跑一轮」不等于「它会被选中」。**

        `sync._export` 的待办判据是 `exported_hash<>content_hash`。vault 里那一篇
        被删掉(清理脚本、同步盘、手动删)而两个 hash 还相等时:export 一个字都
        不会写、index 反而正确地把它那行旧索引收掉 —— 两个阶段都 ok,而那一篇从此
        搜不到,再投一次又得到同一句话(2026-10-16 第 3 轮审查实测)。所以要先把
        这笔欠账写成 export 认得的形状。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        self.assertTrue(grab.known(PAGE)['done'])
        (config.VAULT_ROOT / path).unlink()
        seen = grab.known(PAGE)
        # 三件事分开报:导出过(hash 相等)、文件没了、于是也谈不上索引。
        self.assertEqual((seen['exported'], seen['stored'], seen['indexed'], seen['done']),
                         (True, False, False, False))
        self.assertEqual(grab.owing(seen), 'vault 里那一篇不见了')
        self.assertFalse(self.pending_export())          # 这才是那个洞
        self.assertTrue(grab.reclaim_export(seen))
        self.assertTrue(self.pending_export())           # 下一轮 export 真会选中它
        # 已经落下了就不必再落一次(这时欠的不再是导出)。
        self.assertFalse(grab.reclaim_export(grab.known(PAGE)))

    def test_reclaim_leaves_the_rows_it_does_not_own_alone(self):
        """**反过来钉一边:别乱清别人的水位。** 文件还在(欠的是索引)、或者两个
        hash 本来就不相等(export 自己会选中它)时,这一笔不许落 —— 判据待在写语句
        自己的 WHERE 里(元规则),所以这里连带钉住「0 行不是失败」。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.export_vault(path)                          # 文件在,只是没索引
        seen = grab.known(PAGE)
        self.assertEqual((seen['stored'], seen['indexed']), (True, False))
        self.assertFalse(grab.reclaim_export(seen))
        self.assertFalse(self.pending_export())          # 水位一个字节都没动
        self.assertEqual(grab.owing(seen), '还没进全文索引')

    #: 一份**看不出是正文**的内容:同一段铺满整页(以前那一版判据漏放存下的形状)。
    WALL = ('Sign in to continue reading. Subscribe to unlock this story. ' * 3 + '\n') * 7

    def test_bad_content_is_a_fact_of_its_own_not_a_missing_step(self):
        """**「写进去了」和「写进去的是一篇文章」是两件事。**

        库里那一份看不出是正文时,`owing` 要先说这件事 —— 它欠的不是哪一步:
        导出、文件、索引全齐也只是把一段不是正文的文字写进了知识库(审查原话:
        已导出、已索引不是内容质量证明)。判据随时重算得出(`body_confirmed`),
        所以老库里那些错存的条目一样认得出来,不必在表上多存一列。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': self.WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        seen = grab.known(PAGE)
        # 后半条链确实都走完了,而内容这一件事实单独为假。
        self.assertEqual((seen['exported'], seen['stored'], seen['indexed'], seen['done']),
                         (True, True, True, True))
        self.assertFalse(seen['content_ok'])
        self.assertEqual(grab.owing(seen), '收下的那一份看不出是正文')
        # 反过来钉一边:正常内容不许被说成看不出是正文。
        grab.save({'url': 'https://example.org/ok', 'title': 'B', 'body': BODY,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.assertTrue(grab.known('https://example.org/ok')['content_ok'])

    def test_short_posts_and_other_sources_are_not_called_bad_content(self):
        """**反过来钉一边:这一问只对「我们按文章正文收下的网页」成立。**

        它的门槛借的是正文提取器那道 `MIN_CHARS`(400),而推文、动态、订阅源
        给的摘要本来就短 —— 拿它去判一条三十个字的推文,等于把所有短内容都说成
        坏内容:重复投递会白跑一次重抓(真发 HTTP),面板上还写着「收下的那一份
        看不出是正文」。别的来源另有自己的「壳 / 空正文」判据,这里不替它们判。
        """
        short = '就一句话的推文，三十个字都不到。'
        # ① 手动收录自己的推文(`content_kind='post'`):短也不算坏内容。
        grab.save({'url': TWEET, 'title': '某某 的动态', 'body': short, 'author': 'x',
                   'published': '', 'content_kind': 'post'})
        self.assertTrue(grab.known(TWEET)['content_ok'])
        # ② 别人家来源里那一条(订阅源给的摘要,`content_kind` 默认 feed)。
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        other = 'https://example.org/posts/short-one'
        reg.put_items(social, [{'url': other, 'title': 'A', 'body': short, 'published': ''}])
        self.assertTrue(grab.known(other)['content_ok'])
        self.assertEqual(grab.owing(grab.known(other)), '还没写成 vault 里那一篇')
        # ③ 反过来:手动收录按网页正文收下的那一条,短/复读就是坏内容。
        grab.save({'url': PAGE, 'title': 'Sign in', 'body': SaveTests.WALL, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.assertFalse(grab.known(PAGE)['content_ok'])

    def test_a_confirmed_refetch_replaces_the_bad_body_in_place(self):
        """**`DO NOTHING` 之下唯一的出路:条件替换。**

        换的时候要连带的事照 `fulltext._save` 那一份口径:`content_hash` 跟着
        正文变(导出闸门因此自然成立)、`exported_hash` **不许清**、
        `extract_stale` 置 1(知识库里那份摘要是按旧内容算的)、译文整组作废。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': self.WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        before = self.items()[0]
        with reg.db() as c:          # 先摆一份旧译文:它译的是那张墙
            c.execute("UPDATE feed_items SET title_zh='墙的译文',body_zh='墙的译文',"
                      "translated_at='2026-10-01' WHERE key=?", (before['key'],))
        fresh = {'url': PAGE, 'title': '真正的文章', 'body': BODY, 'author': '作者',
                 'published': '2026-10-15 10:00', 'content_kind': 'content'}
        self.assertTrue(grab.replace(grab.known(PAGE), fresh))
        row = self.items()[0]
        self.assertEqual((row['title'], row['body']), ('真正的文章', BODY))
        self.assertNotEqual(row['content_hash'], before['content_hash'])
        self.assertEqual(row['exported_hash'], before['exported_hash'])   # 没清
        self.assertEqual(row['extract_stale'], 1)
        self.assertEqual((row['title_zh'], row['body_zh'], row['translated_at']),
                         ('', '', ''))
        self.assertTrue(self.pending_export())       # 下一轮导出会选中它
        self.assertTrue(grab.known(PAGE)['content_ok'])

    def test_replace_keeps_its_hands_off_a_row_that_moved(self):
        """**判据待在 UPDATE 自己的 WHERE 里**(元规则):库里那一版还得是我们
        读到的那一版。这期间别人换过它(一轮正文补抓、一次存量复查、另一条并发
        投递)就不覆盖 —— `rowcount == 0` 不是失败。
        """
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': self.WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        seen = grab.known(PAGE)
        with reg.db() as c:                      # 别人先换了
            c.execute("UPDATE feed_items SET body=?,content_hash='别人那一版'"
                      ' WHERE key=?', (BODY, seen['key']))
        fresh = {'url': PAGE, 'title': '我这一版', 'body': BODY + '尾巴',
                 'author': '', 'published': '', 'content_kind': 'content'}
        self.assertFalse(grab.replace(seen, fresh))
        self.assertEqual(self.items()[0]['body'], BODY)       # 一个字节都没动
        # 一模一样的内容也不必换(那一笔只会白白推一次导出和索引)。
        same = grab.known(PAGE)
        self.assertFalse(grab.replace(same, {'url': PAGE, 'title': same['title'],
                                             'body': same['body']}))

    def test_known_answers_across_sources(self):
        # 一条推文已经被「大V观点」收过了:再收一遍只会在另一棵树下多一篇一样的。
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        seen = grab.known(grab.normalize(TWEET))
        self.assertEqual((seen['kind'], seen['title']), ('social', '旧标题'))
        self.assertIsNone(grab.known('https://example.org/never-seen'))


class PipelineTests(GrabCase):
    def test_this_kind_is_registered_everywhere_that_derives_from_feed_types(self):
        from kb.collectors import collection_channels
        from kb.config import SOURCE_GROUPS, SOURCES
        self.assertIn(grab.KIND, feed_types())
        self.assertFalse(feed_types()[grab.KIND].pollable)
        self.assertIn(grab.KIND, SOURCE_GROUPS)
        self.assertIn(grab.KIND, [s.name for s in SOURCES])
        self.assertIn(grab.KIND, [c['id'] for c in collection_channels()])

    def test_the_crawl_stage_spins_idle_instead_of_fetching_a_bucket(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        # 收件桶是启用着的订阅。放过去就会拿站点首页去发一次 HTTP,而
        # `ADAPTERS` 里压根没有 `manual` 这个适配器。
        with patch.object(sync, 'fetch_source', side_effect=AssertionError('不该抓')) as spy:
            summary = sync.collect(grab.KIND, FetchScope())
        spy.assert_not_called()
        self.assertEqual((summary['sources'], summary['added']), (0, 0))

    def test_a_grabbed_link_lands_in_the_vault_as_one_article(self):
        # 整条链的后果:收下 → 导出 → vault 里那一篇 md 带着 inbox 的身份。
        grab.save({'url': PAGE, 'title': '标题', 'body': '正文内容', 'author': '某记者',
                   'published': '2026-10-15 18:02', 'content_kind': 'content'})
        sync.export(grab.KIND)
        files = list((self.root / 'vault' / 'source' / grab.KIND).glob('*.md'))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        meta = yaml.safe_load(text.split('---')[1])
        self.assertEqual(meta['source'], grab.KIND)
        self.assertEqual(meta['url'], PAGE)
        self.assertEqual(meta['account'], '某记者')
        self.assertIn('正文内容', text)


    def test_replacing_a_bad_body_rewrites_the_vault_file(self):
        """**盘上的后果**:错存的那一篇被换掉之后,vault 里那份 md 真的跟着改。

        这是「不可恢复」那半句话的终点 —— 审查三轮都在这里栽:库里那条坏内容
        导出过、索引过,而重投只会得到「已经收过了」。替换之后 `content_hash`
        变了,导出闸门(`exported_hash<>content_hash`)自然成立,下一轮 export
        把同一个路径重写一遍。
        """
        wall = ('Sign in to continue reading. Subscribe to unlock this story. ' * 3 + '\n') * 7
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': wall,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        sync.export(grab.KIND)
        one = next((self.root / 'vault' / 'source' / grab.KIND).glob('*.md'))
        self.assertIn('Sign in to continue reading', one.read_text())
        self.assertTrue(grab.replace(grab.known(PAGE),
                                     {'url': PAGE, 'title': '真正的文章', 'body': BODY,
                                      'author': 'Example', 'published': ''}))
        sync.export(grab.KIND)
        # 还是**同一个路径**(键没变),里面换成了新正文。
        files = list((self.root / 'vault' / 'source' / grab.KIND).glob('*.md'))
        self.assertEqual([f.name for f in files], [one.name])
        text = one.read_text()
        self.assertIn('这是一段确认得了的正文', text)
        self.assertNotIn('Sign in to continue reading', text)


    def test_a_replace_during_export_cannot_be_confirmed_by_the_old_file(self):
        """**导出确认的是刚落盘的那一份,不是 UPDATE 跑到时库里那一份。**

        交错是定点注入的(不靠 sleep 撞概率):导出把旧那一份渲染好、`os.replace`
        刚落盘的那一刻,另一边(HTTP 请求里的 `_recover` → `grab.replace`,它**不
        持导出这把维护锁**)把正文换成了新的。原先那句 `SET
        exported_hash=content_hash` 取的是 SQL 跑到时行上的值,于是把**新正文**的
        hash 盖成了旧文件的完成回执:导出欠账(`exported_hash<>content_hash`)当场
        消掉,`known` 从此报 done、重复投递按成品返回、补导一轮也选不中它 ——
        vault 和全文检索里**永远**是旧那一份(第 3 轮审查用 Event 卡在同一处
        确定性复现)。

        正向一并钉住:没人插手时这一笔照旧确认得上(末尾那次导出清掉欠账)。
        """
        grab.save({'url': PAGE, 'title': 'Membership required', 'body': SOFT_WALL,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        fresh = {'url': PAGE, 'title': '真正的文章', 'body': BODY,
                 'author': 'Example', 'published': ''}
        real, hits = sync.os.replace, []

        def swap(src, dst):
            real(src, dst)
            if not hits:                      # 只插一次:第二轮导出要照常确认
                hits.append(dst)
                self.assertTrue(grab.replace(grab.known(PAGE), fresh))

        with patch.object(sync.os, 'replace', side_effect=swap):
            sync.export(grab.KIND)
        one = next((self.root / 'vault' / 'source' / grab.KIND).glob('*.md'))
        # 盘上那一篇还是旧的 —— 这是事实,不是缺陷;缺陷是**把它确认成新正文**。
        self.assertIn('Sign in to continue', one.read_text())
        self.assertEqual(self.pending_export(), 1)        # 欠账必须还在
        seen = grab.known(PAGE)
        self.assertFalse(seen['exported'])
        self.assertEqual(grab.owing(seen), '还没写成 vault 里那一篇')
        self.assertFalse(seen['done'])
        # 下一轮导出(谁也不插手)把新正文落盘,欠账这才清掉。
        sync.export(grab.KIND)
        self.assertIn('这是一段确认得了的正文', one.read_text())
        self.assertEqual(self.pending_export(), 0)
        self.assertTrue(grab.known(PAGE)['exported'])


class EndpointTests(GrabCase):
    """收录端点:**重复投递时按真实进度回答**,别把暂存说成已完成。"""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from kb.server import app
        self.client = self.enterContext(TestClient(app))
        self.runs: list[list[str]] = []

    def pipeline_patch(self, started=True, reason='其他同步正在运行'):
        """**打桩也要还原那条契约**:`start_pipeline` 返回的是**这一轮的 id**,
        不是 True —— 前端要拿它核对那一轮 export/index 的结果。回 True 的打桩会
        让「端点把 run 原样交出去」这件事测不出来。"""
        def fake(stages, refused=None, crawl_filter=None, **kwargs):
            self.runs.append((list(stages), (crawl_filter or {}).get('channel')))
            if not started and refused is not None:
                refused.append(reason)
            return 'run-7' if started else ''
        return patch.object(pipeline, 'start_pipeline', fake)

    def post(self, url=PAGE, **body):
        return self.client.post('/api/grab', json={'url': url, **body})

    def test_a_staged_duplicate_gets_the_rest_of_the_chain_run_again(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with self.pipeline_patch():
            r = self.post()
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual((data['duplicate'], data['done']), (True, False))
        self.assertEqual(data['pipeline'], 'started')
        # **那一轮的身份要原样交给前端**:没有它,面板只能按「run_id 变了」猜 ——
        # 别人插在中间的一轮、以及我们这一轮红了,都会被写成「已写入知识库」。
        self.assertEqual(data['run'], 'run-7')
        # **欠哪一步要说出来**(`grab.owing` 那一份口径):笼统一句「还没写进知识
        # 库」把「还没导出」和「文件不见了」说成同一件事,而后者补跑一轮撞不到它。
        self.assertIn('还没写成 vault 里那一篇', data['detail'])
        self.assertEqual(data['owing'], '还没写成 vault 里那一篇')
        # **后果断言**:后半条链真被跑起来了 —— 只改一句文案的话这里红。
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_a_finished_duplicate_is_idempotent_and_costs_no_run(self):
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, 'source/inbox/abc.md')
        self.index_doc('source/inbox/abc.md')
        with self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['done']), (True, True))
        self.assertIn('已经在知识库里了', data['detail'])
        self.assertNotIn('pipeline', data)
        # 绝大多数重复投递都落在这一档:不许顺手占一轮管道。
        self.assertEqual(self.runs, [])

    def test_a_cross_source_item_is_written_through_under_its_own_source(self):
        """一条推文已经被「大V观点」收过、但还没导出:起一轮 `inbox` 的 export
        一个字都不会动它(export 按通道筛),而回答里却写着「正在写入知识库」。"""
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        with self.pipeline_patch():
            data = self.post(TWEET).json()
        self.assertEqual((data['duplicate'], data['done'], data['kind']),
                         (True, False, 'social'))
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'social')])
        self.assertIn('大V观点', data['detail'])

    def test_a_busy_pipeline_names_the_button_of_the_owning_source(self):
        """顺延那一句里的**按钮名按归属来源给**:来源页工具栏那颗按钮在可轮询的
        来源里写「同步全部」,只有手动收录写「写入知识库」(判据就是 `pollable`,
        和 `sources.js` 的 `manual` 分支同一个)。一条推文被「大V观点」收过、这一轮
        又没占到管道时,照手动收录那套说就是指着一颗那一页上不存在的按钮。"""
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        with self.pipeline_patch(started=False):
            data = self.post(TWEET).json()
        self.assertEqual(data['pipeline'], 'busy')
        self.assertIn('在「大V观点」里点一次「同步全部」', data['detail'])
        self.assertNotIn('写入知识库', data['detail'])

    def test_a_fresh_link_that_cannot_get_the_pipeline_says_what_to_do(self):
        with patch.object(grab, 'request', return_value=html_resp(page_html())), \
             self.pipeline_patch(started=False):
            data = self.post().json()
        self.assertEqual((data['added'], data['pipeline']), (1, 'busy'))
        self.assertEqual(data['run'], '')          # 没起跑就没有可核对的那一轮
        self.assertIn('其他同步正在运行', data['detail'])
        self.assertIn('写入知识库', data['detail'])
        # 占不到管道不是失败:内容已经在库里了,下一轮 export 会兜住它。
        self.assertEqual(len(self.items()), 1)

    def test_a_refusal_comes_back_as_a_readable_400(self):
        """**一个真实请求都不许发。** 这一条原先拿一条公众号地址当素材,而
        `classify` 不再按域名拒它了(R2-F3)—— 于是用例会**真的去连
        mp.weixin.qq.com**:结论跟着本机网络走,不可重放。地址那一档照旧不发
        HTTP(X 的账号页),响应那一档把 `request` 打桩成那张验证页。
        """
        r = self.client.post('/api/grab', json={'url': 'https://x.com/example'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('大V观点', r.json()['detail'])
        gate = ('https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x'
                '&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2Fabc')
        with patch.object(grab, 'request', return_value=html_resp(
                '<html><body><p>环境异常</p></body></html>', url=gate)):
            r = self.client.post('/api/grab', json={'url': 'https://mp.weixin.qq.com/s/abc'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('公众号', r.json()['detail'])
        self.assertEqual(self.items(), [])
    def test_a_vanished_file_is_queued_for_rewrite_before_the_run(self):
        """**补跑一轮之前先让它变成待办。** 文件被删掉而两个 hash 还相等时,
        `export` 一个字都不会写 —— 端点要先把水位清掉(`grab.reclaim_export`)
        再起这一轮,否则这颗按钮永远只是空转。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        (config.VAULT_ROOT / path).unlink()
        with self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['done']), (True, False))
        self.assertEqual(data['owing'], 'vault 里那一篇不见了')
        self.assertTrue(data['reclaimed'])
        # **后果断言**:这一轮 export 真会选中它(只回一个标志位的话这里红)。
        self.assertEqual(self.pending_export(), 1)
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_the_resume_hint_names_the_button_of_the_owning_source(self):
        """**「等不到就自己点一次」这句里的按钮也按归属来源给。** 前端轮询超时
        后要给一句出路;写死「写入知识库」会让一条被「大V观点」收过的推文指着
        一颗那一页上没有的按钮(和 busy 那一句同一份口径)。"""
        with patch.object(grab, 'request', return_value=html_resp(page_html())), \
             self.pipeline_patch():
            self.assertIn('写入知识库', self.post().json()['resume'])
        social = reg.save_subscription('social', {'name': 'E', 'url': 'https://x.com/example'})
        reg.put_items(social, [{'url': grab.normalize(TWEET), 'title': '旧标题',
                                'body': BODY, 'published': ''}])
        with self.pipeline_patch():
            resume = self.post(TWEET).json()['resume']
        self.assertIn('大V观点', resume)
        self.assertIn('同步全部', resume)
        self.assertNotIn('写入知识库', resume)

    def test_the_state_endpoint_reads_this_item_and_starts_nothing(self):
        """前端收尾问的是**这一篇的事实**,不是那一轮的阶段结果:export/index
        双 ok 也可能一个字都没写到它身上(队列暂扣、别人的一轮、文件后来没了)。
        所以这个口子必须按条目回答,而且**只读** —— 轮询一次占一轮管道的话,
        面板每隔一秒就抢一次同步。
        """
        path = 'source/inbox/abc.md'
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['known'], got['done'], got['owing']),
                         (False, False, '这条还没收进来'))
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['known'], got['done'], got['owing']),
                         (True, False, '还没写成 vault 里那一篇'))
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['known'], got['done'], got['owing']), (True, True, ''))
        self.assertEqual((got['exported'], got['stored'], got['indexed']),
                         (True, True, True))
        # **第四档是这个口子存在的理由**:两个 hash 还相等(`exported` 为真)而
        # vault 里那一篇没了 —— 按库里那两个 hash 算就是「已完成」,而它搜不到。
        # 少了这一档,把 `done` 换成 `exported` 的改法整组全绿(实测红 0 条)。
        (config.VAULT_ROOT / path).unlink()
        with self.pipeline_patch():
            got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['done'], got['owing']), (False, 'vault 里那一篇不见了'))
        self.assertEqual((got['exported'], got['stored']), (True, False))
        # **只读**:这一问不许顺手把水位清掉(那是投递那颗按钮的事)。
        self.assertEqual(self.pending_export(), 0)
        self.assertEqual(self.runs, [])       # 四次轮询都没占管道

    def test_a_bad_duplicate_is_refetched_and_replaced(self):
        """**查重命中坏内容时要受控重抓一次 —— 这是它唯一的出路。**

        `put_items` 是 `DO NOTHING`:再投一次同一条链接一个字节都不会动那一行,
        于是一份错存的内容(以前那一版判据漏放的登录提示页)永远留在知识库里,
        而用户每次只得到同一句「已经收过了」。**这一档不看 `done`** —— 走完整条
        链的坏内容更该换。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': SaveTests.WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        self.assertTrue(grab.known(PAGE)['done'])        # 后半条链全齐
        with patch.object(grab, 'request', return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.post().json()
        self.assertEqual(got.call_count, 1)              # 真去重抓了一次
        self.assertEqual((data['duplicate'], data['content_ok'], data['replaced']),
                         (True, False, True))
        self.assertEqual(data['done'], False)            # 换完要重新走后半条链
        self.assertEqual(data['owing'], '收下的那一份看不出是正文')
        # **后果断言**:库里那一份真换了,而且后半条链被起了起来。
        self.assertIn('这是正文第一段', self.items()[0]['body'])
        self.assertTrue(self.pending_export())
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_a_refetch_that_still_cannot_be_confirmed_changes_nothing(self):
        """重抓到的还是那张墙:**库里原样留着,不占管道,也不谎报换好了**。
        (元规则:0 行不是失败;拿不准别固化终态。)"""
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': SaveTests.WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        wall_page = page_html(title='Sign in', body=[
            'Sign in to continue reading. Subscribe to unlock this story. ' * 3] * 7)
        with patch.object(grab, 'request', return_value=html_resp(wall_page)), \
             self.pipeline_patch():
            data = self.post().json()
        # **「没换成」要如实交出来**(`replaced=False`):界面据它落可恢复的那一档,
        # 而不是照「没有 pipeline 字段」把这一次当成「早就走完整条链了」。
        self.assertEqual((data['duplicate'], data['replaced']), (True, False))
        self.assertIn('看不出是正文', data['detail'])
        # **这不是终态**:`content_ok` 为假要一路交到前端(它据此落可恢复的那一档,
        # 而不是照「没有 pipeline 字段」当成「早就走完整条链了」)。
        self.assertFalse(data['content_ok'])
        self.assertEqual(self.items()[0]['body'], SaveTests.WALL)   # 一个字节都没动
        self.assertEqual(self.runs, [])                             # 没什么可写,不占管道

    def test_an_explicit_refetch_recovers_what_the_gate_cannot_see(self):
        """**恢复出口不许架在那道会误判的质量判据上。**

        这里种进去的是一张**六道全过**的墙(`SOFT_WALL`:五段各不相同的会员说明,
        一个表单都没有),它从此 `content_ok` 恒真、`done` 也为真 —— 只开给
        「看不出是正文」那一档的恢复出口认不出它,于是原站后来恢复可读、用户一遍
        遍重投,得到的始终是同一句「已经在知识库里了」,HTTP 发出 0 次
        (第 3 轮审查实测)。所以人说「重新抓一次」就重抓,**不先问 content_ok**。
        """
        grab.save({'url': PAGE, 'title': 'Membership required', 'body': SOFT_WALL,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, 'source/inbox/a.md')
        self.index_doc('source/inbox/a.md')
        seen = grab.known(PAGE)
        # 先钉住前提:这一份在质量那一道眼里**没毛病**,整条链也走完了。
        self.assertEqual((seen['content_ok'], seen['done']), (True, True))
        # ① 反过来钉一边:不带 refetch 的重复投递照旧连 HTTP 都不发、照旧幂等。
        with patch.object(grab, 'request', side_effect=AssertionError('不许发请求')), \
             self.pipeline_patch():
            plain = self.post().json()
        self.assertEqual((plain['duplicate'], plain['done']), (True, True))
        self.assertIn('已经在知识库里了', plain['detail'])
        # ② 人明确要求重抓:真去抓一次,确认得了就把库里那一份换掉。
        with patch.object(grab, 'request',
                          return_value=html_resp(page_html())) as got, \
             self.pipeline_patch():
            data = self.post(refetch=True).json()
        self.assertEqual(got.call_count, 1)
        self.assertEqual((data['duplicate'], data['replaced']), (True, True))
        self.assertEqual(data['done'], False)        # 换完要重新走后半条链
        self.assertIn('你要求重新抓一次', data['detail'])
        # **后果断言**:库里那一份真换了,导出欠账跟着回来,后半条链被起了起来。
        self.assertIn('这是正文第一段', self.items()[0]['body'])
        self.assertNotIn('Sign in to continue', self.items()[0]['body'])
        self.assertEqual(self.pending_export(), 1)
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_an_explicit_refetch_that_hits_a_wall_leaves_the_row_alone(self):
        """重抓撞上关卡:**库里原样留着,不占管道,也不说「已完成」。**

        (元规则:拿不准别固化终态。`replaced=False` 如实交到前端,它据此落
        可恢复的那一档 —— 那颗按钮还在,再点一次照旧会重抓。)"""
        grab.save({'url': PAGE, 'title': 'Membership required', 'body': SOFT_WALL,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        wall = page_html(title='Just a moment...', body=[
            'Checking your browser before accessing this site. '
            'Please enable JavaScript and cookies to continue. ' * 4])
        with patch.object(grab, 'request', return_value=html_resp(wall)) as got, \
             self.pipeline_patch():
            data = self.post(refetch=True).json()
        self.assertEqual(got.call_count, 1)
        self.assertEqual((data['duplicate'], data['replaced']), (True, False))
        self.assertTrue(data['content_ok'])          # 这一份在质量那道眼里没毛病
        self.assertIn('你要求重新抓一次', data['detail'])
        self.assertEqual(self.items()[0]['body'], SOFT_WALL)   # 一个字节都没动
        self.assertEqual(self.runs, [])                        # 没什么可写,不占管道

    def test_a_good_duplicate_still_costs_no_request(self):
        """**反过来钉一边:内容没问题的重复投递照旧连 HTTP 都不发。** 受控重抓
        只开给「看不出是正文」那一档 —— 不然每次重复投递都要去对方站点跑一趟。"""
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        with patch.object(grab, 'request', side_effect=AssertionError('不许发请求')), \
             self.pipeline_patch():
            data = self.post().json()
        self.assertEqual((data['duplicate'], data['content_ok']), (True, True))
        self.assertEqual(self.runs, [(['export', 'index', 'extract'], 'inbox')])

    def test_the_state_endpoint_tells_bad_content_from_a_missing_step(self):
        """`/api/grab/state` 也要把这件事实交出去:界面据此说「收下的内容看不出
        是正文,再投一次会重抓替换」,而不是「已写入知识库,搜得到了」。"""
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': SaveTests.WALL,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        self.mark_exported(PAGE, path)
        self.index_doc(path)
        got = self.client.get('/api/grab/state', params={'url': PAGE}).json()
        self.assertEqual((got['done'], got['content_ok']), (True, False))
        self.assertEqual(got['owing'], '收下的那一份看不出是正文')


if __name__ == '__main__':
    unittest.main()
