# -*- coding: utf-8 -*-
"""存量回填:已经入库的空正文长文,是怎么被补回来的。

**为什么非得有这条路**:`registry.put_items` 是 `ON CONFLICT(key) DO NOTHING`,
一条推文入库之后再抓到同一条也不会更新它 —— 采集那一侧修好了只对**新**长文生效,
库里躺着的那些(标题「某某 的动态」、正文一个字没有、提取阶段每轮报一次「正文为空,
需回填」)永远不会自己好。2026-10-09 在生产库上实测:近三周 social 的 11 条空正文
里 9 条是长文,1 条纯图推文,1 条推文已被作者删掉。

**每条都先断言后果**:正文真的进了那一行、md 真的被重写、而"确实没有长文"和
"这一次问不到"两档一个字节都不许动 —— 只验「调过几次接口」的话,一个把回包丢掉
的实现也能全绿。

跑:  python -m unittest tests.test_x_article_backfill -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import registry as reg, sync, translate
from kb.feeds.fetching import FeedError
from scripts import x_article_backfill as bf
from tests.test_x_article import answering, article_payload
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound


class BackfillBase(FeedgrabRound):
    """一条真的空正文长文条目:走 `put_items` 入库 + `sync.export` 落成 md。"""

    URL = 'https://x.com/Pluvio9yte/status/2108346962986909740'

    def seed(self, url=None, title='雪踏乌云 的动态', body=''):
        s = self.sub()
        reg.put_items(s, [{'url': url or self.URL, 'title': title, 'body': body,
                           'author': '雪踏乌云', 'published': f'{TODAY} 08:01',
                           'content_kind': 'post'}])
        sync.export('social')
        return s

    def row(self, source_id):
        return self.rows(source_id)['items'][0]

    def md(self, source_id):
        with reg.db() as conn:
            path = conn.execute('SELECT path FROM feed_items').fetchone()['path']
        full = sync.VAULT_ROOT / path
        return full.read_text(encoding='utf-8') if full.exists() else ''

    def run_cmd(self, *argv, script=()):
        req, calls = answering(*script)
        with req:
            code = bf.main(list(argv))
        return code, calls


class BackfillTests(BackfillBase):
    def test_the_article_body_lands_in_the_row_and_in_the_markdown(self):
        s = self.seed()
        self.assertEqual('', self.row(s['id'])['body'], '前提:入库那会儿正文是空的')
        code, calls = self.run_cmd('run', '--yes', script=[article_payload()])
        self.assertEqual(0, code)
        # 后果先断言:库里那一行有正文了,标题也不再是占位。
        row = self.row(s['id'])
        self.assertIn('整理书签', row['body'])
        self.assertEqual('X书签保存Skill x Teamily', row['title'])
        self.assertEqual(['https://api.fxtwitter.com/Pluvio9yte/status/2108346962986909740'],
                         calls.urls)
        # 而且 md 真的被重写了 —— 导出闸门认的是 `exported_hash<>content_hash`。
        self.assertNotIn('整理书签', self.md(s['id']), '导出之前 md 还是旧的')
        sync.export('social')
        self.assertIn('整理书签', self.md(s['id']))

    def test_a_tweet_that_really_has_no_article_is_left_untouched(self):
        """**反过来钉一边**:纯图、纯视频推文确实没有长文,不许编一个出来。"""
        s = self.seed()
        before = dict(self.row(s['id']))
        code, _ = self.run_cmd('run', '--yes', script=[{'tweet': {}}])
        self.assertEqual(0, code)
        self.assertEqual(before['title'], self.row(s['id'])['title'])
        self.assertEqual('', self.row(s['id'])['body'])

    def test_a_failure_changes_nothing_and_says_it_will_retry(self):
        """推文已删、接口故障:这一次问不到,一个字节都不许动(下次再跑就是)。"""
        s = self.seed()
        code, _ = self.run_cmd('run', '--yes', script=[FeedError('HTTP 404')])
        self.assertEqual(0, code)
        self.assertEqual('', self.row(s['id'])['body'])
        self.assertEqual('雪踏乌云 的动态', self.row(s['id'])['title'])

    def test_scan_never_writes(self):
        s = self.seed()
        code, calls = self.run_cmd('scan', script=[article_payload()])
        self.assertEqual(0, code)
        self.assertEqual(1, len(calls.urls), 'scan 也要真问一次才报得准')
        self.assertEqual('', self.row(s['id'])['body'], 'scan 一个字节都不许改')

    def test_run_without_yes_writes_nothing(self):
        s = self.seed()
        code, _ = self.run_cmd('run', script=[article_payload()])
        self.assertEqual(1, code)
        self.assertEqual('', self.row(s['id'])['body'])

    def test_a_tweet_that_already_has_a_body_is_never_asked_about(self):
        self.seed(body='这条推文自己有正文。')
        code, calls = self.run_cmd('run', '--yes', script=[article_payload()])
        self.assertEqual(0, code)
        self.assertEqual([], calls.urls)

    def test_the_language_verdict_is_cleared_so_an_english_article_gets_translated(self):
        """空正文那会儿 `lang` 是按占位标题判的 —— 「GREG ISENBERG 的动态」里有
        「的动态」三个汉字,于是判成中文。正文补上之后那个结论对一篇英文长文就是
        错的,而 `lang='zh'` 的条目永远进不了翻译队列。"""
        s = self.seed(url='https://x.com/gregisenberg/status/2108188646973313195',
                      title='GREG ISENBERG 的动态')
        with reg.db() as conn:
            self.assertEqual('zh', conn.execute(
                'SELECT lang FROM feed_items').fetchone()['lang'])
        english = article_payload(title='Marketing Engineers', blocks=[
            {'type': 'unstyled', 'text': 'Every company will hire marketing engineers.'}])
        self.run_cmd('run', '--yes', script=[english])
        with reg.db() as conn:
            got = conn.execute('SELECT lang, translated_at FROM feed_items').fetchone()
        self.assertEqual('', got['lang'], '清掉之后由 _label_unknown 按真正文重判')
        self.assertEqual('', got['translated_at'])
        # 后果:下一轮同步的那次重判(`_label_unknown`,每轮都跑)认出它是英文,
        # 于是它真的排进了翻译队列 —— 留着 'zh' 的话这一步永远选不到它。
        translate._label_unknown('social')
        self.assertEqual(1, translate.pending('social'))
        with reg.db() as conn:
            self.assertEqual('en', conn.execute(
                'SELECT lang FROM feed_items').fetchone()['lang'])

    def test_nothing_to_do_is_said_plainly(self):
        self.sub()
        code, calls = self.run_cmd('run', '--yes')
        self.assertEqual(0, code)
        self.assertEqual([], calls.urls)


if __name__ == '__main__':
    unittest.main()
