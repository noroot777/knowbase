# -*- coding: utf-8 -*-
"""盖掉一份已经有的日报之前,核对**用户批准的是不是那一份**(审查 R4)。

出报的最后一下是整份替换文件,所以盖错一份是**不可恢复**的。而界面上那个确认框
只能按点击那一刻的浏览器日期去问,**那不是写文件时的目标身份**:勾着「先同步一轮」
的那条路要先跑几小时同步,目标日期是同步跑完之后才由服务当前时刻定下来的 ——
浏览器还是 10-19、服务已经 10-20 时,被盖掉的是没人问过的那一份。

判据落在**那份日报的字节**上,不是"某个函数被调用了":

①对不上就一个字节都不许动(跨了天、压根没问过、期间换了一版,三档各一条);
②**反过来钉一边**:对得上照旧覆盖,没有日报可盖时照旧放行,没有人可问的那条路
  (定时轮、命令行)照旧放行 —— 这道闸管的是「问错了天」,不是「该不该出报」;
③闸在**落盘那一段之前**:被拒的那一次不许动账本(旧版的章还在原处)。

跑:  uv run --offline python -m unittest tests.test_daily_overwrite -v
"""
import unittest

from kb import daily, daily_overwrite as dov
from tests.test_daily_carried import T, _Base, add_doc
from tests.test_daily_seed import write_report


class OverwriteApprovalTests(_Base):
    """「要盖的是不是你批准的那一份」这一问本身。"""

    def setUp(self):
        super().setUp()
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))

    def test_a_day_with_no_report_needs_no_approval(self):
        """反过来钉一边:那一天压根没有日报 —— 没有东西可覆盖,一律放行。"""
        dov.check_overwrite('2026-09-28', dov.Approval('2026-01-01', 'x'))
        dov.check_overwrite('2026-09-28', None)

    def test_nobody_to_ask_still_goes_through(self):
        """反过来钉一边:定时轮和命令行没有人可问,照旧放行。

        这一档写严了的后果是日报从此不再自动出现,而面板上一切正常。
        """
        write_report('2026-09-28', generated='2026-09-28 08:30')
        dov.check_overwrite('2026-09-28', None)

    def test_the_approved_version_goes_through(self):
        """反过来钉一边:批的就是这一天这一版 —— 照旧覆盖。"""
        write_report('2026-09-28', generated='2026-09-28 08:30')
        dov.check_overwrite('2026-09-28',
                            dov.Approval('2026-09-28', '2026-09-28 08:30'))

    def test_a_blanket_approval_covers_any_version(self):
        """反过来钉一边:界面明确说过「查不到有没有,要是有就会覆盖」那一档。"""
        write_report('2026-09-28', generated='2026-09-28 08:30')
        dov.check_overwrite('2026-09-28', dov.Approval('2026-09-28', dov.ANY))

    def test_another_days_approval_is_refused(self):
        """**跨零点那一档**:批的是昨天那一份,要写的是今天这一份 —— 拒。"""
        write_report('2026-09-28', generated='2026-09-28 08:30')
        with self.assertRaises(dov.OverwriteRefused) as caught:
            dov.check_overwrite('2026-09-28', dov.Approval('2026-09-27', 'x'))
        self.assertIn('2026-09-28', str(caught.exception), '没说清要写的是哪一天')
        self.assertIn('2026-09-27', str(caught.exception), '没说清批准的是哪一天')

    def test_a_report_nobody_was_asked_about_is_refused(self):
        """**压根没问过**那一档:界面判定那一天没有日报(所以没弹确认框),而盘上有。

        一句过期的「今天还没有日报」会让再点一次生成不弹确认,静悄悄盖掉刚出的
        那一份 —— 空版本只有「真的没有」才放行。
        """
        write_report('2026-09-28', generated='2026-09-28 08:30')
        with self.assertRaises(dov.OverwriteRefused) as caught:
            dov.check_overwrite('2026-09-28', dov.Approval('2026-09-28', ''))
        self.assertIn('没问过', str(caught.exception))

    def test_a_request_carrying_no_approval_at_all_says_the_same_thing(self):
        """请求里压根没带批准(老脚本、直打接口的摆盘):同一档,说同一句话。

        这一档原来落到「你确认的是另一天」那句上,而他哪一天都没确认过 —— 一句
        指不到任何东西的话会让人去改日期,而要做的其实是再确认一次。
        """
        write_report('2026-09-28', generated='2026-09-28 08:30')
        with self.assertRaises(dov.OverwriteRefused) as caught:
            dov.check_overwrite('2026-09-28', dov.Approval('', ''))
        self.assertIn('没问过', str(caught.exception))
        self.assertNotIn('另一天', str(caught.exception))

    def test_a_newer_version_is_refused(self):
        """期间换了一版(定时轮插进来写了一份):批准的那一版已经不在盘上了。"""
        write_report('2026-09-28', generated='2026-09-28 20:00')
        with self.assertRaises(dov.OverwriteRefused) as caught:
            dov.check_overwrite('2026-09-28',
                                dov.Approval('2026-09-28', '2026-09-28 08:30'))
        self.assertIn('换了一版', str(caught.exception))

    def test_a_file_whose_version_cannot_be_read_is_not_taken_as_absent(self):
        """版本读不出来 ≠ 那一天没有日报(那正好放行一次覆盖)。

        带着具体版本的批准要拒;明确问过的那一档(`ANY`)照旧放行。
        """
        daily.report_dir().mkdir(parents=True, exist_ok=True)
        (daily.report_dir() / '2026-09-28.md').write_bytes(
            b'\xff\xfe not utf-8')
        self.assertEqual(dov.UNKNOWN, dov.report_version('2026-09-28'))
        with self.assertRaises(dov.OverwriteRefused):
            dov.check_overwrite('2026-09-28',
                                dov.Approval('2026-09-28', '2026-09-28 08:30'))
        dov.check_overwrite('2026-09-28', dov.Approval('2026-09-28', dov.ANY))


class OverwriteAtTheWriteBoundaryTests(_Base):
    """判据落在**那份日报的字节**上:真走一次 `generate`。"""

    def setUp(self):
        super().setUp()
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))
        add_doc(self.conn, '2026-09-28 09:00', title='新内容')

    def test_the_existing_report_is_not_touched_by_one_byte(self):
        """批准的是另一天 → 那份日报一个字节都不许动,而且模型钱也别花。"""
        write_report('2026-09-28', body='原来那一份', generated='2026-09-28 08:30')
        path = daily.report_dir() / '2026-09-28.md'
        before = path.read_bytes()
        with self.assertRaises(dov.OverwriteRefused):
            daily.generate(since=T('2026-09-28 00:00'), until=T('2026-09-28 23:00'),
                           approval=dov.Approval('2026-09-27', dov.ANY))
        self.assertEqual(before, path.read_bytes(), '被拒的那一次动了那份日报')
        self.assertFalse(self.chat.called, '对不上就别先花那次模型钱')

    def test_the_ledger_is_untouched_too(self):
        """被拒的那一次不许动账本:旧版的章还在原处。

        放回旧章(它会删掉上一版盖的章、而且当场提交)是落盘那一段的第一步,拒在
        它后面就等于把上一版带走的那些素材放回了队列 —— 日报没变,而下一份会把
        它们再报一遍。**这一条由早一道挡着**(它在取材之前就抛了);闸在那一段里
        的位置另有一条钉着(见本组最后那一条的竞态)。
        """
        write_report('2026-09-28', generated='2026-09-28 08:30')
        daily.mark_carried(self.conn, '2026-09-28', ['source/dr-gzh/a.md'],
                           {'source/dr-gzh/a.md': 'sig'})
        before = self.carried()
        self.assertTrue(before, '这一局的前提:上一版盖过章')
        with self.assertRaises(dov.OverwriteRefused):
            daily.generate(since=T('2026-09-28 00:00'), until=T('2026-09-28 23:00'),
                           approval=dov.Approval('2026-09-28', ''))
        self.assertEqual(before, self.carried(), '被拒的那一次动了账本')

    def test_an_approved_overwrite_really_replaces_it(self):
        """反过来钉一边:对得上就照旧覆盖 —— 这道闸不许把正常出报也拦下来。"""
        write_report('2026-09-28', body='原来那一份', generated='2026-09-28 08:30')
        out = daily.generate(since=T('2026-09-28 00:00'), until=T('2026-09-28 23:00'),
                             approval=dov.Approval('2026-09-28', '2026-09-28 08:30'))
        self.assertEqual('2026-09-28', out['date'])
        self.assertNotIn('原来那一份',
                         (daily.report_dir() / '2026-09-28.md').read_text('utf-8'))

    def test_a_report_that_appears_mid_run_is_still_caught(self):
        """**真正那一道闸在落盘之前**:确认之后、落盘之前冒出来一份也要拦住。

        早一道只是把话早点说清楚(省掉那次模型钱),它不是互斥 —— 模型那几分钟里
        定时轮照样能写一份进来。只在起跑处核对的实现这一条必红。
        """
        daily.mark_carried(self.conn, '2026-09-28', ['source/dr-gzh/a.md'],
                           {'source/dr-gzh/a.md': 'sig'})
        before = self.carried()
        self.assertTrue(before, '这一局的前提:上一版盖过章')

        def write_in_between(*a, **kw):
            write_report('2026-09-28', body='定时轮插进来那一份',
                         generated='2026-09-28 20:00')
            return '## 今日要点\n\n- **有事发生**:说明 [1]'
        self.chat.side_effect = write_in_between
        with self.assertRaises(dov.OverwriteRefused):
            daily.generate(since=T('2026-09-28 00:00'),
                           until=T('2026-09-28 23:00'),
                           approval=dov.Approval('2026-09-28', ''))
        self.assertIn('定时轮插进来那一份',
                      (daily.report_dir() / '2026-09-28.md').read_text('utf-8'))
        # **闸要排在放回旧章之前**:那一步当场提交,排在它后面就把上一版带走的
        # 素材放回了队列,而日报一个字没换 —— 下一份会把它们再报一遍。
        self.assertEqual(before, self.carried(), '被拒的那一次动了账本')


if __name__ == '__main__':
    unittest.main()
