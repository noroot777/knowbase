"""日报的上报账本:没带走的留在队列里,带走过的不再重复上报。

这一组钉的是旧实现那条缝。旧实现两个取材桶都是"时间窗 + 取最新 N 条":要点桶
按 `published` 取窗口内最新 `MAX_ITEMS` 条,补录桶按 `indexed_at` 取这一轮入库的
最新 `LATE_ITEMS` 条。没挤进名额的条目在下一个窗口里**两个桶都选不到** ——
`published` 和 `indexed_at` 都已经早于新的起点,于是永久消失、一行提示都没有
(实测生产库里一份日报 `late_total=679` 而只带得走 30 条)。

所以这里的断言一律落在**后果**上:"下一份日报还捡不捡得回来"、"账本里那几行是
什么"。只验"账本写了没有"的话,一个把所有条目都盖章的实现也能全绿。

两个方向都钉住:该留在队列里的留住(`…_stays_queued…`),该停止重复上报的也真的
停住(`…_is_not_reported_twice`)—— 只验前者会让一个压根不盖章的实现全绿。

迁移那一刻的"一次性盖章"在 DailyLedgerSeedTests;稳态取材与窗口语义在
tests/test_daily_report.py。
"""
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_carry, llm, llm_stages, store

MODEL = llm.Model('openai', 'test-model')


def T(text):
    return datetime.fromisoformat(text)


def add_doc(conn, path, *, source='rss', title='标题', published='2026-09-28 09:00',
            indexed='2026-09-28 09:30', url=''):
    conn.execute(
        'INSERT INTO documents(path,source,title,account,published,url,summary,'
        'indexed_at,extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (path, source, title, '某号', published, url, '摘要', indexed, indexed, ''))
    conn.commit()


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        for p in (patch.object(daily, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(store, 'connect', lambda *a, **k: connect(self.db)),
                  patch.object(llm_stages, 'stage_model', Mock(return_value=MODEL)),
                  # 不指的话落盘/删除那把锁会去 flock **生产** data/ 下的
                  # lockfile(同 maintenance 那条)。
                  patch.object(daily_carry, 'DATA_DIR', self.root)):
            p.start(); self.addCleanup(p.stop)
        self.chat = Mock(return_value='## 今日要点\n\n- **有事发生**:说明 [1]')
        p = patch.object(llm, 'chat', self.chat)
        p.start(); self.addCleanup(p.stop)

    def carried(self):
        return {r['path']: r['day'] for r in
                self.conn.execute('SELECT path,day FROM daily_carried')}


class DailyCarriedTests(_Base):
    """稳态:账本已经启用过,取材只看"这一条带走过没有"。"""

    def setUp(self):
        super().setUp()
        # 分界线钉在样本之前(此刻库里一篇都没有 ⇒ 一个章都不盖)
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))

    def gen(self, since, until):
        return daily.generate(since=T(since), until=T(until))

    def test_overflow_stays_queued_for_the_next_report(self):
        """名额装不下的条目留在队列里 —— 旧实现在这里是永久丢弃。

        三条都发布在第一个窗口里,名额只有 2 个。第一份带走最新的两条,第三条的
        `published` 从此早于任何后续窗口的起点:旧实现下它既进不了要点桶(发布
        时间不在窗口内)、也进不了补录桶(入库时间不在窗口内),从此人间蒸发。
        """
        for i, hhmm in enumerate(('09:00', '10:00', '11:00')):
            add_doc(self.conn, f'source/rss/{i}.md', title=f'第{i}条',
                    published=f'2026-09-28 {hhmm}')
        with patch.object(daily, 'MAX_ITEMS', 2):
            first = self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        self.assertEqual(2, first['docs'])
        self.assertEqual(['第1条', '第2条'],
                         sorted(t for t in ('第0条', '第1条', '第2条')
                                if t in Path(first['path']).read_text(encoding='utf-8')))
        # 下一个窗口:落选的那条还在队列里,按"发布得更早"进补录
        mat = daily.collect(self.conn, T('2026-09-29 08:00'), T('2026-09-30 08:00'))
        self.assertEqual(['第0条'], [i['title'] for i in mat.late])
        self.assertEqual([], [i['title'] for i in mat.items])

    def test_a_carried_entry_is_not_reported_twice(self):
        """反过来钉一边:带走过的不许再出现,否则账本只是个计数器。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文')
        self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        self.assertEqual({'source/rss/a.md': '2026-09-29'}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-29 08:00'), T('2026-09-30 08:00'))
        self.assertEqual(([], []), ([i['title'] for i in mat.items],
                                    [i['title'] for i in mat.late]))

    def test_reindexing_an_old_article_does_not_resurrect_it(self):
        """老文章重新导出会刷新 `indexed_at`,旧口径下它每天都重新算一次
        "这一轮才入库",连着十几天占满补录。账本只认"带走过没有"。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文')
        self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        for day in ('2026-09-29 20:00', '2026-09-30 20:00'):
            self.conn.execute('UPDATE documents SET indexed_at=? WHERE path=?',
                              (day, 'source/rss/a.md'))
            self.conn.commit()
            mat = daily.collect(self.conn, T('2026-09-29 08:00'), T('2026-10-01 08:00'))
            self.assertEqual([], [i['title'] for i in mat.late], f'{day} 又冒出来了')

    def test_regenerating_the_same_day_keeps_what_that_day_already_carried(self):
        """同名文件是整份覆盖的,所以重新生成时它自己上一版带走的那些必须重新
        可选 —— 排除掉的话新版本会把早上已经报过的内容整段丢掉。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文')
        first = self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        add_doc(self.conn, 'source/rss/b.md', title='乙文',
                published='2026-09-29 07:00')
        again = self.gen('2026-09-28 08:00', '2026-09-29 09:00')
        self.assertEqual(first['path'], again['path'])      # 同一份被覆盖
        text = Path(again['path']).read_text(encoding='utf-8')
        self.assertIn('甲文', text)
        self.assertIn('乙文', text)
        self.assertEqual({'source/rss/a.md': '2026-09-29',
                          'source/rss/b.md': '2026-09-29'}, self.carried())

    def test_deleting_a_report_returns_its_entries_to_the_queue(self):
        add_doc(self.conn, 'source/rss/a.md', title='甲文')
        self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        self.assertTrue(daily.delete('2026-09-29'))
        self.assertEqual({}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['甲文'], [i['title'] for i in mat.items])

    def test_deleting_while_a_report_is_being_written_does_not_strand_it(self):
        """「落盘+记账」和「放回+删文件」整体互斥。

        两段各自的顺序只管自己失败时往哪边倒,管不住交错:生成写完文件、还没
        记账的那一瞬间删除跑完整段,接着生成把章补上 —— 留下一批"记着已上报、
        却没有任何日报文件"的条目,两头落空。阅读区那颗删除按钮在生成期间本来
        就能点,所以这不是理论上的交错。

        断言落在**末态**上(文件没了 ⇒ 素材必须还能报):加了锁,无论两段怎么排
        都不会红;去掉锁才会红。
        """
        add_doc(self.conn, 'source/rss/race.md', title='删除后应该可重新报道的')
        first = self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        self.assertEqual(1, len(self.carried()))

        # 删除必须**等文件落盘之后**才发起 —— 先发起的话它在 generate 进锁之前
        # 就整段跑完了,随后那一份又被写出来,测到的是另一个时序。
        written, done = threading.Event(), []
        real_write = daily._write

        def slow_write(path, text):
            real_write(path, text)        # 文件已经落盘,章还没盖
            written.set()
            time.sleep(0.2)               # 给删除线程一个真实的插入机会

        def deleting():
            written.wait(5)
            done.append(daily.delete('2026-09-29'))

        t = threading.Thread(target=deleting)
        with patch.object(daily, '_write', slow_write):
            t.start()
            self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        t.join(10)

        self.assertEqual([True], done)
        self.assertFalse(Path(first['path']).exists())
        self.assertEqual({}, self.carried(), '日报文件没了,章不能还盖着')
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['删除后应该可重新报道的'], [i['title'] for i in mat.all()])

    def test_nothing_is_stamped_when_the_report_never_lands(self):
        """落盘之前一个章都不许盖。盖了的话模型钱花掉、文件没写成,这批素材
        再也选不到 —— 不可恢复。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文')
        self.chat.return_value = '   '
        with self.assertRaises(RuntimeError):
            self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        self.assertEqual({}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['甲文'], [i['title'] for i in mat.items])

    def test_a_write_failure_leaves_the_ledger_untouched(self):
        """落盘失败而章已经盖上 = 模型钱花掉、文件没写成、这批素材再也选不到。

        和上一条用例不是一回事:空回包那一档在 `_write` 之前就抛了,所以它钉不住
        "盖章排在落盘之后"这个顺序 —— 得让 `_write` 自己失败才探得到(反向验证时
        把盖章挪到 `_write` 之前,只有这一条红)。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文')
        with patch.object(daily, '_write', Mock(side_effect=OSError('磁盘满了'))):
            with self.assertRaises(OSError):
                self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        self.assertEqual({}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['甲文'], [i['title'] for i in mat.items])

    def test_an_explicit_overlapping_window_does_not_re_report(self):
        """要点桶也要认账本,不是只有补录桶认。

        日常窗口是首尾相接的,所以要点桶里"已经报过"几乎碰不到;但手工指定窗口
        补一份时(`/api/daily/run` 带 since/until)两段会重叠,那时不认账本就是把
        上一份的内容整段重讲一遍。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-28 20:00')
        # 同一段窗口再跑一次,但落在另一天的文件上 ⇒ 账本该把甲文挡掉
        out = self.gen('2026-09-28 08:00', '2026-09-30 08:00')
        text = Path(out['path']).read_text(encoding='utf-8')
        self.assertIn('乙文', text)
        self.assertNotIn('甲文', text)
        self.assertEqual(1, out['docs'])

    def test_the_oldest_entries_keep_a_reserved_slot(self):
        """持续有新内容进来时,发布得最早的那几条也排得上 —— 不留席位的话账本
        保住了它们、席位分配却让它们等到被 BACKLOG_DAYS 挡掉,等于再丢一次。"""
        for i in range(12):
            add_doc(self.conn, f'source/rss/{i:02d}.md', title=f'第{i:02d}条',
                    published=f'2026-09-{10 + i:02d} 09:00')
        with patch.object(daily, 'LATE_ITEMS', 4), patch.object(daily, 'LATE_DRAIN', 1):
            mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        got = [i['title'] for i in mat.late]
        self.assertEqual(12, mat.late_total)
        self.assertEqual(4, len(got))
        self.assertEqual(['第11条', '第10条', '第09条'], got[:3])   # 新的在前
        self.assertEqual('第00条', got[3])                          # 最老的那个席位

    def test_too_old_is_not_fetched_back_but_the_edge_still_is(self):
        """补录不回头捡太老的;反过来钉一边 —— 刚过线之内的照旧捡得到。"""
        add_doc(self.conn, 'source/rss/old.md', title='一年前的',
                published='2025-09-28 09:00')
        add_doc(self.conn, 'source/rss/edge.md', title='线内的',
                published='2026-09-20 09:00')
        with patch.object(daily, 'BACKLOG_DAYS', 30):
            mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['线内的'], [i['title'] for i in mat.late])

    def test_the_panel_count_and_the_real_pick_are_one_verdict(self):
        """面板上那个"又攒了多少"必须和真生成时带走的是同一批。两处各判一遍的
        话必然各漂各的:用户看着"有 7 条新素材"点下去,出来的日报里是另一批。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-20 09:00')
        now = T('2026-09-29 08:00')
        info = daily.pending(first_days=1, now=now)
        self.assertEqual((1, 1), (info['items'], info['late']))
        mat = daily.collect(self.conn, info['since'], info['until'])
        self.assertEqual((1, 1), (mat.total, mat.late_total))
        # 出过报之后那个数字归零 —— 它答的是"还没进过任何一份日报的有多少"
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(0, sum(daily.pending(first_days=1, now=now)[k]
                                for k in ('items', 'late')))


class DailyLedgerSeedTests(_Base):
    """启用账本那一刻的一次性盖章。

    不盖的话账本一启用,库里的历史存量全都算"没上报过",第一份日报的补录候选是
    上千条、要几十份日报才排得空 —— 而那些内容在旧实现下早就永久落选了,翻出来
    不是修复、是倒灌。
    """

    def _chain(self, date='2026-09-28', since='2026-09-27 08:00:00',
               until='2026-09-28 08:00:00', body='正文'):
        """摆一份已有的日报(窗口写在它自己的 frontmatter 里)。"""
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / f'{date}.md').write_text(
            f'---\ntype: daily-report\ndate: {date}\n'
            f"window_since: '{since}'\nwindow_until: '{until}'\n---\n\n{body}\n",
            encoding='utf-8')

    def test_the_archive_is_written_off_and_attributed_to_its_own_report(self):
        """盖章落到「当初是哪一份带走的」那一天上,不是一律 `#seed`。

        比链起点还早的(和没有发布时间的)记 `#seed`;落在某一份窗口里的记那一
        份的日期 —— 后者是「重新生成那一份时它还选得到」的前提,统一记 `#seed`
        会让覆盖旧日报把它原有的报道一条不剩地抹掉(见下一条用例)。
        """
        self._chain()
        add_doc(self.conn, 'source/rss/ancient.md', title='上个月的',
                published='2026-08-20 09:00', indexed='2026-08-20 10:00')
        add_doc(self.conn, 'source/rss/inside.md', title='那一份窗口里的',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        add_doc(self.conn, 'source/rss/nodate.md', title='没有发布时间的',
                published='', indexed='2026-09-20 10:00')
        head = daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual('2026-09-28 08:00:00', head)     # 分界线 = 链头的截止时刻
        self.assertEqual({'source/rss/ancient.md': daily.SEED_DAY,
                          'source/rss/nodate.md': daily.SEED_DAY,
                          'source/rss/inside.md': '2026-09-28'}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual([], mat.all())                   # 存量不往回捡

    def test_an_article_that_arrived_after_the_last_report_is_not_written_off(self):
        """**这一条是整套迁移的承重点。**

        账本是懒初始化的:第一次真正跑到它,可能已经是升级之后好几天 —— 用户先
        点「重试失败项」补抓一批、再打开日报面板。那一批的发布时间多半早于链头,
        只看发布时间就会把它们一律当成历史存量盖掉,于是**正好是这次要救的那批
        内容被永久写死**。判据因此是「它到得比最后一份日报还晚吗」:`indexed_at`
        只会往前走,所以"到得晚"证得死。

        反过来钉一边在上一条用例里:到得比链头早的存量照旧盖章,不是一律放行。
        """
        self._chain()
        add_doc(self.conn, 'source/rss/retry.md', title='刚补抓回来的前天那篇',
                published='2026-09-26 12:00',      # 比链头(09-28 08:00)早
                indexed='2026-09-28 12:00')        # 但到得比链头晚
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried())
        info = daily.pending(first_days=1, now=T('2026-09-29 08:00'))
        self.assertEqual(1, info['late'], '刚补抓、从未报道的文章必须还在队列里')
        out = daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.assertIn('刚补抓回来的前天那篇',
                      Path(out['path']).read_text(encoding='utf-8'))

    def test_content_in_a_gap_of_the_chain_is_not_written_off(self):
        """日报链有断档时,落在缺口里的内容不盖章 —— 那一份已经不在了。

        盖成 `#seed` 等于宣布"它被某一份带走过",而能证明这件事的那份日报恰恰
        不存在(被删掉、或者手工挪走了)。方向按元规则取宽的那一边:放回队列最多
        多报一遍,盖错了就是永久写死。
        """
        self._chain(date='2026-09-26', since='2026-09-25 08:00:00',
                    until='2026-09-26 08:00:00')
        self._chain(date='2026-09-28', since='2026-09-27 08:00:00',
                    until='2026-09-28 08:00:00')          # 中间缺了 09-27 那一份
        add_doc(self.conn, 'source/rss/gap.md', title='缺口里的',
                published='2026-09-26 20:00', indexed='2026-09-26 21:00')
        add_doc(self.conn, 'source/rss/kept.md', title='有归属的',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/kept.md': '2026-09-28'}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['缺口里的'], [i['title'] for i in mat.late])

    def test_regenerating_a_migrated_report_keeps_its_original_article(self):
        """覆盖一份**迁移之前**生成的旧日报,不能抹掉它原有的报道。

        同名文件是整份覆盖的,而 `UNCARRIED` 放行的是「这一份自己盖的章」——
        旧日报的素材要是统一记成 `#seed`,重新生成时一条都选不回来,产物里只剩
        这期间新补抓的那几条,已经交付的内容当场消失。
        """
        self._chain(body='## 今日要点\n\n- 甲文 [1]')
        add_doc(self.conn, 'source/rss/original.md', title='原日报里已有的甲文',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        # 迁移之后才补抓回来的乙文,发布时间同样落在那一份的窗口里
        add_doc(self.conn, 'source/rss/retry.md', title='补抓回来的乙文',
                published='2026-09-27 22:00', indexed='2026-09-29 09:00')
        out = daily.generate(since=T('2026-09-27 08:00'), until=T('2026-09-28 08:00'))
        text = Path(out['path']).read_text(encoding='utf-8')
        self.assertIn('原日报里已有的甲文', text, '覆盖旧日报不能抹掉它已有的报道')
        self.assertIn('补抓回来的乙文', text)
        self.assertEqual(2, out['docs'])

    def test_entries_that_arrive_after_the_epoch_are_never_written_off(self):
        """盖章只盖"启用那一刻库里已经有的";之后进来的内容无论发布多早都留着
        —— 那正是"重试失败项/新加一个订阅"抓回来的形态。"""
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        add_doc(self.conn, 'source/rss/late.md', title='迟到的老文',
                published='2026-09-25 09:00', indexed='2026-09-29 10:00')
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 12:00'))
        self.assertEqual(['迟到的老文'], [i['title'] for i in mat.late])

    def test_seeding_is_idempotent(self):
        add_doc(self.conn, 'source/rss/a.md', published='2026-08-01 09:00')
        first = daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        add_doc(self.conn, 'source/rss/b.md', published='2026-08-02 09:00')
        again = daily._ensure_ledger(self.conn, now=T('2026-10-20 08:00'))
        self.assertEqual(first, again)                 # 分界线不会被重算
        self.assertEqual(['source/rss/a.md'], list(self.carried()))

    def test_the_seed_batch_is_not_released_by_deleting_a_report(self):
        add_doc(self.conn, 'source/rss/a.md', published='2026-08-01 09:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        with self.assertRaises(ValueError):
            daily.release(self.conn, daily.SEED_DAY)
        with self.assertRaises(ValueError):
            daily.mark_carried(self.conn, daily.SEED_DAY, ['source/rss/b.md'])
        self.assertEqual(['source/rss/a.md'], list(self.carried()))


if __name__ == '__main__':
    unittest.main()
