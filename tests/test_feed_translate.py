"""外文订阅条目在导出进 vault 之前被译成中文:语言判定、队列口径、标题译文、
来源面板那两种"还没有中文"的状态。

从 tests/test_daily_report.py 拆出来的(那份写到 682 行、逼近 700 行上限,而
翻译这一块最独立:只碰 kb/feeds/,不需要日报那套窗口与定时脚手架)。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import llm
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg, sync, translate
from kb.scanner import parse_frontmatter

MODEL = llm.Model('openai', 'test-model')


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

    def test_short_foreign_text_is_not_mistaken_for_chinese(self):
        # 一个汉字都没有就不可能是中文,这一档没有"判不准"可言,也不设长度
        # 下限:那道"至少 6 个字母"的成本闸门会把「Hello」「Great」这类
        # 1-5 字母的纯英文动态确定性地标成中文,永久漏译(2026-09-30 复审
        # 实测)。产品名保留原文靠的是 prompt 里的译文规则,不是把英文冒充
        # 成中文。
        for text in ('Hello world', 'Hello', 'Great', 'Nice', 'Bun 1.3'):
            self.assertEqual('en', reg.text_language(text, ''), text)
        self.assertEqual('zh', reg.text_language('预告', ''))          # 有汉字又太短
        self.assertEqual('zh', reg.text_language('2026 🚀 1.3', ''))   # 没字可判
        self.put('Hello world', 'Short note.')
        self.assertEqual('en', self.item()['lang'])
        self.assertEqual(1, translate.pending('rss'))

    def test_a_title_only_entry_is_translated_without_burning_retries(self):
        # 只有标题的短动态:问模型译一段空正文只会拿回空译文,三次重试白烧。
        self.put('Great', '')
        with patch.object(llm, 'chat', Mock(return_value='很棒')) as chat:
            self.assertEqual(1, translate.translate_pending('rss')['done'])
        chat.assert_called_once()
        self.assertEqual('很棒', self.item()['title_zh'])
        # 译完了就该出队(判据是 translated_at,不是"有没有 body_zh")
        self.assertEqual(0, translate.pending('rss'))

    def test_a_missing_title_marker_is_a_failure_not_a_success(self):
        # 模型漏掉 ###TITLE###/###BODY### 分隔时,原来整段当正文、空标题照样
        # 存下来:条目从此不再进队列,页面标着"已译为中文",标题却永久是英文
        # (2026-09-30 复审实测)。缺了就补问一次标题。
        self.put('A new agent runtime', 'This runtime schedules tools.')
        with patch.object(llm, 'chat', Mock(side_effect=['这个运行时调度工具。',
                                                         '全新的智能体运行时'])):
            self.assertEqual(1, translate.translate_pending('rss')['done'])
        self.assertEqual('全新的智能体运行时', self.item()['title_zh'])

    def test_a_title_that_never_comes_back_keeps_the_entry_in_the_queue(self):
        # 补问也拿不到标题译文时整条算失败:宁可下一轮重试,也不要把一条
        # "正文中文、标题英文"的半成品当成译完了。
        self.put('A new agent runtime', 'This runtime schedules tools.')
        with patch.object(llm, 'chat', Mock(side_effect=['这个运行时调度工具。',
                                                         '###TITLE###'])):
            summary = translate.translate_pending('rss')
        self.assertEqual((0, 1), (summary['done'], summary['failed']))
        self.assertEqual('', self.item()['title_zh'])
        self.assertEqual(1, translate.pending('rss'))       # 还在队列里,下轮重试

    def test_existing_short_english_gets_relabelled_and_translated(self):
        # 存量那批已经按旧规则标成 zh 落在库里了,只改规则救不回来。
        self.put('Hello world', 'Short note.')
        with reg.db() as c:
            c.execute("UPDATE feed_items SET lang='zh'")
        self.assertEqual(0, translate.pending('rss'))
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n你好世界\n'
                                            '###BODY###\n短笔记。')):
            self.assertEqual(1, translate.translate_pending('rss')['done'])
        self.assertEqual('你好世界', self.item()['title_zh'])

    def test_queued_entries_are_marked_as_waiting_in_the_panel(self):
        # 一轮只译 MAX_ITEMS 条,还排在队列里的那些被扣在导出闸门外:vault 里
        # 没有这一份,面板也不能拿英文原文冒充库里那一份。
        self.put('An English post', 'Some English body text for the panel.')
        data = reg.items('rss')
        self.assertEqual((1, 1, 1), (data['total'], data['translating'],
                                     data['items'][0]['translating']))
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n一篇中文\n'
                                            '###BODY###\n中文正文。')):
            translate.translate_pending('rss')
        data = reg.items('rss')
        self.assertEqual((0, 0), (data['translating'], data['items'][0]['translating']))

    def test_without_a_key_the_panel_does_not_claim_a_translation_is_coming(self):
        # 没配 key 时导出闸门放行(原文入库),面板也就不该写"正在等翻译"
        # ——两边各判各的话,面板上标着等翻译的条目其实已经以英文躺在 vault 里。
        self.put('An English post', 'Some English body text for the panel.')
        with patch.object(translate, '_model', Mock(side_effect=RuntimeError('没配 key'))):
            data = reg.items('rss')
        self.assertEqual((0, 0), (data['translating'], data['items'][0]['translating']))

    def test_an_exported_entry_is_not_called_missing_after_a_key_arrives(self):
        # 没配 key 那阵子英文原文按原样进过 vault;补上 key 之后它重新排进
        # 翻译队列,但说它"暂未写入知识库"和文件、索引的事实正相反
        # (2026-09-30 复审实测 panel_translating_count=1 而 vault 里有这一篇)。
        self.put('An English post', 'Some English body text for the panel.')
        with patch.object(translate, '_model', Mock(side_effect=RuntimeError('没配 key'))):
            FeedCollector('rss').materialize()
        path = self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')
        self.assertTrue(path.exists())                 # 原文确实入库了
        data = reg.items('rss')                        # key 已恢复(setUp 的桩)
        item = data['items'][0]
        self.assertEqual((False, True), (item['translating'], item['retranslating']))
        self.assertEqual(0, data['translating'])       # 顶上那句只数"还没入库的"

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
        # 队列有出口:试满次数之后按原文导出,而不是把这条永远挡在知识库外面。
        sync.export('rss', hold_untranslated=True)
        text = (self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')).read_text()
        self.assertIn('Some English body text', text)
        self.assertNotIn('original_title', text)

    def test_an_entry_still_in_the_queue_does_not_land_in_english(self):
        # 一轮只译 MAX_ITEMS 条:一次进来 41 条英文时,没轮到的那条原来会先以
        # 英文写进 vault,而索引/提取/检索/日报读的全是 vault 里那份英文。
        self.put('An English post', 'English body waiting for its turn.')
        path = self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')
        sync.export('rss', hold_untranslated=True)
        self.assertFalse(path.exists())
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n中文标题\n'
                                            '###BODY###\n中文正文。')):
            translate.translate_pending('rss')
        sync.export('rss', hold_untranslated=True)
        self.assertIn('中文正文。', path.read_text())

    def test_without_an_api_key_english_still_reaches_the_vault(self):
        # 翻译整个被跳过时不能拦:那些条目会永远进不了知识库,英文总比没有强。
        self.put('Key-less English post', 'English body with no model configured.')
        with patch.object(translate, '_model', Mock(side_effect=RuntimeError('没配 key'))):
            FeedCollector('rss').materialize()
        path = self.root / 'vault' / 'source' / 'rss' / (self.item()['key'] + '.md')
        self.assertIn('English body with no model', path.read_text())

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
