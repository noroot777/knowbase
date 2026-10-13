"""启用上报账本那一刻的「一次性盖章」:哪些算历史存量、哪些留在队列里。

从 tests/test_daily_carried.py 拆出来只因为行数(两边合起来过了 700 行上限);
那一份留的是**稳态**(账本已经开着,取材只看"带走过没有"),这一份全是**迁移**
那一刻 —— 摆盘、断言和反向验证的思路都和那边同一套,`_Base` / `T` / `add_doc`
也直接用它的,保证两组摆出来的是同一个世界。
"""
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kb import daily, daily_carry
from tests.test_daily_carried import T, _Base, add_doc


def write_report(date='2026-09-28', since='2026-09-27 08:00:00',
                 until='2026-09-28 08:00:00', body='正文', carried=(),
                 generated='', collected='', credit='RSS·某号', plain=None):
    """摆一份已有的日报:窗口在 frontmatter 里,**带走了哪些在素材清单里**。

    清单按 `daily._page` 的真实格式写(链接目标套尖括号、尾巴是「— 来源·
    账号」),迁移盖章认的就是这一节。不传 `carried` 就是摆一份"读不出清单"
    的日报 —— 它对归属提供不了任何证据,那一档照约定一个章都不盖。

    `generated` 是那一份自己记的**出稿完成**时刻(精度到分钟),`collected` 是
    **取材快照**(秒精度)。都不传就是老日报那种只有窗口的形态 —— 盖章那边按
    `collected` → `generated` → `until` 三档回落(见 `PastReport.latest_pickup`)。

    `credit` 是清单每一行收尾那个「来源·账号」。默认和 `add_doc` 的默认来源
    (`rss` + `某号`)拼出来的逐字相同,所以默认摆盘下第四道判据不会插手 ——
    要验那一道就显式传一个不同的值。

    `plain` 是那一份记下的**带走的无链接素材是哪几篇**(frontmatter 的
    `carried_plain`,按文档路径)。**不传就是老日报那种没有这一行的形态** ——
    盖章那边只好回落到按清单推断;传了的那几份直接按名单盖,一道推断都不走。
    传空列表和不传是同一回事(`_page` 没有无链接素材时压根不写这一行)。

    **模块级函数、两个测试文件共用**(口径只有一份):这一份管「历史存量 / 清单
    解析 / 分界线」,`test_daily_seed_identity.py` 管「无 url 时那条归属匹配」。
    """
    daily.report_dir().mkdir(parents=True, exist_ok=True)
    # `carried` 的每一项是 (标题, url) 或 (标题, url, 是不是补录) —— 补录那个
    # 后缀是 `_page` 另加的标记,盖章那边读「来源·账号」时要摘掉它。
    refs = '\n'.join(
        (f'{i}. [{c[0]}](<{c[1]}>) — {credit}' if c[1] else f'{i}. {c[0]} — {credit}')
        + (' · 补录' if len(c) > 2 and c[2] else '')
        for i, c in enumerate(carried, 1))
    tail = (f'\n\n## 素材清单({len(carried)} 条)\n\n{refs}\n') if carried else ''
    meta = (f'generated: {generated}\n' if generated else '') + \
           (f'collected: {collected}\n' if collected else '') + \
           ('carried_plain:\n' + ''.join(f'- {p}\n' for p in plain) if plain else '')
    (daily.report_dir() / f'{date}.md').write_text(
        f'---\ntype: daily-report\ndate: {date}\n'
        f"window_since: '{since}'\nwindow_until: '{until}'\n{meta}---\n\n{body}\n"
        + tail, encoding='utf-8')


class DailyLedgerSeedTests(_Base):
    """启用账本那一刻的一次性盖章。

    不盖的话账本一启用,库里的历史存量全都算"没上报过",第一份日报的补录候选是
    上千条、要几十份日报才排得空 —— 而那些内容在旧实现下早就永久落选了,翻出来
    不是修复、是倒灌。
    """

    _chain = staticmethod(write_report)

    def test_the_archive_is_written_off_and_attributed_to_its_own_report(self):
        """盖章落到「当初是哪一份带走的」那一天上,不是一律 `#seed`。

        比链起点还早的(和没有发布时间的)记 `#seed`;**那一份清单上有的**记那一
        份的日期 —— 后者是「重新生成那一份时它还选得到」的前提,统一记 `#seed`
        会让覆盖旧日报把它原有的报道一条不剩地抹掉(见后面那条用例)。
        """
        self._chain(carried=[('那一份窗口里的', 'https://x/inside')])
        add_doc(self.conn, 'source/rss/ancient.md', title='上个月的',
                published='2026-08-20 09:00', indexed='2026-08-20 10:00')
        add_doc(self.conn, 'source/rss/inside.md', title='那一份窗口里的',
                url='https://x/inside',
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
                    until='2026-09-28 08:00:00',          # 中间缺了 09-27 那一份
                    carried=[('有归属的', 'https://x/kept')])
        add_doc(self.conn, 'source/rss/gap.md', title='缺口里的',
                published='2026-09-26 20:00', indexed='2026-09-26 21:00')
        add_doc(self.conn, 'source/rss/kept.md', title='有归属的',
                url='https://x/kept',
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
        self._chain(body='## 今日要点\n\n- 甲文 [1]',
                    carried=[('原日报里已有的甲文', 'https://x/a')])
        add_doc(self.conn, 'source/rss/original.md', title='原日报里已有的甲文',
                url='https://x/a',
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

    def test_a_late_entry_the_old_report_carried_is_attributed_to_that_report(self):
        """旧日报里的**补录**要记在它自己那一份头上,不能记成 `#seed`。

        补录的发布时间按定义早于它那一份的窗口(那正是它叫补录的原因),所以
        "按窗口推断归属"会把它当成链条之前的历史存量 —— 而 `#seed` 对任何一天的
        重新生成都满足排除条件,于是覆盖那一份旧日报时它一条都选不回来,已经交付
        的内容当场消失(自由工作流审查第 2 轮在真实页面上复现:原补录没了,产物
        里只剩新补抓的那条)。
        """
        self._chain(body='## 今日要点\n\n- 甲文 [1]\n\n## 补录\n\n- 乙文 [2]',
                    carried=[('窗口里的甲文', 'https://x/a'),
                             ('更早发布的乙文', 'https://x/b')])
        add_doc(self.conn, 'source/rss/a.md', title='窗口里的甲文', url='https://x/a',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        add_doc(self.conn, 'source/rss/b.md', title='更早发布的乙文', url='https://x/b',
                published='2026-09-20 09:00',      # 比链起点(09-27 08:00)还早
                indexed='2026-09-27 10:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/a.md': '2026-09-28',
                          'source/rss/b.md': '2026-09-28'}, self.carried())
        out = daily.generate(since=T('2026-09-27 08:00'), until=T('2026-09-28 08:00'))
        text = Path(out['path']).read_text(encoding='utf-8')
        self.assertIn('更早发布的乙文', text, '重新生成不能抹掉它原有的补录')
        self.assertIn('窗口里的甲文', text)

    def test_an_item_the_old_report_did_not_carry_stays_queued(self):
        """窗口里**当时装不下**的那些留在队列里 —— 它们正是这次要救的那批。

        旧实现的条数上限是丢弃线:一份日报 `total=121` 却只列得下 120 条,落选那条
        的 `published` 从此早于任何后续窗口的起点。迁移要是按窗口推断,就会把整个
        窗口里的现存文章一律记成"报过了",于是那一条再也选不到 —— 原任务要修的
        那条缝在迁移这一步原地复发(自由工作流审查第 2 轮反例)。
        """
        self._chain(carried=[('装下了的', 'https://x/in')])
        add_doc(self.conn, 'source/rss/in.md', title='装下了的', url='https://x/in',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        add_doc(self.conn, 'source/rss/out.md', title='名额装不下的', url='https://x/out',
                published='2026-09-27 19:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/in.md': '2026-09-28'}, self.carried())
        info = daily.pending(first_days=1, now=T('2026-09-29 08:00'))
        self.assertEqual(1, info['late'], '当初没带走的必须还在队列里')
        out = daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.assertIn('名额装不下的', Path(out['path']).read_text(encoding='utf-8'))

    def test_a_real_report_is_read_back_by_its_own_material_list(self):
        """素材清单是**拼装**出来的,所以回归要拿真产物回读一遍。

        手写的摆盘永远跟着实现一起改,证明不了「写」和「读」两侧还对得上
        (`_page` 拼 refs / `_carried_keys` 解析,分隔符共用 `_REF_SEP`)。样本
        故意带上两种硬写法:url 里有圆括号(那正是 `_md_dest` 存在的理由)、
        标题里有方括号(`_md_text` 会转义它)。

        kb.db 是可以删掉重建的索引、日报不是,所以"账本从头来一遍"是真实场景:
        归属只能从那一份日报自己的清单里读回来。
        """
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))   # 先启用
        add_doc(self.conn, 'source/rss/a.md', title='带链接的 [视频] 甲文',
                url='https://x/a?b=1&c=(2)', published='2026-09-28 09:00')
        add_doc(self.conn, 'source/rss/b.md', title='没有链接的乙文',
                published='2026-09-28 10:00')
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual({'source/rss/a.md': '2026-09-29',
                          'source/rss/b.md': '2026-09-29'}, self.carried())

    def test_a_title_containing_a_bracket_link_is_read_back(self):
        r"""标题里自带 `](…)` 时,清单那一行认的仍得是**真**链接那一个。

        `md_text` 把标题里的方括号转义成 `\]`,而"链接从哪儿开始"是非贪婪找最早
        的 `](` —— 不排掉转义过的那一个,读出来的 url 是标题里那半截,这一条的
        归属当场失配(而且 `_closed` 会提前认为这一条读完了,把后面那条也带歪)。
        """
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, 'source/rss/a.md', title='讲个写法 [foo](bar) 的坑',
                url='https://x/real', published='2026-09-28 09:00')
        add_doc(self.conn, 'source/rss/b.md', title='紧跟着的下一条',
                url='https://x/next', published='2026-09-28 08:30')
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual({'source/rss/a.md': '2026-09-29',
                          'source/rss/b.md': '2026-09-29'}, self.carried())

    def test_the_oldest_reports_bare_links_are_still_read(self):
        """最老那几份日报的链接目标是**裸** `(url)`。

        `_md_dest` 是后来才加的(实测生产库里 2026-09-30 那一份整份都是裸写法)。
        只认尖括号的话那一份的归属一条都读不出来,它带走过的内容会被当成"没报过"
        重新倒灌进下一份日报 —— 而这正是审查第 2 轮那份反例摆出来的形状。
        """
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_text(
            '---\ntype: daily-report\ndate: 2026-09-28\n'
            "window_since: '2026-09-27 08:00:00'\n"
            "window_until: '2026-09-28 08:00:00'\n---\n\n正文\n\n"
            '## 素材清单(1 条)\n\n'
            '1. [裸链接的甲文](https://x/a?b=(1)) — RSS·某号\n', encoding='utf-8')
        add_doc(self.conn, 'source/rss/a.md', title='裸链接的甲文',
                url='https://x/a?b=(1)',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/a.md': '2026-09-28'}, self.carried())

    def test_a_ledger_seeded_by_the_old_algorithm_is_recomputed(self):
        """已经盖过章的库必须跟着新算法重算一次。

        账本是懒初始化的,盖过章的库直接走快路径返回 —— 于是修好的归属只对"从空
        库开始"的人成立,而最需要修的恰好是按旧算法盖错过的那些库(自由工作流
        审查第 2 轮点出这一条)。版本号不一致时整张表重算,证据(每一份日报自己的
        素材清单)一直在文件里。
        """
        self._chain(carried=[('装下了的', 'https://x/in')])
        add_doc(self.conn, 'source/rss/in.md', title='装下了的', url='https://x/in',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        add_doc(self.conn, 'source/rss/out.md', title='名额装不下的', url='https://x/out',
                published='2026-09-27 19:00', indexed='2026-09-27 21:00')
        # 旧算法留下的状态:分界线盖着、没有版本标记,窗口里那两条都记成报过了
        self.conn.executemany(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            [('source/rss/in.md', '2026-09-28', '2026-09-28 08:00:00'),
             ('source/rss/out.md', '2026-09-28', '2026-09-28 08:00:00')])
        self.conn.execute('INSERT INTO daily_state(key,value) VALUES(?,?)',
                          ('carry_epoch', '2026-09-28 08:00:00'))
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/in.md': '2026-09-28'}, self.carried())
        self.assertEqual(1, daily.pending(first_days=1,
                                          now=T('2026-09-29 08:00'))['late'])

    def test_a_ledger_from_the_previous_revision_is_recomputed(self):
        """上一版**真实留下**的版本标记也要能升上来。

        改的是解析器而不是分类规则时最容易漏掉这一条:版本号不动,上一版按旧
        解析器盖错过的那些库就命中快路径,修好的解析器一次都跑不到 —— 用户装了
        修复版,原补录照样回不来(真实页面复现:上一版初始化过的库升级后仍然
        少一条)。所以**解析器也是盖章算法的一部分**。
        """
        self._chain(carried=[('[视频] 旧日报带走的补录', '')])
        add_doc(self.conn, 'source/rss/late.md', title='[视频] 旧日报带走的补录',
                published='2026-09-25 20:00', indexed='2026-09-27 21:00')
        # 上一版(解析器读不出裸方括号标题)真实留下的状态:章盖错成了存量
        self.conn.execute(
            'INSERT INTO daily_carried(path,day,carried_at) VALUES(?,?,?)',
            ('source/rss/late.md', daily_carry.SEED_DAY, '2026-09-28 08:00:00'))
        self.conn.executemany(
            'INSERT INTO daily_state(key,value) VALUES(?,?)',
            # 摆的是**上一版**那个号。写死一个更老的值等于把这条用例废掉 ——
            # 版本一升它就不再命中快路径,注入「版本没跟着升」也照样绿。
            [('carry_epoch', '2026-09-28 08:00:00'), ('carry_seed_rev', '4')])
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({'source/rss/late.md': '2026-09-28'}, self.carried(),
                         '上一版盖错的 #seed 必须在升级时改回真正的归属')

    def test_changing_the_material_parser_requires_bumping_the_seed_revision(self):
        """改了清单解析就必须把 `SEED_REV` 加一 —— 这条按名单钉住。

        盖过章的库走快路径,所以"解析修好了"只对从空库开始的人成立。这件事
        没法由行为回归抓住(新版本号下的库怎么跑都是对的),只能钉住**名单**:
        下面这几个符号的逻辑一变,这里就红,提醒把版本号也带上。

        指纹走 AST,所以改注释、改 docstring 不会红;改判据才红。红了之后两处
        一起改:`daily_carry.SEED_REV` 加一,再把新指纹填回来。
        """
        import ast
        import hashlib
        import inspect
        import textwrap

        from kb import daily_page as page
        parts = [page._MATERIAL_ROW.pattern, page._MATERIAL_LINK.pattern,
                 page._ENTRY_TAIL.pattern, page._HEAD_LINE.pattern,
                 page.REF_SEP, page._MATERIAL_HEAD]
        for fn in (page._closed, page._ref_key, page._rows, page._split,
                   page.doc_key, page.flat, page.md_dest, page.carried_keys,
                   page.material_rows, page.material_text):
            node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                node.body.pop(0)                      # docstring 不算逻辑
            parts.append(ast.dump(node))
        self.assertEqual(
            'd86a5367d51a63d2',
            hashlib.sha256('\x00'.join(parts).encode()).hexdigest()[:16],
            '清单解析变了:把 daily_carry.SEED_REV 加一,再把新指纹填到这里')

    def test_a_revision_upgrade_does_not_move_the_epoch(self):
        """算法升版重算时,分界线不许跟着往前挪。

        账本是懒初始化的,分界线钉的是「从这一刻起到达的内容都还没被报道过」。
        重算要修的只是**归属**;改用当下的链头当分界线的话,那些"在旧分界之后
        到达、还排在队列里等补录名额"的文章突然变成"到得不晚",再被发布时间判成
        历史存量、永久写死(审查第 3 轮实测:61 篇补抓回来的被一份日报带走 60 篇,
        剩下 1 篇在升级后彻底消失)。

        反过来钉一边:到得比旧分界线**早**的存量照旧盖 `#seed`,不是一律放行。
        """
        self._chain(carried=[('旧日报带走的', 'https://x/covered')])
        self._chain(date='2026-09-30', since='2026-09-29 08:00:00',
                    until='2026-09-30 08:00:00',
                    carried=[('第二份带走的', 'https://x/second')])
        add_doc(self.conn, 'source/rss/covered.md', title='旧日报带走的',
                url='https://x/covered',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        add_doc(self.conn, 'source/rss/second.md', title='第二份带走的',
                url='https://x/second',
                published='2026-09-29 20:00', indexed='2026-09-29 21:00')
        # 旧分界线之后补抓回来、还没轮到它的那一篇(发布时间早于链起点)
        add_doc(self.conn, 'source/rss/waiting.md', title='补抓回来还在排队的',
                url='https://x/waiting',
                published='2026-09-20 09:00', indexed='2026-09-29 10:00')
        # 比旧分界线还早到的真·存量
        add_doc(self.conn, 'source/rss/ancient.md', title='上个月的',
                published='2026-08-20 09:00', indexed='2026-08-20 10:00')
        # 上一版盖过章、没有版本标记
        self.conn.execute('INSERT INTO daily_state(key,value) VALUES(?,?)',
                          ('carry_epoch', '2026-09-28 08:00:00'))
        self.conn.commit()

        head = daily._ensure_ledger(self.conn, now=T('2026-09-30 12:00'))
        self.assertEqual('2026-09-28 08:00:00', head, '分界线不许往前挪')
        self.assertEqual({'source/rss/covered.md': '2026-09-28',
                          'source/rss/second.md': '2026-09-30',
                          'source/rss/ancient.md': daily.SEED_DAY}, self.carried())
        # 取下一份(10-01)的料:09-30 那一份自己盖的章这时不再豁免
        mat = daily.collect(self.conn, T('2026-09-30 08:00'), T('2026-10-01 08:00'))
        self.assertEqual(['补抓回来还在排队的'], [i['title'] for i in mat.late])

    def test_a_numbered_list_inside_a_title_is_not_a_new_entry(self):
        """标题内部的有序列表不许被拆成独立素材。

        大V观点那一档的"标题"是推文正文,里面常常自带「1. …」「2. …」。按行各认
        各的会把那几行当成新条目,真正那一条的链接跟着被劈成两半、读不到 url ——
        于是已经报道过的观点次日又被取材一遍,发布时间早于链起点的那些干脆记成
        `#seed`、重新生成时一条都选不回来(审查第 3 轮,真实页面复现)。

        **样本是真产物**:先 `generate()` 写出那一份,再把账本从头重建 —— 手写
        摆盘永远跟着解析器一起改,证明不了真实清单读得回来。
        """
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, 'source/social/numbered.md', source='social',
                title='今天分享两个实用技巧\n\n1. 先阅读说明\n2. 再运行示例',
                url='https://x.com/example/status/12345',
                published='2026-09-28 09:00')
        add_doc(self.conn, 'source/rss/plain.md', title='普通的一条',
                url='https://x/plain', published='2026-09-28 10:00')
        out = daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.assertIn('2. 再运行示例](', Path(out['path']).read_text(encoding='utf-8'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual({'source/social/numbered.md': '2026-09-29',
                          'source/rss/plain.md': '2026-09-29'}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-29 08:00'), T('2026-09-30 08:00'))
        self.assertEqual([], mat.all(), '带编号段落的观点不许次日再报一遍')

    def test_deleting_before_the_ledger_is_seeded_returns_the_material(self):
        """账本还没启用就先删一份日报,它报道过的内容必须回到队列。

        "它带走过哪些素材"只写在它自己的文件里。先删再启用的话,那次首访读不到
        这一份,它报道过的内容会按发布时间被判成历史存量、记成 `#seed` 永久写死
        —— 而那正是"升级后第一次打开页面就删掉昨天那份"的真实顺序(审查第 3 轮
        在真实页面上复现:删完再点生成,页面说没有新内容)。
        """
        self._chain(carried=[('删除后应该重新可用的', 'https://x/d')])
        add_doc(self.conn, 'source/rss/d.md', title='删除后应该重新可用的',
                url='https://x/d', published='2026-09-27 20:00',
                indexed='2026-09-27 21:00')
        self.assertTrue(daily.delete('2026-09-28'))
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '日报没了,它的章不许还盖着')
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['删除后应该重新可用的'], [i['title'] for i in mat.late])

    def test_a_delete_that_wins_the_race_to_the_lock_still_returns_it(self):
        """删除抢在首访取锁之前整段跑完,末态同上。

        屏障打在「快路径查过、锁还没取」那一瞬(`_lock_and_seed`),这正是审查描述
        的那个窗口;**不能打在 `_seeded` 上** —— 删除自己现在也要走那个函数,
        屏障会让它等自己。
        """
        self._chain(carried=[('删除后应该重新可用的', 'https://x/d')])
        add_doc(self.conn, 'source/rss/d.md', title='删除后应该重新可用的',
                url='https://x/d', published='2026-09-27 20:00',
                indexed='2026-09-27 21:00')
        started, gone, done = threading.Event(), threading.Event(), []
        real = daily_carry._lock_and_seed

        def paused(*a, **kw):
            started.set()
            gone.wait(5)                  # 这段里删除整段跑完
            return real(*a, **kw)

        def deleting():
            try:
                if started.wait(5):
                    done.append(daily.delete('2026-09-28'))
            finally:
                gone.set()

        t = threading.Thread(target=deleting)
        with patch.object(daily_carry, '_lock_and_seed', paused):
            t.start()
            daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        t.join(10)

        self.assertEqual([True], done)
        self.assertEqual({}, self.carried())
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['删除后应该重新可用的'], [i['title'] for i in mat.late])

    def test_deleting_a_report_while_the_ledger_is_seeded_does_not_strand_it(self):
        """盖章途中删掉一份日报,它带走过的内容必须回到队列。

        「读日报 → 分类 → 落账本」整段要和删除互斥:读完之后另一个页面把其中一份
        删掉(放回它的章 + 删文件),这边再按过期的清单把那些章补回去 —— 那一份
        日报已经不在了,它的内容从此两头落空(自由工作流审查第 2 轮用线程屏障实测
        到这个顺序)。两份日报的列表和窗口是并行请求、删除入口在阅读区一直点得到,
        所以这不是理论上的交错。

        断言落在**末态**上(文件没了 ⇒ 章不许还盖着 ⇒ 下一份捡得回来):只验"锁
        取了没有"的话,一个什么都没防住的实现也能全绿。

        **屏障只拦第一次**(`once`):删除那一侧现在自己也要先把账本盖好(见
        `daily.delete`),再被拦一次的话它在这个窗口里跑不完,测到的就是另一个
        时序 —— 而那个时序靠「锁内再问一次状态」就挡住了,锁本身反倒验不到。
        """
        self._chain(carried=[('那一份带走的', 'https://x/r')])
        add_doc(self.conn, 'source/rss/r.md', title='那一份带走的', url='https://x/r',
                published='2026-09-27 20:00', indexed='2026-09-27 21:00')
        read, done, once = threading.Event(), [], []
        real_chain = daily._report_chain

        def slow_chain():
            out = real_chain()            # 清单已经读进内存,章还没落盘
            if not once:
                once.append(1)
                read.set()
                time.sleep(0.3)           # 给删除线程一个真实的插入机会
            return out

        def deleting():
            read.wait(5)
            done.append(daily.delete('2026-09-28'))

        t = threading.Thread(target=deleting)
        with patch.object(daily, '_report_chain', slow_chain):
            t.start()
            daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        t.join(10)

        self.assertEqual([True], done)
        self.assertFalse((daily.report_dir() / '2026-09-28.md').exists())
        self.assertEqual({}, self.carried(), '日报文件没了,章不能还盖着')
        mat = daily.collect(self.conn, T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(['那一份带走的'], [i['title'] for i in mat.late])

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
        # 反过来钉一边:盖过章之后真生成的那几份,不许被"版本重算"顺手清掉 ——
        # 重算只在版本号不一致那一次跑,而那一次会把它们从清单里再读回来。
        daily.mark_carried(self.conn, '2026-09-29', ['source/rss/b.md'])
        daily._ensure_ledger(self.conn, now=T('2026-10-21 08:00'))
        self.assertEqual('2026-09-29', self.carried().get('source/rss/b.md'))

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
