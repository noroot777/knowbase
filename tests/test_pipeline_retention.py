"""历史按**天**留:三天之内跑过的那几轮全查得到,**一条都不删**。

判据一律落在**「那一轮还查不查得到」**上 —— 这一份存在的全部意义就是回查。

从 `test_pipeline_history.py` 里分出来的:那边验「一轮的红在下一轮跑完之后还在不在」
(**留了什么**),这边验「什么时候该把它清掉」(**留多久、留多大**)。分开只为行数,
两边共用同一个 `HistoryCase`;摆局手法也不同 —— 这一族要往文件里摆几天前那几轮,而
`run_round` 一律按此刻落时间。

**各道口径各留一份红,不许合在一条用例里验**:窗口写错了会被下限兜绿,反过来也一样。
所以验窗口的那几条把 `ret._HISTORY_FLOOR` 让开,验下限的那条自己把窗口逼到全都过期。
体量那道只削重物、不删记录,所以它的判据分成两半:**削了什么**和**那一轮还在不在**。
"""
import json
from datetime import datetime, timedelta
from unittest import main
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline_retention as ret, pipeline_state as state
from kb.pipeline_persist import _dump_bytes
from kb.web import pipeline_api
from tests.pipeline_case import HistoryCase


class _Clock:
    """把 `pipeline_retention` 看到的 `datetime.now()` 拨快几年,别的一概照旧。

    **只换 now,不整份换掉 datetime** —— `strptime` 还要照常解析记录里那些时刻,
    换整份的话解析先炸,证不出「窗口判出全都过期之后下限还在不在」。
    """

    def __init__(self, years_ahead):
        self._ahead = years_ahead

    def now(self):
        return datetime.now() + timedelta(days=365 * self._ahead)

    def strptime(self, *a, **kw):
        return datetime.strptime(*a, **kw)


class RetentionTests(HistoryCase):
    def _write(self, runs):
        state._history_file().parent.mkdir(parents=True, exist_ok=True)
        state._history_file().write_text(json.dumps({'runs': runs}, ensure_ascii=False),
                                         encoding='utf-8')

    def _seed(self, *stamps):
        """往历史文件里摆几条**已有**记录(倒序,最新在前)。

        不走 `run_round` 是因为它一律按此刻落时间 —— 要摆的恰恰是几天前那几轮。
        """
        self._write([self._legacy(f'old-{at}', at, failed=False, started=at) for at in stamps])

    @staticmethod
    def _ago(days=0, hours=0, seconds=0):
        return (datetime.now() - timedelta(days=days, hours=hours, seconds=seconds)
                ).strftime('%Y-%m-%d %H:%M:%S')

    def test_rounds_outside_the_window_are_dropped(self):
        """窗口是主判据:三天之内的留着,之外的清掉。"""
        inside, outside = self._ago(days=2), self._ago(days=5)
        self._seed(inside, outside)
        # **把下限让开,这一条只验窗口。** 下限自己那一条在下面(`_a_clock_jump_...`)
        with patch.object(ret, '_HISTORY_FLOOR', 1):
            self.run_round('fresh', 'ok', '新的一轮')
        kept = [r['run_id'] for r in state.load_history()]
        self.assertIn(f'old-{inside}', kept, '三天之内那一轮还要查得到')
        self.assertNotIn(f'old-{outside}', kept, '五天前那一轮该清掉了')

    def test_a_busy_day_does_not_squeeze_out_the_day_before(self):
        """**这条改动的来由,反过来钉一边。** 一天里点二十次重试,按条数封顶的旧口径
        会把前天那一轮连同它的红一起挤掉 —— 而那正是最想回查的时候。
        """
        yesterday = self._ago(days=1)
        self._seed(yesterday)
        for i in range(20):
            self.run_round(f'busy-{i}', 'ok', f'第 {i} 次重试')
        kept = [r['run_id'] for r in state.load_history()]
        self.assertIn(f'old-{yesterday}', kept, '昨天那一轮不许被今天这二十轮挤掉')
        self.assertEqual(21, len(kept), '二十轮加昨天那一轮,一条都没少')

    def test_four_hundred_rounds_inside_the_window_all_stay(self):
        """**窗口之内不许有任何条数上限。**(第 1 轮审查判红的那一条。)

        原来窗口裁完还无条件 `kept[:400]`:400 轮全落在 66 小时 40 分之内、再跑一轮,
        最老那一轮就被删掉 —— 而它距离三天到期还有五个多小时,面板上一个字不说。
        **不是「400 太小」**:任意硬条数上限都优先于时间窗,换个更大的数字照样是
        「保留最近 N 轮」,而用户要的是「保留三天」。
        """
        stamps = [self._ago(hours=h * 10 / 60) for h in range(1, 401)]
        self._seed(*stamps)
        self.run_round('fresh', 'ok', '第 401 轮')
        kept = [r['run_id'] for r in state.load_history()]
        self.assertEqual(401, len(kept), '401 轮全在窗口里,一条都不许删')
        self.assertEqual(f'old-{stamps[-1]}', kept[-1], '最老那一轮还在')

    def test_records_without_a_readable_time_are_kept(self):
        """**判不出时间的一律留着。** 丢一条历史不可恢复(那一轮的红没有第二个地方
        查得到),多留一条只是多一行。三种形态各钉一个:没有字段、不是时间、不是字符串。
        """
        self._write([{'run_id': 'no-field', 'stages': {}, 'finished_at': ''},
                     {'run_id': 'not-a-time', 'stages': {}, 'finished_at': '前天下午'},
                     {'run_id': 'numeric', 'stages': {}, 'finished_at': 1760000000}])
        # 同上:下限让开,否则这三条正好都落在它射程里,留住了也证不出是哪一道留的
        with patch.object(ret, '_HISTORY_FLOOR', 1):
            self.run_round('fresh', 'ok', '新的一轮')
        kept = [r['run_id'] for r in state.load_history()]
        for rid in ('no-field', 'not-a-time', 'numeric'):
            self.assertIn(rid, kept, f'{rid}:判不出时间不等于过期')

    def test_a_clock_jump_does_not_wipe_the_history(self):
        """**下限兜住墙上时钟。** 时钟往前跳(手动改、NTP 校正、换时区)的那一刻
        整份历史看着全都过期了 —— 而那恰恰是最不该清空它的时刻。
        """
        stamps = [self._ago(days=d) for d in range(1, 6)]
        self._seed(*stamps)
        with patch.object(ret, '_HISTORY_FLOOR', 3):
            with patch.object(ret, 'datetime', _Clock(years_ahead=3)):
                self.run_round('after-jump', 'ok', '时钟跳过之后这一轮')
        kept = [r['run_id'] for r in state.load_history()]
        self.assertEqual('after-jump', kept[0], '新那一轮在最前')
        self.assertEqual(3, len(kept), '下限那几轮留住了,不是一条不剩')

    def test_the_byte_guard_sheds_bulk_and_keeps_every_round(self):
        """体量超了**削重物、不删记录**:那一轮的结果和它的红照旧查得到。

        判据分两半,**少哪一半都会让另一半的红没人接**:记录还在(这一条),以及削掉的
        确实是重物而不是结论(下一条)。
        """
        fat = [self._legacy(f'fat-{i}', self._ago(hours=i + 1), failed=True,
                            started=self._ago(hours=i + 1)) for i in range(12)]
        for run in fat:
            run['log'] = [f'第 {n} 行日志' * 20 for n in range(80)]
        self._write(fat)
        with patch.object(ret, '_HISTORY_BYTES', 20_000):
            self.run_round('fresh', 'ok', '新的一轮')
        kept = state.load_history()
        self.assertEqual(13, len(kept), '一条记录都不许删 —— 它的红没有第二个地方查得到')
        self.assertEqual('fat-11', kept[-1]['run_id'], '最老那一轮还在')
        self.assertEqual('error', kept[-1]['stages']['export']['status'], '它那个红还在')
        self.assertIn('503', kept[-1]['stages']['export']['detail'], '红的原因还在')
        self.assertEqual([], kept[-1]['log'], '削掉的是日志正文')
        self.assertTrue(kept[0]['log'], '最新那一轮(下限之内)一个字都没削')

    def test_shedding_details_still_reports_the_real_total(self):
        """明细削到前几条时**总数要说实话**(`failures_total`)。

        面板按 `failures_total || failures.length` 念「明细 · N 项(仅列前 M 条)」——
        不补总数的话 50 条明细被削成 3 条之后它写的是「明细 · 3 项」,削过和没削过长得
        一模一样,而那一轮真有 50 条。
        """
        fails = [{'title': f'第 {n} 篇', 'reason': 'HTTP 503 from the model gateway'}
                 for n in range(50)]
        runs = [self._legacy(f'r-{i}', self._ago(hours=i + 1), failed=True,
                             started=self._ago(hours=i + 1)) for i in range(6)]
        for run in runs:
            run['log'] = []
            run['stages']['export']['failures'] = [dict(f) for f in fails]
        self._write(runs)
        with patch.object(ret, '_HISTORY_BYTES', 6_000):
            self.run_round('fresh', 'ok', '新的一轮')
        oldest = state.load_history()[-1]['stages']['export']
        self.assertEqual(3, len(oldest['failures']), '削到前 3 条')
        self.assertEqual(50, oldest['failures_total'], '总数照旧是 50')

    def _fat(self, count, *, failures=50, log=80):
        """摆 `count` 条**带满重物**的旧记录(各自带日志和明细),最新在前。"""
        runs = [self._legacy(f'fat-{i}', self._ago(hours=i + 1), failed=True,
                             started=self._ago(hours=i + 1)) for i in range(count)]
        for run in runs:
            run['log'] = [f'第 {n} 行日志' * 20 for n in range(log)]
            run['stages']['export']['failures'] = [
                {'title': f'第 {n} 篇', 'reason': 'HTTP 503 from the model gateway ' * 4}
                for n in range(failures)]
        self._write(runs)
        return runs

    def test_shedding_costs_one_pass_over_the_records_not_one_per_shed(self):
        """削重物的**成本必须是线性的**:改一条只重编那一条。(第 2 轮审查判红的那条。)

        这一步跑在收尾那把锁里,而状态端点要同一把锁 —— 慢在这儿就是页面读不到状态。
        原来每削一条都把整份重新编码一遍:420 轮带满明细(约 10 MB)实测累计编码
        5.2 GB、收尾 29.6 秒,面板那条请求 15 秒就超时报「连接失败」。

        **判据是累计编码的字节数,不是墙上时间。** 这个规模下两种实现都在一秒内跑完
        (时间分不开),而字节数差两个数量级:线性那版约 3 倍输入,每削一条重编整份
        那版约 2×(轮数) 倍。所以判在字节上,并且只要求「同一个数量级」—— 预算本来
        就是数量级口径。
        """
        payload = len(_dump_bytes({'runs': self._fat(60)}))
        seen = []
        real = ret._dump_bytes

        def counted(value):
            out = real(value)
            seen.append(len(out))
            return out

        with patch.object(ret, '_HISTORY_BYTES', 20_000), \
             patch.object(ret, '_dump_bytes', counted):
            self.run_round('fresh', 'ok', '新的一轮')
        self.assertLess(sum(seen), 4 * payload,
                        f'累计编码 {sum(seen)} 字节,输入才 {payload} —— 退回按整份重编了')
        self.assertEqual(61, len(state.load_history()), '削完一条记录都不许少')
        self.assertEqual('', state.persist_error(), '这一步不许把留档搞失败')

    def test_one_record_with_a_broken_stages_field_does_not_block_archiving(self):
        """**坏行只许坏自己。**(第 2 轮审查判红的那条。)

        落盘那一层只保证每条记录是字典,**没保证 `stages` 也是字典**。历史里一条
        `stages` 落成非空数组的记录,削明细时 `.values()` 当场抛 AttributeError,
        由收尾那层接住报「留档失败」—— 于是**整份历史一个字节都没写进去**:那条坏行
        还在,下一轮超预算时又抛一次,新跑的几轮从此全都查不到。

        判据落在后果上:新那一轮查得到、别的记录还在、诊断是空的。顺带钉住既有契约
        的另一半 —— 坏行**自己**也留着(解析和面板那边按「标成无效、只跳过这一行」
        处理,删掉它才是静默丢数据)。
        """
        runs = self._fat(12)
        runs[-1]['stages'] = ['export: 完成']        # 非空数组:`.values()` 抛的那一档
        runs[-2]['stages'] = 'export ok'             # 字符串也走同一条路
        self._write(runs)
        with patch.object(ret, '_HISTORY_BYTES', 20_000):
            self.run_round('fresh', 'ok', '新的一轮')
        self.assertEqual('', state.persist_error(), '一条坏行不许让整份留档失败')
        kept = state.load_history()
        self.assertEqual('fresh', kept[0]['run_id'], '新那一轮写进去了')
        self.assertEqual(13, len(kept), '谁也没被删')
        self.assertEqual(['export: 完成'], kept[-1]['stages'], '坏行自己照旧留着')

    def test_the_panel_reads_the_retention_window_from_the_backend(self):
        """面板上那句「保留最近 N 天」从后端取,不在前端另写一个数字。

        **判据落在回包上,不是落在「两个名字相等」上** —— 后者是重导出的同义反复,
        端点里写死一个 7 它照样全绿。
        """
        app = FastAPI()
        app.include_router(pipeline_api.router)
        client = TestClient(app)
        self.assertEqual(ret._HISTORY_DAYS, client.get('/api/pipeline/runs').json()['keep_days'],
                         '端点报的就是裁剪用的那个数')
        with patch.object(ret, '_HISTORY_DAYS', 9):
            self.assertEqual(9, client.get('/api/pipeline/runs').json()['keep_days'],
                             '换了保留天数,端点跟着换')


if __name__ == '__main__':
    main()
