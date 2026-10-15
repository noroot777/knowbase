"""人工从日报队列里丢弃一批(`#drop`),和"停掉一个来源"那两步配合。

两件事放一起是因为它们回答同一个请求:「这几个来源的内容别再进日报了」。
上游不再产出的那个来源(`dr`)连收都停掉,剩下的只是把队列里现存的那些盖章。

断言一律落在后果上:下一份日报还捡不捡得回来、库里那些文档还在不在 ——
只验"章写了没有"的话,一个把章写进去又被下一轮重算清掉的实现也能全绿。

摆盘(`_Base` / `T` / `add_doc`)直接用 tests/test_daily_carried.py 的,保证
和账本那两组测的是同一个世界。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb import config, daily, daily_carry, scanner, store
from tests.test_daily_carried import T, _Base, add_doc


class DroppedEntriesTests(_Base):
    """`#drop` 的语义:从队列里**永久**退出,而且算不回来、不许被重算清掉。"""

    def setUp(self):
        super().setUp()
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))

    def test_a_dropped_entry_is_never_collected_again(self):
        """丢弃过的条目两个桶都选不到 —— 这是这颗按钮唯一的后果。

        反过来钉一边:同一个窗口里没被丢弃的那条照旧选得到,否则一个"把取材
        整个关掉"的实现也能全绿。
        """
        add_doc(self.conn, 'source/social/x.md', source='social', title='推文')
        add_doc(self.conn, 'source/rss/keep.md', title='留下的')
        self.assertEqual(1, daily_carry.mark_dropped(self.conn,
                                                     ['source/social/x.md']))
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['留下的'], [i['title'] for i in mat.items])
        # 后续窗口:两条的发布时间都早于起点,所以都落补录桶 —— 丢弃的那条
        # 必须被挡住,另一条照旧捡得回来(只验前者的话"补录桶整个关掉"也全绿)
        later = daily.collect(self.conn, T('2026-09-30 08:00'), T('2026-10-01 08:00'))
        self.assertEqual(['留下的'], [i['title'] for i in later.late])

    def test_dropping_in_two_batches_keeps_the_first_batch(self):
        """分两次丢,第一批不许被顶掉。

        `mark_carried` 是 DELETE-then-insert(重新生成同一天要整批替换),照它
        的写法写丢弃,第二批会把第一批静默放回队列 —— 过几天那些内容又出现在
        日报里,没有任何地方报错。所以 `mark_dropped` 只增不改。
        """
        add_doc(self.conn, 'source/social/a.md', source='social', title='甲推')
        add_doc(self.conn, 'source/social/b.md', source='social', title='乙推')
        daily_carry.mark_dropped(self.conn, ['source/social/a.md'])
        daily_carry.mark_dropped(self.conn, ['source/social/b.md'])
        self.assertEqual({'source/social/a.md': '#drop',
                          'source/social/b.md': '#drop'}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual([], [i['title'] for i in mat.items])

    def test_already_dropped_paths_are_reported_as_zero_written(self):
        """统计按**实际写入**报:重复丢同一条返回 0,不按名单长度报。"""
        add_doc(self.conn, 'source/social/a.md', source='social')
        self.assertEqual(1, daily_carry.mark_dropped(self.conn, ['source/social/a.md']))
        self.assertEqual(0, daily_carry.mark_dropped(self.conn, ['source/social/a.md']))

    def test_a_seed_rev_bump_does_not_resurrect_dropped_entries(self):
        """盖章算法升版那一趟整张表重算,**`#drop` 必须留下**。

        `#seed` 的证据是各份日报自己的素材清单,重算得到的是同一份;而丢弃是
        人当场拍的,库里没有任何东西能把它算回来 —— 跟着被 DELETE 掉就是把
        丢弃过的几百条静默倒灌回队列,而面板上一切正常。
        """
        add_doc(self.conn, 'source/social/x.md', source='social', title='推文')
        daily_carry.mark_dropped(self.conn, ['source/social/x.md'])
        # 模拟升版:库里记着旧版本号 ⇒ 下一次 ensure_ledger 整张表重算
        self.conn.execute("UPDATE daily_state SET value='0' WHERE key=?",
                          (daily_carry.SEED_REV_KEY,))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual('#drop', self.carried().get('source/social/x.md'),
                         '重算把 #drop 清掉了 —— 那一批算不回来')
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual([], [i['title'] for i in mat.items],
                         '后果:升版之后它又能被取材了')

    def test_reserved_days_are_refused_by_the_report_side(self):
        """`#drop` 不能当一份日报的日期用,也不随日报删除。

        `mark_carried` 会 `DELETE WHERE day=?`,拿 `#drop` 调它等于一次性清空
        整批丢弃;`release` 同理(删一份日报不该把丢弃过的放回队列)。
        """
        for day in (daily_carry.SEED_DAY, daily_carry.DROP_DAY):
            with self.assertRaises(ValueError):
                daily_carry.mark_carried(self.conn, day, ['source/rss/a.md'])
            with self.assertRaises(ValueError):
                daily_carry.release(self.conn, day)


class DisabledSourceTests(unittest.TestCase):
    """停用一个来源 = 停止收新内容,**库里已有的一个都不动**。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # 来源名别用 `on` / `off`:YAML 1.1 把它们解析成布尔,
        # frontmatter 里的 `source` 会变成 True / False
        for name in ('frozen', 'live'):
            (self.root / 'source' / name).mkdir(parents=True)
            (self.root / 'source' / name / 'a.md').write_text(
                f'---\ntitle: {name} 的文章\nsource: {name}\n---\n正文\n',
                encoding='utf-8')
        self.sources = [
            config.Source(name='frozen', root=self.root / 'source' / 'frozen',
                          enabled=False),
            config.Source(name='live', root=self.root / 'source' / 'live'),
        ]
        for p in (patch.object(scanner, 'SOURCES', self.sources),
                  patch.object(scanner, 'VAULT_ROOT', self.root),
                  patch.object(store, 'SOURCES', self.sources)):
            p.start(); self.addCleanup(p.stop)
        self.conn = store.connect(self.root / 'kb.db')
        self.addCleanup(self.conn.close)

    def test_a_disabled_tree_is_not_scanned(self):
        """停用的树不再产出新文档;反过来钉一边:启用的那棵照旧产出。"""
        got = {d.source for d in scanner.scan_all()}
        self.assertEqual({'live'}, got)

    def test_existing_documents_of_a_disabled_source_survive_indexing(self):
        """**停用不许删存量。**

        `scan_all` 的产出同时是"哪些文件还活着"的唯一依据,所以停用一棵树等于
        告诉 `remove_missing_documents`「它底下的文件全没了」—— 不豁免的话库里
        那一批连 chunk、向量、标签一起被删,面板上只多一个 removed 计数,而
        vault 里的文件明明还在。

        反过来钉一边:**启用来源**下真的消失了的文件照旧要被收掉,否则一个
        "什么都不删"的实现也能全绿。
        """
        for path, src in (('source/frozen/a.md', 'frozen'),
                          ('source/live/a.md', 'live'),
                          ('source/live/gone.md', 'live')):
            self.conn.execute(
                'INSERT INTO documents(path,source,title) VALUES(?,?,?)',
                (path, src, '标题'))
        self.conn.commit()
        alive = {d.path for d in scanner.scan_all()}
        self.assertEqual({'source/live/a.md'}, alive)
        removed = store.remove_missing_documents(self.conn, alive)
        left = {r['path'] for r in self.conn.execute('SELECT path FROM documents')}
        self.assertEqual({'source/frozen/a.md', 'source/live/a.md'}, left)
        self.assertEqual(1, removed, '只该收掉启用来源下那条真没了的')

    def test_the_dr_source_is_the_one_that_is_off(self):
        """钉住这次的决定本身:`dr` 停了,别的来源一个都没跟着停。

        名字、来源组和别名一个都不许动(`expand_sources('日报')` 还要查得到
        库里那 108 篇),所以这条只问 `enabled`。
        """
        off = {s.name for s in config.SOURCES if not s.enabled}
        self.assertEqual({'dr'}, off)
        self.assertEqual(['dr'], config.expand_sources('日报'))
        self.assertIn('dr', config.PRIMARY_SOURCE_NAMES)


if __name__ == '__main__':
    unittest.main()
