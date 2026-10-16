# -*- coding: utf-8 -*-
"""**内容版本身份**:异步交付的结果只许写回它针对的那一版。

这一组钉的是「两件事同时在改同一行」那一类。手动收录那颗「重新抓一次」跑在
**HTTP 请求线程**里、**不持这一轮同步的维护锁**(`grab.replace`),而一轮同步里
的正文补抓、提取待办确认都是「先取样 → 等网络/等模型 → 再写回」。原先那几处
写回只按条目身份(`WHERE key=?`),于是迟到的结果会盖掉后来那一版 ——
**而且盖完之后两组 hash 各自自洽,面板照旧说「已经在知识库里了」**。

判据一律先断言后果:库里那一行是哪一版、盘上那一篇是什么标题、下一轮还补不补
得回来、重试额度扣没扣。交错用**定点注入**(在那一步真正发生的位置回调一次
`replace`)而不是 sleep 撞概率。

硬约束:替换与导出确认见 `docs/decisions/link-grab.md` 第一节,补抓写回与提取
待办见 `fulltext.md`。
"""
from __future__ import annotations

from unittest.mock import patch

from kb import pipeline
from kb.feeds import (fulltext, grab, grab_store, registry as reg, sync,
                      translate)
from kb.feeds.fetching import FeedError
from tests.grab_fixtures import BODY, GrabCase, PAGE

#: 一份「迟到的补抓」抓回来的正文:够长、带个一眼认得出的标记。
LATE = 'OLDFULLTEXT 这是补抓那一轮抓回来的旧正文，它说了不少话。' * 10
#: 手动重抓换上去的那一份(用夹具里那段确认得了的正文,再带个标记)。
FRESH = {'url': PAGE, 'title': '真正的文章', 'body': 'FRESHBODY ' + BODY}


#: 一次**只改标题**的手动重抓(正文、作者、发布时间一个字没动)——
#: `grab_store.replace` 的版本身份是整份导出负载,所以它照样换了 `content_hash`。
def renamed(seen) -> dict:
    return {'url': PAGE, 'title': '用户改过的标题', 'body': seen['body'],
            'author': seen['author'], 'published': seen['published']}


class VersionIdentityTests(GrabCase):
    """`replace` 的版本身份要覆盖**整份导出负载**,不只正文。

    导出负载 = vault 里那一篇的 frontmatter 和正文(`sync._export` 的 meta):
    标题、正文、作者、发布时间。少算一列不是「晚一点更新」:`content_hash` 不变
    → `exported_hash` 仍然相等 → 导出一条都选不中,文件和索引**永远**停在旧那
    一版,而 `known` 按两个 hash 相等报 done。
    """

    def _stored(self, **over):
        entry = {'url': PAGE, 'title': 'ALPHA', 'body': BODY, 'author': '某记者',
                 'published': '2026-10-15 10:00', 'content_kind': 'content'}
        entry.update(over)
        grab.save(entry)
        sync.export(grab.KIND)                 # 走完导出:两个 hash 相等
        self.assertEqual(self.pending_export(), 0)
        return grab.known(PAGE)

    def test_a_title_only_change_still_owes_an_export(self):
        """只改标题:**版本要跟着变、导出欠账要成立、盘上那一篇要换过来。**

        第 4 轮审查顺序执行就复现了:列表里已经是新标题,`pending_export=0`、
        `state.done=true`、面板说「全文检索里搜得到了」,而真实 FTS 搜新标题
        0 条、搜旧标题 1 条。
        """
        self._stored()                              # ALPHA
        # **要的是第二次重抓。** 第一次 `replace` 必然改变 `content_hash` ——
        # 它把这一行从 `put_items` 那份口径(整份 entry)换到本条路自己的口径上,
        # 所以「少算一列」在第一次上显不出来(审查实测的序列也是
        # ALPHA → BETA → 只改标题的 GAMMA)。
        body = BODY + '这一版改过一次正文。'
        self.assertTrue(grab.replace(grab.known(PAGE),
                                     {'url': PAGE, 'title': 'BETA', 'body': body}))
        sync.export(grab.KIND)
        self.assertEqual(self.pending_export(), 0)
        before = self.items()[0]['content_hash']
        self.assertTrue(grab.replace(grab.known(PAGE),
                                     {'url': PAGE, 'title': 'GAMMA', 'body': body}))
        row = self.items()[0]
        self.assertEqual(row['title'], 'GAMMA')
        self.assertNotEqual(row['content_hash'], before)
        # **后果断言**:下一轮 export 真的会选中它,而且它不再自称走完了。
        self.assertEqual(self.pending_export(), 1)
        fresh_seen = grab.known(PAGE)
        self.assertFalse(fresh_seen['exported'])
        self.assertFalse(fresh_seen['done'])
        self.assertEqual(grab.owing(fresh_seen), '还没写成 vault 里那一篇')
        sync.export(grab.KIND)
        text = next((self.root / 'vault' / 'source' / grab.KIND).glob('*.md')).read_text()
        self.assertIn('GAMMA', text)
        self.assertNotIn('BETA', text)

    def test_author_or_date_alone_is_not_the_same_item(self):
        """作者、发布时间单独变也算变了 —— 它们都写进 frontmatter。

        早退判据只比标题和正文的话,这两种更新会被说成「和它一样」:`replace`
        回 False,端点那一句于是是「这次重抓到的内容和它一样」,而用户看到的
        作者确实是错的。
        """
        self._stored()
        for field, value in (('author', '另一个记者'), ('published', '2026-10-16 08:00')):
            with self.subTest(field=field):
                seen = grab.known(PAGE)
                before = self.items()[0]['content_hash']
                entry = {'url': PAGE, 'title': seen['title'], 'body': seen['body'],
                         field: value}
                self.assertTrue(grab.replace(seen, entry))
                row = self.items()[0]
                self.assertEqual(row[field], value)
                self.assertNotEqual(row['content_hash'], before)
                self.assertEqual(self.pending_export(), 1)
                sync.export(grab.KIND)         # 收拾干净,下一档从相等开始

    def test_an_identical_refetch_still_changes_nothing(self):
        """**反过来钉一边**:四列一模一样时照旧早退,一个字节都不许动。

        不然每次重抓都会推一轮导出、索引和提取 —— 而内容压根没变。
        """
        seen = self._stored()
        before = dict(self.items()[0])
        self.assertFalse(grab.replace(seen, {'url': PAGE, 'title': seen['title'],
                                             'body': seen['body'],
                                             'author': seen['author'],
                                             'published': seen['published']}))
        self.assertEqual(dict(self.items()[0]), before)
        self.assertEqual(self.pending_export(), 0)

    def test_the_three_hash_recipes_are_not_compared_with_each_other(self):
        """三份 hash 口径算的是不同的东西,**不许拿它们相等当「没变」**。

        `put_items` 哈希整份 entry、`fulltext.body_hash` 只哈希正文、
        `grab_store.item_version` 哈希导出负载 —— 同一条内容在三处算出来的值
        互不相等。CAS 比的是库里那一列的**原值**,不是重算一遍再比(上一轮
        就在这里栽过:`digest == seen['content_hash']` 在真实数据上永不成立)。
        """
        seen = self._stored()
        payload = {'title': seen['title'], 'body': seen['body'],
                   'author': seen['author'], 'published': seen['published']}
        version = grab_store.item_version(payload)
        self.assertNotEqual(version, fulltext.body_hash(seen['body']))
        self.assertNotEqual(version, seen['content_hash'])      # put_items 那一份
        # 同一份负载算两次要一样(它是身份,不是时间戳)。
        self.assertEqual(version, grab_store.item_version(dict(payload)))


def queued_rss(case) -> dict:
    """一条开了补抓的 RSS 条目(`content_kind='summary'`、`fulltext_at` 空)。"""
    source = reg.save_subscription('rss', {'name': 'E', 'url': 'https://e.org/',
                                           'feed_url': 'https://e.org/feed',
                                           'fulltext': True})
    reg.put_items(source, [{'url': PAGE, 'title': 'A', 'body': '订阅源给的那段摘要。' * 4,
                            'published': '', 'content_kind': 'summary'}])
    case.assertEqual(fulltext.pending('rss'), 1)
    return grab.known(PAGE)


class LateFulltextTests(GrabCase):
    """一轮同步里的正文补抓**迟到**了:它取样的那一版已经被换掉。"""

    def _queued(self):
        return queued_rss(self)

    def test_a_late_fulltext_save_cannot_overwrite_a_fresh_replace(self):
        """**迟到的补抓不许盖掉刚换好的正文。**

        第 4 轮审查实测的形态:用户在同步进行中点「重新抓一次」,新正文已经进
        feeds 了,随后那一轮迟到的 `_save` 又把旧内容写回去、导出和索引跟着落
        盘,最后面板说「这条已经在知识库里了」。
        """
        self._queued()

        def fetch_one(url):
            # 网络等待期间,手动收录那颗按钮把这一行换掉了(另一个线程、另一条
            # 连接、不持这一轮的维护锁)。
            self.assertTrue(grab.replace(grab.known(PAGE), FRESH))
            return LATE, ''

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one) as got:
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(got.call_count, 1)
        # **统计按实际写入结果报,跳过的如实列出**(元规则)。
        self.assertEqual((summary['done'], summary['stale']), (0, 1))
        self.assertIn('作废', fulltext.summarize(summary))
        # **后果断言**:库里那一行还是用户换上去的那一版。
        row = self.items()[0]
        self.assertIn('FRESHBODY', row['body'])
        self.assertNotIn('OLDFULLTEXT', row['body'])
        self.assertEqual(row['title'], '真正的文章')

    def test_a_late_failure_is_not_charged_to_the_new_body(self):
        """迟到的**失败**也不许记在新正文头上,更不许扣它的重试额度。

        记了的后果:一篇好内容带着一句「抓取失败」的红字,而那句话说的是
        **另一版**内容去原文链接没抓到;额度扣满之后它还会被挡在补抓队列外。
        """
        self._queued()

        def fetch_one(url):
            self.assertTrue(grab.replace(grab.known(PAGE), FRESH))
            raise FeedError('502 Bad Gateway')

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['failed'], summary['stale']), (0, 1))
        self.assertEqual(summary['errors'], [])
        row = self.items()[0]
        self.assertEqual(row['fulltext_tries'], 0)
        self.assertEqual(row['fulltext_error'], '')
        self.assertIn('FRESHBODY', row['body'])

    def test_a_late_fulltext_save_cannot_overwrite_a_title_only_replace(self):
        """同一类的第二种形态:**期间那次重抓只改了标题,正文一个字没动。**

        正文相等答不出「这一行还是我取样那一版吗」:`replace` 的版本身份是整份
        导出负载,只改标题也换了 `content_hash`,而按 `AND body=?` 判的话这一版
        被当成「还是旧那一版」、迟到的补抓照旧获得覆盖权。实测后果:用户重抓到
        的标题还在、正文却换成补抓那一轮抓回来的另一份内容,md 和全文检索跟着
        落了旧正文,再贴同一链接只说「已经在知识库里了」。
        """
        seen = self._queued()

        def fetch_one(url):
            self.assertTrue(grab.replace(grab.known(PAGE), renamed(seen)))
            # **这一次确实只改了标题**:正文相等,所以 `AND body=?` 判不出来。
            self.assertEqual(grab.known(PAGE)['body'], seen['body'])
            return LATE, ''

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one) as got:
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(got.call_count, 1)
        self.assertEqual((summary['done'], summary['stale']), (0, 1))
        self.assertIn('作废', fulltext.summarize(summary))
        row = self.items()[0]
        self.assertNotIn('OLDFULLTEXT', row['body'])
        self.assertEqual(row['title'], '用户改过的标题')
        # **下一轮还补得回来**:那一条留在补抓队列里,没人给它记成功时间。
        self.assertEqual(row['fulltext_at'], '')
        self.assertEqual(fulltext.pending('rss'), 1)

    def test_a_late_failure_after_a_title_only_replace_is_not_charged(self):
        """失败那一侧同理:这次失败说的是改标题之前那一版去原文链接没抓到。"""
        seen = self._queued()

        def fetch_one(url):
            self.assertTrue(grab.replace(grab.known(PAGE), renamed(seen)))
            raise FeedError('502 Bad Gateway')

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['failed'], summary['stale']), (0, 1))
        self.assertEqual(summary['errors'], [])
        row = self.items()[0]
        self.assertEqual((row['fulltext_tries'], row['fulltext_error']), (0, ''))
        self.assertEqual(row['title'], '用户改过的标题')

    def test_a_late_empty_result_after_a_title_only_replace_is_not_charged(self):
        """第三个交付点(页面抓到了、认不出正文)走的是同一份 `_fail`,也不许
        扣新那一版的额度 —— 扣满三次它就离开补抓队列、按壳入库。"""
        seen = self._queued()

        def fetch_one(url):
            self.assertTrue(grab.replace(grab.known(PAGE), renamed(seen)))
            return '', '页面上那一块判成了样板区'

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['empty'], summary['stale']), (0, 1))
        self.assertEqual(summary['failed_keys'], [])
        row = self.items()[0]
        self.assertEqual((row['fulltext_tries'], row['fulltext_error']), (0, ''))
        self.assertEqual(fulltext.pending('rss'), 1)

    def test_a_fulltext_save_that_nobody_raced_still_lands(self):
        """**反过来钉一边**:没人插手时补抓照旧落得进去、照旧算一条成功。"""
        self._queued()
        with patch.object(fulltext, 'fetch_one', return_value=(LATE, '')):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['done'], summary['stale']), (1, 0))
        row = self.items()[0]
        self.assertIn('OLDFULLTEXT', row['body'])
        self.assertEqual(row['content_kind'], 'content')
        self.assertNotEqual(row['fulltext_at'], '')
    def test_a_fulltext_save_still_lands_when_only_the_translation_moved(self):
        """**反过来钉另一边**:期间动的不是内容,这次补抓照旧落得进去。

        `content_hash` 只认内容 —— `translate._save` 写回译文、
        `grab_store.reclaim_export` 落实导出欠账都不动它。判据换成「这一行变过
        没有」那种更宽的说法,会把一笔真花了钱的抓取白白作废。
        """
        seen = self._queued()

        def fetch_one(url):
            self.assertTrue(translate._save(seen['key'], '译过的标题', '译过的正文',
                                            (seen['title'], seen['body'])))
            return LATE, ''

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['done'], summary['stale']), (1, 0))
        row = self.items()[0]
        self.assertIn('OLDFULLTEXT', row['body'])
        # 旧译文译的是那段摘要,正文一换它就作废了(见 `fulltext._save`)。
        self.assertEqual((row['title_zh'], row['body_zh'], row['translated_at']), ('', '', ''))


def round_trip(case, base: dict) -> None:
    """把标题改成 BETA、再改回 ALPHA —— 两次都是真的重抓、两次都被接受。

    末态:四列逐字回到 `base`(所以 `content_hash` 也回到原值),而代次走过两版。
    """
    case.assertTrue(grab.replace(grab.known(PAGE), {**base, 'title': 'BETA'}))
    case.assertTrue(grab.replace(grab.known(PAGE), base))


class WorkEpochTests(GrabCase):
    """内容**改过去又改回来**:指纹回到原值,而这期间两次更新都已经被接受过。

    `content_hash` 在这种交错下答不出「我取样之后这一行接受过更新吗」——
    指纹不碰撞也会自然重复,所以这不是"再哈希一列"能解决的问题。身份里那个
    **只增不复用**的代次(`content_rev`)才是挡住它的那一道;两道各挡什么收在
    `fulltext.work_version`。
    """

    def _alpha(self) -> dict:
        """一条待补抓的 RSS 条目,**指纹已经在替换那条路自己的口径上**。

        先走一次真实重抓是必需的:第一次 `replace` 必然换 hash 口径(从
        `put_items` 的整份 entry 换到 `item_version` 的导出负载,见那两处说明),
        指纹压根回不到取样那一版 —— 不先做这一步,这一组测不到「改回来」那一档。
        """
        seen = queued_rss(self)
        base = {'url': PAGE, 'title': 'ALPHA', 'body': seen['body'],
                'author': seen['author'], 'published': seen['published']}
        self.assertTrue(grab.replace(seen, base))
        self.assertEqual(fulltext.pending('rss'), 1)       # 替换不消费补抓待办
        return base

    def _round_trip(self, base: dict):
        round_trip(self, base)

    def test_a_late_fulltext_save_cannot_come_back_after_a_round_trip(self):
        """**指纹回到原值之后,迟到的补抓仍然不许交付。**

        实测形态:同步正在补抓,用户先把标题改成 BETA、又改回 ALPHA;此时
        `content_hash` 逐字回到补抓取样那一版,于是按内容相等判的那一版补抓又拿回
        交付权 —— 打开文章看到的是早先那份正文,md 和全文检索跟着落了它,再贴同一
        链接只说「已经在知识库里了」。
        """
        base = self._alpha()
        sampled = fulltext.work_version(self.items()[0])

        def fetch_one(url):
            self._round_trip(base)
            now = self.items()[0]
            # **先钉住这条用例测的就是那一档**:指纹真的回来了(不然它证明不了代次
            # 在起作用),而代次已经走过两版。
            self.assertEqual(fulltext.work_version(now)[0], sampled[0])
            self.assertGreater(fulltext.work_version(now)[1], sampled[1])
            return LATE, ''

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one) as got:
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(got.call_count, 1)
        self.assertEqual((summary['done'], summary['stale']), (0, 1))
        row = self.items()[0]
        self.assertNotIn('OLDFULLTEXT', row['body'])
        self.assertEqual(row['title'], 'ALPHA')
        self.assertEqual(row['fulltext_at'], '')
        # **反过来钉一边:这一条没有被钉死。** 下一轮补抓照旧落得进去 —— 拒的是
        # 「过期的那次工作」,不是这一条本身。
        self.assertEqual(fulltext.pending('rss'), 1)
        with patch.object(fulltext, 'fetch_one', return_value=(LATE, '')):
            again = fulltext.fetch_pending('rss')
        self.assertEqual((again['done'], again['stale']), (1, 0))
        self.assertIn('OLDFULLTEXT', self.items()[0]['body'])

    def test_a_late_failure_cannot_come_back_after_a_round_trip(self):
        """失败那一侧同理:记一笔失败、扣一次额度,说的都是已经不存在的那一版。"""
        base = self._alpha()

        def fetch_one(url):
            self._round_trip(base)
            raise FeedError('502 Bad Gateway')

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['failed'], summary['stale']), (0, 1))
        row = self.items()[0]
        self.assertEqual((row['fulltext_tries'], row['fulltext_error']), (0, ''))

    def test_a_late_empty_result_cannot_come_back_after_a_round_trip(self):
        """第三个交付点(页面抓到了、认不出正文)走的是同一份 `_fail`,也一样。"""
        base = self._alpha()

        def fetch_one(url):
            self._round_trip(base)
            return '', '页面上那一块判成了样板区'

        with patch.object(fulltext, 'fetch_one', side_effect=fetch_one):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((summary['empty'], summary['stale']), (0, 1))
        self.assertEqual(summary['failed_keys'], [])
        row = self.items()[0]
        self.assertEqual((row['fulltext_tries'], row['fulltext_error']), (0, ''))
        self.assertEqual(fulltext.pending('rss'), 1)


class ManualHandoffTests(GrabCase):
    """手动重抓**也是**「先取样 → 等网络 → 再提交」那个模式。

    `/api/grab` 的 `refetch` 先拿一份 `known()` 快照,再去原文链接抓几十秒,最后把
    结果交给 `replace`。两个页面同时在重抓同一条链接时,等着的那一个回来时库里可能
    已经换过好几版 —— 判据只认指纹的话,指纹一复原它就又拿回了交付权。
    """

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from kb.server import app
        self.client = self.enterContext(TestClient(app))
        self.runs: list = []

    def _pipeline(self):
        """打桩那一轮管道(交出 run id,口径同 `EndpointTests.pipeline_patch`)。"""
        def fake(stages, refused=None, crawl_filter=None, **kwargs):
            self.runs.append(list(stages))
            return 'run-7'
        return patch.object(pipeline, 'start_pipeline', fake)

    def _alpha(self) -> dict:
        """一条已经收好、已导出的条目,**指纹已经在替换那条路自己的口径上**。

        先走一次真实替换是必需的:第一次 `replace` 必然换 hash 口径(见
        `item_version`),不先做这一步,指纹压根回不到取样那一版。
        """
        grab.save({'url': PAGE, 'title': 'BASE', 'body': BODY, 'author': '某记者',
                   'published': '2026-10-15 10:00', 'content_kind': 'content'})
        sync.export(grab.KIND)
        base = {'url': PAGE, 'title': 'ALPHA', 'body': BODY, 'author': '某记者',
                'published': '2026-10-15 10:00'}
        self.assertTrue(grab.replace(grab.known(PAGE), base))
        sync.export(grab.KIND)
        return base

    def test_an_earlier_refetch_cannot_land_after_a_round_trip(self):
        """**等着的那个请求回来时指纹已经复原 —— 它那份更早的抓取不许被接受。**

        实测形态:一个页面在等,另一个把标题改成 BETA 又改回 ALPHA(两次都换成
        了);等第一个回来,文章变成它抓到的那份旧内容、`done` 照旧报真,再贴同一
        链接只说「已经在知识库里了」,md 和全文检索跟着落了它。
        """
        base = self._alpha()
        sampled = grab_store.work_version(self.items()[0])
        earlier = {'url': PAGE, 'title': 'EARLIER', 'body': 'OLDMANUALWORK ' + BODY,
                   'author': '某记者', 'published': '2026-10-15 10:00',
                   'content_kind': 'content'}

        def grabbing(url):
            # 这就是「这一个还在等」那段时间:另一个页面来回换了两次。
            round_trip(self, base)
            now = self.items()[0]
            # **先钉住这条用例测的就是那一档**:指纹真的复原了,代次已经走过两版。
            self.assertEqual(grab_store.work_version(now)[0], sampled[0])
            self.assertGreater(grab_store.work_version(now)[1], sampled[1])
            return earlier

        with self._pipeline(), patch.object(grab, 'grab', side_effect=grabbing) as got:
            data = self.client.post('/api/grab', json={'url': PAGE, 'refetch': True}).json()
        self.assertEqual(got.call_count, 1)
        # **如实交出去**:没换成就不许说已完成(前端据 `replaced` 落 pending)。
        self.assertIs(data['replaced'], False)
        self.assertIn('已经有人换过了', data['detail'])
        self.assertNotIn('pipeline', data)
        self.assertEqual(self.runs, [])
        # **后果断言**:库里还是后来那一版,一个字节都没被换回去。
        row = self.items()[0]
        self.assertEqual(row['title'], 'ALPHA')
        self.assertNotIn('OLDMANUALWORK', row['body'])

    def test_a_refetch_that_nobody_raced_still_lands(self):
        """**反过来钉一边**:没人插手时这条链照旧换得成、照旧起后半条链。"""
        self._alpha()
        fresh = {'url': PAGE, 'title': 'FRESH', 'body': 'NEWBODY ' + BODY,
                 'author': '某记者', 'published': '2026-10-15 10:00',
                 'content_kind': 'content'}
        with self._pipeline(), patch.object(grab, 'grab', return_value=fresh):
            data = self.client.post('/api/grab', json={'url': PAGE, 'refetch': True}).json()
        self.assertIs(data['replaced'], True)
        self.assertEqual(self.runs, [['export', 'index', 'extract']])
        row = self.items()[0]
        self.assertEqual(row['title'], 'FRESH')
        self.assertIn('NEWBODY', row['body'])

    def test_a_stale_snapshot_cannot_replace_after_a_round_trip(self):
        """同一条的单元级形态:拿着复原前那份快照提交,`replace` 必须拒。

        端点那条走的是同一个函数,这一条钉的是**判据本身** —— 快照里的代次落后,
        哪怕四列和指纹都逐字相同也不许提交。
        """
        base = self._alpha()
        stale = grab.known(PAGE)
        round_trip(self, base)
        self.assertEqual(grab.known(PAGE)['content_hash'], stale['content_hash'])
        self.assertFalse(grab.replace(stale, {**base, 'title': 'EARLIER'}))
        self.assertEqual(self.items()[0]['title'], 'ALPHA')
        # 快照重新读一次就照旧换得成 —— 拒的是过期的那份工作,不是这一条。
        self.assertTrue(grab.replace(grab.known(PAGE), {**base, 'title': 'EARLIER'}))
        self.assertEqual(self.items()[0]['title'], 'EARLIER')


class StaleExtractionTests(GrabCase):
    """提取待办的**确认**迟到了:它放下的是上一版的标记。"""

    def _with_debt(self):
        """一条已经落盘、而且欠着一笔「旧摘要要作废」的条目。"""
        grab.save({'url': PAGE, 'title': 'A', 'body': BODY, 'author': 'x',
                   'published': '', 'content_kind': 'content'})
        sync.export(grab.KIND)
        with reg.db() as c:
            c.execute('UPDATE feed_items SET extract_stale=1')
        return grab.known(PAGE)

    def test_a_late_retire_cannot_clear_the_new_version_debt(self):
        """**迟到的 `retire_stale` 不许把新版本那笔待办清掉。**

        清掉的后果实测过(第 4 轮审查):下一轮导出不再摘旧的 `topic/`、
        `entity/`,索引按残留标签走 `parse_pretagged`、`extracted_at` 跟着非空
        —— **新正文永远不进重新提取队列,旧主题永久留在那一篇上**,再同步多少
        轮都不变。
        """
        self._with_debt()

        def drop(paths):
            # 清知识库里那份旧提取的同时,用户把这一行换成了新内容。
            self.assertTrue(grab.replace(grab.known(PAGE), FRESH))

        with patch.object(fulltext, '_drop_extraction', side_effect=drop) as dropped:
            self.assertEqual(fulltext.retire_stale(grab.KIND), 0)
        self.assertEqual(dropped.call_count, 1)
        # 新版本那笔待办还在 —— 等它自己落盘之后再由下一轮导出来收。
        self.assertEqual(self.items()[0]['extract_stale'], 1)
        self.assertIn('FRESHBODY', self.items()[0]['body'])

    def test_a_retire_that_nobody_raced_still_puts_the_marker_down(self):
        """**反过来钉一边**:没人插手时照旧收掉,而且按实际清掉的条数报。"""
        self._with_debt()
        with patch.object(fulltext, '_drop_extraction') as dropped:
            self.assertEqual(fulltext.retire_stale(grab.KIND), 1)
        self.assertEqual(dropped.call_count, 1)
        self.assertEqual(self.items()[0]['extract_stale'], 0)
        # 空集合那一档一次 kb.db 都不开(常态走的就是这条)。
        with patch.object(fulltext, '_drop_extraction') as again:
            self.assertEqual(fulltext.retire_stale(grab.KIND), 0)
        again.assert_not_called()

class LateLanguageLabelTests(GrabCase):
    """语种补标也是「先取样、再算、后写回」—— 它写的那一列 `replace` 自己也写。"""

    #: 旧那一版刻意是**英文**:两版算出来的语种必须不同,不然「盖上去了」和
    #: 「没盖上去」在库里长得一模一样 —— 这条用例第一次写成中英都算中文,注入
    #: 之后红 0 条(元规则:0 条先怀疑用例)。
    ENGLISH = 'This is an English article body and it says plenty of things. ' * 10

    def _unlabelled(self):
        grab.save({'url': PAGE, 'title': 'An English title', 'body': self.ENGLISH,
                   'author': 'x', 'published': '', 'content_kind': 'content'})
        with reg.db() as c:                     # 老库里那批:语种还没判过
            c.execute("UPDATE feed_items SET lang=''")

    def test_a_late_language_label_is_not_written_onto_a_new_body(self):
        """**迟到的语种标注不许盖到新正文上。**

        盖上去的是**按旧正文算出来的**那个值:一篇中文新正文被标成英文,于是它
        进翻译队列、白买一次翻译(而 `replace` 刚刚已经按新正文标好了)。
        """
        from kb.feeds import translate

        self._unlabelled()
        real, hits = reg.text_language, []

        def language(title, body):
            # 只在算**旧那一版**时插一次;`replace` 自己也会调到这里,靠标志位
            # 防递归。
            if not hits:
                hits.append(1)
                self.assertTrue(grab.replace(grab.known(PAGE), FRESH))
            return real(title, body)

        with patch.object(reg, 'text_language', side_effect=language):
            translate._label_unknown(grab.KIND)
        row = self.items()[0]
        self.assertIn('FRESHBODY', row['body'])
        # 这一列由 `replace` 按新正文(中文)写过了 —— 迟到那一笔按旧英文算出
        # 'en',一个字都没覆盖上去。
        self.assertEqual(row['lang'], 'zh')
        self.assertEqual(real(row['title'], row['body']), 'zh')

    def test_a_language_label_that_nobody_raced_still_lands(self):
        """**反过来钉一边**:没人插手时照旧标上(不然老库那批永远标不了)。"""
        from kb.feeds import translate

        self._unlabelled()
        self.assertEqual(self.items()[0]['lang'], '')
        translate._label_unknown(grab.KIND)
        self.assertEqual(self.items()[0]['lang'], 'en')
