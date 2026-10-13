"""没有 url 的素材,归属只认日报自己记下的那份名单(`daily_carry` 的盖章)。

从 tests/test_daily_seed.py 拆出来只因为行数(合起来过了 700 行上限)。那一份
留的是「历史存量 / 清单解析 / 分界线 / 并发」,这一份只管一件事:`doc_key` 在
url 为空时退化成标题,于是两篇同名文档共享一个身份 —— 而**身份退化之后没有任何
推断能补回来**。

这件事花了五轮才看清。第 2~5 轮逐个加判据(标题在库里独一份 → 到达不晚于那一份
自己的取材快照 → 发布不晚于它的窗口截止 → 清单记的「来源·账号」不冲突),每一轮
都真的堵住了上一轮那个样本,可每一道的形式都是「排除**它不可能是**那一篇」:全过
只说明没有证据否定。第 5 轮的复现证明了上界 —— 名额装不下而落选的那篇同名同署名
文章,在库里能读到的每个字段上都和被带走的那篇相同,四道一道都分不开。第 6 轮又
用 `git archive` 解出版本 8 的代码真实生成日报、真实升级,复现了同一条:**而旧
日报恰恰是升级时手里唯一存在的形态**。

所以判据整个退役,改成:有 url 就按 url 认(url 本身是身份),没有 url 就只认那一
份自己记下的名单(`carried_plain`),名单上没有就按证据不足办、留在队列里。代价是
老日报带走过的无链接素材会各多报一遍(生产库实测 11 条),换掉的是「从此再也选不
到」这个不可恢复的终态 —— 这一份里有一条用例专门钉住这笔交换的两头。

摆盘、断言和反向验证的思路都和那边同一套,`_Base` / `T` / `add_doc` /
`write_report` 也直接用它们的,保证两组摆出来的是同一个世界。
"""
import unittest
from datetime import datetime
from unittest.mock import patch

from kb import daily
from kb.scanner import parse_frontmatter
from tests.test_daily_carried import T, _Base, add_doc
from tests.test_daily_seed import write_report


class NoUrlAttributionTests(_Base):
    """无 url 的标题命中一律不盖章,以及这件事的两头代价。"""

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

    def test_a_report_that_left_a_namesake_behind_does_not_stamp_it(self):
        """名额装不下而落选的同名同署名文章,不许被那一份盖成已报道。

        第 5 轮那条复现的最小形态:两篇文档**标题、来源、账号逐字相同**,一篇进了
        清单、另一篇因为条数上限落选。落选那篇入库早于取材快照、发布在窗口里,而
        旧那篇被清理之后它在库里成了独一份 —— 退役掉的那四道判据**全过**。

        摆的是**老日报**(没有 `carried_plain` 那一行):生产库里现有的每一份都是
        这个形态,也是升级时手里唯一存在的形态。断言落在**后果**上:它还捡不捡得
        回来。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], collected='2026-09-28 08:00:00')
        add_doc(self.conn, 'source/rss/left.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '同名不是身份,一个章都不许盖')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/left.md', [i['path'] for i in picked],
                      '落选那篇必须还选得到')

    def test_a_url_in_the_list_is_still_proof(self):
        """反过来钉一边:**有 url** 的素材照旧认那一份的归属。

        不钉的话一个"一律不盖任何章"的实现也全绿 —— 而那会让每一份日报的全部素材
        在升级后重新变成候选,整批倒灌进下一份(盖章这件事最初就是为了防它)。url
        本身就是身份:同一条内容被两个源收录时标题可能重名,url 不会。
        """
        self._chain(carried=[('有链接的那一篇', 'https://example.org/a')])
        add_doc(self.conn, 'source/rss/a.md', title='有链接的那一篇',
                url='https://example.org/a', published='2026-09-27 20:00',
                indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/a.md': '2026-09-28'}, self.carried(),
                         'url 是身份,这条归属读得回来')

    def test_a_namesake_on_a_list_is_never_written_off_as_archive(self):
        """清单上露过面的无链接素材,**一个章都不盖**不等于可以判成历史存量。

        两件事差着「补不补得回来」:留在队列里是下一份多报一遍,判成 `#seed`
        (链条之前的存量)是**写死** —— `#seed <> 那一天` ⇒ 重新生成那一份时
        `UNCARRIED` 把它排除掉。所以清单命中之后一律 `continue`,不往下走存量
        那一档。

        `_on_a_list` 那道兜底在这里**接不住**:它的标题支要求"库里独一份"
        (同稿副本不许靠标题豁免),而这一条摆的正是同名两篇 —— 少了 `continue`
        两篇会一起落 `#seed`。
        """
        title = '某一份老日报带走过的无链接补录'
        self._chain(carried=[(title, '', True)])
        for name in ('a', 'b'):                      # 同名两篇,兜底的标题支失效
            add_doc(self.conn, f'source/rss/{name}.md', title=title,
                    published='2026-09-20 09:00', indexed='2026-09-27 10:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '不盖章,更不许判成历史存量')
        again = daily.collect(self.conn, T('2026-09-27 08:00'),
                              T('2026-09-28 08:00'), day='2026-09-28').all()
        self.assertEqual({'source/rss/a.md', 'source/rss/b.md'},
                         {i['path'] for i in again},
                         '两篇都要还选得到(落了 #seed 就一篇都选不到)')

    def test_an_old_reports_unlinked_material_is_reported_once_more(self):
        """这笔交换的两头,一条用例一起钉住。

        老日报真的带走过的无链接素材,升级后归属放回。**代价**是下一份日报会把它
        多报一遍;**换到的**是它没有被写死 —— 重新生成那一份时照旧选得到。

        前几轮那些「期望盖章」的用例都论证过"不盖章等于把补录归属整类丢掉、重新
        生成那一份时一条都选不回来"。那个论证只对**盖错成 `#seed`** 成立
        (`#seed <> 那一天` ⇒ `UNCARRIED` 排除它),对「一个章都不盖」不成立:
        没有任何章的文档 `NOT EXISTS` 必然放行。这一条把三件事一起测了,所以那个
        混淆不会再回来。
        """
        title = '某一份老日报带走过的无链接补录'
        self._chain(carried=[(title, '', True)])
        add_doc(self.conn, 'source/rss/late.md', title=title,
                published='2026-09-20 09:00', indexed='2026-09-27 10:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '证据不足 ⇒ 放回队列')
        again = daily.collect(self.conn, T('2026-09-27 08:00'),
                              T('2026-09-28 08:00'), day='2026-09-28').all()
        self.assertIn('source/rss/late.md', [i['path'] for i in again],
                      '重新生成那一份时它必须还选得到(没有被写死)')
        nxt = daily.collect(self.conn, T('2026-09-28 08:00'),
                            T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/late.md', [i['path'] for i in nxt],
                      '代价如实记着:下一份会把它多报一遍')

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

    def test_the_page_writes_down_when_the_materials_were_taken(self):
        """落盘的 frontmatter 里要有**取材快照**,而且它不等于出稿完成的时刻。

        `collected` 现在没有程序消费方了(盖章不再做任何时间推断),留着是因为它是
        日报文件里**给人看的事实**:这一份的素材是什么时候定下来的。和 `generated`
        (出稿完成,只到分钟、面板列表在读)差着一次模型请求,两个时刻不许混。这一条
        和下一条合起来钉住这个字段的语义:**写得下来**、而且**取的是调模型之前那
        一刻**。
        """
        add_doc(self.conn, 'source/rss/a.md', title='一篇', url='https://x/a',
                published='2026-09-28 09:00', indexed='2026-09-28 09:30')
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        since, until = T('2026-09-28 08:00'), T('2026-09-29 08:00')
        mat = daily.collect(self.conn, since, until)
        self.assertTrue(mat, '摆盘自检:这一篇要选得上')
        page = daily._page(since, until, mat, '正文', 'test-model', None,
                           T('2026-09-28 08:00:10'))
        # 断言走**真实的读取口径**(`parse_frontmatter`),不验原始文本 —— 序列化
        # 会给带冒号的值加引号,验文本只能验到我自己这一行怎么写的。
        meta, _ = parse_frontmatter(page)
        self.assertEqual('2026-09-28 08:00:10', str(meta.get('collected')))
        self.assertTrue(str(meta.get('generated')).startswith(
            f'{datetime.now():%Y-%m-%d}'),
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
    """日报自己记下的那份名单(`PastReport.plain`):无 url 素材唯一的身份证据。

    名单是版本 9 才开始写的,所以**升级时手里的每一份日报都没有它** —— 那条路上
    一度留着四道推断做回落,而第 6 轮用真实版本 8 的代码证明了那等于原地复发。
    现在没有名单就是没有证据,一个章都不盖。
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

    def test_a_partly_unreadable_list_still_stamps_what_it_can(self):
        """手改过的名单里混进认不出的成员时,名单上**其余**那些仍然盖得上。

        日报是 vault 里的 markdown,用户随时会去改。认不出的成员一概丢掉 —— 不是
        当场抛(整个账本启用不了),也不是 `str()` 强转一个路径出来(那会给一篇没
        关系的文档盖上章),更不是让整份名单失效(名单上真实那几条跟着放回,各多报
        一遍)。
        """
        title = '每日 AI 资讯'
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_text(
            '---\ntype: daily-report\ndate: 2026-09-28\n'
            "window_since: '2026-09-27 08:00:00'\n"
            "window_until: '2026-09-28 08:00:00'\n"
            'carried_plain:\n- source/rss/good.md\n- 123\n-\n  nested: x\n'
            "- '  '\n---\n\n正文\n\n"
            f'## 素材清单(1 条)\n\n1. {title} — RSS·某号\n', encoding='utf-8')
        add_doc(self.conn, 'source/rss/good.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/good.md': '2026-09-28'}, self.carried(),
                         '认不出的丢掉就好,名单上真实那几条照旧盖')

    def test_a_list_that_is_not_even_a_list_does_not_break_the_stamping(self):
        """`carried_plain` 被手改成一个**不可迭代**的值时,不许抛。

        判据是 `isinstance(value, (list, tuple))` 而不是"防一下 `None`" ——
        frontmatter 是 yaml,`carried_plain: 123` 解析出来是 `int`,`for v in 123`
        当场 `TypeError`,而它坐在 `_report_chain` 那条路上:**整个账本启用不了**,
        面板上看到的是一句和日报毫无关系的报错。字符串那一档同理(会逐字符迭代成
        一堆单字符 path),只是它不抛。
        这一条拿**有 url 的素材**当探针:名单当没有,整份日报别的证据照旧生效。
        """
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_text(
            '---\ntype: daily-report\ndate: 2026-09-28\n'
            "window_since: '2026-09-27 08:00:00'\n"
            "window_until: '2026-09-28 08:00:00'\n"
            'carried_plain: 123\n---\n\n正文\n\n'
            '## 素材清单(1 条)\n\n'
            '1. [有链接的那一篇](<https://example.org/a>) — RSS·某号\n',
            encoding='utf-8')
        add_doc(self.conn, 'source/rss/a.md', title='有链接的那一篇',
                url='https://example.org/a', published='2026-09-27 20:00',
                indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/a.md': '2026-09-28'}, self.carried(),
                         '名单不是个名单就当没有,别的证据照旧算')

    def test_a_genuine_legacy_report_does_not_consume_the_namesake(self):
        """真实的旧格式升级:**没有名单的日报 + 上一版的账本**。

        这一条是第 6 轮那条复现的回归。上一轮那条升级用例摆的是「带名单的日报 +
        版本 8 的账本」—— **那个组合在现实里不存在**(版本 8 的代码压根不写名单),
        所以它只证明了"新格式重算",证不了"旧格式升级",而升级恰恰是唯一会发生的
        那条路。摆盘因此一个字都不能给 `plain`。
        """
        title = '每日 AI 资讯'
        self._chain(carried=[(title, '')], collected='2026-09-28 08:00:00')
        add_doc(self.conn, 'source/rss/left.md', title=title,
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        # 上一版(回落到四道推断)真实留下的状态:落选那篇被盖成已报道
        self.conn.execute(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            ('source/rss/left.md', '2026-09-28', '2026-09-28 08:00:00'))
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            [('carry_epoch', '2026-09-28 08:00:00'), ('carry_seed_rev', '9')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '上一版盖错的那个章必须放回')
        picked = daily.collect(self.conn, T('2026-09-28 08:00'),
                               T('2026-09-29 08:00')).all()
        self.assertIn('source/rss/left.md', [i['path'] for i in picked],
                      '放回之后下一份日报要带得到它')


if __name__ == '__main__':
    unittest.main()
