import unittest
from copy import deepcopy
from unittest.mock import patch

from kb import pipeline_failures
from kb.collectors import wechat_local


class WeChatCrawlFailureTests(unittest.TestCase):
    def item(self, suffix='one'):
        return {'name': '同名号 · 同名文章', 'id': f'archive-{suffix}', 'account': '同名号',
                'title': '同名文章', 'link': f'https://mp.weixin.qq.com/s/{suffix}'}

    def report(self, result, code=0):
        with patch.object(wechat_local, '_stage') as stage:
            wechat_local.WeChatLocalCollector._report(code, result)
        return stage.call_args

    def test_crawl_and_export_use_the_same_identity_with_their_original_stage_reasons(self):
        item = self.item() | {'reason': '正文暂未获取，已安排自动重试'}
        result = self.report({'errors': [], 'body_failures': [item]})
        self.assertEqual(('crawl', 'ok'), result.args[:2])
        failure = result.kwargs['failures'][0]
        exported = wechat_local.collector._no_body_failure(self.item())
        self.assertEqual(item['name'], failure['title'])
        self.assertEqual(item['reason'], failure['reason'])
        self.assertTrue(failure['pending'])
        self.assertEqual(exported['url'], failure.get('url'))
        self.assertEqual(exported['drop'], failure.get('drop'))
        self.assertIn('缺少正文', exported['reason'])
        for run_id in ('latest', 'history'):
            snapshot = {'run_id': run_id, 'stages': {'crawl': {'failures': [failure]},
                                                   'export': {'failures': [exported]}}}
            self.assertEqual(2, pipeline_failures.strip(snapshot, {'wechat_local:archive-one'}))

    def test_legacy_crawl_shapes_remain_visible_without_invented_identity(self):
        items = ['同名号 · 同名文章', {'title': '同名号 · 同名文章', 'reason': '旧版退避原因'},
                 {'name': '同名号 · 同名文章', 'id': '', 'link': '', 'account': ''}]
        result = self.report({'errors': [], 'body_failures': items})
        failures = result.kwargs['failures']
        self.assertEqual(3, len(failures))
        for failure in failures:
            self.assertEqual('同名号 · 同名文章', failure['title'])
            self.assertTrue(failure['pending'])
            self.assertNotIn('url', failure)
            self.assertNotIn('drop', failure)
        self.assertEqual('旧版退避原因', failures[1]['reason'])
        self.assertIn('正文暂未获取', failures[0]['reason'])

    def test_structured_article_errors_do_not_duplicate_or_misidentify_legacy_errors(self):
        message = '同名号 · 同名文章: 磁盘写入失败'
        items = [self.item(suffix) | {'error': message, 'reason': '文章入库失败：磁盘写入失败'}
                 for suffix in ('one', 'two')]
        snapshot = {'errors': ['读取器不可用', message], 'article_failures': items}
        original = deepcopy(snapshot)
        result = self.report(snapshot, code=1)
        self.assertEqual(('crawl', 'error'), result.args[:2])
        self.assertIn('失败 3 项', result.args[2])
        failures = result.kwargs['failures']
        self.assertEqual(3, len(failures))
        self.assertEqual('读取器不可用', failures[0]['title'])
        self.assertNotIn('drop', failures[0])
        for failure, item in zip(failures[1:], items):
            self.assertEqual(item['link'], failure['url'])
            self.assertEqual(item['id'], failure['drop']['id'])
            self.assertEqual(item['reason'], failure['reason'])
            self.assertFalse(failure['pending'])
        self.assertEqual(original, snapshot)
