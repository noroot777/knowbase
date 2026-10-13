"""没有 url 时,那条按标题匹配上的归属算不算数(`daily_carry._match_holds`)。

从 tests/test_daily_seed.py 拆出来只因为行数(合起来过了 700 行上限)。那一份
留的是「历史存量 / 清单解析 / 分界线 / 并发」,这一份只管一件事:`doc_key` 在
url 为空时退化成标题,于是两篇同名文档共享一个身份,而三道判据各排除一种「它
不可能是那一篇」—— 缺任何一道都是**永久漏报**,面板上却写着「没有新内容」。
摆盘、断言和反向验证的思路都和那边同一套,`_Base` / `T` / `add_doc` /
`write_report` 也直接用它们的,保证两组摆出来的是同一个世界。
"""
import unittest

from kb import daily, store
from tests.test_daily_carried import T, _Base, add_doc
from tests.test_daily_seed import write_report


class NoUrlAttributionTests(_Base):
    """无 url 的归属匹配:三道判据各自的射程,外加升版重算。"""

    _chain = staticmethod(write_report)

    def test_a_same_titled_newcomer_without_a_url_keeps_its_slot(self):
        """没有 url 的那条归属匹配不是硬证据 —— 同名的新文章不许被它抢走。

        `daily_page.doc_key` 在 url 为空时退化成标题,于是两篇同名文档共享一个
        身份:旧那篇真的被旧日报带走过,新抓到的这篇只是撞了名字,却一起被盖成
        「那一份带走了它」。`UNCARRIED` 从此永远排除它 —— **永久漏报,而面板上
        写着「没有新内容」**(审查实测 `pending.items=0`,生成直接答"没有未上报
        过的新内容")。

        断言落在**后果**上:这一篇还捡不捡得回来。证据不足时两篇都不盖章,所以
        旧那篇会多报一遍 —— 那是按元规则取的安全方向(多报看得见,写死补不回)。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')])
        add_doc(self.conn, 'source/rss/old.md', title=title, source='rss',
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        add_doc(self.conn, 'source/rss/new.md', title=title, source='social',
                published='2026-09-28 12:00', indexed='2026-09-28 13:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '证据不足的一律不盖章')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked],
                      '真的新抓到的那篇必须还选得到')

    def test_a_same_titled_newcomer_arriving_too_late_keeps_its_slot(self):
        """同名、无 url,而且**到得比那一份生成还晚** —— 它不可能被那一份带走过。

        和上一条不是一回事:这一条的新文章在那一份日报生成**之后**才入库,所以连
        "它当时可能在库里"都谈不上。原来那道迟到保护排在归属匹配**之后**,匹配一
        命中就 `continue`,这一档压根到不了它。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')])
        add_doc(self.conn, 'source/rss/old.md', title=title,
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        add_doc(self.conn, 'source/rss/new.md', title=title,
                published='2026-09-28 12:00', indexed='2026-09-29 07:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertNotIn('source/rss/new.md', self.carried(),
                         '到得比那一份生成还晚,它不可能带走过这一篇')

    def test_a_unique_title_without_a_url_still_gets_its_report(self):
        """反过来钉一边:**没有重名**的无 url 条目照旧认那一份的归属。

        不钉的话一个"无 url 一律不盖章"的实现也全绿,而那等于把补录的归属整类
        丢掉 —— 重新生成那一份时它一条都选不回来(见前面那条覆盖用例)。
        """
        self._chain(carried=[('只有这一篇叫这个名字', '')])
        add_doc(self.conn, 'source/rss/only.md', title='只有这一篇叫这个名字',
                published='2026-09-25 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-09-28'}, self.carried())

    def test_an_upgrade_frees_a_slot_the_previous_revision_had_taken(self):
        """升版那一趟要把上一版**抢错的归属**改回候选,不是只修 `#seed` 那一档。

        盖过章的库走快路径,所以这条缺陷"修好了"只对从空库开始的人成立 —— 而最
        需要修的恰好是已经按旧算法盖错过的那些库。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')])
        add_doc(self.conn, 'source/rss/old.md', title=title,
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        add_doc(self.conn, 'source/rss/new.md', title=title,
                published='2026-09-28 12:00', indexed='2026-09-28 13:00')
        # 上一版真实留下的状态:同名那两篇都被记成"2026-09-28 那一份带走了"
        self.conn.executemany(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            [('source/rss/old.md', '2026-09-28', '2026-09-28 08:00:00'),
             ('source/rss/new.md', '2026-09-28', '2026-09-28 08:00:00')])
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-28 08:00:00'), ('carry_seed_rev', '4')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '抢错的那条归属必须在升级时放回')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked])

    def test_the_last_copy_left_is_no_proof_once_it_postdates_that_report(self):
        """旧那篇被清理之后,「库里独一份」这道判据当场失效。

        `once` 数的是**当前** `documents`:`store.remove_missing_documents` 把旧
        那篇收掉之后,撞了名字的新文章在库里就成了唯一叫这个名字的 —— 上一版那道
        "标题独一份"检查于是放行,归属照旧被旧日报抢走(审查实测:新文章被盖上
        `2026-09-28`、`pending.items` 归零、生成直接答"没有未上报过的新内容")。

        拦下它的是另一个维度:**日报筛的是 `published`**,比那一份的窗口截止还晚
        才发布的内容不可能出现在它的清单里。这一条的摆盘只让这一道成立 —— 新文章
        到得**不**比那一份生成的时刻晚,所以另外两道在这里都是瞎的。
        """
        title = '每日 AI 资讯'
        # 那一份覆盖到 08:00,却是当天 20:00 才生成的(自定义区间 / 延迟生成),
        # 所以下面那篇 13:00 入库的"到得不晚" ⇒ 第二道在这一条上是瞎的。
        self._chain(date='2026-09-27', since='2026-09-26 08:00:00',
                    until='2026-09-27 08:00:00', carried=[(title, '')],
                    generated='2026-09-27 20:00')
        add_doc(self.conn, 'source/rss/old.md', title=title,
                published='2026-09-26 12:00', indexed='2026-09-26 13:00')
        add_doc(self.conn, 'source/rss/new.md', title=title, source='social',
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        # 旧那篇从 vault 里消失了(来源下线、被清理脚本删掉),索引跟着收掉它
        self.assertEqual(1, store.remove_missing_documents(
            self.conn, {'source/rss/new.md'}))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertNotIn('source/rss/new.md', self.carried(),
                         '发布得比那一份的截止还晚,不可能被它带走过')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked],
                      '真的新抓到的那篇必须还选得到')

    def test_the_last_copy_left_is_no_proof_once_it_arrived_too_late(self):
        """同一条缺陷的另一半:只剩一篇、而它是那一份生成之后才入库的。

        这一条的发布时间**落在那一份的窗口里**,所以上一条那道"发布晚于截止"在
        这里是瞎的;分得开的只有到达时间 —— 一份日报只可能带走它生成那一刻已经
        在库里的东西。这正是补抓回一批旧文章之后的真实形态。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')])
        add_doc(self.conn, 'source/rss/old.md', title=title,
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        add_doc(self.conn, 'source/rss/new.md', title=title, source='social',
                published='2026-09-27 20:00', indexed='2026-09-29 07:00')
        self.assertEqual(1, store.remove_missing_documents(
            self.conn, {'source/rss/new.md'}))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertNotIn('source/rss/new.md', self.carried(),
                         '到得比那一份生成还晚,它不可能带走过这一篇')

    def test_an_upgrade_frees_a_slot_the_single_copy_check_had_taken(self):
        """从上一版升级时要把"独一份"那道放行过的归属也改回候选。

        盖过章的库走快路径,所以修好的判据只对从空库开始的人成立 —— 而最需要修的
        恰好是已经按上一版盖错过的那些库(审查实测:从版本 4/5 升级同样失败)。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')])
        add_doc(self.conn, 'source/rss/new.md', title=title, source='social',
                published='2026-09-28 12:00', indexed='2026-09-28 13:00')
        # 上一版(只检查"标题独一份")真实留下的状态
        self.conn.execute(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            ('source/rss/new.md', '2026-09-28', '2026-09-28 08:00:00'))
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-28 08:00:00'), ('carry_seed_rev', '5')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '抢错的那条归属必须在升级时放回')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked])

    def test_arriving_between_two_reports_is_not_that_reports_material(self):
        """补抓到达在**两份日报之间**时,不许被前一份抢走归属。

        这一条治的是「拿整条链的分界当证据」:`stamp` 是链头(或已冻结的那个全局
        分界),它只回答"账本启用之后才到的",答不了"**那一份**生成的时候它在不在
        库里"。于是一篇晚于它匹配的那份日报、却早于后面那几份的补抓文章,三道检查
        全部放行 —— 审查实测:发布 09-27 20:00、入库 09-28 13:00 的新来源同名文章
        被盖上 `2026-09-28`,待报道归零、重新生成也找不回来。

        摆盘只让新那道成立:发布早于 09-28 那份的截止(第三道瞎)、库里标题唯一
        (第一道瞎)、到达早于链头 09-29 08:00(旧那道瞎)。
        """
        title = '每日 AI 资讯'
        self._chain(date='2026-09-28', since='2026-09-27 08:00:00',
                    until='2026-09-28 08:00:00', carried=[(title, '')])
        # 后面还有一份,所以链头(全局分界)落在 09-29 08:00 —— 比下面那篇的到达晚
        self._chain(date='2026-09-29', since='2026-09-28 08:00:00',
                    until='2026-09-29 08:00:00',
                    carried=[('另一篇有链接的', 'https://x/other')])
        add_doc(self.conn, 'source/rss/old.md', title=title,
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        add_doc(self.conn, 'source/dr-gzh/new.md', title=title, source='dr-gzh',
                published='2026-09-27 20:00', indexed='2026-09-28 13:00')
        add_doc(self.conn, 'source/rss/other.md', title='另一篇有链接的',
                url='https://x/other',
                published='2026-09-28 20:00', indexed='2026-09-28 21:00')
        self.assertEqual(1, store.remove_missing_documents(
            self.conn, {'source/dr-gzh/new.md', 'source/rss/other.md'}))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-30 08:00'))
        self.assertNotIn('source/dr-gzh/new.md', self.carried(),
                         '到得比 09-28 那一份生成还晚,它不可能带走过这一篇')
        picked = daily.collect(self.conn, T('2026-09-27 00:00'),
                               T('2026-09-30 08:00')).all()
        self.assertIn('source/dr-gzh/new.md', [i['path'] for i in picked],
                      '真的新抓到的那篇必须还选得到')

    def test_the_report_own_generation_time_counts_not_its_window_end(self):
        """反过来钉一边:判据是那一份**生成的时刻**,不是它的窗口截止。

        自定义历史区间的 `until` 可能比真正生成的那一刻早好几个月(覆盖
        2026-01-01→01-02 的那一份是今天生成的),而它确实带走了今天入库的补录。
        一律拿 `until` 当生成时刻的话,那条合法归属被判成"到得太晚"、放回队列 ——
        重新生成那一份时它一条都选不回来。
        """
        title = '只有这一篇叫这个名字'
        self._chain(date='2026-01-02', since='2026-01-01 00:00:00',
                    until='2026-01-02 00:00:00', carried=[(title, '')],
                    generated='2026-09-28 08:00')
        add_doc(self.conn, 'source/rss/only.md', title=title,
                published='2026-01-01 12:00',   # 落在那一份的窗口里
                indexed='2026-09-27 20:00')     # 晚于 until、早于它真正生成的时刻
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-01-02'}, self.carried(),
                         '它确实是那一份带走的,归属要留着')

    def test_a_document_the_chain_picked_up_after_the_epoch_keeps_it(self):
        """反过来钉一边:到得比**全局分界**晚,但确实被某一份带走过的,归属要留着。

        上一版那道拿分界来问,于是账本启用之后入库、随后真的被一份日报带走的无
        链接文章,在下一次升版重算时归属被整条清掉 —— 它会重新进下一份日报,同一
        条内容报两遍(审查实测:待报道从 0 变 1,又真的进了 09-30 那一份)。
        """
        title = '启用之后真实带走的无链接文章'
        self._chain(date='2026-09-29', since='2026-09-28 08:00:00',
                    until='2026-09-29 08:00:00', carried=[(title, '')],
                    generated='2026-09-29 08:00')
        add_doc(self.conn, 'source/rss/already.md', title=title,
                published='2026-09-28 12:00', indexed='2026-09-28 13:00')
        # 分界线钉在它到达**之前**(账本在 09-28 08:00 就启用过了)
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-28 08:00:00')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-30 08:00'))
        self.assertEqual({'source/rss/already.md': '2026-09-29'}, self.carried(),
                         '那一份生成时它已经在库里,归属是真的')

    def test_an_upgrade_frees_a_slot_the_chain_wide_epoch_had_taken(self):
        """从上一版升级时要把「按全局分界」放行过的归属也改回候选。

        盖过章的库走快路径,所以修好的判据只对从空库开始的人成立 —— 而最需要修的
        恰好是已经按上一版盖错过的那些库(审查实测:从版本 5 升级同样失败)。
        """
        title = '每日 AI 资讯'
        self._chain(date='2026-09-28', since='2026-09-27 08:00:00',
                    until='2026-09-28 08:00:00', carried=[(title, '')])
        self._chain(date='2026-09-29', since='2026-09-28 08:00:00',
                    until='2026-09-29 08:00:00',
                    carried=[('另一篇有链接的', 'https://x/other')])
        add_doc(self.conn, 'source/dr-gzh/new.md', title=title, source='dr-gzh',
                published='2026-09-27 20:00', indexed='2026-09-28 13:00')
        # 上一版(按全局分界判到达)真实留下的状态
        self.conn.execute(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            ('source/dr-gzh/new.md', '2026-09-28', '2026-09-29 08:00:00'))
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-29 08:00:00'), ('carry_seed_rev', '6')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-30 08:00'))
        self.assertEqual({}, self.carried(), '抢错的那条归属必须在升级时放回')
        picked = daily.collect(self.conn, T('2026-09-27 00:00'),
                               T('2026-09-30 08:00')).all()
        self.assertIn('source/dr-gzh/new.md', [i['path'] for i in picked])


if __name__ == '__main__':
    unittest.main()
