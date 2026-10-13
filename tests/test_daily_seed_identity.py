"""没有 url 时,那条按标题匹配上的归属算不算数(`daily_carry._match_holds`)。

从 tests/test_daily_seed.py 拆出来只因为行数(合起来过了 700 行上限)。那一份
留的是「历史存量 / 清单解析 / 分界线 / 并发」,这一份只管一件事:`doc_key` 在
url 为空时退化成标题,于是两篇同名文档共享一个身份,而三道判据各排除一种「它
不可能是那一篇」—— 缺任何一道都是**永久漏报**,面板上却写着「没有新内容」。
摆盘、断言和反向验证的思路都和那边同一套,`_Base` / `T` / `add_doc` /
`write_report` 也直接用它们的,保证两组摆出来的是同一个世界。
"""
import unittest
from datetime import datetime
from unittest.mock import patch

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
        add_doc(self.conn, 'source/rss/new.md', title=title,
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
        add_doc(self.conn, 'source/rss/new.md', title=title,
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
        add_doc(self.conn, 'source/rss/new.md', title=title,
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
        add_doc(self.conn, 'source/rss/new.md', title=title,
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
        (第一道瞎)、到达早于链头 09-29 08:00(旧那道瞎)、**来源和清单记的一致**
        (第四道瞎 —— 审查那次实测用的是另一个来源,那会让第四道抢在前面拦住它,
        这一条就验不到时间这一维了)。
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
        add_doc(self.conn, 'source/rss/new.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-28 13:00')
        add_doc(self.conn, 'source/rss/other.md', title='另一篇有链接的',
                url='https://x/other',
                published='2026-09-28 20:00', indexed='2026-09-28 21:00')
        self.assertEqual(1, store.remove_missing_documents(
            self.conn, {'source/rss/new.md', 'source/rss/other.md'}))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-30 08:00'))
        self.assertNotIn('source/rss/new.md', self.carried(),
                         '到得比 09-28 那一份生成还晚,它不可能带走过这一篇')
        picked = daily.collect(self.conn, T('2026-09-27 00:00'),
                               T('2026-09-30 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked],
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
        add_doc(self.conn, 'source/rss/new.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-28 13:00')
        # 上一版(按全局分界判到达)真实留下的状态
        self.conn.execute(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            ('source/rss/new.md', '2026-09-28', '2026-09-29 08:00:00'))
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-29 08:00:00'), ('carry_seed_rev', '6')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-30 08:00'))
        self.assertEqual({}, self.carried(), '抢错的那条归属必须在升级时放回')
        picked = daily.collect(self.conn, T('2026-09-27 00:00'),
                               T('2026-09-30 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked])

    def test_a_document_that_arrived_while_the_model_ran_is_not_material(self):
        """出稿要等模型跑完,那中间入库的文章没被这一份看见过 —— 不许盖章。

        `generated` 记的是**出稿完成**:先读素材、再等模型几十秒到几分钟、最后
        才落盘。拿它当上界的话,取材之后、出稿之前入库的文章满足「到得不晚」,
        却压根没进过模型输入和这一份的清单(审查实测:14:07:16 模型已拿到输入、
        里面没有 B,B 随后入库,14:08 写下 `generated: 14:08`;升级重算把 B 盖成
        那一份的已报道素材,待报道 1 → 0,当天生成直接答「没有未上报过的新内容」)。

        所以判据要的是**取材快照**(`collected`)。摆盘只让这一道成立:来源和清单
        记的一致、发布在窗口里、库里标题唯一、到达也早于 `generated`。
        """
        title = '每日 AI 资讯'
        self._chain(date='2026-09-28', since='2026-09-27 08:00:00',
                    until='2026-09-28 08:00:00', carried=[(title, '')],
                    collected='2026-09-28 08:00:10',   # 素材定下来的那一刻
                    generated='2026-09-28 08:03')      # 模型跑完、出稿落盘
        add_doc(self.conn, 'source/rss/old.md', title=title,
                published='2026-09-27 12:00', indexed='2026-09-27 13:00')
        add_doc(self.conn, 'source/rss/new.md', title=title,
                published='2026-09-27 20:00',
                indexed='2026-09-28 08:01:00')         # 取材之后、出稿之前
        self.assertEqual(1, store.remove_missing_documents(
            self.conn, {'source/rss/new.md'}))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertNotIn('source/rss/new.md', self.carried(),
                         '取材都定下来了它才入库,这一份不可能带走它')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked])

    def test_arriving_in_the_same_minute_as_the_materials_keeps_its_slot(self):
        """反过来钉一边:取材**那一分钟**里入库的,归属要留着。

        `collected` 是秒精度,所以同一分钟里"它到得早一点"分得清。换成只到分钟的
        `generated` 去比字符串,`10:00:05` 字典序大于 `10:00`,一篇确实被带走过的
        文章会被判成"到得太晚"、归属放回,下一份再报一遍(审查实测,列为不拦)。
        """
        title = '只有这一篇叫这个名字'
        self._chain(date='2026-10-12', since='2026-10-11 08:00:00',
                    until='2026-10-12 10:00:20', carried=[(title, '')],
                    collected='2026-10-12 10:00:20', generated='2026-10-12 10:00')
        add_doc(self.conn, 'source/rss/reported.md', title=title,
                published='2026-10-12 09:00', indexed='2026-10-12 10:00:05')
        daily._ensure_ledger(self.conn, now=T('2026-10-13 10:00'))
        self.assertEqual({'source/rss/reported.md': '2026-10-12'}, self.carried(),
                         '它在取材之前就入库了,归属是真的')

    def test_a_source_that_clashes_with_the_list_is_not_that_material(self):
        """清单那一行记着来源和账号 —— 和这篇文档明确冲突时一律拒。

        前三道都只能排除「它不可能是那一篇」;清单里那半截「来源·账号」是唯一的
        **正面**身份证据,丢掉它等于自愿只拿间接证据判身份。审查连着两轮点名:
        无链接匹配仍不核对来源和账号,于是另一个号发的同名文章只要时间对得上就
        被记到旧日报名下。

        摆盘让三道时间判据**全部放行**:入库早于取材、发布在窗口里、库里标题唯一。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], credit='RSS·原来源A',
                    collected='2026-09-28 08:00:10')
        add_doc(self.conn, 'source/rss/new.md', title=title, account='新来源B',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(),
                         '清单记的是原来源A,这一篇是另一个号的')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked])

    def test_a_list_without_a_recorded_source_does_not_veto(self):
        """反过来钉一边:那一份**没记下**来源时,第四道不许插手。

        老格式的日报、或者收尾读不出来的那几行,拿一个没读准的来源去否决真实
        归属就是把「证据缺失」当成「证据冲突」—— 那一份合法带走的素材会被放回
        队列、重新报一遍。写成"读不出就拒"的实现在这一条上会红。
        """
        title = '只有这一篇叫这个名字'
        self._chain(carried=[(title, '')], credit='')   # 收尾那半截整个没有
        add_doc(self.conn, 'source/rss/only.md', title=title, account='某号',
                published='2026-09-25 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-09-28'}, self.carried(),
                         '那一份没记来源,不构成冲突')

    def test_an_upgrade_frees_a_slot_the_generated_stamp_had_taken(self):
        """从上一版升级时要把「按出稿时刻」放行过的归属也改回候选。

        盖过章的库走快路径,所以修好的判据只对从空库开始的人成立 —— 而最需要修的
        恰好是已经按上一版盖错过的那些库。
        """
        title = '每日 AI 资讯'
        self._chain(date='2026-09-28', since='2026-09-27 08:00:00',
                    until='2026-09-28 08:00:00', carried=[(title, '')],
                    collected='2026-09-28 08:00:10', generated='2026-09-28 08:03')
        add_doc(self.conn, 'source/rss/new.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-28 08:01:00')
        # 上一版(拿出稿完成的时刻当上界)真实留下的状态
        self.conn.execute(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            ('source/rss/new.md', '2026-09-28', '2026-09-28 08:03:00'))
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-28 08:00:00'), ('carry_seed_rev', '7')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '抢错的那条归属必须在升级时放回')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/new.md', [i['path'] for i in picked])

    def test_a_late_marker_is_not_part_of_the_recorded_source(self):
        """补录那个后缀不属于「来源·账号」—— 不摘掉就会把真实归属判成冲突。

        `_page` 给补录条目在收尾后面另加一个 ` · 补录`。读清单时不摘掉的话,那一行
        记的来源变成 `RSS·某号 · 补录`、和文档拼出来的 `RSS·某号` 明确不同,第四道
        当场把一条**真实的**归属否掉 —— 而补录正是最该保住归属的那一档(重新生成
        那一份时它一条都选不回来)。
        """
        title = '只有这一篇叫这个名字'
        self._chain(carried=[(title, '', True)])     # 第三项 = 这一条是补录
        add_doc(self.conn, 'source/rss/only.md', title=title,
                published='2026-09-25 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-09-28'}, self.carried(),
                         '补录标记不是来源的一部分')

    def test_the_page_writes_down_when_the_materials_were_taken(self):
        """落盘的 frontmatter 里要有**取材快照**,而且它不等于出稿完成的时刻。

        盖章那边的第二道判据全靠这一行(`PastReport.latest_pickup`);`_page` 不写
        它的话,所有新日报都退回到只有 `generated` 可用 —— 也就是审查复现的那个
        漏报形态。这一条和下一条合起来钉住整条证据链:**写得下来**、而且**取的是
        调模型之前那一刻**。
        """
        add_doc(self.conn, 'source/rss/a.md', title='一篇', url='https://x/a',
                published='2026-09-28 09:00', indexed='2026-09-28 09:30')
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        since, until = T('2026-09-28 08:00'), T('2026-09-29 08:00')
        mat = daily.collect(self.conn, since, until)
        self.assertTrue(mat, '摆盘自检:这一篇要选得上')
        page = daily._page(since, until, mat, '正文', 'test-model', None,
                           T('2026-09-28 08:00:10'))
        # 断言落在**读回来的那一份**上:frontmatter 的序列化会给带冒号的值加引号,
        # 验原始文本只能验到我自己这一行怎么写的,验不到盖章那边拿到的是什么。
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-29.md').write_text(page, encoding='utf-8')
        [past] = daily._report_chain()
        self.assertEqual('2026-09-28 08:00:10', past.taken)
        self.assertEqual('2026-09-28 08:00:10', past.latest_pickup(),
                         '取材快照就是那个上界')
        self.assertTrue(past.made.startswith(f'{datetime.now():%Y-%m-%d}'),
                        '出稿完成照旧记当下 —— 两个时刻是两件事')

    def test_generate_takes_the_snapshot_before_the_model_runs(self):
        """取材快照要在**调模型之前**取 —— 取在之后就等于出稿完成的时刻。

        真实顺序是读素材 → 关连接 → 调模型(几十秒到几分钟)→ 落盘。快照取晚一步,
        模型跑那段时间里入库的文章就全部满足「到得不晚」,而它们一个都没进过这一份
        的输入(审查实测的漏报形态)。这一条按**观测到的先后**断言,不依赖睡够一秒。
        """
        add_doc(self.conn, 'source/rss/a.md', title='一篇', url='https://x/a',
                published='2026-09-28 09:00', indexed='2026-09-28 09:30')
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        seen, asked = {}, []
        real_page = daily._page

        def spy(*a, **k):
            seen['taken'] = a[6] if len(a) > 6 else k.get('taken')
            return real_page(*a, **k)

        def slow_model(*a, **k):
            asked.append(datetime.now())
            return '## 今日要点\n\n- **有事发生**:说明'

        self.chat.side_effect = slow_model
        with patch.object(daily, '_page', spy):
            daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.assertEqual(1, len(asked), '摆盘自检:模型要真被问过一次')
        self.assertIsNotNone(seen.get('taken'), '取材快照压根没传给 _page')
        self.assertLessEqual(seen['taken'], asked[0],
                             '快照取晚了:它必须早于(或等于)模型开跑那一刻')


class CarriedListTests(_Base):
    """日报自己记下的那份名单(`PastReport.plain`):盖章时唯一的硬证据。

    上面那四道推断**加到头也只能排除「它不可能是那一篇」**。证不了「它就是那一
    篇」这件事不是某一道写松了,是这条路本身的上界:同名、同来源、同账号、入库
    早于取材快照、发布在窗口里的两篇文档,四道一道都分不开 —— 而「窗口内 121 条
    素材撞上 120 条上限」在生产库里天天发生,落选的那篇照旧被盖成已报道、从此
    任何一份日报都选不到(审查第 5 轮实测待报道 1 → 0)。

    所以修在上游:`daily._page` 把**实际带走的那几篇无链接素材**按文档路径写进
    frontmatter,盖章直接按名单认。这一组钉的是名单和推断之间的分工。
    """

    _chain = staticmethod(write_report)

    def test_a_report_with_a_list_does_not_stamp_a_namesake_it_left_behind(self):
        """名额装不下而落选的同名同署名文章,不许被那一份盖成已报道。

        这一条是审查第 5 轮那条复现的最小形态:两篇文档**标题、来源、账号逐字
        相同**,一篇进了清单、另一篇因为条数上限落选。落选那篇入库早于取材快照、
        发布在窗口里,而旧那篇被清理之后它在库里成了独一份 —— 四道推断全过。
        分得开它们的只有那一份自己记下的名单。

        断言落在**后果**上:它还捡不捡得回来。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], collected='2026-09-28 08:00:00',
                    plain=['source/rss/picked.md'])
        add_doc(self.conn, 'source/rss/left.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '名单之外的同名文档一个章都不许盖')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/left.md', [i['path'] for i in picked],
                      '落选那篇必须还选得到')

    def test_a_document_on_the_list_keeps_its_report(self):
        """反过来钉一边:名单**上**的那一篇照旧认那一份的归属。

        不钉的话一个"有名单就一律不盖"的实现也全绿,而那等于把这些素材整类放回
        队列 —— 重新生成那一份时它们还在,但下一份日报会把已经报过的再报一遍。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], collected='2026-09-28 08:00:00',
                    plain=['source/rss/picked.md'])
        add_doc(self.conn, 'source/rss/picked.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/picked.md': '2026-09-28'}, self.carried(),
                         '名单上的那一篇就是它带走的,按名单盖')

    def test_the_list_beats_a_material_row_it_cannot_parse(self):
        """名单还顺带兜住「清单那一行解析不出身份」那一档。

        名单在 frontmatter 里,不经过清单解析器。解析器漏一种写法时,原来那一篇
        会连 `#seed` 一起判错(它发布得早、又不在任何清单上),而那是永久写死。
        """
        self._chain(body='正文', plain=['source/rss/odd.md'])
        add_doc(self.conn, 'source/rss/odd.md', title='清单里认不出的那一篇',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/odd.md': '2026-09-28'}, self.carried(),
                         '清单读不出来时,名单仍然答得出"是哪一份带走的"')

    def test_an_old_report_without_a_list_still_falls_back_to_the_judgements(self):
        """反过来钉另一边:**没有名单**的老日报照旧走四道推断。

        不钉的话一个"没名单就一律不盖"的实现也全绿 —— 而生产库里现有的每一份
        日报都没有这一行,那等于账本一升版就把上千条历史归属整批放回、倒灌进下
        一份日报(那正是盖章这件事最初要防的)。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')])
        add_doc(self.conn, 'source/rss/only.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-09-28'}, self.carried(),
                         '老日报没有名单,推断那四道还得照旧管事')

    def test_arriving_in_the_very_second_of_the_snapshot_is_not_proof(self):
        """同**秒**到达算"到得晚"(只对没名单的老日报这条路成立)。

        取材快照和 `indexed_at` 都只到秒,同一秒里谁先谁后分不出来 —— 按元规则
        判不准就扣留。拒了最多多报一遍,盖错是永久写死。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], collected='2026-09-28 08:00:20')
        add_doc(self.conn, 'source/rss/same.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-28 08:00:20')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '同一秒到达分不出先后,不许盖章')

    def test_a_hand_edited_list_cannot_derail_the_stamping(self):
        """手改过的 frontmatter 不许把盖章带崩,也不许盖到别处去。

        日报是 vault 里的 markdown,用户随时会去改。认不出的成员一概丢掉、那一份
        退回按清单推断 —— 不是当场抛(整个账本启用不了),也不是 `str()` 强转一个
        路径出来(那会给一篇没关系的文档盖上章)。
        """
        title = '每日 AI 资讯'
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_text(
            '---\ntype: daily-report\ndate: 2026-09-28\n'
            "window_since: '2026-09-27 08:00:00'\n"
            "window_until: '2026-09-28 08:00:00'\n"
            'carried_plain:\n- 123\n-\n  nested: x\n- \'  \'\n---\n\n正文\n\n'
            f'## 素材清单(1 条)\n\n1. {title} — RSS·某号\n', encoding='utf-8')
        add_doc(self.conn, 'source/rss/only.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-09-28'}, self.carried(),
                         '名单认不出来就退回按清单推断,不许抛也不许瞎盖')

    def test_a_list_that_is_not_even_a_list_falls_back_to_the_material_rows(self):
        """`carried_plain` 被手改成**一行字符串**时,退回按清单推断。

        和上一条不是一回事:上一条是名单里混进认不出的成员,这一条是整份名单压根
        不是个名单。只防 `None` 的实现在这里会**逐字符**迭代那串字符,于是名单变成
        一堆单字符 path(盖不上任何东西)、而"那一份交了名单"这件事成立 —— 推断被
        否决,那一篇一个章都不盖。它发布在链起点之前,下一份日报会把它当补录再报
        一遍;这一条钉的就是别走到那一步。
        """
        title = '每日 AI 资讯'
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_text(
            '---\ntype: daily-report\ndate: 2026-09-28\n'
            "window_since: '2026-09-27 08:00:00'\n"
            "window_until: '2026-09-28 08:00:00'\n"
            'carried_plain: source/rss/only.md\n---\n\n正文\n\n'
            f'## 素材清单(1 条)\n\n1. {title} — RSS·某号\n', encoding='utf-8')
        add_doc(self.conn, 'source/rss/only.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/only.md': '2026-09-28'}, self.carried(),
                         '名单不是个名单就退回按清单推断,不许当成"交了名单"')

    def test_the_page_writes_down_which_unlinked_materials_it_carried(self):
        """跑真实 `generate`:名单落进了文件,而且只记**没有链接**那几条。

        这一条是上面几条的前提 —— 名单是 `_page` 写的,摆盘摆得出来不代表产品
        真的会写。有链接的不记:url 本身就是身份,而 path 会随标题或账号改名而变。
        """
        add_doc(self.conn, 'source/rss/linked.md', title='有链接的一篇',
                url='https://example.org/a', published='2026-09-28 09:00',
                indexed='2026-09-28 09:30')
        add_doc(self.conn, 'source/rss/bare.md', title='没有链接的一篇',
                published='2026-09-28 09:10', indexed='2026-09-28 09:30')
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        [past] = daily._report_chain()
        self.assertEqual(('source/rss/bare.md',), past.plain,
                         '没有链接的那篇要记下路径,有链接的不记')

    def test_upgrading_releases_a_namesake_the_old_revision_had_stamped(self):
        """升版重算要把上一版盖错的那些归属**放回**,不只是改变新盖的行为。

        账本是懒初始化的:已经盖错过的库走快路径直接返回,修好的算法一次都跑不
        到 —— 而最需要修的恰好是那些库。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], collected='2026-09-28 08:00:00',
                    plain=['source/rss/picked.md'])
        add_doc(self.conn, 'source/rss/left.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute("INSERT INTO daily_carried(path,day,carried_at)"
                          " VALUES('source/rss/left.md','2026-09-28','x')")
        self.conn.execute("UPDATE daily_state SET value='8'"
                          " WHERE key='carry_seed_rev'")
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual({}, self.carried(), '上一版盖错的那个章必须被放回')


if __name__ == '__main__':
    unittest.main()
