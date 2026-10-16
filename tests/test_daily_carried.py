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

迁移那一刻的"一次性盖章"在 tests/test_daily_seed.py;稳态取材与窗口语义在
tests/test_daily_report.py。
"""
import sqlite3
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_carry, daily_run, llm, llm_stages, store
from kb import daily_state

MODEL = llm.Model('openai', 'test-model')


def T(text):
    return datetime.fromisoformat(text)


def add_doc(conn, path, *, source='rss', title='标题', published='2026-09-28 09:00',
            indexed='2026-09-28 09:30', url='', account='某号'):
    # `account` 默认和 `write_report` 清单里写的那个账号一致 —— 盖章第四道比的
    # 就是「来源·账号」,两侧对不上会让别的用例各验一半(见
    # test_daily_seed_identity 的 `_chain(credit=...)`)。
    conn.execute(
        'INSERT INTO documents(path,source,title,account,published,url,summary,'
        'indexed_at,extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (path, source, title, account, published, url, '摘要', indexed, indexed, ''))
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

    def test_overwriting_frees_the_old_stamps_before_the_new_file_lands(self):
        """覆盖一份日报时,上一版带走、这一版没选上的素材必须回到队列。

        覆盖是**整份替换文件**:被名额挤出去的那条从盘上消失了,而它的章还盖着
        —— 日报里看不到、下一份也再选不到,两头落空且不可恢复(自由工作流审查
        实测:120 条新素材挤掉旧那条,记账一失败,甲文在下一份的候选里就没了)。
        所以「放回」排在落盘**之前**,三步的失败方向一律倒向「多报一遍」。

        断言落在**后果**上:记账那一步失败之后,下一份日报还捡不捡得回来。只验
        "放回那句执行了没有"的话,一个放回之后又把章补回去的实现也能全绿。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-29 07:00')
        with patch.object(daily, 'MAX_ITEMS', 1), \
             patch.object(daily, 'mark_carried',
                          Mock(side_effect=sqlite3.OperationalError('记账写失败'))):
            out = self.gen('2026-09-28 08:00', '2026-09-29 09:00')
        text = Path(out['path']).read_text(encoding='utf-8')
        self.assertNotIn('甲文', text)      # 名额只有 1 个,甲文被挤出了这一份
        self.assertIn('记账写失败', out.get('carry_error', ''))
        self.assertEqual({}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-29 09:00'), T('2026-09-30 09:00'))
        got = [i['title'] for i in mat.all()]
        self.assertIn('甲文', got, '文件里已经没有它了,章还盖着 ⇒ 永久丢失')
        self.assertIn('乙文', got, '这一份的章没盖上,下一份重复报一遍是预期代价')

    def test_a_release_failure_leaves_the_previous_report_byte_for_byte(self):
        """反过来钉一边:放回自己失败时整轮抛错,旧日报和旧账本都不许动。

        上一条钉的是"放回必须排在落盘之前",这一条钉的是同一个顺序的另一头 ——
        放回没做成就先把文件覆盖掉的话,留下的是一份新文件配一份旧账本,正是
        上一条要治的那个末态。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        first = self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        before = Path(first['path']).read_text(encoding='utf-8')
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-29 07:00')
        with patch.object(daily, 'release',
                          Mock(side_effect=sqlite3.OperationalError('放回失败'))):
            with self.assertRaises(sqlite3.Error):
                self.gen('2026-09-28 08:00', '2026-09-29 09:00')
        self.assertEqual(before, Path(first['path']).read_text(encoding='utf-8'))
        self.assertEqual({'source/rss/a.md': '2026-09-29'}, self.carried())

    def test_a_write_failure_while_overwriting_only_costs_a_repeat(self):
        """三步顺序的第三个失败方向:覆盖时落盘失败。

        旧日报逐字不变(`_write` 是临时文件 + rename),而这一天的章已经放回 ⇒
        它报过的内容会在下一份里再出现一遍。**这是代价,不是最优末态** —— 理想是
        连章一起恢复,但那又要一次可能同样失败的写入,所以按元规则倒向"多报一遍"。
        钉住它是为了别让谁把放回改成"只在成功时放回":那时旧文件还在、章也还在,
        看起来"没事发生",可覆盖那条路上的旧素材就又回到了两头落空的末态。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        first = self.gen('2026-09-28 08:00', '2026-09-29 08:00')
        before = Path(first['path']).read_text(encoding='utf-8')
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-29 07:00')
        with patch.object(daily, '_write', Mock(side_effect=OSError('磁盘满了'))):
            with self.assertRaises(OSError):
                self.gen('2026-09-28 08:00', '2026-09-29 09:00')
        self.assertEqual(before, Path(first['path']).read_text(encoding='utf-8'))
        self.assertEqual({}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 09:00'))
        self.assertEqual({'甲文', '乙文'}, {i['title'] for i in mat.all()})

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
        """tooltip 里那个"队列里还积着多少"必须和真取材认的是同一批。两处各判
        一遍的话必然各漂各的:用户看着"还有 7 条没报过",生成时带走的是另一批。

        **这个数不上按钮** —— 按钮边上写的是「这一次会多带进来几条」(`gain`,
        下面几条);抓取自己的事实(今天第 N 次抓了多少)钉在
        tests/test_fetch_rounds.py。三个数混成一个的后果各写在那几份的 docstring 里。"""
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

    def test_this_time_counts_only_what_the_next_report_would_really_add(self):
        """按钮边上那个数(`gain`)答的是"**再生成一次**会多带进来几条"。

        判据落在后果上:出过报之后它归零(面板据此把「重新生成日报」整颗藏掉 ——
        点下去只会原样挑回同一批,花一次模型钱拿回同一份日报);随后真进来一条
        新的,它只算那一条 **1**。算成 2 的实现是把今天那一份自己带走的也数了
        进去("是不是今天自己盖的章"那一列没过滤);出过报还不归零的,面板上
        就永远有一颗消不掉的按钮。名额那一侧的射程在下面那条。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        now = T('2026-09-29 08:00')
        info = daily.pending(first_days=1, now=now)
        self.assertEqual(1, info['gain'])
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(0, daily.pending(first_days=1, now=now)['gain'])
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-29 07:00')
        again = daily.pending(first_days=1, now=now)
        self.assertEqual((1, 1), (again['gain'], again['items']))

    def test_an_update_to_the_same_article_counts_as_new_content(self):
        """**只按路径盖章答不了"还有没有新东西"。** 同一篇文章的正文后来补全了、
        摘要刚提取出来,下一次送进模型的就是另一份内容;按路径判的话它永远算
        "这篇报过了",按钮整颗藏着,人再没有入口把这些更新报出来(审查实测:前后
        两次真实 prompt 确实不同,而面板那个数两次都是 0)。

        判据落在后果上:**真取材那一侧拿到的摘要已经换了一份**,同一时刻面板上
        那个数必须跟着是 1。反过来也钉一边:内容改回去、只有 `indexed_at` 变了的
        话它得回到 0 —— 指纹掺进跟 prompt 无关的东西,每次重新索引都会报一次假的
        「有新内容」。另外钉住它**不算进队列积压**:那个数答的是"一份日报都没进
        过的有多少",这一篇进过。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        now = T('2026-09-29 08:00')
        info = daily.pending(first_days=1, now=now)
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(0, daily.pending(first_days=1, now=now)['gain'])

        self.conn.execute('UPDATE documents SET summary=? WHERE path=?',
                          ('结果已经公布,补上了关键结论', 'source/rss/a.md'))
        self.conn.commit()
        mat = daily.collect(self.conn, info['since'], info['until'])
        self.assertEqual(['结果已经公布,补上了关键结论'],
                         [i['excerpt'] for i in mat.items])
        after = daily.pending(first_days=1, now=now)
        self.assertEqual((1, 0), (after['gain'], after['items']))

        self.conn.execute('UPDATE documents SET summary=?, indexed_at=? WHERE path=?',
                          ('摘要', '2026-10-01 00:00', 'source/rss/a.md'))
        self.conn.commit()
        self.assertEqual(0, daily.pending(first_days=1, now=now)['gain'])

    def test_a_stamp_without_a_signature_answers_unknown_not_zero(self):
        """账本里**没有**指纹的那些行(这一列是后加的;`seed_locked` 整表重算出来
        的那批也没有)—— 这一问**答不上来,就得说答不上来**(`gain is None`),
        不许当成 0。

        曾经退一步比「带走之后有没有重新索引过」(`indexed_at > carried_at`),
        **那是错的**:真正换掉素材的那条路是提取 —— `extract.save_extraction` 写
        `summary` / 实体 / 主题 / `extracted_at`,**一个字都不碰 `indexed_at`**,
        而"日报先出、提取随后补上摘要"正是常见顺序。于是"索引时刻没动"被当成
        "送进模型的还是同一份",按钮跟着藏掉,人再没有入口把这些更新报出来
        (审查实测 12 组:模型输入全变、`indexed_at` 全没动、`gain` 全是 0)。

        三边都钉:①什么都没改也答「不知道」(0 意味着"确定没有",而我们确定不了);
        ②**提取改了摘要之后照旧答「不知道」** —— 这一条才是旧判据答错的那一档,
        它当时会答 0;③重新生成一次(账本把指纹补上)之后回到**确定的 0** ——
        不然一个"永远答不知道"的实现也能把前两条蒙过去,而那等于按钮再也藏不掉。
        顺带钉住队列积压不受影响:那个数答的是"一份日报都没进过的有多少"。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        now = T('2026-09-29 08:00')
        info = daily.pending(first_days=1, now=now)
        daily.generate(since=info['since'], until=info['until'])
        self.conn.execute('UPDATE daily_carried SET material_sig=NULL, carried_at=?',
                          ('2026-09-29 08:30',))
        self.conn.commit()
        blind = daily.pending(first_days=1, now=now)
        self.assertEqual((None, 0), (blind['gain'], blind['items']))

        # 真实提取那条路:摘要换了一份,`indexed_at` 原样不动。
        before = self.conn.execute(
            'SELECT indexed_at FROM documents WHERE path=?',
            ('source/rss/a.md',)).fetchone()['indexed_at']
        self.conn.execute(
            'UPDATE documents SET summary=?, extracted_at=? WHERE path=?',
            ('结果已经公布,补上了关键结论', '2026-09-29 09:00', 'source/rss/a.md'))
        self.conn.commit()
        self.assertEqual(before, self.conn.execute(
            'SELECT indexed_at FROM documents WHERE path=?',
            ('source/rss/a.md',)).fetchone()['indexed_at'])
        self.assertIsNone(daily.pending(first_days=1, now=now)['gain'])

        # 重新生成一次就自愈:这一轮的指纹落进账本,又答得出确定的数了。
        daily.generate(since=info['since'], until=info['until'])
        self.assertEqual(0, daily.pending(first_days=1, now=now)['gain'])

    def test_the_report_file_itself_is_reported_with_the_same_read(self):
        """「那一天的日报此刻在不在」和 `gain` 必须**同一次调用**给出来。

        界面要两个条件一起成立才敢把「重新生成日报」整颗藏掉。账本盖的章和 vault
        里那个文件是两头东西:在 Obsidian 里把今天那份移走(没走界面那条删除)
        之后章还在、`gain` 照样是 0 —— 这时要是还说"那一天有日报",唯一能重建它
        的入口就被藏掉了(审查实测:页面上按钮一直不回来,只有整页刷新才回来)。

        三边都钉:没出过报时 `has_report` 是 False;出过是 True;**文件在外面被
        移走之后当场变回 False,而 `gain` 仍然是 0**(两件事各答各的,后者本来
        就不该变)。`day` 报的是这一次会写到哪一天 —— 界面拿它确认"后端答的是不是
        我要问的那一天"。
        """
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        now = T('2026-09-29 08:00')
        info = daily.pending(first_days=1, now=now)
        self.assertEqual(('2026-09-29', False), (info['day'], info['has_report']))

        daily.generate(since=info['since'], until=info['until'])
        after = daily.pending(first_days=1, now=now)
        self.assertEqual((0, True), (after['gain'], after['has_report']))

        (daily.report_dir() / '2026-09-29.md').unlink()
        gone = daily.pending(first_days=1, now=now)
        self.assertEqual((0, False), (gone['gain'], gone['has_report']))

    def test_a_long_queue_with_nothing_new_this_time_reads_as_nothing_new(self):
        """**队列总长不许冒充它。** 名额每轮只带得走固定条数,生产库实测队列
        1097 条而重新生成一次只多出 11 条 —— 把队列写在按钮边上,用户会照着它
        花一次模型钱,拿回一份几乎没变的日报。

        这里把名额缩到 1:三条全在窗口里,今天那一份带走了最新的那条;剩下两条
        发布得更早,**下一次生成照样轮不到它们**(名额里挑的还是同一条)。所以
        "队列里还积着 2 条"和"这一次一条都多带不进来"同时成立,两个数各报各的。

        **这一条还是「取材口径必须和 `collect` 逐字一致」的独占射程**:不截名额
        (或者拿 `day=''` 去问 —— 今天那一份带走的不在候选里,名额就空出来给了
        根本抢不到席位的旧条目)都会在这里答出一个大于 0 的数,而真生成一次它们
        一条都进不去。"""
        for i, hhmm in enumerate(('09:00', '10:00', '11:00')):
            add_doc(self.conn, f'source/rss/{i}.md', title=f'第{i}条',
                    published=f'2026-09-28 {hhmm}')
        now = T('2026-09-29 08:00')
        with patch.object(daily, 'MAX_ITEMS', 1):
            info = daily.pending(first_days=1, now=now)
            self.assertEqual((3, 1), (info['items'], info['gain']))
            daily.generate(since=info['since'], until=info['until'])
            after = daily.pending(first_days=1, now=now)
        self.assertEqual((2, 0), (after['items'], after['gain']))

    def test_the_two_buckets_are_read_from_one_snapshot(self):
        """两个桶是两条 SELECT,而 legacy `isolation_level` 下 SELECT 自己不开
        事务 —— 中间夹进别人一次记账提交的话,折出来的是**两个版本拼起来的数**
        (上一版就这么报出过一个凭空的新增)。

        摆法:在要点桶那条查询的行循环里放一个真线程,让它用自己的连接把两篇
        都盖上章,等它提交完再让补录桶那条查询跑。判据落在**自洽**上:两篇要么
        都算没报过(读事务开在前,拿的是同一份快照),要么都算报过;一个 1 一个
        0 就是把半份新事实拼进了旧事实里。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        add_doc(self.conn, 'source/rss/b.md', title='乙文', published='2026-09-20 09:00')
        started = threading.Event()

        def stamp_elsewhere():
            other = store.connect()
            try:
                daily_carry.mark_carried(other, '2026-09-29',
                                         ['source/rss/a.md', 'source/rss/b.md'])
            finally:
                other.close()

        real_parse = daily.parse_ts

        def once(value):
            # 第一条查询正在逐行解析 ⇒ 快照(如果有的话)已经定下来了
            if not started.is_set():
                started.set()
                t = threading.Thread(target=stamp_elsewhere)
                t.start(); t.join(10)
            return real_parse(value)

        with patch.object(daily, 'parse_ts', once):
            info = daily.pending(first_days=1, now=T('2026-09-29 08:00'))
        self.assertIn((info['items'], info['late']), ((1, 1), (0, 0)),
                      '两个桶读到了两个版本')

    def test_a_report_whose_accounting_failed_keeps_offering_the_button(self):
        """**这个数的误差方向只许倒向「多报」。**

        账本是允许出现「日报文件写好了、记账没写上」的(那一档故意倒向多报一遍)。
        那时今天带走的那几条看着像没进过日报,`gain` 跟着偏大 —— 面板多显示一次
        那颗按钮,代价是用户可能白花一次模型钱,**看得见**。反过来偏小就是把按钮
        藏掉而确实有新内容:一次出报机会被静默吃掉,面板上一个字都不说。"""
        add_doc(self.conn, 'source/rss/a.md', title='甲文', published='2026-09-28 09:00')
        now = T('2026-09-29 08:00')
        info = daily.pending(first_days=1, now=now)
        with patch.object(daily, 'mark_carried',
                          Mock(side_effect=sqlite3.OperationalError('disk I/O error'))):
            daily.generate(since=info['since'], until=info['until'])
        self.assertEqual({}, self.carried())            # 账本确实空着
        self.assertEqual(1, daily.pending(first_days=1, now=now)['gain'])


class CarryFailureNoticeTests(_Base):
    """记账没写成是**降级完成**,不是干净的成功 —— 面板上那句话要说出来。

    `daily_run` 的收尾文案另有一组在 tests/test_daily_report.py(通道缺口那几条),
    这一条留在账本这边:它钉的是"记账失败那一档对外说什么",而同一档往哪边倒的
    另一半(放回与盖章的顺序)就在上面那两条用例里,放一起才看得出是一对。
    """

    def test_the_finish_notice_says_the_accounting_did_not_land(self):
        before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(before))
        daily_run._state.update({**before, 'running': True, 'log': [], 'channels': []})
        gen = Mock(return_value={'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
                                 'docs': 2, 'total': 2, 'late': 0, 'model': 'm',
                                 'carry_error': 'database is locked'})
        with patch.object(daily_state, 'STATE_FILE', self.root / 'last_run.json'), \
             patch.object(daily, 'generate', gen):
            daily_run._run(None, None, False, [], daily.FIRST_DAYS)
        self.assertEqual('done', daily_run._state['phase'])
        self.assertIn('database is locked', daily_run._state['detail'])
        self.assertIn('重复出现', daily_run._state['detail'])

    def test_a_clean_round_says_nothing_about_the_ledger(self):
        """反过来钉一边:正常那一轮不许凭空多出一句"记录没写进库"。"""
        before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(before))
        daily_run._state.update({**before, 'running': True, 'log': [], 'channels': []})
        gen = Mock(return_value={'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
                                 'docs': 2, 'total': 2, 'late': 0, 'model': 'm', 'carried': 2})
        with patch.object(daily_state, 'STATE_FILE', self.root / 'last_run.json'), \
             patch.object(daily, 'generate', gen):
            daily_run._run(None, None, False, [], daily.FIRST_DAYS)
        self.assertEqual('日报已生成:2 条素材', daily_run._state['detail'])


class RunIdentityTests(unittest.TestCase):
    """每一轮日报都有自己的身份 —— 面板拿它认"有没有出现一轮新的已结束运行"。

    后果在面板上(tests/daily_probe.mjs 那一组:没见过 running 的那一轮也要把
    列表和正文刷上),这里只钉住接口确实给得出这个身份、而且两轮不会同号:
    `finished_at` 只精确到秒,拿它当身份时同一秒里跑完的两轮分不开。

    「同一进程里的两轮」和「跨重启的两轮」是**两条**,缺一不可:只钉前者的话,
    一个纯进程内计数器也全绿,而重启恰好把它归零。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(self.before))
        for p in (patch.object(daily_state, 'STATE_FILE', Path(temp.name) / 'last_run.json'),
                  patch.object(daily_run, 'SETTINGS_FILE', Path(temp.name) / 's.json'),
                  patch.object(daily_run.threading, 'Thread', Mock())):
            p.start(); self.addCleanup(p.stop)

    def round_id(self) -> str:
        """起一轮,返回它的身份。"""
        daily_run._state.update({**self.before, 'running': False})
        self.assertTrue(daily_run.start(sync=False))
        got = daily_run.get_state()['run_id']
        self.assertTrue(got, '状态接口得给得出这一轮的身份')
        return got

    def test_two_rounds_never_share_an_identity(self):
        self.assertNotEqual(self.round_id(), self.round_id())

    def test_identities_do_not_repeat_across_a_restart(self):
        """重启之后那一轮也不许和重启前的任何一轮同号。

        计数器在模块加载时归零,而**旧页面还开着**、手里攥的是重启前那个 id ——
        同号的话它把重启后这一轮判成"同一轮",列表和正文永永远远停在旧版(审查
        实测:重启 + 生成都落在一次空闲轮询里,中间那个空状态页面压根没读到)。

        这里模拟的就是重启:计数器归零 + 命名空间**重新现取一次**。所以把那段
        命名空间写死成常量时这一条会红,而上一条不会。
        """
        before_restart = self.round_id()
        with patch.object(daily_run, '_RUN_NS', daily_run._new_namespace()), \
             patch.object(daily_run, '_run_seq', 0):
            after_restart = self.round_id()
        self.assertNotEqual(before_restart, after_restart)


if __name__ == '__main__':
    unittest.main()
