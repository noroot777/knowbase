"""`--pending-translation`:按**翻译队列**清理某个源的存量条目。

和 `test_feed_trim.py` 同一份摆盘(`feed_trim_fixture.py`),按形状拆开 ——
那边钉的是**日期**口径(下界、共享条目、收藏、三步续跑、跨进程锁),这边钉的是
另一条筛选维度,以及它和日期口径之间的互斥。挤在一个文件里会越过 700 行上限。
"""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from kb.feeds import registry as reg, sync

from feed_trim_fixture import TrimFixture, trim  # noqa: E402


class PendingTranslationTests(TrimFixture, unittest.TestCase):
    """`--pending-translation`:清掉某个源还排在翻译队列里的条目。

    给服务已经停掉的源用(2026-10-08:HN Top 100 Blogs 的源站停了,284 条还排
    在队列里,每一轮都占着 `translate.MAX_ITEMS` 的额度把还活着的源挤到后面)。
    筛的维度是翻译队列,不是日期 —— 所以这一路**不钉 `history_floor`**。
    """

    def english(self, url, **over):
        return dict(url=url, title='Historical post', body='historical review content',
                    published='2026-07-01 08:00', author='Review',
                    content_kind='feed') | over

    def mark(self, url, **cols):
        sets = ','.join(f'{k}=?' for k in cols)
        with reg.db() as c:
            c.execute(f'UPDATE feed_items SET {sets} WHERE url=?', (*cols.values(), url))

    def test_the_cut_is_exactly_what_translation_still_has_queued(self):
        """口径直接取 `translate.queued_sql()`,不在清理脚本里另写一遍。

        两处各判各的话,清掉的和翻译那头还排着的不是同一批。
        """
        from kb.feeds import translate
        s = self.sub()
        reg.put_items(s, [self.english('https://example.org/queued'),
                          self.english('https://example.org/done'),
                          self.english('https://example.org/gaveup')])
        self.mark('https://example.org/done', translated_at='2026-10-01 09:00',
                  body_zh='中文正文')
        self.mark('https://example.org/gaveup', translate_tries=translate.MAX_TRIES,
                  translate_error='503')
        with reg.db() as c:
            rows = trim._items(c, s['id'])
            queued = {r['url'] for r in c.execute(
                'SELECT url FROM feed_items WHERE ' + translate.queued_sql()[0],
                translate.queued_sql()[1])}
        self.assertEqual({'https://example.org/queued'},
                         {r['url'] for r in trim.pending_translation(rows)})
        self.assertEqual(queued, {r['url'] for r in trim.pending_translation(rows)})

    def test_it_removes_the_queued_entry_from_all_three_places(self):
        s = self.legacy(self.sub())
        queued = self.seed(s, self.english('https://example.org/queued'))
        kept = self.seed(s, self.english('https://example.org/done'))
        self.mark('https://example.org/done', translated_at='2026-10-01 09:00',
                  body_zh='中文正文')
        self.assertEqual(0, trim.cmd_run(self.args(s, pending_translation=True)))
        gone = self.state(queued)
        self.assertEqual((False, False, 0), (gone['feed_row'], gone['file'], gone['documents']))
        # 已经译完的那条一个字都不许动 —— 它不在队列里。
        still = self.state(kept)
        self.assertEqual((True, True, 1), (still['feed_row'], still['file'], still['documents']))

    def test_it_does_not_pin_a_history_floor(self):
        """下界属于"清历史存量"那条路。顺手钉一个就是做了没被要求的事 ——
        这个源将来真要重新启用时,那条下界会把它自己的存档永久挡在门外。"""
        s = self.sub()
        # 先钉一个下界:空下界的订阅看不出区别 —— 不管走哪条路写回去的都是空串。
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='2026-01-01' WHERE id=?",
                      (s['id'],))
        s = reg.subscription('rss', s['id'])
        self.seed(s, self.english('https://example.org/queued'))
        self.assertEqual(0, trim.cmd_run(self.args(s, pending_translation=True)))
        self.assertEqual('2026-01-01', self.floor_of(s))

    def test_the_two_filters_cannot_be_combined(self):
        """`--floor` 筛日期、`--pending-translation` 筛队列,凑在一起没有定义。"""
        s = self.legacy(self.sub())
        self.seed(s, self.english('https://example.org/queued'))
        self.assertEqual(2, trim.cmd_run(
            self.args(s, pending_translation=True, floor='2026-08-01')))
        # 拦下 = 一个字节都没动:行还在、下界还是空的、备份目录都没建。
        with reg.db() as c:
            self.assertEqual(1, c.execute('SELECT count(*) FROM feed_items').fetchone()[0])
        self.assertEqual('', self.floor_of(s))
        self.assertFalse(list((self.root / 'backups').glob('feedtrim-*')))

    def test_an_empty_queue_changes_nothing(self):
        """队列里没有这个源的条目 = 真的无事可做,不该顺手改任何设置。"""
        s = self.legacy(self.sub())
        item = self.seed(s, self.english('https://example.org/done'))
        self.mark('https://example.org/done', translated_at='2026-10-01 09:00',
                  body_zh='中文正文')
        self.assertEqual(0, trim.cmd_run(self.args(s, pending_translation=True)))
        self.assertEqual('', self.floor_of(s))
        self.assertTrue(self.state(item)['file'])
        self.assertFalse(list((self.root / 'backups').glob('feedtrim-*')))

    def test_an_illegal_combo_does_not_resume_an_unfinished_plan(self):
        """有一份没做完的计划时,非法参数组合**照样**一个字节都不许动。

        互斥检查原来排在「续上没做完的那一份」后面,于是带两个开关重跑会先把
        那份计划**续完** —— feeds 行、vault md、`documents`、全文索引和
        `history_floor` 全动了 —— 然后才打印「不能一起用」并退 2:用户看到的
        是一句拒绝加一篇再也读不到的文章(2026-10-08 审查实测,四处同时归零)。
        上一条用例只摆了「没有旧计划」的局,那条路上两种实现都是绿的 —— 所以
        判据必须是**有旧计划时也不许动**,光验提示和退出码证明不了任何事。

        **三种显式写法各走一遍**:`--floor ""` / `--floor " "` 走的是 argparse
        的空字符串,而「给了 --floor 没有」原来是拿**真值**判的 —— 空串于是和
        「压根没给」长得一模一样,互斥检查放行、`norm_floor` 也没被调用,这条
        本该一个字节都不动的命令把那份没做完的**日期**计划续完了(文章四处
        归零、下界被钉上、退 0;2026-10-08 第 3 轮审查用真 argparse + 真子进程
        独立复现三次)。三种写法都必须报**互斥**那句话:显式给了就算「给了
        日期」,而不是先被 `norm_floor` 当成一个写错的日期拒掉 —— 后者在
        「只修了 `norm_floor` 那一处」的实现上同样是绿的,也同样不动数据,
        只有文案能把两者分开。
        """
        s = self.legacy(self.sub())
        item = self.seed(s, self.english('https://example.org/queued'))
        # 备份和 progress.json 落盘之后、任何删除之前中断:这是脚本明确支持的
        # 恢复场景(进程被杀、I/O 异常),所以此刻三处数据必须逐字未变。
        with patch.object(trim, '_apply', side_effect=RuntimeError('备份之后中断')):
            with self.assertRaises(RuntimeError):
                trim.cmd_run(self.args(s))
        self.assertEqual(1, len(trim._plans(s['id'])))
        before = self.state(item)
        self.assertTrue(before['feed_row'] and before['file'])
        self.assertEqual((1, 1), (before['documents'], before['searchable']))

        # 带两个互斥开关重跑:拒绝,而且那份计划一步都没续。
        for floor in ('2026-08-01', '', ' '):
            with self.subTest(repr(floor)):
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(2, trim.cmd_run(
                        self.args(s, pending_translation=True, floor=floor)))
                self.assertIn('这两个开关不能一起用', err.getvalue())
                self.assertEqual(before, self.state(item))
                self.assertEqual('', self.floor_of(s))
                # 计划留着 —— 改对参数还能接着续,这是拒绝与「删一半」的分界。
                self.assertEqual(1, len(trim._plans(s['id'])))

    def test_resuming_by_the_printed_command_keeps_the_translated_article(self):
        """照工具自己给出的恢复提示续跑,**不许**再按别的口径清一轮。

        `cmd_scan` 的提示是 `run --source N --yes`(不带筛选开关),而续跑原来
        只是这一趟的第一步:续完那份 `--pending-translation` 的计划之后,它接着
        按**默认的日期口径**新建第二份计划再删一轮 —— 已经译好、还读得到搜得到
        的旧文章连 feeds 行、中文 md、`documents` 和全文索引一起没了,
        `history_floor` 也被钉上,而命令以 0 退出(2026-10-08 第 2 轮审查实测,
        独立复现三次;恢复时手动补上 `--pending-translation` 则只删该删的那篇)。

        判据是**已译那篇四处都还在、下界还是空的、没有第二个备份目录**,不是
        提示里有没有那个开关:光把提示写全,续完之后那一轮照样会开。
        """
        s = self.legacy(self.sub())
        queued = self.seed(s, self.english('https://example.org/queued'))
        kept = self.seed(s, self.english('https://example.org/translated'))
        # 已经译好、导出成中文的那一篇:它不在翻译队列里,这一趟压根不该碰它。
        self.mark('https://example.org/translated', translated_at='2026-10-08 04:00',
                  title_zh='已经译好的历史文章', body_zh='已经译好的中文正文。',
                  exported_hash='')
        sync.export('rss')
        self.assertIn('已经译好的中文正文',
                      (self.vault / kept['path']).read_text(encoding='utf-8'))
        # 备份和 progress.json 落盘之后、任何删除之前中断(脚本明确支持的恢复场景)。
        with patch.object(trim, '_apply', side_effect=OSError('备份之后 I/O 中断')):
            with self.assertRaises(OSError):
                trim.cmd_run(self.args(s, pending_translation=True))
        plans = trim._plans(s['id'])
        self.assertEqual(['pending-translation'], [pl['mode'] for _, pl in plans])
        before = self.state(kept)

        # 照提示恢复 —— 提示里没有 `--pending-translation`,所以这里也不给。
        self.assertEqual(0, trim.cmd_run(self.args(s)))
        self.assertFalse(trim._plans(s['id']))                  # 那份计划做完了
        gone = self.state(queued)
        self.assertEqual((False, False, 0), (gone['feed_row'], gone['file'],
                                             gone['documents']))
        # 已经译好的那一篇:feeds 行、中文文件、documents 一处都没少,还读得到。
        for field in ('feed_row', 'memberships', 'exported', 'file', 'documents'):
            self.assertEqual(before[field], self.state(kept)[field], field)
        self.assertEqual((True, True, 1),
                         (before['feed_row'], before['file'], before['documents']))
        self.assertIn('已经译好的中文正文',
                      (self.vault / kept['path']).read_text(encoding='utf-8'))
        self.assertEqual(1, self.state(kept)['searchable'])     # 它那条检索命中还在
        # 下界没被钉上、也没有第二个备份目录 —— 第二轮日期清理压根没开。
        self.assertEqual('', self.floor_of(s))
        self.assertEqual(1, len(list((self.root / 'backups').glob('feedtrim-*'))))

    def test_the_resume_hint_says_which_filter_that_plan_used(self):
        """没做完的那份计划是按什么筛的,`scan` 得写出来。

        续跑按计划里记着的 keys/paths 走、不看筛选开关,所以提示里不带
        `--pending-translation` 是对的;但「这份计划当初筛的是翻译队列」是人
        判断「要不要续」的依据,不该只剩一个备份目录路径。
        """
        s = self.legacy(self.sub())
        self.seed(s, self.english('https://example.org/queued'))
        with patch.object(trim, '_apply', side_effect=OSError('备份之后 I/O 中断')):
            with self.assertRaises(OSError):
                trim.cmd_run(self.args(s, pending_translation=True))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, trim.cmd_scan(self.args(s)))
        hint = [ln for ln in out.getvalue().splitlines() if ln.startswith('!!')]
        self.assertEqual(1, len(hint))
        self.assertIn('翻译队列', hint[0])
        self.assertIn(f"run --source {s['id']} --yes", hint[0])

    def test_an_empty_floor_on_the_real_command_line_is_not_no_floor(self):
        """走**真 argparse**:没写 `--floor` 和 `--floor ""` 必须是两件事。

        上面那几条拿 Namespace 直接喂 `cmd_run`,证明不了 argparse 这一侧 ——
        给 `--floor` 配一个 `default=''` 同样会让「没提供」变成空串,于是
        `is not None` 把每一条普通的 `run --source N --yes` 都拒掉,而所有
        Namespace 用例(传的是 `floor=None`)照旧全绿。所以这里按**命令行**
        各走一遍:两种空写法都拒且一个字节不动,不写这个开关的那条照旧按订阅
        自己的默认下界把旧文清掉。
        """
        s = self.legacy(self.sub())
        reg.put_items(s, [self.english('https://example.org/queued')])
        item = self.seed(s, self.english('https://example.org/old'))
        before = self.state(item)

        def run(*argv):
            with patch.object(sys, 'argv', ['feed_trim_history.py', 'run',
                                            '--source', str(s['id']), *argv]):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    return trim.main()

        for empty in ('', ' '):
            with self.subTest(repr(empty)):
                self.assertEqual(2, run('--floor', empty, '--yes'))
                self.assertEqual(2, run('--floor', empty, '--yes',
                                        '--pending-translation'))
                self.assertEqual(before, self.state(item))
                self.assertFalse(list((self.root / 'backups').glob('feedtrim-*')))

        # 不给这个开关才是「没有下界覆盖」:照旧按默认下界清掉那篇旧文。
        self.assertEqual(0, run('--yes'))
        gone = self.state(item)
        self.assertEqual((False, False, 0),
                         (gone['feed_row'], gone['file'], gone['documents']))
        self.assertNotEqual('', self.floor_of(s))

if __name__ == '__main__':
    unittest.main()
