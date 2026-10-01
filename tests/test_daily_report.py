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
        self.assertIn('[甲文](<https://e.org/a>) — 公众号·某号', body)   # 有 url 的给链接
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

    def test_a_report_says_which_sources_did_not_sync(self):
        # 降级拿已有内容出报是对的,但一份"只用旧素材"的日报不能长得跟"全都同步
        # 到了"一模一样 —— 实测三个来源全部连接失败,页面上只有一句绿色的
        # 「日报已生成」,文件里一个字都没提(2026-09-30 审查)。
        add_doc(self.conn, 'source/dr-gzh/a.md', title='甲文')
        out = daily.generate(since=self.since, until=self.until, sync_issues=[
            {'channel': 'rss', 'label': 'RSS', 'status': 'error', 'detail': '无法连接来源'},
            {'channel': 'social', 'label': '大V观点', 'status': 'unknown', 'detail': '成败未知'}])
        meta, body = parse_frontmatter(Path(out['path']).read_text(encoding='utf-8'))
        self.assertEqual(['RSS：无法连接来源', '大V观点：成败未知'], meta['sync_failed'])
        self.assertIn('本轮有 2 个来源没有同步成功', body)
        self.assertIn('> - RSS：无法连接来源', body)
        # 读回来也带着:历史日报翻回去仍然看得出当时缺了什么
        self.assertEqual(2, len(daily.report('2026-09-29')['sync_failed']))
        self.assertEqual(2, len(daily.reports()[0]['sync_failed']))

    def test_a_clean_sync_adds_no_warning(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', title='甲文')
        out = daily.generate(since=self.since, until=self.until, sync_issues=[])
        meta, body = parse_frontmatter(Path(out['path']).read_text(encoding='utf-8'))
        self.assertNotIn('sync_failed', meta)
        self.assertNotIn('没有同步成功', body)
        self.assertEqual([], daily.report('2026-09-29')['sync_failed'])

    def test_body_citations_become_links_to_the_source(self):
        # 正文里那串 [18][23][29] 原来是纯文本:素材清单在底下,读到一半想
        # 点开出处只能自己数行。链接**写进文件**,不留到阅读时 —— 日报是拿走
        # 就读的产物(复制出去、在 Obsidian 里翻回来),只在 kb-web 上可点的话
        # 换个读法就退回一串对不上号的数字。
        add_doc(self.conn, 'source/dr-gzh/a.md', title='甲文', url='https://e.org/a',
                published='2026-09-28 20:00')
        add_doc(self.conn, 'source/rss/b.md', source='rss', title='乙 [视频]',
                account='Blog', url='https://e.org/b(26)x', published='2026-09-28 19:00')
        add_doc(self.conn, 'source/rss/c.md', source='rss', title='丙文',
                account='Blog', published='2026-09-28 18:00')
        self.chat.return_value = ('## 今日要点\n\n- **甲乙**:两条 [1][2]\n'
                                  '- **顿号**:也认 [1、2]\n'
                                  '- **没链接**:第三条 [3],编号超范围 [9]\n'
                                  '- **已是链接**:[1](http://x) 不动\n')
        body = parse_frontmatter(Path(self.gen()['path']).read_text(encoding='utf-8'))[1]
        self.assertIn('[\\[1\\]](<https://e.org/a> "甲文 — 公众号·某号")', body)
        # url 里的圆括号:裸写进 ](…) 会在第一个 ) 提前收尾,链接当场断掉
        self.assertIn('[\\[2\\]](<https://e.org/b(26)x> "乙 [视频] — RSS·Blog")', body)
        self.assertIn('也认 [\\[1\\]](<https://e.org/a>', body)   # 顿号/逗号分隔
        self.assertIn('第三条 [3],编号超范围 [9]', body)    # 没 url / 对不上号的原样留着
        self.assertIn('[1](http://x) 不动', body)           # 已经是链接的不套娃
        # 素材清单里标题的方括号要转义,落单一个就能把这条链接从中间截断
        self.assertIn('2. [乙 \\[视频\\]](<https://e.org/b(26)x>) — RSS·Blog', body)

    def test_citation_linking_never_rewrites_code_or_existing_links(self):
        # 出处编号是替换进**文件**的,改错了就是把日报原文改坏 —— 页面、复制全文、
        # Obsidian 一起受影响,不是渲染层的事。两种真实形态光靠"后面别跟 `(`"挡不住
        # (2026-09-30 审查实测):行内代码 `items[1]` 被改写之后,照着复制的命令当场
        # 是错的;已有链接的文字 `[版本 [1]](…)` 里那个 [1] 后面跟的是 `]`,照样被
        # 改写,一条链接当场裂成两个不同目的地。
        add_doc(self.conn, 'source/dr-gzh/a.md', title='甲文', url='https://e.org/a',
                published='2026-09-28 20:00')
        self.chat.return_value = (
            '## 今日要点\n\n'
            '- 行内代码 `items[1]` 不动;出处 [1]。\n'
            '- 多反引号 ``code `x` items[1]`` 也不动。\n'
            '- 已有链接:[版本 [1]](https://e.org/keep) 仍指向原处。\n'
            '- 图片:![封面 [1]](https://e.org/cover.png)。\n'
            '- 带圆括号目标:[论文 [pdf]](https://e.org/p-(c).pdf)。\n\n'
            '```\n围栏里的 [1] 也不动\n```\n')
        body = parse_frontmatter(Path(self.gen()['path']).read_text(encoding='utf-8'))[1]
        for kept in ('`items[1]` 不动', '``code `x` items[1]`` 也不动',
                     '[版本 [1]](https://e.org/keep) 仍指向原处',
                     '![封面 [1]](https://e.org/cover.png)',
                     '[论文 [pdf]](https://e.org/p-(c).pdf)',
                     '围栏里的 [1] 也不动'):
            self.assertIn(kept, body)
        # 正文里唯一该被改写的就是那个句末出处
        self.assertIn('出处 [\\[1\\]](<https://e.org/a> "甲文 — 公众号·某号")。', body)
        self.assertEqual(1, body.split('## 素材清单')[0].count('<https://e.org/a>'))

    def test_citation_linking_survives_pipes_and_gfm(self):
        # 库里 215 篇文章标题带着 `|`(`AI Weekly 016 | 开源项目与科技进展` 那种)。
        # GFM 先按行拆单元格再解析 inline,标题或网址里一个裸 `|` 就把那一格劈开:
        # 出处那格变成半截标题,右边「已可用」被挤出表外(2026-09-30 第 4 轮审查实测)。
        add_doc(self.conn, 'source/dr-gzh/a.md', title='技术 | 新闻',
                url='https://e.org/a|b', published='2026-09-28 20:00')
        self.chat.return_value = (
            '## 今日要点\n\n'
            '| 主题 | 出处 | 状态 |\n| --- | --- | --- |\n| 产品 | [1] | 已可用 |\n\n'
            '- 裸网址 https://e.org/path/[1] 原样保留;出处 [1]。\n')
        body = parse_frontmatter(Path(self.gen()['path']).read_text(encoding='utf-8'))[1]
        cell = '[\\[1\\]](<https://e.org/a%7Cb> "技术 \\| 新闻 — 公众号·某号")'
        self.assertIn(f'| 产品 | {cell} | 已可用 |', body)   # 状态列还在原位
        self.assertIn('裸网址 https://e.org/path/[1] 原样保留', body)
        self.assertIn(f'出处 {cell}。', body)

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
                               lambda: {'wechat_local': '公众号', 'rss': 'RSS'}),
                  patch.object(daily_run, 'default_channels',
                               lambda: ['wechat_local', 'rss'])):
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
        self.assertEqual(['wechat_local', 'rss'], daily_run.load_settings()['channels'])

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

    def replaced_mid_poll(self, result=None):
        """跑一趟 `_sync_channel`:我们那一轮在第一跳之前就收尾、别人顶了上来。"""
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
             patch.object(pl, 'run_result', Mock(return_value=result)), \
             patch.object(pl, 'request_stop', Mock()) as stop, \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_run, '_wait_idle', Mock(return_value=True)):
            entry = daily_run._sync_channel('rss', 'RSS', time.monotonic() + 600)
        stop.assert_not_called()                      # 别人那一轮不该被我们停
        self.assertEqual('', daily_run._state['pipeline_run'])
        return entry

    def test_a_replaced_run_is_judged_by_its_own_recorded_outcome(self):
        # `_sync_channel` 只认 run id 相同的快照。换人之后既不去停它、也不拿
        # 它的阶段结果当我们的成绩单——改从管道那份按 id 存的收尾快照里取。
        entry = self.replaced_mid_poll(
            {'run_id': 'run-1', 'stopped': False, 'finished_at': '',
             'stages': {'crawl': {'status': 'error', 'detail': '我们那轮的失败'}}})
        self.assertEqual('error', entry['status'])
        self.assertIn('我们那轮的失败', entry['detail'])
        self.assertNotIn('别人那轮的失败', entry['detail'])

    def test_an_unobtainable_outcome_is_not_recorded_as_success(self):
        # 连收尾记录都取不到时**不许算成功**:那一轮的阶段状态我们一眼都没
        # 看到,成败、是否被停全都不知道,写"同步完成"是拿没人证明过的结果去
        # 记账(2026-09-30 第 3 轮审查实测这里硬编码成了 ok)。
        entry = self.replaced_mid_poll(None)
        self.assertEqual('unknown', entry['status'])
        self.assertIn('未知', entry['detail'])
        self.assertNotIn('别人那轮的失败', entry['detail'])

    def test_the_pipeline_keeps_each_finished_run_addressable_by_id(self):
        # 上面那两条靠的是管道真留了这份记录:终态在 running 放下之前按 id 存。
        from kb import pipeline_state as ps
        state, finished = deepcopy(ps._state), dict(ps._finished)
        try:
            ps._state.update({'running': True, 'run_id': 'run-42', 'log': [],
                              'stop_requested': False,
                              'stages': {s: {'status': 'ok', 'detail': '好了'}
                                         for s in ps.STAGES}})
            with patch.object(ps, '_save_last_run', Mock()):
                ps._end_run()
            got = ps.run_result('run-42')
            self.assertEqual('ok', got['stages']['crawl']['status'])
            self.assertFalse(got['stopped'])
            self.assertIsNone(ps.run_result('run-41'))     # 没跑过的查不到
            # 存的是快照,不是同一个对象:之后再起一轮不会把它改掉
            ps._state['stages']['crawl'] = {'status': 'running', 'detail': '新一轮'}
            self.assertEqual('ok', ps.run_result('run-42')['stages']['crawl']['status'])
        finally:
            ps._state.clear(); ps._state.update(state)
            ps._finished.clear(); ps._finished.update(finished)

    def test_a_failed_channel_reaches_the_report_and_the_finish_notice(self):
        # 通道失败原来只留在 _state['channels'] 里:没人读,也没写进日报,完成
        # 提示照样是一句绿色的"日报已生成"(2026-09-30 审查实测)。
        gen = self.run_once(channels=[
            {'channel': 'rss', 'label': 'RSS', 'status': 'error', 'detail': '无法连接来源'},
            {'channel': 'social', 'label': '大V观点', 'status': 'ok', 'detail': '同步完成'},
            {'channel': 'wechat_local', 'label': '公众号', 'status': 'skipped',
             'detail': '超出时间预算,未同步'}])
        issues = gen.call_args.kwargs['sync_issues']
        self.assertEqual(['RSS', '公众号'], [i['label'] for i in issues])  # ok 的不算
        self.assertEqual('done', daily_run._state['phase'])
        self.assertIn('2 个来源未同步成功(RSS、公众号)', daily_run._state['detail'])

    def sync_once(self, stages):
        """跑一趟 `_sync_channel` 到正常收尾,stages 之外的阶段按"没选中"处理。"""
        from kb import pipeline as pl
        from kb.pipeline_state import STAGES
        # 没被选中的阶段一开跑就是 skipped(见 pipeline.start_pipeline)
        full = {s: {'status': 'skipped', 'detail': ''} for s in STAGES}
        full.update(stages)
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': '', 'log': [], 'detail': ''})
        with patch.object(pl, 'start_pipeline', Mock(return_value='run-1')), \
             patch.object(pl, 'get_state', Mock(return_value={
                 'running': False, 'run_id': 'run-1', 'current_stage': '', 'stages': full})), \
             patch.object(pl, 'request_stop', Mock()), \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_run, '_wait_idle', Mock(return_value=True)):
            return daily_run._sync_channel('social', '大V观点', time.monotonic() + 600)

    def test_a_skipped_stage_is_not_a_successful_sync(self):
        # 2026-09-30 实测:51 个大V账号全缺 X 桥接配置,crawl 整个落 skipped
        # (「待配置 51 个」)。这里原来只数 error,于是通道报绿色的「同步完成」,
        # 日报里那一行只剩 extract 的结果 —— "一条都没抓到"连同原因一起看不见。
        entry = self.sync_once({
            'crawl': {'status': 'skipped',
                      'detail': '检查 0/51 个来源，新增 0 条，待配置 51 个（请填写 X 桥接或专用订阅链接）'},
            'export': {'status': 'ok', 'detail': '已保存 0 条到知识库'},
            'index': {'status': 'ok', 'detail': '新增/更新 0'},
            'extract': {'status': 'ok', 'detail': '没有待提取的文档'}})
        self.assertEqual('skipped', entry['status'])
        self.assertIn('待配置 51 个', entry['detail'])
        # 没选中的阶段也是 skipped,不能跟着报出来
        self.assertNotIn('projects', entry['detail'])
        self.assertNotIn('wiki', entry['detail'])

    def test_a_stage_stopped_by_the_budget_is_reported_too(self):
        entry = self.sync_once({
            'crawl': {'status': 'ok', 'detail': '检查 8/8 个来源'},
            'export': {'status': 'stopped', 'detail': '已停止，未继续执行'},
            'index': {'status': 'error', 'detail': '索引失败: 磁盘满'},
            'extract': {'status': 'stopped', 'detail': ''}})
        self.assertEqual('error', entry['status'])          # 有红就是红
        self.assertIn('index 索引失败: 磁盘满', entry['detail'])
        self.assertIn('export 已停止，未继续执行', entry['detail'])
        self.assertIn('extract 本轮未执行', entry['detail'])

    def test_a_fully_green_round_still_reads_as_done(self):
        entry = self.sync_once({s: {'status': 'ok', 'detail': f'{s} 好了'}
                                for s in daily_run.SYNC_STAGES})
        self.assertEqual(('ok', '同步完成'), (entry['status'], entry['detail']))

    def test_a_clean_round_keeps_its_plain_finish_notice(self):
        gen = self.run_once(channels=[
            {'channel': 'rss', 'label': 'RSS', 'status': 'ok', 'detail': '同步完成'}])
        self.assertEqual([], gen.call_args.kwargs['sync_issues'])
        self.assertNotIn('未同步成功', daily_run._state['detail'])

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
