from unittest.mock import patch

from tests.local_collect_case import LocalCollectCase, push


class CrawlFailureTests(LocalCollectCase):
    def test_empty_body_reports_the_actual_archive_identity_and_original_link(self):
        from src.crawler.local_wechat import parse_push

        article = parse_push(push())[0] | {'id': 'existing-archive-id'}
        self.repo.upsert_article_from_blog('档案名', article)
        self.repo.commit()
        self.fill.return_value = ('请求错误', None, '')
        result = self.collect()
        row = self.repo.conn.execute('SELECT * FROM articles').fetchone()
        failure = result['body_failures'][0]
        self.assertEqual('existing-archive-id', row['id'])
        self.assertEqual({'name': '档案名 · 测试文章', 'id': row['id'], 'account': '档案名',
                          'title': row['title'], 'link': row['link'],
                          'reason': '正文暂未获取，已安排自动重试'}, failure)
        self.assertEqual([], result['errors'])
        self.assertEqual([], result['article_failures'])

    def test_article_exceptions_keep_identity_without_changing_legacy_error_strings(self):
        with patch('api._ingest_crawl_article', side_effect=OSError('磁盘写入失败')):
            result = self.collect()
        self.assertEqual(['档案名 · 测试文章: 磁盘写入失败'], result['errors'])
        failure = result['article_failures'][0]
        self.assertEqual('档案名', failure['account'])
        self.assertEqual('档案名 · 测试文章', failure['name'])
        self.assertEqual(result['errors'][0], failure['error'])
        self.assertIn('磁盘写入失败', failure['reason'])
        self.assertTrue(failure['id'])
        self.assertIn('mp.weixin.qq.com', failure['link'])
        self.assertEqual([], result['body_failures'])
        self.assertEqual(1, result['download_failed'])

    def test_identical_titles_and_error_text_do_not_merge_different_article_identities(self):
        self.reader.messages.return_value = [push(idx='1'), push(idx='2')]
        with patch('api._ingest_crawl_article', side_effect=OSError('磁盘写入失败')):
            result = self.collect()
        self.assertEqual(1, len(result['errors']))
        failures = result['article_failures']
        self.assertEqual(2, len(failures))
        self.assertEqual(2, len({item['id'] for item in failures}))
        self.assertEqual(2, len({item['link'] for item in failures}))
        self.assertEqual(2, result['download_failed'])
