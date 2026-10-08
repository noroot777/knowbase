"""外文订阅条目在导出进 vault 之前被译成中文:语言判定、队列口径、标题译文、
来源面板那两种"还没有中文"的状态、重导出不丢已有标签、翻译失败如实上报。

从 tests/test_daily_report.py 拆出来的(那份写到 682 行、逼近 700 行上限,而
翻译这一块最独立:只碰 kb/feeds/,不需要日报那套窗口与定时脚手架)。
"""
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kb import daily, extract, llm, maintenance, pipeline_state, scanner, search, store
from kb.chunker import split_chunks
from kb.collectors.feeds import FeedCollector
from kb.config import Source
from kb.feeds import registry as reg, sync, translate
from kb.scanner import parse_frontmatter

MODEL = llm.Model('openai', 'test-model')


class FeedCase(unittest.TestCase):
    """脚手架:临时 registry + 临时 vault + 一个固定模型。自己不带断言。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这些用例会去 flock 生产
        # 那一个,被 kb-web 正在跑的一轮索引(整趟持锁)挂住(见 kb.maintenance)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(translate, '_model', lambda: MODEL)):
            p.start(); self.addCleanup(p.stop)
        self.source = reg.save_subscription('rss', {'name': 'Blog', 'url': 'https://e.org/f.xml'})

    def put(self, title, body, published='2026-09-28 09:00'):
        reg.put_items(self.source, [{'url': 'https://e.org/p' + str(abs(hash(title)) % 999),
                                     'title': title, 'body': body, 'published': published}])

    def item(self, index=0):
        return reg.items('rss')['items'][index]


class TranslateTests(FeedCase):

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

    def legacy_partial(self):
        """装入老解析器留下的那份半成品:正文中文、标题空、完成时间非空、已导出。

        这形态是 2026-09-30 之前"漏掉 ###TITLE### 就整段当正文"的直接产物,
        真实可达(第 3 轮审查用它复现)。
        """
        self.put('A new agent runtime', 'This runtime schedules tools.')
        with reg.db() as c:
            c.execute("UPDATE feed_items SET title_zh='', body_zh='这个运行时调度工具。',"
                      " translated_at='2026-09-29T00:00:00+00:00',"
                      ' exported_hash=content_hash')

    def test_a_legacy_half_translated_entry_is_not_called_done(self):
        # 页脚原来按"任一字段有译文"写「已译为中文」,而标题明明是一行英文
        # (第 3 轮审查实测)。完成的判据前后端同一个:正文和标题都得有译文。
        self.legacy_partial()
        item = reg.items('rss')['items'][0]
        self.assertTrue(item['partial'])
        self.assertEqual((False, False), (item['translating'], item['retranslating']))

    def test_a_legacy_half_translated_entry_gets_its_title_filled_in(self):
        # 新队列判据只选 translated_at 空的,这批"完成时间非空、标题空"的存量
        # 永远选不到,再同步也不补——所以要一次幂等迁移把它们清回队列。
        self.legacy_partial()
        self.assertEqual(0, translate.pending('rss'))          # 迁移前选不到
        with patch.object(llm, 'chat', Mock(return_value='全新的智能体运行时')) as chat:
            summary = translate.translate_pending('rss')
        self.assertEqual(1, summary['done'])
        # 只补标题那一次调用:正文已经是中文,整篇重译是把同一份正文再买一遍
        chat.assert_called_once()
        item = self.item()
        self.assertEqual('全新的智能体运行时', item['title_zh'])
        self.assertEqual('这个运行时调度工具。', item['body_zh'])
        self.assertEqual(0, translate.pending('rss'))          # 补完就出队
        # 补完之后才轮到「已译为中文」,而且 md 会被重写(exported_hash 清了)
        panel = reg.items('rss')['items'][0]
        self.assertEqual((False, False, False),
                         (panel['partial'], panel['translating'], panel['retranslating']))
        self.assertEqual('', panel['exported_hash'])

    def test_requeueing_legacy_partials_is_idempotent(self):
        # 幂等:清回一次之后它由正常流程收尾,不会每轮都被重新清一遍。
        self.legacy_partial()
        self.assertEqual(1, translate._requeue_partial('rss'))
        self.assertEqual(0, translate._requeue_partial('rss'))
        # 整条译好的不会被误当半成品清回去
        with patch.object(llm, 'chat', Mock(return_value='全新的智能体运行时')):
            translate.translate_pending('rss')
        self.assertEqual(0, translate._requeue_partial('rss'))

    def test_a_title_only_entry_is_not_mistaken_for_a_partial(self):
        # 只有标题的条目译完之后 body_zh 本来就是空的,它不是半成品。
        self.put('Great', '')
        with patch.object(llm, 'chat', Mock(return_value='很棒')):
            translate.translate_pending('rss')
        self.assertEqual(0, translate._requeue_partial('rss'))
        self.assertFalse(reg.items('rss')['items'][0]['partial'])

    def test_translated_text_is_searchable_in_the_panel(self):
        self.put('An English title', 'English body about agents and runtimes.')
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n中文标题\n###BODY###\n关于智能体的正文。')):
            translate.translate_pending('rss')
        self.assertEqual(1, reg.items('rss', q='智能体')['total'])


class ExportStageTests(FeedCase):
    """翻译失败不许在阶段状态里蒸发。

    导出是这条链上最后一个报阶段状态的人:失败只写进日志的话,它随后把阶段刷成
    绿色,日报那头照抄成"同步完成"——而那一轮的新文章一篇都没进知识库
    (2026-09-30 审查实测)。
    """

    def setUp(self):
        super().setUp()
        p = patch.dict(pipeline_state._state, {
            'stages': deepcopy(pipeline_state._state['stages']),
            'log': [], 'stop_requested': False})
        p.start(); self.addCleanup(p.stop)

    def stage(self):
        return pipeline_state._state['stages']['export']

    def test_a_translation_failure_turns_the_export_stage_red(self):
        self.put('New release today', 'A new release became available.')
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('HTTP 503 translator'))):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('error', stage['status'])
        self.assertIn('1 条翻译失败', stage['detail'])
        # 原因要留在明细里:不然用户只看到一个红点,重跑一次才知道是 503
        self.assertIn('HTTP 503', json.dumps(stage.get('failures'), ensure_ascii=False))
        self.assertFalse(list((self.root / 'vault' / 'source' / 'rss').glob('*.md')))

    def test_a_normal_backlog_is_not_reported_as_a_failure(self):
        # "这一轮只译得下 MAX_ITEMS 条,其余下一轮"是正常积压,不是故障。两者
        # 合成一个数就等于把故障说成积压(或者反过来天天报红)。
        self.put('First English post', 'Body one in English.')
        self.put('Second English post', 'Body two in English.')
        with patch.object(translate, 'MAX_ITEMS', 1), \
                patch.object(llm, 'chat', Mock(return_value='###TITLE###\n中文标题\n'
                                               '###BODY###\n中文正文。')):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('ok', stage['status'])
        self.assertIn('1 条待翻译', stage['detail'])
        self.assertNotIn('翻译失败', stage['detail'])

    def test_giving_up_after_max_tries_is_not_reported_as_a_failure(self):
        # 试满 MAX_TRIES 之后按原文入库是设计好的出口:内容进了知识库,阶段不该
        # 一直红着——否则这一条永远译不动的内容会把每一轮同步都标成失败。
        self.put('Stubborn English post', 'Body that never gets translated.')
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('502 bad gateway'))):
            for _ in range(translate.MAX_TRIES - 1):
                FeedCollector('rss').materialize()
            self.assertEqual('error', self.stage()['status'])   # 还扣着,还没入库
            FeedCollector('rss').materialize()      # 第三次失败:出队,按原文入库
        self.assertEqual('ok', self.stage()['status'])
        self.assertIn('已保存 1 条', self.stage()['detail'])

    def test_a_backfill_failure_on_a_shipped_entry_is_not_called_success(self):
        # 按英文导出过、内容又没变的条目 exported_hash==content_hash,压根不在
        # "待导出"集合里,可它照样排在队列里等补译。拿待导出集合去数失败,这一
        # 档一条也数不出来:导出阶段绿、日报记「同步完成」,而库里躺的还是英文、
        # 素材清单里就是那行英文标题(2026-09-30 第 2 轮审查实测)。
        self.put('Legacy release', 'Legacy release supports agent scheduling.')
        sync.export('rss')                  # 没配 key 那阵子按英文入过库
        path = self.root / 'vault/source/rss' / (self.item()['key'] + '.md')
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('HTTP 503 translator'))):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('error', stage['status'])
        self.assertIn('1 条补译失败（库里仍是原文）', stage['detail'])
        self.assertIn('HTTP 503', json.dumps(stage.get('failures'), ensure_ascii=False))
        # 原文照样读得到:补译失败不该把已经入库的内容撤下来
        self.assertIn('Legacy release supports agent scheduling.',
                      path.read_text(encoding='utf-8'))

    def test_a_shipped_entry_waiting_its_turn_is_not_called_uningested(self):
        # 已经按原文入库、只是还没轮到补译的那些:说它"译完再入库"是谎报——
        # 它此刻在 vault 里读得到,等的只是中文覆盖上去。
        self.put('First legacy post', 'Body one in English.')
        self.put('Second legacy post', 'Body two in English.')
        sync.export('rss')
        with patch.object(translate, 'MAX_ITEMS', 1), \
                patch.object(llm, 'chat', Mock(return_value='###TITLE###\n中文标题\n'
                                               '###BODY###\n中文正文。')):
            FeedCollector('rss').materialize()
        stage = self.stage()
        self.assertEqual('ok', stage['status'])
        self.assertIn('1 条等待中文译文覆盖', stage['detail'])
        self.assertNotIn('待翻译', stage['detail'])
        self.assertNotIn('翻译失败', stage['detail'])


class MixedBatchTests(unittest.TestCase):
    """同一轮里有译成的也有译不成的:译成的那些必须照样进知识库。

    实测过的坏结果:导出收尾那句汇总里,形参 `translated`(这一轮的翻译摘要)
    被循环中同名的布尔变量覆盖,`.get('errors')` 当场 AttributeError —— 那时
    成功的文章已经落盘、exported_hash 也更新过,异常却把 materialize 连同后面
    的 index/extract 一起带崩:同一轮译成的新文章一篇都进不了检索和当天日报,
    页面上只说这个来源没同步成功(2026-09-30 第 2 轮审查实测)。

    所以 setUp 里那一趟 `materialize()` 本身就是断言:它抛异常这一整类就红。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # 维护锁的 lockfile 在 DATA_DIR 下:这两类都真走 `sync.export`(整段在锁
        # 里),不指到 tmp 就会去 flock **生产**那一个,被 kb-web 正在跑的一轮索引
        # (整趟持锁)挂住几分钟(见 kb.maintenance)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.vault),
                  patch.object(scanner, 'VAULT_ROOT', self.vault),
                  patch.object(translate, '_model', lambda: MODEL),
                  patch.dict(pipeline_state._state, {
                      'stages': deepcopy(pipeline_state._state['stages']),
                      'log': [], 'stop_requested': False})):
            p.start(); self.addCleanup(p.stop)
        self.conn = store.connect(self.root / 'kb.db')
        self.addCleanup(self.conn.close)
        source = reg.save_subscription('rss', {'name': 'Blog', 'url': 'https://e.org/f.xml'})
        for key, title in (('good', 'Good release'), ('bad', 'Bad release')):
            reg.put_items(source, [{'url': f'https://e.org/{key}', 'title': title,
                                    'body': f'{title} supports agent tool scheduling.',
                                    'published': '2026-09-28 09:00'}])
        with patch.object(llm, 'chat', self.answer):
            FeedCollector('rss').materialize()

    @staticmethod
    def answer(prompt, *args, **kwargs):
        if 'Bad release' in prompt:
            raise RuntimeError('HTTP 503 translator unavailable')
        return '###TITLE###\n新发布\n###BODY###\n这个新发布支持智能体工具调度。'

    def index(self) -> list[str]:
        """把 vault 里此刻的东西真的索引一遍,返回入库的路径。"""
        spec = Source(name='rss', root=self.vault / 'source/rss')
        paths = []
        for path in sorted(spec.root.glob('*.md')):
            doc = scanner._doc_from_file(path, spec)
            store.upsert_document(self.conn, doc, split_chunks(doc.body),
                                  '2026-09-28 09:30:00')
            paths.append(str(path.relative_to(self.vault)))
        self.conn.commit()
        return paths

    def test_the_translated_article_still_lands_in_the_vault(self):
        files = sorted((self.vault / 'source/rss').glob('*.md'))
        self.assertEqual(1, len(files))                 # 译成的那篇进来了
        self.assertIn('这个新发布支持智能体工具调度。', files[0].read_text(encoding='utf-8'))
        # 译不成的那篇仍然扣着,重试次数和原因都记上了
        bad = [i for i in reg.items('rss')['items'] if i['title'] == 'Bad release'][0]
        self.assertEqual(1, bad['translate_tries'])
        self.assertIn('HTTP 503', bad['translate_error'])
        self.assertEqual('', bad['exported_hash'])

    def test_the_round_is_reported_as_partly_failed(self):
        stage = pipeline_state._state['stages']['export']
        self.assertEqual('error', stage['status'])
        self.assertIn('已保存 1 条', stage['detail'])
        self.assertIn('1 条翻译失败', stage['detail'])
        self.assertIn('HTTP 503', json.dumps(stage.get('failures'), ensure_ascii=False))

    def test_the_translated_article_is_searchable(self):
        paths = self.index()
        hits = search.search(self.conn, '智能体', search.Filters(), use_vector=False)
        self.assertEqual(paths, [h.path for h in hits])

    def test_the_translated_article_reaches_the_same_day_report(self):
        paths = self.index()
        mat = daily.collect(self.conn, datetime(2026, 9, 28), datetime(2026, 9, 28, 23, 59))
        self.assertEqual(paths, [d['path'] for d in mat.items])
        self.assertEqual(['新发布'], [d['title'] for d in mat.items])


class ReexportMetadataTests(unittest.TestCase):
    """补译会重写已经导出过的 md —— 提取回写的标签必须留下来。

    实测过的坏结果:一篇已提取的英文旧文补成中文后,frontmatter 里的 tags 被
    整份重写抹掉,索引按 frontmatter 重建 doc_tags,而 `extracted_at` 还在、
    提取不会再做一遍 —— 按原话题检索当场从 1 条归零,再同步也不恢复
    (2026-09-30 审查实测)。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vault = self.root / 'vault'
        # 维护锁的 lockfile 在 DATA_DIR 下:这两类都真走 `sync.export`(整段在锁
        # 里),不指到 tmp 就会去 flock **生产**那一个,被 kb-web 正在跑的一轮索引
        # (整趟持锁)挂住几分钟(见 kb.maintenance)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(sync, 'VAULT_ROOT', self.vault),
                  patch.object(scanner, 'VAULT_ROOT', self.vault),
                  patch.object(extract, 'VAULT_ROOT', self.vault),
                  patch.object(translate, '_model', lambda: MODEL)):
            p.start(); self.addCleanup(p.stop)
        self.conn = store.connect(self.root / 'kb.db')
        self.addCleanup(self.conn.close)
        source = reg.save_subscription('rss', {'name': 'Blog', 'url': 'https://e.org/f.xml'})
        reg.put_items(source, [{'url': 'https://e.org/old', 'title': 'Agent runtime',
                                'body': 'The runtime supports tool scheduling.',
                                'published': '2026-09-28 09:00'}])
        # 没配 key 那阵子英文原文是按原样入过库的,提取也跑过了(这就是补译要
        # 重写的那批文件)。
        sync.export('rss')
        self.path = self.vault / 'source/rss' / (reg.items('rss')['items'][0]['key'] + '.md')
        self.spec = Source(name='rss', root=self.path.parent)
        self.index('2026-09-28 09:10:00')
        data = {'entities': [{'name': 'AgentRuntime', 'kind': 'project'}],
                'topics': ['ai-coding'], 'summary': '智能体工具调度运行时'}
        rel = str(self.path.relative_to(self.vault))
        stale = extract.save_extraction(self.conn, rel, data)
        extract.writeback_extraction(self.conn, rel, data, stale)
        self.conn.commit()

    def index(self, now):
        doc = scanner._doc_from_file(self.path, self.spec)
        store.upsert_document(self.conn, doc, split_chunks(doc.body), now)
        self.conn.commit()
        return doc

    def tags(self):
        return sorted(r[0] for r in self.conn.execute(
            'SELECT tag FROM doc_tags WHERE path=?',
            (str(self.path.relative_to(self.vault)),)))

    def translate_and_reexport(self):
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n智能体运行时\n'
                                            '###BODY###\n运行时支持工具调度。')):
            self.assertEqual(1, translate.translate_pending('rss')['done'])
        sync.export('rss', hold_untranslated=True)

    def test_a_backfilled_translation_keeps_the_extracted_tags(self):
        self.assertEqual(['entity/agentruntime', 'topic/ai-coding'], self.tags())
        self.translate_and_reexport()
        self.assertIn('运行时支持工具调度。', self.path.read_text(encoding='utf-8'))
        # 文件本身要留住 tags:doc_tags 是按 frontmatter 重建的
        meta, _ = parse_frontmatter(self.path.read_text(encoding='utf-8'))
        self.assertEqual(['entity/agentruntime', 'topic/ai-coding'], sorted(meta['tags']))
        self.index('2026-09-28 09:20:00')
        self.assertEqual(['entity/agentruntime', 'topic/ai-coding'], self.tags())

    def test_the_topic_filter_still_finds_it_after_the_backfill(self):
        rel = str(self.path.relative_to(self.vault))
        hits = search.search(self.conn, 'runtime',
                             search.Filters(tags=['topic/ai-coding']), use_vector=False)
        self.assertEqual([rel], [h.path for h in hits])
        self.translate_and_reexport()
        self.index('2026-09-28 09:20:00')
        # 提取不会重做(extracted_at 还在),所以标签只能靠导出保留下来
        self.assertNotIn(rel, [r['path'] for r in extract.pending_docs(self.conn)])
        hits = search.search(self.conn, '运行时',
                             search.Filters(tags=['topic/ai-coding']), use_vector=False)
        self.assertEqual([rel], [h.path for h in hits])

    def test_export_still_owns_its_own_fields(self):
        # 保留别人的字段,不等于把自己那几个键也冻住:标题/语言标记每轮按
        # registry 的当前事实重写,否则译完了 md 里还写着 lang: en。
        self.translate_and_reexport()
        meta, _ = parse_frontmatter(self.path.read_text(encoding='utf-8'))
        self.assertEqual('智能体运行时', meta['title'])
        self.assertEqual('Agent runtime', meta['original_title'])
        self.assertEqual('zh', meta['lang'])


class ColumnBackfillTests(unittest.TestCase):
    """译文那几列是 ALTER 补上去的:补列和它前面那次列集合读取必须在同一把写锁里。

    分开跑的话,升级后的第一次并发访问会有一个请求 500(`duplicate column name`,
    2026-09-30 审查实测)。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'feeds.db'
        p = patch.object(reg, 'DB_PATH', self.db)
        p.start(); self.addCleanup(p.stop)
        old = sqlite3.connect(self.db)              # 一个还没有译文列的老库
        old.executescript(reg.SCHEMA)
        old.commit(); old.close()

    def test_the_backfill_runs_inside_the_write_lock(self):
        seen, real = [], reg._migrate
        with patch.object(reg, '_migrate',
                          lambda c: (seen.append(c.in_transaction), real(c))[1]):
            with reg.db() as c:
                c.execute('SELECT count(*) FROM feed_items').fetchone()
        self.assertEqual([True], seen)

    def test_two_first_accesses_both_succeed(self):
        # 屏障卡在"列集合已读出来"那一刻:锁外跑时两个连接都能走到这里,于是
        # 后一个 ALTER 撞 duplicate;锁内跑时第二个还堵在 BEGIN IMMEDIATE 上,
        # 屏障超时后由第一个自己走完(所以超时要吞掉,不能当失败)。
        barrier = threading.Barrier(2)
        connect = sqlite3.connect

        class Watched:
            def __init__(self, inner):
                object.__setattr__(self, 'inner', inner)

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def __setattr__(self, name, value):     # row_factory 要落到真连接上
                setattr(self.inner, name, value)

            def execute(self, sql, *a):
                rows = self.inner.execute(sql, *a)
                if sql.startswith('PRAGMA table_info'):
                    rows = rows.fetchall()
                    try:
                        barrier.wait(timeout=1)
                    except threading.BrokenBarrierError:
                        pass
                return rows

        def read(_):
            try:
                with reg.db() as c:
                    c.execute('SELECT count(*) FROM feed_items').fetchone()
                return ''
            except Exception as e:                   # noqa: BLE001 - 就是要看清它
                return f'{type(e).__name__}: {e}'

        with patch.object(reg, 'sqlite3', SimpleNamespace(
                connect=lambda *a, **k: Watched(connect(*a, **k)), Row=sqlite3.Row)), \
                ThreadPoolExecutor(max_workers=2) as pool:
            problems = [p for p in pool.map(read, (1, 2)) if p]
        self.assertEqual([], problems)
        with reg.db() as c:
            self.assertIn('lang', {r['name'] for r in
                                   c.execute('PRAGMA table_info(feed_items)')})


if __name__ == '__main__':
    unittest.main()
