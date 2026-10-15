# -*- coding: utf-8 -*-
"""一份日报装多少条、写多长 —— 两个上限各有各的理由,混在一处就互相撑爆。

**名额**(`daily.MAX_ITEMS` / `LATE_ITEMS`)答的是"这一份带走哪些条目",上界是模型
吃得下多少素材。要点名额**小于窗口内发布的实际进水量**时,它自己就是积压的来源:
被挤出去的条目在下一个窗口已经走过自己的发布时间,只能落进补录桶,而补录席位
固定 60 个 —— 实测进水 184 条/天(峰值 215)对 120 个席位,每天 50~64 条转成补录,
补录那边净涨 ~24 条/天,**队列排干之后会自己重新涨起来**。

**出稿长度**(`daily_prompt._LATE_CAP`)答的是"这一批条目写成多长的一篇",上界是
`llm.chat` 的 `max_tokens`(8192)。补录那一节原来写死「每条一行」,超过约 150 条时
出稿会从素材清单中间直接截断,**而这一批条目照样全部被盖章**(盖章认的是"进过
这一份的输入",不是"正文里提到了")—— 一份坏产物配一批写死的章,重跑也选不回来。

所以这两组断言都落在后果上:下一轮还剩多少没带走、提示词里那一节许的是什么。
取材账本本身在 tests/test_daily_carried.py,丢弃在 tests/test_daily_drop.py。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_carry, daily_prompt, llm, llm_stages, store

MODEL = llm.Model('openai', 'test-model')


def T(text):
    return datetime.fromisoformat(text)


class _Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'kb.db'
        connect = store.connect
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        self.chat = Mock(return_value='## 今日要点\n\n- **有事**:说明 [1]')
        for p in (patch.object(daily, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(daily_carry, 'DATA_DIR', self.root),
                  patch.object(store, 'connect', lambda *a, **k: connect(self.db)),
                  patch.object(llm_stages, 'stage_model', Mock(return_value=MODEL)),
                  patch.object(llm, 'chat', self.chat)):
            p.start(); self.addCleanup(p.stop)

    def add(self, n, *, first, step_minutes=5, source='rss', prefix='d'):
        """从 `first` 起每隔几分钟发布一条。返回它们的路径。"""
        out = []
        for i in range(n):
            when = (first + timedelta(minutes=step_minutes * i)).strftime('%Y-%m-%d %H:%M')
            path = f'source/{source}/{prefix}{i}.md'
            self.conn.execute(
                'INSERT INTO documents(path,source,title,account,published,url,'
                'summary,indexed_at,extracted_at,promo_level) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (path, source, f'第 {i} 条', '某号', when, '', '摘要',
                 '2026-09-29 09:30', '2026-09-29 09:30', ''))
            out.append(path)
        self.conn.commit()
        return out

    def seed(self):
        """先把账本立起来(空库,一个章都不盖),再加素材 —— 不然 `generate` 里那次
        `_ensure_ledger` 会把已有文章整批盖成 `#seed`,一条候选都不剩。"""
        daily._ensure_ledger(self.conn, now=T('2026-09-01 00:00'))

    def prompt(self):
        """这一轮真发给模型的那段提示词。"""
        self.assertTrue(self.chat.called, '模型压根没被调用')
        return self.chat.call_args[0][0]


class QuotaCoversTheInflowTests(_Base):
    """要点名额要盖得住窗口内发布的进水量,否则它自己制造积压。"""

    def test_a_heavy_day_leaves_nothing_behind(self):
        """实测峰值那一天(215 条窗口内发布)一条都不该被名额挤出去 —— 挤出去的
        在下一个窗口只能落补录,而补录席位是固定的,于是积压单调上涨。"""
        since, until = T('2026-09-29 00:00'), T('2026-09-30 00:00')
        self.add(215, first=T('2026-09-29 01:00'))
        mat = daily.collect(self.conn, since, until, day='2026-09-30')
        self.assertEqual(215, mat.total)
        self.assertEqual(215, len(mat.items), '有条目被名额挤出去了')
        self.assertEqual(0, mat.late_total)

    def test_the_squeezed_out_ones_become_backlog(self):
        """反过来钉一边 —— 名额**不够**时那批条目确实会变成下一轮的补录积压。
        这是上一条要防的那件事,不钉住的话"名额提多少都行"就没有判据。"""
        since, until = T('2026-09-29 00:00'), T('2026-09-30 00:00')
        self.add(215, first=T('2026-09-29 01:00'))
        with patch.object(daily, 'MAX_ITEMS', 120):
            mat = daily.collect(self.conn, since, until, day='2026-09-30')
        self.assertEqual(120, len(mat.items))
        # 没带走的 95 条:下一个窗口里它们的发布时间已经在起点之前 ⇒ 落补录
        later = daily.collect(self.conn, until, until + timedelta(days=1),
                              day='2026-10-01')
        self.assertEqual(0, later.total)
        self.assertEqual(215, later.late_total)

    def test_the_late_bucket_still_caps_what_one_report_carries(self):
        """名额提了,补录那一侧的限流照旧 —— 不是"全带走",而是"每份带 60 条、
        其余留在队列里"(`late_total` 如实报全部,`late` 只装带得走的)。"""
        since, until = T('2026-09-30 00:00'), T('2026-09-30 12:00')
        self.add(100, first=T('2026-09-20 01:00'), step_minutes=60)
        mat = daily.collect(self.conn, since, until, day='2026-09-30')
        self.assertEqual(daily.LATE_ITEMS, len(mat.late))
        self.assertEqual(100, mat.late_total)


class OutputStaysBoundedTests(_Base):
    """出稿长度不许随素材条数线性增长。"""

    def test_a_small_batch_is_still_written_one_line_each(self):
        """少的时候照旧一条一行 —— 退化是为量大准备的,不该顺手把正常那一档
        也改成归并(那会让每天的补录都读不出具体是什么)。"""
        got = daily_prompt.late_section(daily_prompt._LATE_CAP)
        self.assertIn('每条一行', got)
        self.assertNotIn('归并', got)

    def test_a_big_batch_asks_for_a_bounded_number_of_lines(self):
        """量大时许的是"最多 N 行、按来源和主题归并",而**不能**再出现「每条一行」
        —— 后者正是让出稿从素材清单中间断掉的那句。"""
        got = daily_prompt.late_section(305)
        self.assertNotIn('每条一行', got)
        self.assertIn(f'最多 {daily_prompt._LATE_CAP} 行', got)
        self.assertIn('305 条', got)          # 告诉模型这一批有多少,它才好归并

    def test_the_instruction_does_not_grow_with_the_batch(self):
        """这一条才是后果:条数翻 10 倍,那一节的指令长度不许跟着涨
        (涨的话上限就是假的 —— 素材一多,光指令就能把出稿挤没)。"""
        small = len(daily_prompt.late_section(31))
        huge = len(daily_prompt.late_section(3100))
        self.assertLess(huge - small, 20, f'{small} → {huge}')

    def test_generate_really_sends_the_degraded_instruction(self):
        """**接线**:`generate` 得按这一轮补录的实际条数挑指令。写死成一个常量的
        话(旧实现就是),上面三条全绿而真发出去的还是「每条一行」。"""
        since, until = T('2026-09-30 00:00'), T('2026-09-30 12:00')
        self.seed()
        self.add(80, first=T('2026-09-20 01:00'), step_minutes=60)
        daily.generate(since=since, until=until)
        sent = self.prompt()
        self.assertIn(f'最多 {daily_prompt._LATE_CAP} 行', sent)
        self.assertNotIn('每条一行,写清它是什么', sent)

    def test_a_small_batch_keeps_the_per_line_instruction_end_to_end(self):
        """反过来钉一边:补录只有几条时真发出去的仍是「每条一行」。"""
        since, until = T('2026-09-30 00:00'), T('2026-09-30 12:00')
        self.seed()
        self.add(3, first=T('2026-09-29 01:00'), step_minutes=60)
        daily.generate(since=since, until=until)
        sent = self.prompt()
        self.assertIn('每条一行,写清它是什么', sent)
        self.assertNotIn('按来源和主题归并', sent)

    def test_sections_that_grow_with_the_input_are_capped(self):
        """要点名额提到 260 之后,输入翻了一倍 —— 出稿里**每一节都得有上限**,
        否则换一节撑爆而已。工具那一节原来一条没有。"""
        for want in ('3~6 条', '最多 20 行', '1~4 条', '最多 12 行'):
            self.assertIn(want, daily_prompt.PROMPT, want)


if __name__ == '__main__':
    unittest.main()
