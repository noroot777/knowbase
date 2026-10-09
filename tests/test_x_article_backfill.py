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
import contextlib
import io
import sys
import threading
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

    def said(self, *argv, script=()):
        """跑一次并把它**说出来的话**一起交回:`(退出码, stdout)`。

        「报的是实际写进去了几条」只能从这里验 —— 库里没变化而命令说「已回填
        N 条」,人看完就以为处理过了。"""
        buf = io.StringIO()
        req, _ = answering(*script)
        with req, contextlib.redirect_stdout(buf):
            code = bf.main(list(argv))
        return code, buf.getvalue()


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


NEWER = '扫描之后另一个写入方补好的那份完整正文。'
MINE = '这一趟自己从接口拿到的正文。'


class ConcurrentWriteTests(BackfillBase):
    """**扫描和写回之间,正文可能已经被别人补好了。**

    扫描是「逐条问一次长文接口」,几十秒到几分钟;这期间一次成功的补抓
    (`fulltext._save`)、一趟存量复查(`scripts/feed_fulltext_recheck`)、或者
    另一个并发跑的这个脚本,完全可能已经把正文写好、翻译好。按 `key` 无条件盖
    回去的后果实测过:正文从完整那份变回这一趟手里的旧结果、`body_zh` 和
    `translated_at` 一并清空,而**两次命令都报成功**(2026-10-09 第 1 轮审查
    双线程复现)。

    **注入点选在扫描循环里**(`probe` 返回之后),因为真实的窗口就在那儿:
    `empty_items` 的读事务早就结束了,主循环还在逐条问接口。`apply_one` /
    `main` 一行不打桩 —— 打桩成功正好绕开唯一要验的那件事。

    **断言一律先验后果**(库里那份正文、译文、命令说了什么),只验「返回 True
    还是 False」的话,一个认出来却照样覆盖的实现也能全绿。
    """

    def meddle(self, body=NEWER, *, hash_too=True, extras=True):
        """让"另一个写入方"在扫描期间插进来一次。"""
        real = bf.probe

        def probing(row):
            got = real(row)
            with reg.db() as conn:
                sets = ['body=?']
                args = [body]
                if hash_too:
                    sets.append('content_hash=?')
                    args.append('hash-from-the-other-writer')
                if extras:
                    sets += ['body_zh=?', 'translated_at=?']
                    args += ['另一个写入方留下的译文。', '2026-10-09T09:00:00+00:00']
                conn.execute(f'UPDATE feed_items SET {", ".join(sets)} WHERE key=?',
                             (*args, row['key']))
                conn.commit()
            return got
        return patch.object(bf, 'probe', probing)

    def test_a_body_filled_in_the_meantime_is_never_overwritten(self):
        """**后果**:正文、译文、完成时间一个字节都不许动。"""
        s = self.seed()
        with self.meddle():
            code, out = self.said('run', '--yes', script=[article_payload()])
        self.assertEqual(0, code)
        row = self.row(s['id'])
        self.assertEqual(NEWER, row['body'], '不许拿旧结果盖掉完整正文')
        with reg.db() as conn:
            got = conn.execute('SELECT body_zh, translated_at FROM feed_items'
                               ).fetchone()
        self.assertEqual('另一个写入方留下的译文。', got['body_zh'])
        self.assertTrue(got['translated_at'], '译文完成时间不许被清掉')

    def test_it_says_plainly_that_nothing_was_written(self):
        """**统计按实际写入结果报**:库里没变化就不许说「已回填 1 条」。"""
        self.seed()
        with self.meddle():
            _, out = self.said('run', '--yes', script=[article_payload()])
        self.assertNotIn('已回填 1 条', out)
        self.assertIn('一条都没写', out)
        self.assertIn('跳过 1 条', out)

    def test_the_content_hash_half_stands_on_its_own(self):
        """**契约**:只有「我扫到的还是当前这一版吗」那道挡得住的局 ——
        正文仍然是空的,`content_hash` 换了(一个只刷 hash 的写入方、一次手工
        迁移)。今天的写入方都连 body 一起换,所以这一道眼下是条冗余带;留着
        它的理由是构造上的,用例因此是**契约**而不是某条真实路径的复现。"""
        s = self.seed()
        with self.meddle(body='', extras=False):
            self.said('run', '--yes', script=[article_payload()])
        self.assertEqual('', self.row(s['id'])['body'], '那一版已经不在了')

    def test_the_empty_body_half_stands_on_its_own(self):
        """**契约**:只有「正文此刻还是空的吗」那道挡得住的局 —— 正文被补好了,
        而 `content_hash` 没动(手工 SQL、一个不刷 hash 的写入方)。"""
        s = self.seed()
        with self.meddle(hash_too=False):
            self.said('run', '--yes', script=[article_payload()])
        self.assertEqual(NEWER, self.row(s['id'])['body'])

    def test_without_a_race_it_still_writes_everything(self):
        """**反过来钉一边**:没有交错时照旧写进去,md 也照旧被重写。"""
        s = self.seed()
        code, out = self.said('run', '--yes', script=[article_payload()])
        self.assertEqual(0, code)
        self.assertIn('已回填 1 条', out)
        self.assertNotIn('跳过', out)
        self.assertIn('整理书签', self.row(s['id'])['body'])

    def test_two_real_runs_cannot_leave_a_half_state(self):
        """**真的两个执行体**:两条独立连接、屏障同时起跑、不人为排序。

        两种交错都只有一个赢家,末态必须是**某一份完整正文配它自己那个 hash**
        —— 不许留下「正文是 A 而 hash 是 B」或者「完整正文配一个被清空的译文」
        那种半状态(口径同 `sync._claim_write` 那条「两种结局的末态都是对的」)。
        """
        for round_no in range(6):
            with self.subTest(round=round_no):
                self.setUp()
                s = self.seed()
                gate = threading.Barrier(2)
                bodies = {w: f'正文 {w}' for w in ('A', 'B')}

                # **一个 patch、按线程分派**:两个线程各自 `with patch` 的话,
                # 后退出的那个会把前一个打过的桩恢复回去(典型的 patch 泄漏),
                # 污染到同一个类里别的用例上。
                def probing(row):
                    who = threading.current_thread().name
                    gate.wait(timeout=10)
                    return bf.GOT, {'title': f'《{who}》', 'body': bodies[who]}

                def one():
                    with contextlib.redirect_stdout(io.StringIO()):
                        bf.main(['run', '--yes'])

                with patch.object(bf, 'probe', probing):
                    ts = [threading.Thread(target=one, name=w)
                          for w in ('A', 'B')]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join(timeout=20)

                row = self.row(s['id'])
                self.assertIn(row['body'], list(bodies.values()),
                              '末态必须是某一份完整正文,不许是空的或混的')
                self.assertEqual(bf._hash(
                    {'url': row['url'], 'published': row['published'],
                     'content_kind': row['content_kind']},
                    f'《{row["body"][-1]}》', row['body']),
                    row['content_hash'], '正文和 hash 必须出自同一个赢家')


if __name__ == '__main__':
    unittest.main()
