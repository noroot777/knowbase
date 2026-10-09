# -*- coding: utf-8 -*-
"""「这一批抓全了吗」,以及没抓全时**下一轮从哪儿接着读**。

和 `test_x_feedgrab_contract.py` 按形状分开(那边是产物落在哪里、调的是哪一个
可执行文件、登录提示给的是不是它):这里全是**边界** —— 翻页半途中断认不认得
出来、推文自己逐字引用那句报错算不算中断、首轮没读完把下界存在哪儿。挤进那个
文件会越过仓库那条 700 行上限。

摆盘和那个真能跑的假 feedgrab 共用 `test_x_feedgrab_contract` 那一份:各写一份
的话两边的前提会各漂各的,而这几条用例要的恰恰是"外部程序真实的样子"。

跑:  python -m unittest tests.test_x_feedgrab_resume -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import registry as reg, x_feedgrab
from tests.test_x_feedgrab_contract import TODAY, YESTERDAY, FeedgrabRound, tweet


class PaginationAbortTests(FeedgrabRound):
    """**翻页半途失败也是「没抓全」,而那两道判据一个字都看不出来。**

    上游分页时某一页所有轮换账号都失败,它只打一行
    `logger.error('[UserTweets] >>> 第 N 页所有 M 个账号均失败 <<< 已抓取 X 条,
    停止分页')` 然后 `break`(`fetchers/twitter_user_tweets.py` 的
    `if not response` 分支)。这一页**压根没拿到**,没拿到的条目不会进
    `tweet_list` —— 于是批量记录里只有成功那几行、收尾摘要写的是「失败: 0」、
    退出码照旧是 0:**现有两道判据各自跑一遍都是 0**。

    后果和逐条失败那一档一模一样。审查实测:账号还有 10-07 的推文,首次同步
    `added=1`、`errors=[]`、`partial=[]`,水位从 10-05 前进到 10-08;把第二页的
    故障解除再点同步 `added=0`、库里仍然只有 10-08 那条 —— 这一轮传给上游的
    下界已经是 `X_USER_TWEETS_SINCE=2026-10-08`,10-07 那条**任何一轮增量都
    选不到**(2026-10-09 第 3 轮审查,拿上游 0.26.2 原版 `fetch_user_tweets`
    复现;对照组用最初的下界跑同一个健康上游,两条推文都在)。
    """

    def plan(self, *, abort=2, **spec):
        """同一个号两页:第一页今天那条拿到了,第二页本该有昨天那条。"""
        return {'example': {'display': 'Example Person', 'abort_page': abort,
                            'tweets': [
            tweet(id_='30001', title='今天那条', body='第一页,正常拿到。'),
            tweet(id_='30000', title='昨天那条', body='第二页,这一轮没拿到。',
                  published=YESTERDAY, page=2),
        ]} | spec}

    def test_a_pagination_abort_keeps_the_old_watermark_and_says_so(self):
        s = self.start_from()
        self.cli(**self.plan())
        summary = self.collect()
        # 后果先断言:水位一步都不许动 —— 动了,第二页那条就永久选不到了。
        self.assertEqual(self.day(4), self.water(s['id']))
        # 第一页那条照旧入库:这不是一次失败(不记 errors、不进退避)。
        self.assertEqual(1, summary['added'])
        self.assertEqual([], summary['errors'])
        self.assertEqual(['Example'], [p['title'] for p in summary['partial']])
        self.assertIn('翻页', summary['partial'][0]['reason'])
        # 面板那条「部分同步」读的是订阅行上的 `last_note`,所以连端点一起验。
        rows = {r['name']: r for r in self.client.get('/api/intake/social')
                .json()['sources']}
        self.assertIn('翻页', rows['Example']['last_note'])
        self.assertEqual('', rows['Example']['last_error'])

    def test_the_tweet_behind_the_abort_comes_back_once_the_page_recovers(self):
        """审查那份证据的反面:第二页恢复之后再同步,昨天那条要补回来。

        靠的就是上一条留着的旧水位 —— 推进了的话这一轮的下界已经越过它。
        """
        s = self.start_from()
        self.cli(**self.plan())
        self.collect()
        self.cli(**self.plan(abort=0))                # 第二页恢复
        again = self.collect()
        self.assertEqual(1, again['added'])           # 补回第二页那条
        self.assertEqual([], again['partial'])
        urls = sorted(r['url'] for r in self.rows(s['id'])['items'])
        self.assertEqual(['https://x.com/Example/status/30000',
                          'https://x.com/Example/status/30001'], urls)
        self.assertEqual(2, self.rows(s['id'])['total'])
        # 这一轮读全了:水位跟到最新那天,提示也清掉。
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])

    def test_neither_existing_judgment_can_see_a_pagination_abort(self):
        """这一档为什么非得单独认一次:它发生在逐条处理**之前**。

        没拿到的条目不会进批量记录,收尾摘要数的也只是拿到的那些 —— 两道判据
        各自单独跑一遍都是 0,证据只有那行日志。这里不走 `harvest`(它收尾会把
        产物目录删掉),直接拿真的那个外部程序跑一次看它留下了什么。
        """
        exe = self.cli(**self.plan())
        out = self.root / 'probe'
        out.mkdir()
        env = x_feedgrab._env(out, str(self.root / 'fg'), self.day(4))
        output = x_feedgrab._run(exe, 'Example', env, self.root)
        self.assertEqual(0, x_feedgrab._batch_failures(out))
        self.assertIn('失败: 0', output)
        self.assertIn('均失败', output)               # 证据只有这一行
        self.assertIn('翻页', x_feedgrab._incomplete(output, out))

    def test_an_abort_and_a_per_tweet_failure_are_both_reported(self):
        """两档同时发生时两句话都要说出来:它们是不同的原因,修法也不一样。"""
        s = self.start_from()
        self.cli(example={'display': 'Example Person', 'abort_page': 2, 'tweets': [
            tweet(id_='30001', title='今天那条'),
            tweet(id_='30002', title='今天另一条', fails=True),
            tweet(id_='30000', title='昨天那条', published=YESTERDAY, page=2),
        ]})
        summary = self.collect()
        self.assertEqual(self.day(4), self.water(s['id']))
        reason = summary['partial'][0]['reason']
        self.assertIn('翻页', reason)
        self.assertIn('没抓下来', reason)


class QuotedDiagnosticTests(FeedgrabRound):
    """**推文自己逐字引用那句报错,不是一次翻页中断。**

    上游把推文标题回显进我们手里那份输出只有一条路:每存一篇都打一行
    `Saved to Markdown: <路径>`,而路径的最后一段正是标题
    (`utils/storage.py` 的 `_generate_filename`)。于是在整份输出上裸 `search`
    那句话时,命中的是**推文自己**。

    审查实测:两条推文明明都抓到了、上游摘要写着「失败: 0」、没有任何 ERROR
    日志,而面板显示「部分同步 · 翻页中途失败」、水位永久冻在 10-05,再同步也
    清不掉 —— 只要那条推文还在抓取范围里就一直这样(2026-10-09 第 4 轮审查,
    拿上游原版 `fetch_user_tweets` 复现,唯一命中的那行是它的 INFO 保存日志)。
    **误报比漏报还难看出来**:面板上是"同步成功但没读完",而真相是读完了。
    """

    QUOTE = '排查记录：第 2 页所有 1 个账号均失败'

    def plan(self):
        return {'example': {'display': 'Example Person', 'tweets': [
            tweet(id_='50001', title=self.QUOTE,
                  body='日志原文:第 2 页所有 1 个账号均失败。'),
            tweet(id_='50000', title='昨天那条', published=YESTERDAY),
        ]}}

    def test_a_tweet_quoting_the_error_does_not_freeze_the_watermark(self):
        s = self.start_from()
        self.cli(**self.plan())
        summary = self.collect()
        # 后果先断言:水位照常前进。冻住的话每一轮都重抓整个窗口,而面板上
        # 那句「部分同步」再也下不去。
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(2, summary['added'])
        self.assertEqual([], summary['partial'])
        rows = {r['name']: r for r in self.client.get('/api/intake/social')
                .json()['sources']}
        self.assertEqual('', rows['Example']['last_note'])

    def test_a_second_healthy_sync_leaves_no_note_behind(self):
        """审查那份证据的另一半:「再同步也清不掉」。"""
        s = self.start_from()
        self.cli(**self.plan())
        self.collect()
        again = self.collect()
        self.assertEqual(0, again['added'])
        self.assertEqual([], again['partial'])
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])
        self.assertEqual(TODAY, self.water(s['id']))

    def test_the_fixture_really_echoes_the_quote_into_the_output(self):
        """上面两条的**前提**:那个外部程序真的把标题回显进了输出。

        不钉住它的话,假 CLI 哪天不打那行保存日志,上面两条就成了空用例 ——
        绿得没有任何意义。顺手也证明清理之后剩下的是"没有标记的那句话"
        (`<` `>` 被上游的文件名清理换成了空格)。
        """
        exe = self.cli(**self.plan())
        out = self.root / 'probe'
        out.mkdir()
        output = x_feedgrab._run(exe, 'Example',
                                 x_feedgrab._env(out, str(self.root / 'fg'), self.day(4)),
                                 self.root)
        echoed = [ln for ln in output.splitlines()
                  if 'Saved to Markdown' in ln and '页所有 1 个账号均失败' in ln]
        self.assertTrue(echoed, '夹具没把推文标题回显进输出,上面两条用例是空的')
        self.assertNotIn('>>>', echoed[0])
        self.assertEqual('', x_feedgrab._incomplete(output, out))


class DisplayNameEchoTests(FeedgrabRound):
    """**作者昵称里逐字写着那句报错,也不是一次翻页中断。**

    上游在分页之前就把昵称**原样**打进日志、一个字都不清理
    (`fetchers/twitter_user_tweets.py:171` 的
    `[UserTweets] 用户信息: @{screen_name} ({display_name}), ID: {user_id}`)。
    这是用户内容进到我们手里那份输出的**第二条路**,它和上面那条不一样:**不带
    路径**,所以"跳过回显产物目录的行"那一道救不了它;`<` `>` 也没被清理,所以
    标记那一道同样救不了它。

    审查实测:昵称是 `>>> 第 2 页所有 1 个账号均失败 <<<` 的号,两条观点都入库
    了、上游摘要写着「失败: 0」、stderr 里一行 ERROR 都没有,面板却显示
    「部分同步 · 翻页中途失败」、水位永久冻在 10-05,再同步也不恢复 —— 只要这个
    号还叫这个名字就一直这样(2026-10-09 第 5 轮审查,拿上游原版
    `fetch_user_tweets` 复现,唯一命中的那行正是它的 INFO 用户信息日志)。

    救它的是**整句话必须落在行尾**那一道:那条回显后面永远还跟着 `), ID: …`,
    所以不管昵称里写成什么样都够不到行尾(见 `x_feedgrab._PAGE_ABORT` ③)。
    """

    NAME = '>>> 第 2 页所有 1 个账号均失败 <<<'

    def plan(self):
        return {'example': {'display': self.NAME, 'tweets': [
            tweet(id_='60001', title='最新观点'),
            tweet(id_='60000', title='较早的观点', published=YESTERDAY),
        ]}}

    def test_a_nickname_quoting_the_error_does_not_freeze_the_watermark(self):
        s = self.start_from()
        self.cli(**self.plan())
        summary = self.collect()
        # 后果先断言:水位照常前进。冻住的话每一轮都重抓整个窗口,而面板上
        # 那句「部分同步」再也下不去。
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(2, summary['added'])
        self.assertEqual([], summary['partial'])
        self.assertEqual([], summary['errors'])
        rows = {r['name']: r for r in self.client.get('/api/intake/social')
                .json()['sources']}
        self.assertEqual('', rows['Example']['last_note'])

    def test_a_second_healthy_sync_leaves_no_note_behind(self):
        """审查那份证据的另一半:「再同步也不恢复」。"""
        s = self.start_from()
        self.cli(**self.plan())
        self.collect()
        again = self.collect()
        self.assertEqual(0, again['added'])
        self.assertEqual([], again['partial'])
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual(2, self.rows(s['id'])['total'])

    def test_the_fixture_really_echoes_the_nickname_into_the_output(self):
        """上面两条的**前提**:那个外部程序真的把昵称原样回显进了输出。

        不钉住它的话,假 CLI 哪天不打那行用户信息日志,上面两条就成了空用例 ——
        绿得没有任何意义。顺手钉住这一行的两个关键事实:两个标记都还在(没过
        清理),而且**它不在本轮产物目录下面**(所以路径那一道跳不到它)。
        """
        exe = self.cli(**self.plan())
        out = self.root / 'probe'
        out.mkdir()
        output = x_feedgrab._run(exe, 'Example',
                                 x_feedgrab._env(out, str(self.root / 'fg'), self.day(4)),
                                 self.root)
        echoed = [ln for ln in output.splitlines() if '用户信息:' in ln]
        self.assertTrue(echoed, '夹具没把昵称回显进输出,上面两条用例是空的')
        self.assertIn('>>>', echoed[0])
        self.assertIn('<<<', echoed[0])
        self.assertNotIn(str(out), echoed[0])
        self.assertEqual('', x_feedgrab._incomplete(output, out))


class PaginationAbortShapeTests(unittest.TestCase):
    """中断那一行要连「第 N 页」「M 个账号」一起认,正常收尾一律不算中断。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_the_abort_line_is_recognised(self):
        self.assertIn('翻页', x_feedgrab._incomplete(
            '[UserTweets] 获取第 2 页...\n'
            '[UserTweets] >>> 第 2 页所有 3 个账号均失败 <<< 已抓取 1 条，停止分页\n'
            '总数: 1, 成功: 1, 跳过: 0, 失败: 0\n', self.folder))

    def test_a_cookie_degradation_is_not_a_pagination_abort(self):
        """`twitter.py` 那句「所有 GraphQL Cookie 均失败,降级到 FxTwitter」说的是
        降级之后照样拿到了内容,不是漏。松成裸「均失败」会把它也算进来,而误报把
        水位**永久**冻住、每一轮重抓整个窗口,比漏报还难看出来。"""
        self.assertEqual('', x_feedgrab._incomplete(
            '[Twitter] 所有 GraphQL Cookie 均失败，降级到 FxTwitter\n'
            '总数: 1, 成功: 1, 跳过: 0, 失败: 0\n', self.folder))

    def test_a_bare_quote_of_the_sentence_is_not_an_abort(self):
        """**标记那一道**:推文标题里逐字写着那句话。

        这是上游清理之后剩下的形状(`_ILLEGAL_CHARS_RE` 把 `<` `>` 换成空格),
        所以 `>>>` / `<<<` 带不进来 —— 要求这两个标记,等于把"用户内容"整类挡在
        外面。这条用例里的路径**故意不在**本轮产物目录下,好让它只能靠标记那一道
        救(两道各自单独成立)。
        """
        self.assertEqual('', x_feedgrab._incomplete(
            'INFO Saved to Markdown: /elsewhere/X/status_author/Example/'
            'Example_2026-10-08：排查记录：第 2 页所有 1 个账号均失败.md\n'
            '总数: 2, 成功: 2, 跳过: 0, 失败: 0\n', self.folder))

    def test_a_saved_path_under_our_own_output_dir_is_not_an_abort(self):
        """**路径那一道**:回显了本轮产物目录的行一律跳过。

        上游真会打的那两行(`Saved to Markdown:` / `批量记录已保存:`)永远以
        `.md`/`.json` 结尾,所以行尾那一道已经把它们涵盖了 —— 这条用例顺手把这件事
        也钉住(下面那句 `_PAGE_ABORT`),路径这一道因此是一条零风险的冗余带:真正
        那句报错里压根没有路径,跳过它不可能漏报,而它在行尾那一道被谁改松时还顶着。
        """
        line = (f'INFO Saved to Markdown: {self.folder}/X/status_author/Example/'
                '[UserTweets] >>> 第 2 页所有 1 个账号均失败 <<< 已抓取 1 条.md')
        self.assertEqual('', x_feedgrab._incomplete(
            line + '\n总数: 1, 成功: 1, 跳过: 0, 失败: 0\n', self.folder))
        self.assertIsNone(x_feedgrab._PAGE_ABORT.search(line))

    def test_a_line_echoing_our_own_output_dir_is_never_evidence(self):
        """路径那一道自己的射程:**含着本轮产物目录的行,不管结尾长什么样**。

        这是它的契约,不是某一行上游日志的复现 —— 上游哪天在路径后面再接一截
        (大小、耗时),行尾那一道就落空了,而那一截后面正是一条推文标题能落在
        行尾的位置。产物目录是我们这一轮自己建的,它出现在某一行里只说明那行在
        回显我们交给它的东西,不是一次故障的证据。
        """
        self.assertEqual('', x_feedgrab._incomplete(
            f'INFO Saved to Markdown: {self.folder}/X/a.md —— '
            '[UserTweets] >>> 第 2 页所有 1 个账号均失败 <<< 已抓取 1 条，停止分页\n'
            '总数: 1, 成功: 1, 跳过: 0, 失败: 0\n', self.folder))

    def test_markers_on_two_different_lines_do_not_join(self):
        """**逐行那一道**:整份输出上 `search` 的话,一行的 `>>>` 能和另一行的
        `<<<` 凑出一次命中 —— 而那两行可以分别是两条推文的标题。"""
        self.assertEqual('', x_feedgrab._incomplete(
            'INFO [UserTweets] 用户信息: @Example (>>> 一则吐槽)\n'
            '[UserTweets] 第 2 页所有 1 个账号均失败?并没有 <<<\n'
            '总数: 1, 成功: 1, 跳过: 0, 失败: 0\n', self.folder))

    def test_the_real_nickname_echo_is_not_an_abort(self):
        """**行尾那一道**:上游原样回显昵称的那一行(`twitter_user_tweets.py:171`)。

        标记齐全、也不在产物目录下面 —— 前两道都救不了它,只能靠"整句话落在
        行尾"。这一行逐字取自第 5 轮审查用原版上游跑出来的 stderr。
        """
        self.assertEqual('', x_feedgrab._incomplete(
            'INFO [UserTweets] 用户信息: @Example '
            '(>>> 第 2 页所有 1 个账号均失败 <<<), ID: fixture-user\n'
            '总数: 2, 成功: 2, 跳过: 0, 失败: 0\n', self.folder))

    def test_a_nickname_carrying_the_whole_sentence_is_not_an_abort(self):
        """同一道判据的**最坏形态**:昵称自己把方括号标签和整句话都带齐了。

        方括号那一道(② )对它是瞎的 —— 括号里写什么都算用户内容。能救它的只有
        行尾:回显后面永远还跟着 `), ID: …`。
        """
        self.assertEqual('', x_feedgrab._incomplete(
            'INFO [UserTweets] 用户信息: @Example ([UserTweets] >>> 第 2 页所有 '
            '1 个账号均失败 <<< 已抓取 1 条，停止分页), ID: fixture-user\n'
            '总数: 2, 成功: 2, 跳过: 0, 失败: 0\n', self.folder))

    def test_a_save_folder_echo_of_the_whole_sentence_is_not_an_abort(self):
        """**方括号那一道**自己的射程:上游紧接着昵称回显打的那行「保存目录」。

        它**以用户内容结尾**(`status_author/<目录名>`),所以行尾那一道对它是瞎的;
        路径那一道也跳不到它(这是个相对路径,不含本轮产物目录)。今天救它的是上游
        的文件名清理(`<` `>` 换成空格),清理哪天放开就只剩方括号这一道了 —— 要求
        `>>>` 紧挨在 `[标签] ` 后面,而这里它前面是 `status_author/`。
        """
        self.assertEqual('', x_feedgrab._incomplete(
            'INFO [UserTweets] 保存目录: status_author/>>> 第 2 页所有 1 个账号'
            '均失败 <<< 已抓取 1 条，停止分页\n'
            '总数: 2, 成功: 2, 跳过: 0, 失败: 0\n', self.folder))

    def test_the_abort_line_is_recognised_under_loguru_default_format(self):
        """真二进制的那份输出走 loguru 默认格式(`logger.add(sys.stderr, level=INFO)`),
        前面有时间、级别和模块位置。判据按**消息自己的形状**认,所以两种格式都认
        得出来 —— 没有按级别判正是为了这个方向上不漏报(见 `_PAGE_ABORT`)。"""
        self.assertIn('翻页', x_feedgrab._incomplete(
            '2026-10-09 04:26:00.123 | ERROR    | '
            'feedgrab.fetchers.twitter_user_tweets:fetch_user_tweets:230 - '
            '[UserTweets] >>> 第 2 页所有 1 个账号均失败 <<< 已抓取 1 条，停止分页\n'
            '总数: 1, 成功: 1, 跳过: 0, 失败: 0\n', self.folder))

    def test_reaching_the_date_floor_is_not_an_abort(self):
        """读到下界就停是「读完了」的正面证据(上游那句 `stop_pagination`)。
        连它也报中断的话,这条通道的水位永远不会前进。"""
        self.assertEqual('', x_feedgrab._incomplete(
            '[UserTweets] 第 1 页获取 2 条（过滤 1 条），累计 2 条\n'
            '[UserTweets] 已到达日期过滤边界，停止分页\n'
            '总数: 2, 成功: 2, 跳过: 0, 失败: 0\n', self.folder))
class _LaterClock(datetime):
    """把**采集那一侧**的"今天"往后推几天 —— 只影响首轮下界怎么现算。

    推系统时钟或者改样本日期都不行:样本日期得留在历史闸门
    (`reg.HISTORY_DAYS`)之内,而要复现的恰恰是"下界自己往前滑走"。
    """

    days = 0

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(days=cls.days)


class FirstRoundFloorTests(FeedgrabRound):
    """**首轮没读完,这一轮用过的下界必须留下来。**

    新订阅的水位是空的,「没抓全就留着旧水位」在它身上什么都没留下 —— 空值压根
    不产生 config 补丁(`x_channels.watermark_patch`),于是下一轮又按「当天往回
    `FIRST_DAYS` 天」现算一个下界,那段没读完的日期**随时间往前滑走**。

    审查实测:9-20 建的订阅首轮翻页中断只收进 1 条(正确报了「部分同步」),
    10-01 解除故障再同步,下界变成 9-17、9-15 那条推文再也选不到,`added=0`、
    `partial=[]`、`last_note` 清空 —— 面板上写的是「已同步」,而那条观点永久
    丢了(2026-10-09 第 4 轮审查;那条订阅自己的 `history_floor` 还停在 9-15,
    按原下界取回来的那条照样过得了 `sync._within_history`,所以不是历史闸门
    拦的)。
    """

    AGE = 4        # 第二页那条推文的年纪(天):要落在 `reg.HISTORY_DAYS` 之内
    LATER = 12     # 过这么多天再同步:现算下界的话它会越过上面那条推文

    def plan(self, *, abort=2):
        return {'example': {'display': 'Example Person', 'abort_page': abort,
                            'tweets': [
            tweet(id_='40001', title='今天那条', body='第一页,正常拿到。'),
            tweet(id_='40000', title='几天前那条', body='第二页,这一轮没拿到。',
                  published=self.day(self.AGE), page=2),
        ]}}

    def floor_now(self, days=0):
        return (datetime.now(timezone.utc)
                - timedelta(days=x_feedgrab.FIRST_DAYS - days)).strftime('%Y-%m-%d')

    def later(self):
        _LaterClock.days = self.LATER
        return patch.object(x_feedgrab, 'datetime', _LaterClock)

    def test_an_unfinished_first_round_stores_the_boundary_it_used(self):
        """存下来的得是**这一轮真正交给上游的那个下界**,不是空串、也不是"现在"。"""
        s = self.sub()
        self.cli(**self.plan())
        summary = self.collect()
        self.assertEqual(self.floor_now(), self.water(s['id']),
                         '首轮没读完,这一轮用过的下界没存下来')
        self.assertEqual(1, summary['added'])
        self.assertEqual(['Example'], [p['title'] for p in summary['partial']])

    def test_a_much_later_retry_still_reads_from_that_boundary(self):
        """审查那份证据的反面:过了 12 天再同步,第二页那条要补回来。

        这里**没有**旧水位可留 —— 靠的就是首轮把下界存了下来。
        """
        s = self.sub()
        self.cli(**self.plan())
        self.collect()
        self.cli(**self.plan(abort=0))                # 第二页恢复
        with self.later():
            again = self.collect()
        # 后果先断言:那条推文真的回来了。只看水位的话,一个"存了个现在"的实现
        # 也能把水位那条断言糊过去。
        urls = sorted(r['url'] for r in self.rows(s['id'])['items'])
        self.assertEqual(['https://x.com/Example/status/40000',
                          'https://x.com/Example/status/40001'], urls)
        self.assertEqual(1, again['added'])
        self.assertEqual([], again['partial'])
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])
        self.assertEqual(TODAY, self.water(s['id']))

    def test_a_complete_first_round_still_advances_to_what_it_saw(self):
        """反过来钉一边:读全了的那一轮照旧推进到见过的最新那条。

        把下界一律存回去的实现会让这条红 —— 那样一个从不发推的号永远按最初那个
        下界重抓整个窗口。
        """
        s = self.sub()
        self.cli(**self.plan(abort=0))
        self.collect()
        self.assertEqual(TODAY, self.water(s['id']))
        self.assertEqual('', reg.subscription('social', s['id'])['last_note'])
