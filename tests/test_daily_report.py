"""日报:窗口按发布时间、素材口径、落盘产物、定时与停止语义。

外文条目的翻译在 tests/test_feed_translate.py。
"""
import json
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily, daily_run, llm, llm_stages, store
from kb.scanner import parse_frontmatter
from kb.web import daily_api

MODEL = llm.Model('openai', 'test-model')


def T(text):
    return datetime.fromisoformat(text)


def add_doc(conn, path, *, source='dr-gzh', title='标题', account='某号',
            published='2026-09-28 09:00', indexed='2026-09-28 09:30',
            summary='摘要', url='', promo='', body='', entities=(), topics=()):
    conn.execute(
        'INSERT INTO documents(path,source,title,account,published,url,summary,'
        'indexed_at,extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (path, source, title, account, published, url, summary,
         indexed, indexed, promo))
    if body:
        conn.execute('INSERT INTO chunks(path,seq,heading,text) VALUES(?,?,?,?)',
                     (path, 0, '', body))
    for e in entities:
        conn.execute('INSERT INTO entities(path,entity,kind,norm) VALUES(?,?,?,?)',
                     (path, e, 'project', e.lower()))
    for t in topics:
        conn.execute('INSERT INTO doc_tags(path,tag) VALUES(?,?)', (path, 'topic/' + t))
    conn.commit()


class DailyReportTests(unittest.TestCase):
    """窗口 = (上一份日报的截止时刻, 这一次的生成时刻],筛的是**发布时间**。"""

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
                  patch.object(llm_stages, 'stage_model', Mock(return_value=MODEL))):
            p.start(); self.addCleanup(p.stop)
        self.chat = Mock(return_value='## 今日要点\n\n- **有事发生**:说明 [1]')
        p = patch.object(llm, 'chat', self.chat)
        p.start(); self.addCleanup(p.stop)
        self.since, self.until = T('2026-09-28 08:00'), T('2026-09-29 08:00')

    def collect(self, since=None, until=None):
        return daily.collect(self.conn, since or self.since, until or self.until)

    def gen(self, since=None, until=None):
        return daily.generate(since=since or self.since, until=until or self.until)

    def test_report_page_carries_material_list_and_counts(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', title='甲文', url='https://e.org/a',
                published='2026-09-28 09:00')
        add_doc(self.conn, 'source/rss/b.md', source='rss', title='乙文', account='Blog',
                published='2026-09-28 20:00')
        out = self.gen()
        self.assertEqual(2, out['docs'])
        text = Path(out['path']).read_text(encoding='utf-8')
        meta, body = parse_frontmatter(text)
        self.assertEqual('daily-report', meta['type'])
        self.assertEqual({'公众号': 1, 'RSS': 1}, meta['sources'])
        self.assertEqual('2026-09-28 08:00:00', meta['window_since'])
        self.assertEqual('2026-09-29 08:00:00', meta['window_until'])
        self.assertIn('按发布时间,不是抓取时间', body)
        self.assertIn('[甲文](https://e.org/a)', body)       # 有 url 的给链接
        self.assertIn('乙文 — RSS·Blog', body)               # 没有 url 的只列名字
        prompt = self.chat.call_args[0][0]
        self.assertIn('1. [09-28 20:00|RSS·Blog] 乙文', prompt)   # 新的排前面

    def test_window_follows_publish_time_not_fetch_time(self):
        # 窗口外发布、窗口内抓到:不算这一份日报的正文素材(否则日报讲的是
        # "今天抓到了什么",而不是"这段时间发生了什么")。
        add_doc(self.conn, 'source/rss/old.md', source='rss', title='三天前发的',
                published='2026-09-25 09:00', indexed='2026-09-28 20:00')
        # 窗口内发布、窗口结束后才抓到:这一次还看不见它,下一次窗口会补录。
        add_doc(self.conn, 'source/rss/slow.md', source='rss', title='窗口内发的',
                published='2026-09-28 10:00', indexed='2026-09-30 01:00')
        mat = self.collect()
        self.assertEqual(['窗口内发的'], [i['title'] for i in mat.items])
        self.assertEqual(['三天前发的'], [i['title'] for i in mat.late])

    def test_timestamps_compare_as_time_not_as_strings(self):
        # 库里 published 有四种写法。'T'(0x54) 排在空格(0x20) 后面,所以
        # '2026-09-28T07:00:00+08:00' 字典序上大于 '2026-09-28 08:00' ——
        # 按字符串筛窗口会把八点之前发的内容算进八点才开始的窗口。
        add_doc(self.conn, 'source/dr-youtube-dig/early.md', source='dr-youtube-dig',
                title='窗口前一小时', published='2026-09-28T07:00:00+08:00')
        add_doc(self.conn, 'source/dr-youtube-dig/inside.md', source='dr-youtube-dig',
                title='窗口内', published='2026-09-28T09:00:00+08:00')
        add_doc(self.conn, 'source/dr/day.md', source='dr', title='只有日期',
                published='2026-09-28')
        titles = [i['title'] for i in self.collect().items]
        self.assertEqual(['窗口内'], titles)
        # 只有日期的按当天 00:00 —— 那是这种写法唯一能确定的下界。
        self.assertIn('只有日期', [i['title'] for i in
                                   self.collect(T('2026-09-27 08:00')).items])

    def test_material_skips_derived_trees_and_hard_promo(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', title='正常', url='https://e.org/a')
        add_doc(self.conn, 'source/dr-gzh/ad.md', title='投放稿', promo='hard',
                url='https://e.org/ad')
        add_doc(self.conn, 'source/dr-gzh-projects/x.md', source='dr-gzh-projects',
                title='从投放稿拆出的条目', url='https://e.org/ad')
        mat = self.collect()
        self.assertEqual(['正常'], [i['title'] for i in mat.items])
        self.assertEqual(1, mat.total)

    def test_missing_summary_falls_back_to_first_chunk(self):
        add_doc(self.conn, 'source/rss/a.md', source='rss', summary='',
                body='正文第一段,提取还没轮到它。')
        self.assertEqual('正文第一段,提取还没轮到它。', self.collect().items[0]['excerpt'])

    def test_late_arrivals_go_to_their_own_section(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', title='窗口内的')
        add_doc(self.conn, 'source/rss/late.md', source='rss', title='迟到的',
                published='2026-09-20 09:00', indexed='2026-09-28 22:00')
        out = self.gen()
        self.assertEqual((1, 1), (out['docs'], out['late']))
        prompt = self.chat.call_args[0][0]
        self.assertIn('## 补录', prompt)                      # 结构里多一节
        self.assertIn('[补录·发布于 09-20 09:00|RSS·某号] 迟到的', prompt)
        text = Path(out['path']).read_text(encoding='utf-8')
        self.assertIn('迟到的 — RSS·某号 · 补录', text)
        self.assertEqual(1, parse_frontmatter(text)[0]['late'])

    def test_entry_without_publish_time_is_reported_once(self):
        # 没有发布时间就没法归进任何一段"生产时间",但也不能让它人间蒸发:
        # 按入库时间进补录,而且只进一次。
        add_doc(self.conn, 'source/rss/a.md', source='rss', published='',
                indexed='2026-09-28 20:00')
        mat = self.collect()
        self.assertEqual(([], 1), (mat.items, len(mat.late)))
        self.assertEqual(0, len(self.collect(T('2026-09-29 08:00'),
                                             T('2026-09-30 08:00')).late))

    def test_empty_window_refuses_to_overwrite_an_existing_report(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        path = Path(self.gen()['path'])
        before = path.read_text(encoding='utf-8')
        self.conn.execute('DELETE FROM documents'); self.conn.commit()
        with self.assertRaises(LookupError):
            self.gen()
        self.assertEqual(before, path.read_text(encoding='utf-8'))

    def test_empty_model_answer_leaves_yesterday_report_untouched(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        self.chat.return_value = '   '
        with self.assertRaises(RuntimeError):
            self.gen()
        self.assertFalse((self.root / 'vault' / 'daily' / '2026-09-29.md').exists())

    def test_truncated_window_reports_the_full_count(self):
        for i in range(daily.MAX_ITEMS + 5):
            add_doc(self.conn, f'source/rss/{i}.md', source='rss', title=f'第{i}条',
                    published=f'2026-09-28 {9 + i % 12:02d}:{i % 60:02d}')
        out = self.gen()
        self.assertEqual((daily.MAX_ITEMS, daily.MAX_ITEMS + 5), (out['docs'], out['total']))
        self.assertIn(f'窗口内共 {daily.MAX_ITEMS + 5} 条',
                      Path(out['path']).read_text(encoding='utf-8'))

    def test_next_window_starts_where_the_last_report_ended(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        self.gen()                                   # 截止 09-29 08:00
        self.assertEqual((T('2026-09-29 08:00'), T('2026-09-30 08:00')),
                         daily.next_window(now=T('2026-09-30 08:00')))

    def test_regenerating_today_ignores_todays_own_report(self):
        # 重新生成今天这一份要的是"从昨天那一刻起的完整一天",不是"从今早那份
        # 的截止时刻起的几小时"——后者会把上午已经写进旧版本的内容整段丢掉。
        add_doc(self.conn, 'source/dr-gzh/a.md', published='2026-09-27 12:00')
        add_doc(self.conn, 'source/dr-gzh/b.md', published='2026-09-28 12:00')
        self.gen(T('2026-09-27 08:00'), T('2026-09-28 08:00'))
        self.gen(T('2026-09-28 08:00'), T('2026-09-29 08:00'))
        self.assertEqual(T('2026-09-28 08:00'),
                         daily.next_window(now=T('2026-09-29 20:00'))[0])

    def test_a_long_outage_does_not_move_the_window_start(self):
        # 停服几天之后,起点仍然接上一份日报的截止时刻。按"最长回溯 N 天"截断的
        # 话,被截掉那几天的内容再也进不了任何一份日报——素材多是限量问题
        # (collect 按条数截、日报里写明),窗口断链没法补。
        add_doc(self.conn, 'source/dr-gzh/a.md', published='2026-09-01 12:00')
        self.gen(T('2026-09-01 08:00'), T('2026-09-02 08:00'))
        self.assertEqual(T('2026-09-02 08:00'),
                         daily.next_window(first_days=3, now=T('2026-09-29 08:00'))[0])
        # 一份日报都没有时才回溯 first_days 天,不是回溯到库的开天辟地。
        daily.delete('2026-09-02')
        self.assertEqual(T('2026-09-26 08:00'),
                         daily.next_window(first_days=3, now=T('2026-09-29 08:00'))[0])
        self.assertEqual(T('2026-09-28 08:00'),
                         daily.next_window(now=T('2026-09-29 08:00'))[0])

    def test_listing_reading_and_deleting(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        self.gen()
        self.assertEqual(['2026-09-29'], [r['date'] for r in daily.reports()])
        self.assertIn('今日要点', daily.report('2026-09-29')['body'])
        self.assertTrue(daily.delete('2026-09-29'))
        self.assertFalse(daily.delete('2026-09-29'))
        self.assertEqual([], daily.reports())

    def test_only_a_real_date_can_name_a_report(self):
        # 日期直接拼进 vault 路径:不校验就等于把整个 vault 变成可读的。
        for bad in ('../../index', '2026-13-99', 'wiki/index'):
            with self.assertRaises(ValueError):
                daily.report(bad)
        app = FastAPI(); app.include_router(daily_api.router)
        client = TestClient(app)
        self.assertEqual(400, client.get('/api/daily/reports/2026-13-99').status_code)
        self.assertEqual(404, client.get('/api/daily/reports/2026-09-28').status_code)
        self.assertEqual(400, client.post('/api/daily/run',
                                          json={'since': '不是时刻'}).status_code)


class DailyScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'daily_report.json'
        for p in (patch.object(daily_run, 'SETTINGS_FILE', self.path),
                  patch.object(daily_run, '_channel_labels',
                               lambda: {'wechat_oa': '微信', 'rss': 'RSS'}),
                  patch.object(daily_run, 'default_channels', lambda: ['wechat_oa', 'rss'])):
            p.start(); self.addCleanup(p.stop)
        self.clock = Mock(wraps=datetime)
        self.clock.now.return_value = datetime(2026, 9, 28, 16, 42)
        p = patch.object(daily_run, 'datetime', self.clock)
        p.start(); self.addCleanup(p.stop)
        self.start = Mock(return_value=True)
        p = patch.object(daily_run, 'start', self.start)
        p.start(); self.addCleanup(p.stop)

    def test_saving_schedules_the_next_occurrence_without_catching_up(self):
        saved = daily_run.save_settings(True, '07:30', ['rss'])
        self.assertEqual('2026-09-29T07:30', saved['next_run'])
        self.assertFalse(daily_run.tick())
        self.clock.now.return_value = datetime(2026, 9, 29, 7, 30)
        self.assertTrue(daily_run.tick())
        # 定时这一轮不钉窗口:起点接上一份日报,终点是同步跑完的那一刻。
        self.start.assert_called_once_with(sync=True, channels=['rss'], first_days=1,
                                           trigger='schedule', sched_day='2026-09-29')
        self.assertFalse(daily_run.tick())          # 起过一轮就不会重复起

    def test_a_transient_failure_is_retried_the_same_day(self):
        # "已触发"不等于"已出报":在起跑那一刻就记下今天已经出过报的话,一次
        # 模型 502 就让用户整天等不到日报,而且面板上看不出还会不会再试。
        daily_run.save_settings(True, '07:30', ['rss'])
        self.clock.now.return_value = datetime(2026, 9, 29, 7, 30)
        self.assertTrue(daily_run.tick())
        self.assertEqual('', daily_run.load_settings()['last_fired'])
        self.assertEqual('2026-09-29T08:00', daily_run.load_settings()['next_run'])
        moment = datetime(2026, 9, 29, 7, 30)
        for attempt in range(1, daily_run.MAX_RETRIES + 1):   # 前几次失败:同日重试
            daily_run._finish_schedule('2026-09-29', retry=True)
            self.assertEqual((attempt, ''), (daily_run.load_settings()['fail_count'],
                                             daily_run.load_settings()['last_fired']))
            moment += daily_run.RETRY_AFTER
            self.clock.now.return_value = moment
            self.assertTrue(daily_run.tick())
        daily_run._finish_schedule('2026-09-29', retry=True)   # 补满次数就收手
        self.assertEqual('2026-09-29', daily_run.load_settings()['last_fired'])
        self.assertEqual('2026-09-30T07:30', daily_run.load_settings()['next_run'])
        self.assertFalse(daily_run.tick())

    def test_a_successful_run_books_tomorrow_and_clears_the_failure_count(self):
        daily_run.save_settings(True, '07:30', ['rss'])
        self.clock.now.return_value = datetime(2026, 9, 29, 7, 30)
        daily_run.tick()
        daily_run._finish_schedule('2026-09-29', retry=True)
        daily_run._finish_schedule('2026-09-29', retry=False)
        s = daily_run.load_settings()
        self.assertEqual(('2026-09-29', '2026-09-30T07:30', 0),
                         (s['last_fired'], s['next_run'], s['fail_count']))

    def test_a_run_crossing_midnight_is_booked_against_the_day_it_fired(self):
        # 同步能跑几小时:23:50 起的那一轮可能 00:50 才收尾。按收尾时刻记的话
        # last_fired 落在第二天,第二天那一跳当场被自己吃掉。
        daily_run.save_settings(True, '23:50', ['rss'])
        self.clock.now.return_value = datetime(2026, 9, 29, 0, 50)
        daily_run._finish_schedule('2026-09-28', retry=False)
        s = daily_run.load_settings()
        self.assertEqual(('2026-09-28', '2026-09-29T23:50'), (s['last_fired'], s['next_run']))
        self.clock.now.return_value = datetime(2026, 9, 29, 23, 50)
        self.assertTrue(daily_run.tick())           # 29 号那一跳还在

    def test_old_settings_keep_working_under_the_new_key(self):
        # 老配置里这个键叫 days / max_days,意思变了(现在只管第一份回溯多久),
        # 值照读——不读的话升级之后这个设置悄悄回到默认。
        self.path.write_text(json.dumps({'enabled': True, 'time': '07:30', 'max_days': 5}))
        self.assertEqual(5, daily_run.load_settings()['first_days'])

    def test_unknown_channels_fall_back_to_defaults(self):
        self.path.write_text(json.dumps({'enabled': True, 'time': '07:30',
                                         'channels': ['gone']}))
        self.assertEqual(['wechat_oa', 'rss'], daily_run.load_settings()['channels'])

    def test_disabled_schedule_never_fires(self):
        daily_run.save_settings(False, '07:30', ['rss'])
        self.clock.now.return_value = datetime(2026, 9, 29, 9, 0)
        self.assertFalse(daily_run.tick())
        self.start.assert_not_called()

    def test_bad_time_is_rejected_before_touching_the_file(self):
        with self.assertRaises(ValueError):
            daily_run.save_settings(True, '25:00', ['rss'])
        self.assertFalse(self.path.exists())


class DailyStopTests(unittest.TestCase):
    """按下「停止」= 这一轮到此为止;同步超预算 = 别再同步了,照样出报。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Path(temp.name) / 'daily_report.json'
        for p in (patch.object(daily_run, 'STATE_FILE', Path(temp.name) / 'last_run.json'),
                  patch.object(daily_run, 'SETTINGS_FILE', self.settings)):
            p.start(); self.addCleanup(p.stop)
        self.before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(self.before))

    def run_once(self, **flags):
        daily_run._state.update({**self.before, 'running': True, 'log': [],
                                 'channels': [], **flags})
        gen = Mock(return_value={'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
                                 'docs': 2, 'total': 2, 'late': 0, 'model': 'm'})
        with patch.object(daily, 'generate', gen):
            daily_run._run(None, None, False, [], daily.FIRST_DAYS)
        return gen

    def test_user_stop_does_not_spend_another_model_call(self):
        # 界面上写的是"当前阶段结束后停止",用户的预期是不再出报;继续生成等于
        # 把一份同步到一半的日报当成功交出去,还再花一次模型钱。
        gen = self.run_once(stop_requested=True)
        gen.assert_not_called()
        self.assertEqual(('stopped', False),
                         (daily_run._state['phase'], daily_run._state['running']))

    def test_running_out_of_budget_still_generates(self):
        # 超预算是"别再同步了,拿现有内容出报",生成正是它的目的。
        gen = self.run_once(over_budget=True)
        gen.assert_called_once()
        self.assertEqual('done', daily_run._state['phase'])

    def test_stop_is_refused_once_the_model_is_writing(self):
        # 生成阶段停不了:模型已经在出稿,`daily.generate()` 自己把文件写完才
        # 返回。受理下来只会承诺一件做不到的事——界面写着"这一轮不生成日报",
        # 实际照样多出一份,钱也照花(2026-09-30 审查实测)。
        daily_run._state.update({**self.before, 'running': True, 'phase': 'generate'})
        accepted, detail = daily_run.request_stop()
        self.assertFalse(accepted)
        self.assertIn('不能中途停止', detail)
        self.assertFalse(daily_run._state['stop_requested'])

    def test_stopping_mid_generation_does_not_promise_anything_it_cannot_do(self):
        # 审查者的复现形态:生成桩阻塞在模型调用上时按停止。受理与否必须和
        # 实际行为一致——这里是"拒掉 + 照常出报",而不是"受理 + 照常出报"。
        seen = {}

        def generating(**kwargs):
            seen['stop'] = daily_run.request_stop()
            return {'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
                    'docs': 2, 'total': 2, 'late': 0, 'model': 'm'}

        daily_run._state.update({**self.before, 'running': True, 'log': [], 'channels': []})
        with patch.object(daily, 'generate', Mock(side_effect=generating)):
            daily_run._run(None, None, False, [], daily.FIRST_DAYS)
        self.assertFalse(seen['stop'][0])
        self.assertEqual('done', daily_run._state['phase'])
        self.assertFalse(daily_run._state['stop_requested'])

    def test_stop_while_waiting_leaves_another_task_pipeline_alone(self):
        # 日报允许在别人的管道后面排队等。这时候按停止只该结束我们自己的等待,
        # 顺手把别人启动的那一轮同步也停掉是越权。
        from kb import pipeline as pl
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': ''})
        with patch.object(pl, 'get_state', Mock(return_value={'running': True})), \
             patch.object(pl, 'request_stop', Mock()) as stop:
            accepted, _ = daily_run.request_stop()
        self.assertTrue(accepted)
        stop.assert_not_called()
        self.assertTrue(daily_run._state['stop_requested'])

    def test_stop_does_end_the_round_we_started(self):
        from kb import pipeline as pl
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': 'run-7'})
        with patch.object(pl, 'get_state', Mock(return_value={'running': True})), \
             patch.object(pl, 'request_stop', Mock()) as stop:
            self.assertEqual(True, daily_run.request_stop()[0])
        # 带上 id:停的是"我们那一轮",不是"现在在跑的那一轮"
        stop.assert_called_once_with('run-7')

    def test_stop_targets_our_run_by_id_not_whatever_runs_now(self):
        # 审查者的复现:我们那一轮在两次轮询之间跑完,另一项普通同步紧接着
        # 拿到全局管道。只记布尔所有权的话,停止会落到别人头上
        # (2026-09-30 审查实测 pipeline_stop_calls=1 落在替换后的同步上)。
        from kb import pipeline_state as ps
        state = deepcopy(ps._state)
        try:
            ps._state.update({'running': True, 'run_id': 'run-2',
                              'stop_requested': False, 'log': []})
            daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                     'pipeline_run': 'run-1'})
            self.assertEqual(True, daily_run.request_stop()[0])
            # 真的走一遍 pipeline.request_stop:别人那一轮一个字节都没动
            self.assertFalse(ps._state['stop_requested'])
            self.assertTrue(daily_run._state['stop_requested'])
        finally:
            ps._state.clear(); ps._state.update(state)

    def test_a_replaced_run_neither_gets_stopped_nor_reported_as_ours(self):
        # 同一件事的执行侧:`_sync_channel` 只认 run id 相同的快照。换人之后
        # 既不去停它(那是别人的活),也不把它的阶段结果当成我们的成绩单。
        from kb import pipeline as pl
        snapshots = [
            {'running': True, 'run_id': 'run-1', 'current_stage': 'crawl',
             'stages': {'crawl': {'status': 'running', 'detail': '抓取中'}}},
            # 我们那一轮收尾了,普通同步马上接手:running 看起来从没断过
            {'running': True, 'run_id': 'run-2', 'current_stage': 'index',
             'stages': {'index': {'status': 'error', 'detail': '别人那轮的失败'}}},
        ]
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': '', 'log': [], 'detail': ''})
        with patch.object(pl, 'start_pipeline', Mock(return_value='run-1')), \
             patch.object(pl, 'get_state', Mock(side_effect=snapshots)), \
             patch.object(pl, 'request_stop', Mock()) as stop, \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_run, '_wait_idle', Mock(return_value=True)):
            entry = daily_run._sync_channel('rss', 'RSS', time.monotonic() + 600)
        stop.assert_not_called()
        self.assertEqual('ok', entry['status'])
        self.assertNotIn('别人那轮的失败', entry['detail'])
        self.assertEqual('', daily_run._state['pipeline_run'])

    def test_syncing_nothing_is_refused_at_both_layers(self):
        # 勾了"先同步"却一个来源都没选:同步阶段什么都不做、却一路报成功,
        # "同步全部并生成"静静地降级成"直接生成"(2026-09-30 审查实测)。
        with self.assertRaises(ValueError):
            daily_run.start(sync=True, channels=[])
        app = FastAPI(); app.include_router(daily_api.router)
        client = TestClient(app)
        self.assertEqual(400, client.post('/api/daily/run',
                                          json={'sync': True, 'channels': []}).status_code)
        # "直接生成"照旧:它本来就不同步,空范围是正常形态。
        with patch.object(daily_run.threading, 'Thread', Mock()):
            self.assertTrue(daily_run.start(sync=False, channels=[]))
        daily_run._state.update(self.before)

    def test_a_stopped_scheduled_round_is_not_retried(self):
        self.settings.write_text(json.dumps({'enabled': True, 'time': '07:30'}))
        self.run_once(stop_requested=True, sched_day='2026-09-29')
        s = daily_run._raw_settings()
        self.assertEqual(('2026-09-29', '2026-09-30T07:30', 0),
                         (s['last_fired'], s['next_run'], s['fail_count']))


if __name__ == '__main__':
    unittest.main()
