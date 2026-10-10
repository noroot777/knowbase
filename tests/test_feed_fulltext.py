# -*- coding: utf-8 -*-
"""聚合型订阅的正文补抓(kb/feeds/fulltext.py + readable.py)。

被测的那件事:HN Buzzing 那类来源的 feed 正文只有几十个字符 —— 标题、域名、
发帖时刻、`↑ 101 HN Points`。开了补抓开关之后,同步时顺着原文链接把正文取回来,
**在翻译之前**替换掉那份壳,然后才翻译、才写进 vault。

HTTP 传输那一层(`fetching.request`)打桩,别的一行不打:解码、正文提取、队列
取数、落库、翻译与导出两道闸门全部真跑。打桩打在传输上是因为 `request` 只连
公网地址(SSRF 防护),本地测试服务器连不上 —— 而那一层有它自己的回归。

**「原文链接后面不是网页」那一组(PDF、宽编码)在 `tests/test_fulltext_binary.py`**:
它自带一份结构有效的 PDF 和一张编码样本表,挤在这儿是 737 行、越过 700 行上限。
那边复用这里的 `FullTextCase` / `PAGE` / `SHELL`。
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import config, llm, maintenance
from kb.collectors.feeds import FeedCollector
from kb.feeds import fulltext, readable, registry as reg, sync, translate
from kb.feeds.fetching import FeedError, Response
from kb.scanner import parse_frontmatter

MODEL = llm.Model('openai', 'test-model')

# 库里那 567 条壳长的就是这样(2026-10-09 从生产 feeds.db 取的真实形态)。
SHELL = 'Yes, and (\nhtmx.org\n)\n10-08\n↑ 108 HN Points'

# 一篇正常文章页:正文在 <article> 里,外面套着站点页眉、侧栏、页脚和评论区。
ARTICLE_BODY = (
    '<p>' + 'The runtime schedules tools for coding agents and keeps the transcript. ' * 6 + '</p>'
    '<h2>Why it matters</h2>'
    '<p>' + 'Every call is replayed from the same deterministic log, so a crash resumes. ' * 6 + '</p>')
PAGE = (
    '<html><head><title>Yes, and</title></head><body>'
    '<header class="site-header"><nav><a href="/a">Home</a><a href="/b">Archive</a></nav></header>'
    '<div class="wrapper"><div class="col"><article class="post">'
    '<h1>Yes, and</h1>' + ARTICLE_BODY + '</article></div>'
    '<aside class="sidebar"><div class="share"><a href="/x">Tweet</a></div>'
    '<ul class="related"><li><a href="/1">One</a></li><li><a href="/2">Two</a></li></ul></aside></div>'
    '<section class="comments"><p>' + 'Nice post, thanks for writing it up. ' * 30 + '</p></section>'
    '<footer class="site-footer">© 2026</footer></body></html>')


def page(html: str, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, 'https://e.org/post', html.encode(),
                    headers or {'content-type': 'text/html; charset=utf-8'})


class FullTextCase(unittest.TestCase):
    """脚手架:临时 registry + 临时 vault + 一条开了补抓的订阅。自己不带断言。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # 维护锁的 lockfile 在 DATA_DIR 下:不指到 tmp 的话这些用例会去 flock
        # 生产那一个,被 kb-web 正在跑的一轮索引挂住(见 kb.maintenance)。
        # kb.db 同理指到 tmp:导出收尾会让补抓过的条目在知识库里那份旧提取结果
        # 作废(`fulltext.retire_stale`),不指的话这些用例写的是生产索引库。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(translate, '_model', lambda: MODEL)):
            p.start(); self.addCleanup(p.stop)
        # `extract: True` 不是顺手加的:feeds 那几类来源默认不买提取(订阅级
        # 开关,见 `extract.pending_docs`),不开的话「正文从壳换成全文之后要
        # 重新提取」那一组(test_fulltext_extract_stale / test_extraction_
        # body_version)的 `pending` 恒为空 —— 它们要验的东西会被另一道闸遮掉。
        # 这条订阅在语境里正是「值得提取的聚合源」,开着才对得上。
        self.source = reg.save_subscription(
            'rss', {'name': '聚合源', 'url': 'https://agg.example.org/feed.xml',
                    'fulltext': True, 'extract': True})
        self.plain = reg.save_subscription(
            'rss', {'name': '单号源', 'url': 'https://solo.example.org/feed.xml'})

    def put(self, source=None, url='https://e.org/post', title='Yes, and', body=SHELL):
        reg.put_items(source or self.source,
                      [{'url': url, 'title': title, 'body': body,
                        'published': '2026-10-08 09:00', 'content_kind': 'summary'}])
        # key 直接按 registry 那条规则算,不去列表里取第 0 条:排序是
        # published DESC,几条同一时刻的条目之间谁在前面不该由用例来猜。
        return hashlib.sha256(b'rss:' + url.encode()).hexdigest()[:32]

    def row(self, key):
        with reg.db() as c:
            return dict(c.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone())

    def item(self, key):
        return next(i for i in reg.items('rss')['items'] if i['key'] == key)


class ExtractionTests(unittest.TestCase):
    """正文提取本身:认出正文那一块,把页面外围部件挡在外面。"""

    def test_picks_the_article_and_drops_the_furniture(self):
        text = readable.extract(PAGE)
        self.assertIn('The runtime schedules tools for coding agents', text)
        self.assertIn('Why it matters', text)
        for noise in ('Home', 'Archive', 'Tweet', 'Nice post', '© 2026'):
            self.assertNotIn(noise, text)
        # 段落边界还在:翻译按 '\n\n' 切块、vault md 按它分段(见 fetching.plain)。
        self.assertIn('\n\n', text)

    def test_a_nested_decomposed_node_does_not_crash(self):
        # `find_all` 交出来的是快照,父节点 decompose 之后列表里的后代就成了
        # 已销毁节点,再碰一下就是 AttributeError —— 实测 ken.arneson.name 和
        # frontiersin.org 两篇真实页面都当场抛出来(见 readable._gone)。
        nested = ('<html><body>'
                  '<div class="sidebar"><div class="share"><a href="/x">Tweet</a></div>'
                  '<ul class="related"><li class="promo"><a href="/1">One</a></li></ul></div>'
                  '<article>' + ARTICLE_BODY + '</article></body></html>')
        text = readable.extract(nested)
        self.assertIn('The runtime schedules tools', text)
        self.assertNotIn('Tweet', text)

    def test_a_link_list_is_not_mistaken_for_an_article(self):
        links = ''.join(f'<li><a href="/p{i}">A fairly long headline number {i} here</a></li>'
                        for i in range(80))
        self.assertEqual('', readable.extract(f'<html><body><main><ul>{links}</ul></main></body></html>'))

    def test_a_page_without_a_body_block_yields_nothing(self):
        self.assertEqual('', readable.extract('<html><body><p>Too short.</p></body></html>'))
        self.assertEqual('', readable.extract(''))

    def test_an_oversized_page_is_refused_rather_than_chewed_on(self):
        self.assertEqual('', readable.extract('<p>x</p>' * 10 + 'y' * readable.MAX_HTML))

    def test_wrapper_chain_shrinks_to_the_article(self):
        # `<div class="box"><div><article>…` 这种层层包装很常见,外层只多出
        # 几个字却因为更长而得分更高 —— 取外层就会把面包屑一起带进正文。
        wrapped = ('<html><body><div class="box"><p>Home / Essays</p><div class="inner">'
                   '<article>' + ARTICLE_BODY + '</article></div></div></body></html>')
        self.assertNotIn('Home / Essays', readable.extract(wrapped))

    def test_semantic_header_inside_an_article_is_kept(self):
        # `<article class="post-header">` 里的 header 说的是"文章的开头",不是
        # "站点页眉"。按 class 名把语义标签也删掉会从正文顶上切掉一截。
        inner = ('<html><body><article><header class="post-header"><h1>Yes, and</h1>'
                 '<p>By Carson Gross, February 2026</p></header>' + ARTICLE_BODY + '</article></body></html>')
        self.assertIn('By Carson Gross', readable.extract(inner))


class QueueTests(FullTextCase):
    """队列口径:谁进、谁不进、出口在哪。"""

    def test_only_subscriptions_with_the_switch_on_are_queued(self):
        on = self.put()
        off = self.put(self.plain, url='https://k.org/post', title='Deep dive')
        self.assertTrue(self.row(on)['fulltext_at'] == '')
        self.assertEqual({on}, self.queued())
        self.assertNotIn(off, self.queued())

    def test_an_item_shared_with_a_plain_source_is_still_queued(self):
        # 一条内容可能同时属于聚合源和单号源(两边收同一篇)。任意一条订阅开了
        # 补抓就该抓 —— 补到的正文对另一条同样有用。
        key = self.put()
        reg.put_items(self.plain, [{'url': 'https://e.org/post', 'title': 'Yes, and',
                                    'body': SHELL, 'published': '2026-10-08 09:00'}])
        self.assertEqual({key}, self.queued())

    def test_the_queue_has_an_exit_after_max_tries(self):
        key = self.put()
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fulltext_tries=? WHERE key=?', (fulltext.MAX_TRIES, key))
        self.assertEqual(set(), self.queued())

    def test_a_fetched_item_leaves_the_queue(self):
        self.put()
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))):
            fulltext.fetch_pending('rss')
        self.assertEqual(set(), self.queued())

    def queued(self):
        cond, args = fulltext.queued_sql()
        with reg.db() as c:
            return {r[0] for r in c.execute(
                f'SELECT key FROM feed_items WHERE {cond}', args)}


class FetchTests(FullTextCase):
    """抓一轮之后库里是什么样。"""

    def test_the_shell_is_replaced_and_the_item_is_requeued_for_translation(self):
        key = self.put()
        with reg.db() as c:          # 这一条此前按壳译过:那份译文译的是壳,作废
            c.execute("""UPDATE feed_items SET title_zh='是的，而且',body_zh='壳的译文',
                translated_at='2026-10-08 10:00',translate_tries=2,exported_hash='old' WHERE key=?""",
                (key,))
        before = self.row(key)['content_hash']
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))) as req:
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(1, summary['done'])
        after = self.row(key)
        self.assertIn('The runtime schedules tools for coding agents', after['body'])
        self.assertNotIn('HN Points', after['body'])
        self.assertEqual('content', after['content_kind'])   # 不再是「订阅源提供的摘要」
        self.assertNotEqual(before, after['content_hash'])   # 导出闸门靠它重写 md
        # **`exported_hash` 原样留着**:它是「这一篇入没入过库」的事实来源,而
        # 重导出已经由 content_hash 变了这件事担保。清掉它的代价是一篇已经能
        # 展开阅读的旧条目在面板上连标题一起消失(见 `fulltext._save`)。
        self.assertEqual('old', after['exported_hash'])
        self.assertEqual(1, after['extract_stale'])          # 旧摘要/主题待作废
        self.assertEqual('en', after['lang'])
        # **旧译文必须一起作废**:它译的是那份壳,留着的话导出会把"壳的译文"
        # 当成这篇文章的中文正文写进 vault,而 translated_at 非空意味着再也
        # 不会有人去订正它。
        self.assertEqual(('', '', '', 0), (after['title_zh'], after['body_zh'],
                                           after['translated_at'], after['translate_tries']))
        # 抓的是网页不是 feed:Accept 要换,否则有些站回一份 XML 或者直接 406。
        self.assertIn('text/html', req.call_args[0][1]['Accept'])
        self.assertEqual('https://e.org/post', req.call_args[0][0])

    def test_a_failed_fetch_counts_a_try_and_keeps_the_shell(self):
        key = self.put()
        with patch.object(fulltext, 'request', Mock(side_effect=FeedError('订阅连接超时'))):
            summary = fulltext.fetch_pending('rss')
        row = self.row(key)
        self.assertEqual((1, [key]), (summary['failed'], summary['failed_keys']))
        self.assertEqual(SHELL, row['body'])        # 壳留着:条目照旧读得到
        self.assertEqual(1, row['fulltext_tries'])
        self.assertIn('超时', row['fulltext_error'])
        self.assertEqual('', row['fulltext_at'])    # 还在队列里,下一轮重试

    def test_a_page_without_a_recognisable_body_also_counts_a_try(self):
        # 不计次的话这一条永远排在队首,每一轮都重抓同一个读不懂的页面(付费墙、
        # 纯 JS 渲染),而它永远不会离开队列、也就永远挡着后面的条目。
        key = self.put()
        with patch.object(fulltext, 'request',
                          Mock(return_value=page('<html><body><p>Please enable JS.</p></body></html>'))):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((1, 0), (summary['empty'], summary['done']))
        self.assertEqual((SHELL, 1), (self.row(key)['body'], self.row(key)['fulltext_tries']))

    def test_one_bad_link_does_not_take_the_round_down(self):
        good = self.put()
        bad = self.put(url='https://e.org/broken', title='Broken')
        calls = {'https://e.org/post': page(PAGE)}
        with patch.object(fulltext, 'request',
                          Mock(side_effect=lambda u, h=None, **k: calls.get(u) or (_ for _ in ()).throw(
                              FeedError('HTTP 404')))):
            summary = fulltext.fetch_pending('rss')
        self.assertEqual((1, 1), (summary['done'], summary['failed']))
        self.assertIn('coding agents', self.row(good)['body'])
        self.assertEqual(SHELL, self.row(bad)['body'])

    def test_retried_items_are_taken_first(self):
        # 升序排的话重试永远排在"从没试过"的后面:几百条积压下要几十轮同步才
        # 轮得到一次重试,而面板每一轮都在报同一个数字(口径同 translate)。
        fresh = self.put(url='https://e.org/fresh', title='Fresh')
        retry = self.put(url='https://e.org/retry', title='Retry')
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fulltext_tries=1 WHERE key=?', (retry,))
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))):
            fulltext.fetch_pending('rss', limit=1)
        self.assertNotEqual('', self.row(retry)['fulltext_at'])
        self.assertEqual('', self.row(fresh)['fulltext_at'])

    def test_nothing_to_do_when_no_source_opted_in(self):
        self.put(self.plain, url='https://k.org/post', title='Deep dive')
        with patch.object(fulltext, 'request', Mock()) as req:
            summary = fulltext.fetch_pending('rss')
        self.assertEqual(0, req.call_count)
        self.assertEqual({'done': 0, 'failed': 0, 'empty': 0, 'left': 0,
                          'errors': [], 'failed_keys': [], 'stopped': False}, summary)


class ReadingStateTests(FullTextCase):
    """补抓成功到译文落盘之间,已入库的旧条目不许丢掉"读得到"这个事实。

    `exported_hash` 是「这一篇入没入过库」的事实来源。补抓时清掉它的代价实测
    过:一篇**已经能展开阅读**的旧文章在面板上标题连同「展开阅读」一起消失、
    换成一句"译成中文之后才写进知识库",而 vault 里那份 md 好好地躺着。而且这
    不是一瞬间的事 —— 翻译排队或失败时它会一直这样(2026-10-09 审查实测+截图)。
    """

    def shipped(self):
        """一条已经按壳导出过的旧条目(打开这个开关时看到的就是这一批)。"""
        reg.save_subscription('rss', {'fulltext': False}, self.source['id'])
        key = self.put()
        sync.export('rss')
        reg.save_subscription('rss', {'fulltext': True}, self.source['id'])
        return key

    def test_a_shipped_item_keeps_its_title_and_reader_after_the_fetch(self):
        key = self.shipped()
        before = self.item(key)
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))):
            fulltext.fetch_pending('rss')
        after = self.item(key)
        # 后果先断言:这一档说的是「外文原文已入库,正在等中文译文覆盖」,而不是
        # 「译成中文之后才写进知识库」—— 前端按 `translating` 收走标题和阅读入口。
        self.assertEqual((False, True), (after['translating'], after['retranslating']))
        self.assertFalse(after['fetching'] or after['refetching'])
        self.assertTrue((self.root / 'vault' / after['path']).is_file())
        self.assertEqual('Yes, and', after['title'])
        # 反过来钉一边:抓之前它确实是"已入库 + 待补抓"那一档。
        self.assertEqual((False, True), (before['fetching'], before['refetching']))

    def test_a_brand_new_item_is_still_held_back(self):
        """反过来钉一边:从没导出过的条目照旧扣在闸门外(一律放行的实现会红)。"""
        key = self.put()
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))):
            fulltext.fetch_pending('rss')
        after = self.item(key)
        self.assertEqual((True, False), (after['translating'], after['retranslating']))
        self.assertEqual('', self.row(key)['exported_hash'])

    def test_the_new_body_is_still_exported_over_the_old_shell(self):
        """保留 `exported_hash` 不能顺带把重导出也拦掉:闸门靠 content_hash 变。"""
        key = self.shipped()
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))), \
                patch.object(llm, 'chat',
                             Mock(return_value='###TITLE###\n是的，而且\n###BODY###\n正文译文')):
            FeedCollector('rss').materialize()
        text = (self.root / 'vault' / 'source' / 'rss' / (key + '.md')).read_text()
        self.assertIn('正文译文', text)
        self.assertNotIn('HN Points', text)
        row = self.row(key)
        self.assertEqual(row['content_hash'], row['exported_hash'])


class GateTests(FullTextCase):
    """两道闸门:还在补抓队列里的条目既不翻译也不导出。"""

    def test_an_unfetched_item_is_not_translated(self):
        # 顺序反过来的代价是实打实的:先按壳译一遍、补上正文再译一遍,同一条
        # 内容买两次模型调用,而第一份译文随后就被 `fulltext._save` 丢掉。
        self.put()
        with patch.object(llm, 'chat', Mock()) as chat:
            summary = translate.translate_pending('rss')
        self.assertEqual(0, chat.call_count)
        self.assertEqual(0, summary['done'])

    def test_an_unfetched_item_is_not_exported(self):
        key = self.put()
        sync.export('rss')
        self.assertFalse((self.root / 'vault' / 'source' / 'rss' / (key + '.md')).exists())
        self.assertEqual('', self.row(key)['exported_hash'])

    def test_an_item_from_a_plain_source_is_exported_as_before(self):
        # 反过来钉一边:没开开关的来源一个字都不该受影响。两道闸门都是无条件
        # 加在 WHERE 里的,判反了的话整个 rss 来源会一起被扣住。
        key = self.put(self.plain, url='https://solo.example.org/post', title='Deep dive',
                       body='A long enough body for the exporter to take seriously.')
        sync.export('rss')
        self.assertTrue((self.root / 'vault' / 'source' / 'rss' / (key + '.md')).exists())

    def test_the_exhausted_item_falls_through_both_gates(self):
        # 队列的出口:抓不动的条目按 feed 原本那份壳走完翻译和导出。一个付费墙
        # 后面的链接不该让这条内容永远进不了知识库。
        key = self.put()
        with reg.db() as c:
            c.execute('UPDATE feed_items SET fulltext_tries=? WHERE key=?', (fulltext.MAX_TRIES, key))
        with patch.object(llm, 'chat', Mock(return_value='###TITLE###\n是的，而且\n###BODY###\n壳的译文')):
            translate.translate_pending('rss')
        sync.export('rss')
        self.assertTrue((self.root / 'vault' / 'source' / 'rss' / (key + '.md')).exists())

    def test_the_panel_says_which_items_are_waiting_for_a_body(self):
        key = self.put()
        data = reg.items('rss')
        self.assertEqual(1, data['fetching'])
        self.assertTrue(self.item(key)['fetching'])
        # 四档互斥:补抓那一档排在最前面,而 translate.queued_sql 已经把这批
        # 条目排除在翻译队列之外 —— 同一条不会既说"等翻译"又说"等正文"。
        self.assertEqual([False, False, False],
                         [self.item(key)[k] for k in ('translating', 'retranslating', 'partial')])

    def test_a_shell_already_in_the_vault_is_a_separate_tier(self):
        """「在队列里」和「没入库」是两件事,按 `exported_hash` 分两态。

        打开这个开关最常见的时机就是"这个源已经同步过、几百条壳摆在眼前",
        那一批全是 `exported_hash == content_hash`:它们**真的在 vault 里**。
        合成一档的后果是面板对它们说"抓到之后才写进知识库"(和文件、索引的事实
        正相反),而那句文案配着"不给展开阅读",等于把一批本来读得到的条目一起
        藏起来。口径同翻译那边的 `translating` / `retranslating`。
        """
        fresh, shipped = self.put(), self.put(url='https://e.org/already')
        with reg.db() as c:
            c.execute('UPDATE feed_items SET exported_hash=content_hash WHERE key=?', (shipped,))
        data = reg.items('rss')
        self.assertEqual((True, False), (self.item(fresh)['fetching'],
                                         self.item(fresh)['refetching']))
        self.assertEqual((False, True), (self.item(shipped)['fetching'],
                                         self.item(shipped)['refetching']))
        # 顶上那句计数只数"还没入库"那一档 —— 另一档已经读得到了。
        self.assertEqual(1, data['fetching'])
        # 两档都还排在补抓队列里:分态只影响文案,不影响"要不要去抓"。
        cond, cargs = fulltext.queued_sql()
        with reg.db() as c:
            still = {r[0] for r in c.execute(f'SELECT key FROM feed_items WHERE {cond}', cargs)}
        self.assertEqual({fresh, shipped}, still)

    def test_the_export_stage_counts_them_out_loud(self):
        from kb import pipeline_state
        self.put()
        with patch.dict(pipeline_state._state, {'stages': {}}, clear=False):
            sync.export('rss')
            note = pipeline_state._state['stages']['export']['detail']
        self.assertIn('待补抓原文正文', note)


class EndToEndTests(FullTextCase):
    """一轮 materialize 走完:壳 → 正文 → 译文 → vault。"""

    def test_a_shell_becomes_a_translated_article_in_the_vault(self):
        key = self.put()
        chat = Mock(return_value='###TITLE###\n是的，而且\n###BODY###\n'
                                 '这个运行时为编码智能体调度工具，并保留完整记录。')
        with patch.object(fulltext, 'request', Mock(return_value=page(PAGE))), \
                patch.object(llm, 'chat', chat):
            FeedCollector('rss').materialize()
        text = (self.root / 'vault' / 'source' / 'rss' / (key + '.md')).read_text()
        meta, body = parse_frontmatter(text)
        self.assertEqual('是的，而且', meta['title'])
        self.assertEqual('content', meta['content_kind'])      # 不再写「订阅源提供的摘要」
        self.assertIn('这个运行时为编码智能体调度工具', body)
        self.assertNotIn('HN Points', body)
        # 买给模型的是抓回来的正文,不是那份壳 —— 顺序对了才会这样。
        self.assertIn('The runtime schedules tools', ' '.join(str(c) for c in chat.call_args_list))

    def test_the_round_is_a_no_op_when_nothing_opted_in(self):
        key = self.put(self.plain, url='https://solo.example.org/post', title='Deep dive',
                       body='A long enough body for the exporter to take seriously.')
        with patch.object(fulltext, 'request', Mock()) as req, \
                patch.object(llm, 'chat', Mock(return_value='###TITLE###\n深潜\n###BODY###\n正文译文')):
            FeedCollector('rss').materialize()
        self.assertEqual(0, req.call_count)
        self.assertTrue((self.root / 'vault' / 'source' / 'rss' / (key + '.md')).exists())


class SwitchTests(FullTextCase):
    """开关本身:存在哪儿、什么时候会被改。"""

    def test_changing_the_link_does_not_silently_turn_it_off(self):
        # `save_subscription` 的 reset 把 config 整个清成 '{}' —— 那是采集进度
        # (水位、etag)该有的下场。开关是用户对这条订阅的配置,所以它是独立一列,
        # 不在 config 里:改一次链接就被静默关掉的话,面板上什么痕迹都没有。
        reg.save_subscription('rss', {'url': 'https://agg.example.org/v2.xml'}, self.source['id'])
        self.assertTrue(reg.subscription('rss', self.source['id'])['fulltext'])

    def test_it_can_be_turned_on_and_off(self):
        self.assertFalse(reg.subscription('rss', self.plain['id'])['fulltext'])
        reg.save_subscription('rss', {'fulltext': True}, self.plain['id'])
        self.assertTrue(reg.subscription('rss', self.plain['id'])['fulltext'])
        reg.save_subscription('rss', {'fulltext': False}, self.plain['id'])
        self.assertFalse(reg.subscription('rss', self.plain['id'])['fulltext'])

    def test_editing_another_field_leaves_it_alone(self):
        # 面板上的「停用/启用」只发 {enabled} 一个字段(见 sources.js 的
        # actOnSource),默认值取 old 那一份 —— 取 False 的话点一下停用就把
        # 补抓关了。
        reg.save_subscription('rss', {'enabled': False}, self.source['id'])
        self.assertTrue(reg.subscription('rss', self.source['id'])['fulltext'])


class DefaultsTests(unittest.TestCase):
    """默认订阅里标了 fulltext 的那一条,建新库时真的带着这个开关落下来。

    这一段只对**新库**跑(`defaults-v1` 一写上就不再碰),所以得起一个空库验;
    那条 INSERT 原来压根没有 `fulltext` 这一列,给 defaults.json 加字段而不改
    它的话,JSON 里那个 true 一声不响地丢掉 —— 而这正是用户点名要开的那个源。
    """

    def test_a_default_marked_as_an_aggregator_comes_up_with_the_switch_on(self):
        import json
        import tempfile
        from pathlib import Path as P
        defaults = json.loads((P(reg.__file__).with_name('defaults.json')).read_text())
        marked = {s['name'] for s in defaults if s.get('fulltext')}
        self.assertTrue(marked, 'defaults.json 里一条聚合源都没标')
        with tempfile.TemporaryDirectory(prefix='kb-defaults-') as tmp:
            with patch.object(reg, 'DB_PATH', P(tmp) / 'feeds.db'):
                on = {s['name'] for s in reg.subscriptions('rss') if s['fulltext']}
                total = len(reg.subscriptions('rss'))
        self.assertTrue(total > 1, '默认订阅没建出来')
        # 只有标了的那些开着,别的一条都没被顺手打开。
        self.assertEqual(marked & {s['name'] for s in defaults if s['kind'] == 'rss'}, on)


class TrimTests(FullTextCase):
    """存量清理脚本的 `--pending-translation` 口径要跟着一起走。"""

    def test_an_item_waiting_for_its_body_is_not_counted_as_pending_translation(self):
        # 那个选项删的是"排在翻译队列里的条目"。还在等正文的条目一次模型调用
        # 都没花过,把它们算进去就是按另一个口径删东西 —— 而这是不可逆删除。
        from scripts import feed_trim_history as trim
        waiting = self.put()
        queued = self.put(self.plain, url='https://solo.example.org/post', title='Deep dive',
                          body='A long enough English body that needs translating.')
        with reg.db() as c:
            rows = list(c.execute('SELECT * FROM feed_items ORDER BY key'))
            picked = {r['key'] for r in trim.pending_translation(c, rows)}
        self.assertIn(queued, picked)
        self.assertNotIn(waiting, picked)
