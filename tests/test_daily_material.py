"""日报素材清单的「写出去 → 读回来」往返,以及读不回来时那一道兜底。

从 tests/test_daily_seed.py 分出来只因为行数;那边是**盖章判据**(哪些算历史
存量、版本升级怎么重算),这边全是**清单这段文本**:`daily._page` 怎么写、
`daily_page` 怎么读、两侧对不上时会怎样。

**样本一律是真产物**:每条都先 `daily.generate()` 真写一份日报,再把账本从头
重建。手写摆盘永远跟着解析器一起改,证明不了"写"和"读"两侧还对得上 —— 连着
几轮审查找出来的新写法(跨行标题、标题自带有序列表、裸方括号开头、成对反斜杠)
全是真实生成器写得出、而解析器读不回来的那一类。
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from kb import daily, daily_carry, daily_page
from tests.test_daily_carried import T, _Base, add_doc

DAY = '2026-09-29'          # generate(until=09-29 08:00) 写出来的那一份


class MaterialRoundTripTests(_Base):
    """一份日报写出去的素材清单,必须能原样读回它带走了哪些。

    读不回来不是"晚一点再说":那一条交不出归属,随后被发布时间判成链条之前的
    历史存量、记成 `#seed`,而 `#seed` 对任何一天的重新生成都满足排除条件 ——
    用户重新生成那一天的日报时,已经交付过的内容一条都选不回来。
    """

    def _roundtrip(self, title, url='', *, source='rss', companion=True):
        """真写一份日报,再把账本从头重建一次,返回重建出来的章。"""
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, 'source/rss/a.md', title=title, url=url,
                source=source, published='2026-09-28 09:00')
        if companion:
            add_doc(self.conn, 'source/rss/b.md', title='紧跟在后的另一条',
                    url='https://x/companion', published='2026-09-28 08:30')
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        return self.carried()

    def test_a_bare_bracket_title_without_a_link_is_read_back(self):
        """没有 url 的那一支标题是**裸**写进去的,所以它可能以 `[` 开头。

        `_page` 对无链接那一支一个字符都不转义,于是「[视频] …」这种标题在清单里
        长得和链接那一支的开头一模一样。要求它等一个永远不会出现的 `](…)`,下一条
        就被并进来 —— 两条的归属一起丢掉,双双记成 `#seed`(真实页面复现)。
        """
        self.assertEqual({'source/rss/a.md': DAY, 'source/rss/b.md': DAY},
                         self._roundtrip('[视频] 此前已经报道的补录文章'))

    def test_a_title_ending_in_a_backslash_is_read_back(self):
        r"""标题末尾的反斜杠被 `md_text` 写成**成对**的,那不是转义。

        光问「闭括号紧挨着的那一个是不是反斜杠」,`[… C:\\models\\](<url>)` 这条
        合法链接就被拒了,清单跨到下一条去,原补录同样变成 `#seed`。判据得按
        连续反斜杠的**奇偶**数来。
        """
        self.assertEqual({'source/rss/a.md': DAY, 'source/rss/b.md': DAY},
                         self._roundtrip('Windows 本地缓存目录 C:\\models\\',
                                         'https://x/win'))

    def test_a_dash_inside_a_title_does_not_end_the_entry(self):
        """标题里自带一个 ` — ` 不算这一条读完了。

        反过来钉一边:上一条用「有没有分隔符」当结束判据的话,这条推文会在
        「新版发布 — 三个变化」处被切成两半,后半截的编号(推文正文自带的「2.」)
        又恰好接得上,于是真正那一条读不出 url、归属丢掉。收尾认的必须是整个
        `— 来源·账号` 结构,而且只在最后一行上问。
        """
        self.assertEqual(
            {'source/rss/a.md': DAY},
            self._roundtrip('新版发布 — 三个变化\n\n1. 更快\n2. 更稳',
                            'https://x.com/e/status/1', source='social',
                            companion=False))

    def test_a_dotted_phrase_in_a_title_does_not_end_the_entry(self):
        """标题里自带的那种「… — 本期·第 3 篇」写法,长得和收尾一模一样。

        这一条同时钉住两道判据各自的独有射程(**缺一不可**):
        ①**编号得接得上** —— 正文里的「1.」接在真实第 1 条后面时,`_closed` 已经
        被标题里那个假收尾骗过去了,拦住它的只有编号;②**收尾只在最后一行上问**
        —— 轮到正文里的「2.」时编号恰好接得上,拦住它的只有"最后一行没有收尾"。

        样本里**不能有空行**:空行一并回来,"最后一行"就成了空串,`_closed` 自己
        把两道都挡住了,这条用例也就测不到编号那一道(第一版就是这么写的,注入
        编号那道时红 0)。
        """
        self.assertEqual(
            {'source/rss/a.md': DAY},
            self._roundtrip('AI 周报 — 本期·第 3 篇\n1. 第一条\n2. 第二条',
                            'https://x.com/e/status/3', source='social',
                            companion=False))

    def test_a_numbered_line_that_does_not_follow_in_sequence_is_folded_back(self):
        """编号接不上的行不是新的一条。

        清单是 `enumerate(shown, 1)` 写出来的,所以编号必然 1、2、3…… 连着走;
        推文正文里那截有序列表多半从 1 重新数起。两道判据各有各的射程 —— 这一条
        测的是编号那道:正文里的「1.」紧跟在真实第 1 条后面,`_closed` 那道对它
        是瞎的(上一条确实还没读完,可下一行的编号同样不该开新条目)。
        """
        self.assertEqual(
            {'source/rss/a.md': DAY},
            self._roundtrip('今天分享两个技巧\n\n1. 先读说明\n2. 再跑示例',
                            'https://x.com/e/status/2', source='social',
                            companion=False))


class ListHeadingTests(_Base):
    """清单从哪一行开始 —— 「## 素材清单」这几个字在正文里到处都可能出现。

    起点选错一次,**它前面那几条真素材连同它们的 url 一起读不出来**,全被判成
    `#seed`:用户重新生成那一天的日报时,好几篇已经交付过的补录一起消失,而页面
    照样提示生成成功(真实页面复现)。假抬头有两个相反的方向,所以判据不能是
    「取最前」或「取最后」—— 只能问抬头自己声明的条数对不对。
    """

    def _rebuild(self, *docs):
        """真写一份日报,再把账本从头重建,返回重建出来的章。"""
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        for n, (title, url) in enumerate(docs):
            add_doc(self.conn, f'source/rss/{n}.md', title=title, url=url,
                    source='social' if '\n' in title else 'rss',
                    published=f'2026-09-28 0{n + 1}:00')
        daily.generate(since=T('2026-09-28 00:00'), until=T('2026-09-29 08:00'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        return self.carried()

    def test_the_heading_words_inside_a_title_do_not_move_the_start(self):
        """一篇文章的标题里写着这几个字 —— 假抬头落在清单**内部**。

        按「最后一次出现」找起点时它赢了真抬头,于是排在它前面的那几条真素材
        一条都读不出来。
        """
        self.assertEqual(
            {'source/rss/0.md': DAY, 'source/rss/1.md': DAY},
            self._rebuild(('普通的一条旧补录', 'https://x/plain'),
                          ('日报模板中的 ## 素材清单 应该怎样组织', 'https://x/howto')))

    def test_a_markdown_template_in_a_tweet_does_not_move_the_start(self):
        """推文原样贴了一段 markdown 提示词模板,那一行**就在行首**。

        所以「行首」这一道拦不住它,拦得住的只有「抬头自己声明了几条」。
        """
        self.assertEqual(
            {'source/rss/0.md': DAY, 'source/rss/1.md': DAY},
            self._rebuild(
                ('普通的一条旧补录', 'https://x/plain'),
                ('分享我的信息整理提示词\n## 素材清单\n1. 收集原文\n2. 标注出处',
                 'https://x.com/e/status/9')))

    def test_the_heading_words_in_the_summary_do_not_move_the_start(self):
        """反过来钉一边:模型在**要点**里提了这几个字 —— 假抬头落在真抬头前面。

        这一档正是「取最后一次出现」当初要解决的,换判据不许把它弄丢。
        """
        self.chat.return_value = ('## 今日要点\n\n'
                                  '- **有事发生**:这一份的 ## 素材清单 写得很全 [1]')
        self.assertEqual({'source/rss/0.md': DAY},
                         self._rebuild(('普通的一条旧补录', 'https://x/plain')))

    def test_a_fake_heading_with_a_count_before_the_list(self):
        """假抬头带着条数、而且落在真抬头**前面**(模型在要点里照抄了一行)。

        这一档单靠「取最前」会选中它:要点自己是有序列表时,那几行当场被当成
        素材切出来,真清单里的编号反倒接不上、被并进去 —— 归属全错。
        """
        # 模型偶尔会把整份模板抄一遍,连带把这一行原样写进要点里
        self.chat.return_value = (
            '## 今日要点\n\n## 素材清单(9 条)\n\n'
            '1. 第一件事 [1]\n2. 第二件事\n')
        self.assertEqual({'source/rss/0.md': DAY},
                         self._rebuild(('普通的一条旧补录', 'https://x/plain')))

    def test_a_fake_heading_with_a_count_inside_a_title(self):
        """假抬头带着条数、而且落在真抬头**后面**(一条推文里抄了一行)。

        这一档单靠「取最后」会选中它。两条合起来说明:位置选不出真抬头,
        能选出来的只有「它说有几条、就得切出几条」。
        """
        self.assertEqual(
            {'source/rss/0.md': DAY, 'source/rss/1.md': DAY},
            self._rebuild(
                ('普通的一条旧补录', 'https://x/plain'),
                ('日报长这样\n## 素材清单(9 条)\n1. 第一条\n2. 第二条',
                 'https://x.com/e/status/8')))

    def test_a_truncated_list_is_not_read_at_half(self):
        """抬头说有 2 条、实际只切得出 1 条时,整份清单都不作数。

        半截的清单比读不出来更坏:前面那几条交不出归属,随后被发布时间判成历史
        存量、永久写死。读不出来只是多报一遍 —— 而"别写死"这一半靠的是兜底拿到
        **整份正文**(它不经过切分),所以样本的发布时间特意压在链起点之前,好让
        这一条真的走到"历史存量"那一档上。
        """
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_text(
            '---\ntype: daily-report\ndate: 2026-09-28\n'
            "window_since: '2026-09-27 08:00:00'\n"
            "window_until: '2026-09-28 08:00:00'\n---\n\n正文\n\n"
            '## 素材清单(2 条)\n\n'
            '1. [只剩这一条](<https://x/kept>) — RSS·某号\n', encoding='utf-8')
        add_doc(self.conn, 'source/rss/a.md', title='只剩这一条',
                url='https://x/kept',
                published='2026-09-26 20:00', indexed='2026-09-27 21:00')
        daily._ensure_ledger(self.conn, now=T('2026-09-29 08:00'))
        self.assertEqual({}, self.carried(), '切不准就一条都别信,但也别写死')


class UnreadableRowFallbackTests(_Base):
    """清单上露过面的内容,不许被判成「链条之前的历史存量」。

    解析器漏一种写法的后果本来是**不可恢复**的:读不出归属 ⇒ 不盖归属 ⇒ 落到
    "发布早于链起点"那一档 ⇒ `#seed` ⇒ 重新生成时永远选不回来。连着几轮审查
    各找出一种新写法,这份清单不会收敛,所以给结论本身加一道 —— 露过面就别下
    这个终态,代价只是多报一遍。
    """

    def _old_closed(self, text):
        """上一版的条目边界判据(对「裸方括号开头的无链接标题」是错的)。"""
        return (daily_page._MATERIAL_LINK.match(text) is not None
                if text.lstrip().startswith('[') else daily_page.REF_SEP in text)

    def _report_then_rebuild(self, title, published='2026-09-27 20:00'):
        # 两条都发布在链起点之前(都是补录),无链接那条更新 ⇒ 它排清单第一位,
        # 后面紧跟一条有链接的 —— 这正是真实故障的形状。
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, 'source/rss/a.md', title=title, published=published)
        add_doc(self.conn, 'source/rss/b.md', title='紧跟在后的另一条',
                url='https://x/companion', published='2026-09-26 20:00')
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()

    def test_an_entry_the_parser_cannot_read_is_not_written_off(self):
        """解析器又漏了一种写法时,那一条也只是多报一遍,不许被写死。

        这一条的射程是**兜底自己**:把 `_closed` 换回上一版那个读不出裸方括号
        标题的实现(模拟"将来又漏一种"),它的归属照样读不回来 —— 但它在清单上
        露过面,所以不该落 `#seed`,下一份日报还选得到它。

        **这时候整份清单的归属都不作数**:两条被并成一条,切出来的条数和抬头
        声明的对不上,`_rows` 按约定交空的(宁可读不出,不许读出半截)。所以
        紧跟在后那条也一起回到队列 —— 一条都没被写死,代价只是多报一遍。
        """
        title = '[视频] 此前已经报道的补录文章'
        self._report_then_rebuild(title)
        with patch.object(daily_page, '_closed', self._old_closed):
            daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual({}, self.carried(),
                         '读不出归属就该留在队列里,不是记成历史存量')
        mat = daily.collect(self.conn, T('2026-09-29 08:00'), T('2026-09-30 08:00'))
        self.assertIn(title, [i['title'] for i in mat.late])

    def test_another_copy_of_the_same_title_is_still_written_off(self):
        """反过来钉一边:同一个标题在库里有好几份时,标题不作数。

        每天重发的那种稿子(实测一次迁移里 324 条兜底命中,323 条是这个形态)
        只有被带走的那一份该免,剩下几份是**另外的文档**、确实从没报道过 ——
        `#seed` 对它们是正确结论。拿标题兜底会把几百条同名稿子倒灌进后面几份
        日报。
        """
        title = '[视频] 此前已经报道的补录文章'
        self._report_then_rebuild(title)
        add_doc(self.conn, 'source/rss/copy.md', title=title,
                published='2026-09-19 09:00')
        with patch.object(daily_page, '_closed', self._old_closed):
            daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual(daily_carry.SEED_DAY, self.carried().get('source/rss/copy.md'))

    def test_a_short_title_matching_mid_entry_is_still_written_off(self):
        """反过来钉一边:标题撞在某一条的**中间**不算露过面。

        标题总写在一条的开头,而短标题(实测「git」「测试」「-」)在六百多条
        清单里随便就能撞上某一条的中间 —— 裸子串会把一批从没报道过的历史存量
        一起豁免掉,每一条都要多报一遍。
        """
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, 'source/rss/a.md', title='如何用 git 管理配置',
                url='https://x/guide', published='2026-09-28 09:00')
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        add_doc(self.conn, 'source/rss/short.md', title='git',
                published='2026-09-20 09:00')
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        self.assertEqual(daily_carry.SEED_DAY,
                         self.carried().get('source/rss/short.md'))

    def test_a_url_on_the_list_is_enough_on_its_own(self):
        """url 是身份,不受"库里独一份"那道限制。

        同一条内容被两个源收录时标题可能重名,但 url 不会 —— 清单上写着这个
        url,就说明某一份日报确实把它交付出去过。
        """
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, 'source/rss/a.md', title='标题会重名的那一篇',
                url='https://x/only-me', published='2026-09-20 09:00')
        add_doc(self.conn, 'source/rss/b.md', title='紧跟在后的另一条',
                url='https://x/companion', published='2026-09-28 08:30')
        daily.generate(since=T('2026-09-28 08:00'), until=T('2026-09-29 08:00'))
        add_doc(self.conn, 'source/rss/twin.md', title='标题会重名的那一篇',
                url='https://x/someone-else', published='2026-09-18 09:00')
        self.conn.execute('DELETE FROM daily_carried')
        self.conn.execute('DELETE FROM daily_state')
        self.conn.commit()
        with patch.object(daily_page, '_closed', lambda text: False):
            daily._ensure_ledger(self.conn, now=T('2026-09-29 09:00'))
        got = self.carried()
        self.assertNotIn('source/rss/a.md', got, 'url 在清单上,不许写死')
        self.assertEqual(daily_carry.SEED_DAY, got.get('source/rss/twin.md'),
                         '只是同名的另一篇照旧算存量')


class ReportBodyTests(_Base):
    """落盘的那份正文本身(清单写成什么样)。"""

    def test_the_material_list_is_numbered_from_one(self):
        """编号那道判据的前提:清单确实是 1、2、3…… 连着写的。

        它是"编号接不上就不是新条目"这条规则的事实依据,写在这里钉住 ——
        哪天 `_page` 改成别的编号方式,读那一侧必须跟着改。
        """
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        for n in range(3):
            add_doc(self.conn, f'source/rss/{n}.md', title=f'第 {n} 篇',
                    url=f'https://x/{n}', published=f'2026-09-28 0{n + 1}:00')
        out = daily.generate(since=T('2026-09-28 00:00'), until=T('2026-09-29 08:00'))
        body = Path(out['path']).read_text(encoding='utf-8')
        rows = body[body.rfind('## 素材清单'):].splitlines()[2:]
        self.assertEqual(['1', '2', '3'],
                         [r.split('.', 1)[0] for r in rows if r.strip()])


if __name__ == '__main__':
    unittest.main()
