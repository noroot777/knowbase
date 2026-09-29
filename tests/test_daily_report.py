"""日报:素材口径、落盘产物、定时语义;以及外文条目在导出前被译成中文。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily, daily_run, llm, llm_stages, store
from kb.feeds import registry as reg, sync, translate
from kb.scanner import parse_frontmatter
from kb.web import daily_api

MODEL = llm.Model('openai', 'test-model')


def add_doc(conn, path, *, source='dr-gzh', title='标题', account='某号',
            published='2026-09-28 09:00', summary='摘要', url='', promo='',
            body='', entities=(), topics=()):
    conn.execute(
        'INSERT INTO documents(path,source,title,account,published,url,summary,'
        'indexed_at,extracted_at,promo_level) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (path, source, title, account, published, url, summary,
         '2026-09-28 10:00', '2026-09-28 10:00', promo))
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

    def test_report_page_carries_material_list_and_counts(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', title='甲文', url='https://e.org/a',
                published='2026-09-28 08:00')
        add_doc(self.conn, 'source/rss/b.md', source='rss', title='乙文', account='Blog',
                published='2026-09-28 20:00')
        out = daily.generate('2026-09-28')
        self.assertEqual(2, out['docs'])
        text = Path(out['path']).read_text(encoding='utf-8')
        meta, body = parse_frontmatter(text)
        self.assertEqual('daily-report', meta['type'])
        self.assertEqual({'公众号': 1, 'RSS': 1}, meta['sources'])
        self.assertIn('今日要点', body)
        self.assertIn('[甲文](https://e.org/a)', body)       # 有 url 的给链接
        self.assertIn('乙文 — RSS·Blog', body)               # 没有 url 的只列名字
        prompt = self.chat.call_args[0][0]
        self.assertIn('1. [RSS·Blog] 乙文', prompt)          # 新的排前面

    def test_material_skips_derived_trees_and_hard_promo(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', title='正常', url='https://e.org/a')
        add_doc(self.conn, 'source/dr-gzh/ad.md', title='投放稿', promo='hard',
                url='https://e.org/ad')
        add_doc(self.conn, 'source/dr-gzh-projects/x.md', source='dr-gzh-projects',
                title='从投放稿拆出的条目', url='https://e.org/ad')
        items, total = daily.collect(self.conn, '2026-09-28', '2026-09-28')
        self.assertEqual(['正常'], [i['title'] for i in items])
        self.assertEqual(1, total)

    def test_missing_summary_falls_back_to_first_chunk(self):
        add_doc(self.conn, 'source/rss/a.md', source='rss', summary='',
                body='正文第一段,提取还没轮到它。')
        items, _ = daily.collect(self.conn, '2026-09-28', '2026-09-28')
        self.assertEqual('正文第一段,提取还没轮到它。', items[0]['excerpt'])

    def test_undated_entries_fall_back_to_index_day(self):
        add_doc(self.conn, 'source/rss/a.md', source='rss', published='')
        items, _ = daily.collect(self.conn, '2026-09-28', '2026-09-28')
        self.assertEqual(1, len(items))

    def test_empty_day_refuses_to_overwrite_an_existing_report(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        path = Path(daily.generate('2026-09-28')['path'])
        before = path.read_text(encoding='utf-8')
        self.conn.execute('DELETE FROM documents'); self.conn.commit()
        with self.assertRaises(LookupError):
            daily.generate('2026-09-28')
        self.assertEqual(before, path.read_text(encoding='utf-8'))

    def test_empty_model_answer_leaves_yesterday_report_untouched(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        self.chat.return_value = '   '
        with self.assertRaises(RuntimeError):
            daily.generate('2026-09-28')
        self.assertFalse((self.root / 'vault' / 'daily' / '2026-09-28.md').exists())

    def test_truncated_day_reports_the_full_count(self):
        for i in range(daily.MAX_ITEMS + 5):
            add_doc(self.conn, f'source/rss/{i}.md', source='rss', title=f'第{i}条',
                    published=f'2026-09-28 {i % 24:02d}:00')
        out = daily.generate('2026-09-28')
        self.assertEqual(daily.MAX_ITEMS, out['docs'])
        self.assertEqual(daily.MAX_ITEMS + 5, out['total'])
        self.assertIn(f'当日共 {daily.MAX_ITEMS + 5} 条', Path(out['path']).read_text(encoding='utf-8'))

    def test_multi_day_window_is_inclusive_on_both_ends(self):
        add_doc(self.conn, 'source/dr-gzh/a.md', published='2026-09-26 09:00')
        add_doc(self.conn, 'source/dr-gzh/b.md', published='2026-09-25 09:00')
        items, _ = daily.collect(self.conn, *daily._range('2026-09-28', 3))
        self.assertEqual(1, len(items))       # 26 在窗口内,25 不在

    def test_listing_reading_and_deleting(self):
        add_doc(self.conn, 'source/dr-gzh/a.md')
        daily.generate('2026-09-28')
        self.assertEqual(['2026-09-28'], [r['date'] for r in daily.reports()])
        self.assertIn('今日要点', daily.report('2026-09-28')['body'])
        self.assertTrue(daily.delete('2026-09-28'))
        self.assertFalse(daily.delete('2026-09-28'))
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
        self.start.assert_called_once_with('2026-09-29', sync=True, channels=['rss'],
                                           days=1, trigger='schedule')
        self.assertEqual('2026-09-29', daily_run.load_settings()['last_fired'])
        self.assertFalse(daily_run.tick())          # 同一天不会跑第二次

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


class TranslateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(translate, '_model', lambda: MODEL)):
            p.start(); self.addCleanup(p.stop)
        self.source = reg.save_subscription('rss', {'name': 'Blog', 'url': 'https://e.org/f.xml'})

    def put(self, title, body, published='2026-09-28 09:00'):
        reg.put_items(self.source, [{'url': 'https://e.org/p' + str(abs(hash(title)) % 999),
                                     'title': title, 'body': body, 'published': published}])

    def item(self, index=0):
        return reg.items('rss')['items'][index]

    def test_english_entry_is_translated_before_it_reaches_the_vault(self):
        self.put('A new agent runtime', 'The runtime schedules tools for coding agents.')
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n全新的智能体运行时\n'
                                            '###BODY###\n这个运行时为编码智能体调度工具。')):
            summary = translate.translate_pending('rss')
        self.assertEqual(1, summary['done'])
        sync.export('rss')
        text = (self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')).read_text()
        meta, body = parse_frontmatter(text)
        self.assertEqual('全新的智能体运行时', meta['title'])
        self.assertEqual('A new agent runtime', meta['original_title'])
        self.assertIn('这个运行时为编码智能体调度工具。', body)
        self.assertIn('自动翻译', body)

    def test_chinese_entry_costs_no_model_call(self):
        self.put('国产大模型发布', '今天发布了一个新的国产大模型，支持长上下文。')
        chat = Mock()
        with patch.object(llm, 'chat', chat):
            self.assertEqual(0, translate.translate_pending('rss')['done'])
        chat.assert_not_called()
        self.assertEqual('zh', self.item()['lang'])

    def test_failure_keeps_the_original_and_stops_after_max_tries(self):
        self.put('Another English post', 'Some English body text for the reader.')
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('502 bad gateway'))):
            for _ in range(translate.MAX_TRIES + 1):
                translate.translate_pending('rss')
        self.assertEqual(0, translate.pending('rss'))         # 不再自动重试
        self.assertEqual(translate.MAX_TRIES, self.item()['translate_tries'])
        sync.export('rss')
        text = (self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')).read_text()
        self.assertIn('Some English body text', text)
        self.assertNotIn('original_title', text)

    def test_missing_api_key_is_not_charged_against_retries(self):
        self.put('English again please', 'Body text in English for this entry.')
        with patch.object(translate, '_model', Mock(side_effect=RuntimeError('没配 key'))):
            summary = translate.translate_pending('rss')
        self.assertIn('没配 key', summary['skipped'])
        self.assertEqual(1, summary['left'])
        self.assertEqual(0, self.item()['translate_tries'])

    def test_retranslation_forces_a_fresh_export(self):
        self.put('English entry here', 'English body that will be translated later.')
        sync.export('rss')
        path = self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')
        self.assertIn('English body', path.read_text())
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n中文标题\n###BODY###\n中文正文。')):
            translate.translate_pending('rss')
        sync.export('rss')
        self.assertIn('中文正文。', path.read_text())

    def test_long_body_is_translated_in_chunks_and_marked_as_truncated(self):
        self.put('Long English post', 'paragraph of english text. ' * 700)
        calls = []

        def chat(prompt, *a, **k):
            calls.append(prompt)
            return ('###TITLE###\n长文\n###BODY###\n中文段落。' if len(calls) == 1 else '中文段落。')
        with patch.object(translate, 'MAX_BODY', 6000), patch.object(llm, 'chat', chat):
            translate.translate_pending('rss')
        self.assertGreater(len(calls), 1)
        self.assertIn('以上为前 6000 字', self.item()['body_zh'])

    def test_translated_text_is_searchable_in_the_panel(self):
        self.put('An English title', 'English body about agents and runtimes.')
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n中文标题\n###BODY###\n关于智能体的正文。')):
            translate.translate_pending('rss')
        self.assertEqual(1, reg.items('rss', q='智能体')['total'])


if __name__ == '__main__':
    unittest.main()
