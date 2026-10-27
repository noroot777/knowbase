# -*- coding: utf-8 -*-
"""**搁置要拦住交付那一头** —— 这一份只验「人点完之后,知识库里真的没有多出东西」。

单独一个文件是因为这一道的**方向和别的三道是反的**,而那正是上一版漏掉它的原因
(审查第 1 轮 F01)。补抓、翻译、待办三族问的是「这一条要不要排进来」,搁置作为
一道**正向**筛选 AND 上去就行;而交付层问的是「这一条可以写进 vault 了吗」,它的
判据是那两个队列的**反集**(`sync._export` 的 `AND NOT (fulltext.queued_sql())` /
`AND NOT (translate.queued_sql())`)—— 于是同一道正向筛选在这头**取反成放行**:
人点完「不再收录」,这一条当场「不在补抓队列里」「不在翻译队列里」,导出照旧把它
写成 md。**离开某个队列不等于获准进入下一个环节。**

三组:
① **真跑一次 `sync.export`**,看 vault 里有没有多出文件、`exported_hash` 有没有
   从空变成非空。不打桩、不读 SQL 字符串 —— 上一版那三份回归全绿的时候,这件事
   正在真实发生。
② **在途的写回**:人点下按钮的那一刻可能正有一轮补抓在路上。三种写回(成功、
   异常、证据不足)都要认搁置,**而译文写回刻意不认** —— 那一笔钱已经付了。
   两边都钉住,不然后来的人只会看见其中一半、顺手"统一"掉。
③ **取数之后、文件落盘之前**那一段(审查第 2 轮 R1)。① 只验得了「取数时已经
   搁置的选不出来」—— 而那之后还隔着渲染、写临时文件、`os.replace` 和回执,人在
   那期间点下去照旧会多出一篇 md。这一组在取数之后的位置真调一次 `shelf.shelve`。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kb.feeds import fulltext, registry as reg, sync, translate
from kb.feeds.paths import export_path
from kb.intake_diag import shelf
from tests.intake_diag_case import DiagCase, key_of

SNOOZED = 'https://news.example.org/snoozed'
EXCLUDED = 'https://news.example.org/excluded'
INGEST = 'https://news.example.org/ingest'
HELD = 'https://news.example.org/held'


class ShelvedItemsAreNotDeliveredTests(DiagCase):
    """①:真写一次 vault。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def unexported(self) -> set[str]:
        with reg.db() as c:
            return {r[0] for r in c.execute(
                "SELECT key FROM feed_items WHERE kind='rss'"
                ' AND exported_hash<>content_hash')}

    def files(self) -> set[str]:
        root = sync.VAULT_ROOT / 'source' / 'rss'
        return {p.name for p in root.glob('*.md')} if root.exists() else set()

    def test_a_real_export_skips_the_shelved_items_and_still_ships_the_others(self):
        shelved = {key_of('rss', SNOOZED), key_of('rss', EXCLUDED)}
        before = self.unexported()
        # 这两条此刻**两个队列都不在**(搁置把它们从补抓队列里摘掉了),所以
        # 上一版的导出闸门对它们是放行的 —— 下面这一句就是那个前提。
        cond, args = fulltext.queued_sql()
        for key in shelved:
            with self.subTest(key=key):
                self.assertIn(key, before)
                self.assertEqual(0, self.count(f'key=? AND ({cond})', [key, *args]))
        sync.export('rss')
        after = self.unexported()
        # 搁置的那两条:还没导出、vault 里没有它们的文件、`path` 那一格还空着。
        self.assertEqual(shelved, shelved & after)
        for key in shelved:
            with self.subTest(key=key):
                self.assertNotIn(export_path('rss', key).name, self.files())
                row = self.row(key)
                self.assertEqual('', row['exported_hash'])
                self.assertEqual('', row['path'])
        # **反过来钉一边**:同一轮里没被搁置的那几条照旧写进去了 —— 不然这道闸门
        # 「全拦住」也能让上面那几条断言变绿。
        shipped = before - after
        self.assertTrue(shipped)
        self.assertEqual(set(), shipped & shelved)
        self.assertIn(key_of('rss', INGEST), shipped)
        self.assertIn(export_path('rss', key_of('rss', INGEST)).name, self.files())

    def test_releasing_them_puts_them_back_on_the_delivery_path(self):
        """恢复不是「再也不要」:这一条照旧要走完它该走的流程。

        恢复之后那两条壳回到**补抓队列**(所以这一轮还不该导出),补抓额度用尽
        之后才按壳落盘 —— 口径同 `fulltext.queued_sql` 那个降级出口。
        """
        key = key_of('rss', SNOOZED)
        ids = self.shelve([key], 'rss', shelf.SNOOZE)
        self.assertEqual(1, shelf.release([key], 'rss', ids))
        cond, args = fulltext.queued_sql()
        self.assertEqual(1, self.count(f'key=? AND ({cond})', [key, *args]))
        sync.export('rss')
        self.assertEqual('', self.row(key)['exported_hash'])
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fulltext_tries=? WHERE key=?',
                      (fulltext.MAX_TRIES, key))
        sync.export('rss')
        self.assertNotEqual('', self.row(key)['exported_hash'])
        self.assertIn(export_path('rss', key).name, self.files())


class LateWriteBacksTests(DiagCase):
    """②:人点下按钮的那一刻,路上那一轮的结果怎么处理。"""

    def setUp(self):
        super().setUp()
        self.ids = self.seed()
        self.key = key_of('rss', HELD)
        self.seen = fulltext.work_version(self.row(self.key))

    def test_a_body_that_lands_after_the_shelving_is_discarded(self):
        """成功那一支:正文作废,**一列都不许动**。

        写进去的后果不是「多一篇 md」(交付那道闸门挡着),是搁置那颗按钮承诺的
        「正文、文件和已经入库的内容一个字都不动」当场不成立 —— 而且
        `fulltext_at` 一置,自动补抓此后再也不碰这一条:人恢复之后看到的是一份
        他没同意过的正文,面板上写着「正文已补齐」。
        """
        before = self.row(self.key)
        self.shelve([self.key], 'rss', shelf.SNOOZE)
        self.assertFalse(fulltext._save(self.key, '补抓回来的真正文。' * 40, self.seen))
        self.assertEqual(before, self.row(self.key))

    def test_a_failure_that_lands_after_the_shelving_does_not_spend_a_try(self):
        """异常那一支:错误不记、**额度不扣**。

        扣掉的话人恢复之后这一条已经离重试耗尽近了一步,而那一次失败发生在他说
        「先别动」之后。
        """
        before = self.row(self.key)
        self.shelve([self.key], 'rss', shelf.SNOOZE)
        self.assertFalse(fulltext._fail(self.key, RuntimeError('对方 403'), self.seen))
        self.assertEqual(before, self.row(self.key))

    def test_an_unconfirmed_verdict_that_lands_after_the_shelving_is_discarded(self):
        """证据不足那一支:`fulltext_hold` 不许被改写。

        这一条本来就挂着一句「等人看一眼」,再写一次等于把人刚做的决定(暂缓)
        和机器的结论混在一起 —— 而两者是两回事。
        """
        before = self.row(self.key)
        self.shelve([self.key], 'rss', shelf.SNOOZE)
        self.assertFalse(fulltext._fail(self.key, '看不出哪一段是正文', self.seen,
                                        '页面上没有识别出正文；已挂起等人看一眼',
                                        count=False))
        self.assertEqual(before, self.row(self.key))

    def test_the_same_three_write_backs_go_through_when_it_is_not_shelved(self):
        """**反过来钉一边**:没搁置的时候三种写回照旧成立 —— 不然上面三条用例在
        「写回整个坏掉」的实现上一样是绿的。
        """
        self.assertTrue(fulltext._fail(self.key, '看不出哪一段是正文', self.seen,
                                       '页面上没有识别出正文；已挂起等人看一眼',
                                       count=False))
        seen = fulltext.work_version(self.row(self.key))
        self.assertTrue(fulltext._fail(self.key, RuntimeError('对方 403'), seen))
        seen = fulltext.work_version(self.row(self.key))
        self.assertTrue(fulltext._save(self.key, '补抓回来的真正文。' * 40, seen))
        self.assertNotEqual('', self.row(self.key)['fulltext_at'])

    def test_a_paid_translation_is_still_accepted_while_shelved(self):
        """**译文写回刻意不认搁置**,这里把那个取舍钉住。

        拒收等于把一笔**已经花掉**的模型调用扔掉,而恢复之后还要再付一次;而它
        写的那几列不参与交付 —— 交付那道闸门已经把整条挡住了(上面第一组验过)。
        两边代价不同,所以口径不同;改这一条之前先读 `fulltext._save` 那段说明。
        """
        key = key_of('rss', 'https://solo.example.org/translate-queued')
        row = self.row(key)
        self.shelve([key], 'rss', shelf.SNOOZE)
        self.assertTrue(translate._save(key, '译好的标题', '译好的正文',
                                        (row['title'], row['body'])))
        self.assertNotEqual('', self.row(key)['translated_at'])


class DeliveryCommitTests(DiagCase):
    """③:**取数之后、文件落盘之前**那一段。

    ① 验的是「取数时已经搁置的选不出来」,而那之后还隔着渲染、写临时文件、
    `os.replace` 和回执 —— 人点「不再收录」拿到成功之后,这一篇照旧会出现在 vault
    里、回执照旧落下(审查第 2 轮 R1 实测:在 `_kept_meta` 这个取数之后才跑到的
    位置真调一次 `shelf.shelve(...,'exclude')`,拿到 `shelved=1`、条目当场分到
    「已搁置」那一档,而 export 返回之后文件在、`exported_hash==content_hash`)。
    **「取数时获准」不是「一直获准」**,所以判据要待在确认那条写语句自己的 WHERE 里。
    """

    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def export_with(self, hook):
        """真跑一次导出,在**取数之后**那个位置插一下手(`_kept_meta` 每篇各调一次)。"""
        original = sync._kept_meta
        fired = []

        def interleave(path):
            if not fired:
                fired.append(hook())
            return original(path)

        with patch.object(sync, '_kept_meta', interleave):
            sync.export('rss')
        return fired[0] if fired else None

    def file_of(self, key: str) -> Path:
        return sync.VAULT_ROOT / export_path('rss', key)

    def test_shelving_between_the_select_and_the_file_still_keeps_it_out(self):
        key = key_of('rss', INGEST)
        self.assertEqual('', self.row(key)['exported_hash'])
        done = self.export_with(lambda: shelf.shelve([key], 'rss', shelf.EXCLUDE))
        # 搁置那一下确实成功了(人看到的就是这个),而这一篇没有进知识库:
        # 文件不在、回执没落、`path` 那一格还空着 —— 三样都要,少一样就是
        # 「屏幕上没有、库里有」或者反过来。
        self.assertEqual(1, done['shelved'])
        self.assertFalse(self.file_of(key).exists())
        row = self.row(key)
        self.assertEqual('', row['exported_hash'])
        self.assertEqual('', row['path'])
        # 它照旧欠着账:恢复之后下一轮导出自然选中它(不是静默丢掉)。
        self.assertNotEqual(row['exported_hash'], row['content_hash'])

    def test_the_same_round_still_delivers_everything_else(self):
        """**反过来钉一边。** 不然「交付整个坏掉」也能让上面那条变绿。"""
        key = key_of('rss', INGEST)
        # 被搁置的那一条要**本来就在这一轮里**(样例里 `held` 那条欠着交付、这一轮
        # 会被交付),否则这条反面断言只是「导出还能跑」,和交错压根没关系。
        other = key_of('rss', 'https://news.example.org/held')
        self.export_with(lambda: shelf.shelve([other], 'rss', shelf.EXCLUDE))
        self.assertFalse(self.file_of(other).exists())
        self.assertTrue(self.file_of(key).exists())
        row = self.row(key)
        self.assertEqual(row['content_hash'], row['exported_hash'])
        self.assertEqual(str(export_path('rss', key)), row['path'])

    def test_nothing_is_taken_back_when_nobody_shelves_anything(self):
        """**再反过来钉一边**:没人插手的那一轮,它认下的每一条都真落了盘。

        判据跟着**这一轮自己报的那份清单**走,不按 url 写死:欠着交付的和「这一轮
        会交付」不是一回事(搁置的、正等着补正文的都欠着账却本来就不该落盘),写死
        一个名字只会钉住样例今天长什么样。
        """
        delivered = sync.export('rss')
        self.assertGreater(len(delivered), 1)
        self.assertIn(f"rss:{key_of('rss', INGEST)}",
                      {one.content_id for one in delivered})
        for one in delivered:
            with self.subTest(path=one.path):
                self.assertTrue((sync.VAULT_ROOT / one.path).exists())
                row = self.row(one.content_id.split(':', 1)[1])
                # 回执三样齐全:文件在、哈希对上、交付时刻落下(后者是「已完成」
                # 那一档的判据,见 `kb.intake_diag.lanes`)。
                self.assertEqual(row['content_hash'], row['exported_hash'])
                self.assertEqual(str(one.path), row['path'])
                self.assertNotEqual('', row['exported_at'])

    def test_a_body_swapped_mid_export_keeps_its_file_instead_of_being_taken_back(self):
        """**另一头的反向钉**:确认被拒的另一个理由是「正文在这期间被换过」,
        那一档的善后方式**相反** —— 文件照旧留在 vault 里。

        那一条是别处早就钉下的不变量(`tests/test_grab_store.py` 的
        `test_a_replace_during_export_cannot_be_confirmed_by_the_old_file`、
        `daily.md` 的「已经落进 vault 的文件不许因为后面那次确认失败而消失」)。
        搁置这道闸和它共用同一条 UPDATE,所以**收回那一下必须先归因**:不归因的
        写法会把正文换过的那一篇也删掉,而那一篇是既成事实、回执是它唯一的机会。
        """
        key = key_of('rss', INGEST)

        def swap():
            with reg.db() as c:
                c.execute("UPDATE feed_items SET body=?,content_hash='deadbeef' "
                          'WHERE key=?', ('后来换上去的正文。' * 40, key))

        self.export_with(swap)
        row = self.row(key)
        self.assertTrue(self.file_of(key).exists())      # 留着,不许收回
        self.assertEqual('', row['exported_hash'])       # 确认照旧被拒
        self.assertNotEqual(row['exported_hash'], row['content_hash'])  # 欠账还在
        # 它压根没被搁置过(归因那一句问的就是这个) —— 下一轮导出按新正文重渲染。
        self.assertEqual(0, self.count(shelf.live_sql() + ' AND key=?', [key]))

    def test_an_already_delivered_article_keeps_its_file_and_says_so(self):
        """**这一档拦不住,所以把它钉成「知情的末态」而不是留着让人以为拦住了。**

        一篇**已经交付过**的文章在重导出中途被搁置时:回执拒掉了(欠账还在),
        但那个路径上原先就有文件、刚刚被这一份覆盖 —— 旧内容我们手里没有,恢复
        不了。它本来就已经在知识库里,所以这不是「搁置之后新进了一篇」。
        **不许为了这一档把 `os.replace` 挪到确认之后**:那会换来一个更坏的方向
        —— 确认落了、进程在替换前被杀掉时,库里说「已交付」而 vault 里没有这一篇,
        而且再也没有任何一轮会选中它(元规则:漏报是静默丢数据)。
        """
        key = key_of('rss', INGEST)
        sync.export('rss')
        self.assertTrue(self.file_of(key).exists())
        with reg.db() as c:          # 让它重新欠账(正文换过那种形态)
            c.execute("UPDATE feed_items SET body=?,content_hash='deadbeef' WHERE key=?",
                      ('改过的正文。' * 40, key))
        self.export_with(lambda: shelf.shelve([key], 'rss', shelf.EXCLUDE))
        row = self.row(key)
        self.assertTrue(self.file_of(key).exists())       # 文件还在(收不回去)
        self.assertNotEqual(row['content_hash'], row['exported_hash'])  # 回执没落
