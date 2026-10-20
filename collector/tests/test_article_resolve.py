import contextlib
import io
import json

import cli
from src.utils.article_identity import stable_article_id
from tests.local_collect_case import LocalCollectCase


class ArticleResolveTests(LocalCollectCase):
    def resolve(self, link):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(['articles', 'resolve', '--link', link, '--json'])
        return code, json.loads(output.getvalue())

    def test_resolve_uses_the_link_identity_to_find_a_legacy_archive_id(self):
        link = 'https://mp.weixin.qq.com/s?__biz=biz-a&mid=101&idx=1'
        self.repo.upsert_article_from_blog('档案名', {
            'id': 'legacy-archive-id', 'link': link, 'title': '相同的标题'})
        self.repo.commit()
        code, result = self.resolve(link + '&scene=99')
        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual('legacy-archive-id', result['id'])
        self.assertEqual('档案名', result['account'])
        self.assertEqual(1, self._count('articles'))
        self.assertEqual(0, self._count('deleted_articles'))

    def test_drop_rejects_an_archive_id_with_another_original_link(self):
        self.collect()
        row = self.repo.conn.execute('SELECT id FROM articles').fetchone()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.main(['articles', 'drop', '--id', row['id'], '--link',
                             'https://mp.weixin.qq.com/s/another-post', '--json'])
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertIn('没有改动', json.loads(output.getvalue())['detail'])
        self.assertEqual(1, self._count('articles'))
        self.assertEqual(0, self._count('deleted_articles'))

    def test_missing_archive_uses_a_verifiable_link_identity_and_can_be_excluded(self):
        link = 'https://mp.weixin.qq.com/s?__biz=biz-a&mid=missing&idx=1'
        code, result = self.resolve(link)
        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual(stable_article_id(link), result['id'])
        self.assertEqual('', result['account'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main([
                'articles', 'drop', '--id', result['id'], '--link', link, '--json']))
        self.assertTrue(self.repo.is_excluded(result['id'], link))
        self.assertEqual(result['id'], self.resolve(link)[1]['id'])

    def test_unverifiable_urls_are_refused_without_changing_any_articles(self):
        for link in ('', 'https://example.org/post', 'https://mp.weixin.qq.com/s?unknown=1',
                     'file://mp.weixin.qq.com/s/post'):
            with self.subTest(link=link):
                code, result = self.resolve(link)
                self.assertEqual(cli.EXIT_ERROR, code)
                self.assertFalse(result['ok'])
        self.assertEqual(0, self._count('articles'))
        self.assertEqual(0, self._count('deleted_articles'))
