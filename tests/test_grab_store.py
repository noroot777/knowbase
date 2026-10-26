# -*- coding: utf-8 -*-
"""手动收录的**入库**那一半:保存、查重与完成事实、crawl 空转、整条链的后果。

抓取那一半在 `tests/test_grab.py`,`/api/grab` 那一层在 `tests/test_grab_endpoint.py`,
夹具共用 `tests/grab_fixtures.py`。判据一律先断言后果(见夹具模块的说明)。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import yaml

from kb import config
from kb.collectors import FetchScope
from kb.feeds import grab, registry as reg, sync
from kb.source_types import feed_types
from tests.grab_fixtures import (BODY, DIGEST_SUMMARY, SOFT_WALL, WALL,
                                 GrabCase, PAGE, TWEET)


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

    def test_bad_content_is_a_fact_of_its_own_not_a_missing_step(self):
        """**「写进去了」和「写进去的是一篇文章」是两件事。**

        库里那一份看不出是正文时,`owing` 要先说这件事 —— 它欠的不是哪一步:
        导出、文件、索引全齐也只是把一段不是正文的文字写进了知识库(审查原话:
        已导出、已索引不是内容质量证明)。判据随时重算得出(`body_confirmed`),
        所以老库里那些错存的条目一样认得出来,不必在表上多存一列。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
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
        grab.save({'url': PAGE, 'title': 'Sign in', 'body': WALL, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        self.assertFalse(grab.known(PAGE)['content_ok'])

    def test_a_confirmed_refetch_replaces_the_bad_body_in_place(self):
        """**`DO NOTHING` 之下唯一的出路:条件替换。**

        换的时候要连带的事照 `fulltext._save` 那一份口径:`content_hash` 跟着
        正文变(导出闸门因此自然成立)、`exported_hash` **不许清**、
        `extract_stale` 置 1(知识库里那份摘要是按旧内容算的)、译文整组作废。
        """
        path = 'source/inbox/abc.md'
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
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
        grab.save({'url': PAGE, 'title': 'Sign in to read', 'body': WALL,
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


class OriginBodyGateTests(GrabCase):
    """`replace()` 自己那条 WHERE 里的判据(`origin_body_sql`)。

    **和入口那一道各钉一次**(入口在 `tests/test_grab_endpoint.py`):入口答的是
    「这次该不该抓」,这一道答的是「这一行的正文该不该被这次重抓换掉」。只有后者
    挡得住绕过入口的调用方,而这个入口曾经是五个写 `body` 的 UPDATE 里唯一漏接的
    那个 —— 漏接的形态是**静默覆盖**,不是报错。

    **判据问的是「这一行的 `url` 指向的是不是它自己」,不是「这一类进不进补抓队列」。**
    三类来源把两边都钉住:简报(链接指向别人家那篇 → 拒)、手动收录(链接就是人要的
    那一篇 → 换)、大V观点(**两个问题在这一类上分叉**:不进补抓队列,但链接是它本人
    的永久链接 → 换)。只摆前两类的后果实测过:把通用路那道闸拿来当这里的判据,两条
    用例照旧全绿,而推文那条唯一的人工出路被整条打死。
    """

    def fresh(self):
        return {'url': PAGE, 'title': 'openai/openai-cli: a terminal client',
                'body': BODY, 'author': 'github.com', 'published': '',
                'content_kind': 'content'}

    def test_a_row_that_owns_its_body_is_not_replaced(self):
        """简报那条简讯:CAS 全对、内容也确认得了,照旧一个字都不许换。"""
        key = self.digest_item()
        seen = grab.known(PAGE)
        self.assertEqual(seen['kind'], 'digest')     # 查重按 url,命中的不是手动收录
        self.assertTrue(seen['content_ok'])          # 质量那道判据对它恒真
        self.assertFalse(grab.replace(seen, self.fresh()))
        row = next(r for r in self.items() if r['key'] == key)
        self.assertEqual(row['body'], DIGEST_SUMMARY)
        self.assertEqual((row['content_rev'], row['body_zh']), (0, '简报摘要译文'))

    def test_a_tweet_is_replaced_because_its_url_points_at_itself(self):
        """推文那条短残片:不进补抓队列,但照旧换得成 —— 它的链接就是它自己。

        **这一条是上面那条的分叉边**:只按「进不进补抓队列」判的话它会被连带拒掉,
        而这是那几百条残片唯一的人工出路。`content_kind` 不许动(换了正文还是推文),
        译文整组作废(旧译文译的是那段残片)。
        """
        key = self.social_item()
        seen = grab.known(TWEET)
        self.assertEqual(seen['kind'], 'social')     # 查重按 url,命中的不是手动收录
        self.assertTrue(seen['content_ok'])          # 质量那道判据对它恒真
        self.assertTrue(grab.replace(seen, {**self.fresh(), 'url': TWEET,
                                            'title': '某某：这次取回了全文',
                                            'content_kind': 'post'}))
        row = next(r for r in self.items() if r['key'] == key)
        self.assertIn('这是一段确认得了的正文', row['body'])
        self.assertEqual(row['content_kind'], 'post')
        self.assertEqual((row['content_rev'], row['body_zh']), (1, ''))

    def test_a_row_whose_body_is_its_links_page_is_still_replaced(self):
        """反过来钉一边:手动收录那一类的正文**就是**外链那一页,照旧换得成。

        不钉这一边的话,「全都拦住」也能让上面那条绿 —— 而那等于把错存唯一的出路
        (`put_items` 是 DO NOTHING)整条打死。
        """
        grab.save({'url': PAGE, 'title': 'Membership required', 'body': SOFT_WALL,
                   'author': 'Example', 'published': '', 'content_kind': 'content'})
        seen = grab.known(PAGE)
        self.assertEqual(seen['kind'], grab.KIND)
        self.assertTrue(grab.replace(seen, self.fresh()))
        self.assertIn('这是一段确认得了的正文', self.items()[0]['body'])


if __name__ == "__main__":
    unittest.main()
