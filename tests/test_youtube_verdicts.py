# -*- coding: utf-8 -*-
"""**一个已经判出来的分档,穿过公共归并层、落进库里、再读回页面之后还得是它。**

事故形态(审查用真实子进程复现):`login.classify` 认得出 `js_challenge`,但归并层
(`login.merge`)原先拿按**证据强度**排的 `_STRENGTH` 兼着当**白名单** —— 新加一档时
没人记得同步第二份。于是同步一条没有字幕的视频、去下音频撞上那道挑战时,分档一过归并
就变成 `undecided`、完整原文跟着清空:视频失败详情里只剩开头那截普通工具输出,而
`collector.wall` 也因此收不到这一档、进不了失败归并。

所以这一份的判据是**集合**:`login.VERDICTS` 里每一档都要穿得过去。只补 `js_challenge`
那一条触发路径等于等下一档再来一遍 —— 排序名单不许兼任白名单,因为谁都会忘记同步第二份。

**这条链有两段,两段都要钉。** 内存里归并对了之后,它还要落进 `youtube_videos` 再被
读回候选排名 —— 而那个末端原先自己 `str(error)[:1000]`,把权威诊断换回了被裁过的摘要:
分档、`collector.wall`、同步汇总全都对,**页面上那一行仍然只有开头那截普通工具输出**
(审查用真实子进程 + 真实库 + 真实页面复现)。所以下半段的判据一律落在
**重新读库、经真实接口拿回来的那一份**里,不落在异常对象上。
"""
import ast
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.feeds import registry as reg
from kb.feeds.sync import FAILURE_CAP, _save_failure
from kb.youtube import collector, discovery, login, media, storage
from kb.youtube.diagnosis import LOCAL, MODEL, YOUTUBE, ToolFailure
from tests.youtube_fixture import YouTubeFixture

#: 落库那两列的名字。**凡是往它们写字的调用都算一个失败展示出口。**
TOLD = ('reason', 'last_error')

#: 决定性那一行**在头还是在尾,不是我们能预先知道的**:429 是「网页根本没下下来」、
#: 印在最前面;格式不可用是工具最后一句话、在尾部。两种都实测过,所以诊断**两头都要钉** ——
#: 只钉尾部的那一版,摘要一长就把诊断开头挤进省略的中段,而分档恰恰是开头那一行定的。
#: 这个头部判据刻意不用 429:`coded` 认限流那一道在分档之前,写 429 会让下面每一档都变成
#: `rate_limited`,整个集合级循环当场空转。429 那一幕由 `AnEarlyLineIsNotSqueezedOut` 原样复现。
HEAD = 'ERROR: nsig extraction failed; Some formats may be missing'
TAIL = 'ERROR: Requested format is not available'
NOISE = (HEAD + '\n' + 'WARNING: [youtube] player client fallback\n' * 20 +
         'WARNING: [youtube] [jsc] challenge solver script was skipped\n' + TAIL)


def failure(code, diagnostic=NOISE, origin=YOUTUBE):
    return ToolFailure('yt-dlp 执行失败', diagnostic, code, origin)


#: 字幕那一步失败时留在**摘要**里的那句话。权威诊断里没有它 —— 它是「这一轮试了哪几步、
#: 最后停在哪」,只有摘要还记得前面那几次(`media` 把每条原因裁到 300 字拼起来)。
TRIED = '没有可用字幕，且本地语音转录这一步随后也失败了'


#: 判出档那一次失败的**阶段摘要**。真实的那一份就是这种长度(「自动字幕 en-orig 下载失败：
#: 第几次、哪条轨道、哪个客户端」),所以 `media` 把它裁到 300 字时**裁不到诊断**——
#: carrier 里于是一个字的诊断都没有。摘要写短了的话 300 字正好够伸进诊断开头,
#: 下面「诊断开头也要在」那条断言就会被摘要那一路顺带满足,**变成恒绿**。
STAGE = '自动字幕 en-orig 下载失败：' * 20


def carried(code, diagnostic=NOISE):
    """`media` 合并多条原因时交出来的那个 carrier,**按它的原样造**:两条原因。

    第一条只在摘要里留下一句话(没有分档、没有诊断),第二条才是判出档的那次工具失败。
    消息里**每条原因都被裁到 300 字**(`media` 那两处 `str(error)[:300]`)—— 摘要本来
    就该这样,所以末端既不能拿它当完整证据,**也不能把它整个扔掉**。
    """
    broken = [ValueError(TRIED), ValueError('；'.join(['人工字幕失败' * 20, '自动字幕 en 失败' * 20])),
              ToolFailure(STAGE, diagnostic, code, YOUTUBE)]
    return login.merge(RuntimeError('；'.join(str(error)[:300] for error in broken)), broken)


class VerdictsSurviveTheMerge(unittest.TestCase):
    """**公共归并边界上的不变量。** 判据落在「归并之后 `coded` 还认不认」。"""

    def test_every_verdict_keeps_its_code_and_its_original_text(self):
        """`VERDICTS` 里一档都不许在归并层掉队。

        这是**集合级**那一条:名单从 `_MARKS` 派生,所以以后往 `classify` 加一档、
        而忘了动 `_STRENGTH`,这里照旧绿 —— 新那一档穿得过去,只是优先级垫底。
        反过来,要是谁又把排序名单当白名单用,这一条当场红。
        """
        self.assertIn('js_challenge', login.VERDICTS, '这一档都不在名单里,下面全是空转')
        for code in login.VERDICTS:
            with self.subTest(code=code):
                merged = login.merge(RuntimeError('字幕都没取到'), [failure(code)])
                self.assertEqual(code, login.coded(merged), '归并之后分档没了')
                self.assertIn('Requested format is not available', merged.diagnostic,
                              '完整原文没跟过来,面板上只剩开头那截')

    def test_the_strongest_evidence_still_wins(self):
        """反过来钉一边:**白名单放开了,排序照旧说话。**

        429 是「网页根本没下下来」,后面那些墙都是它的下游 —— 两条一起回来时要说 429,
        照挑战那一档说会让人去装解题脚本,而这一轮压根没走到挑战那一步。
        """
        merged = login.merge(RuntimeError('两条都失败'),
                             [failure('js_challenge'), failure('rate_limited', '429 原文')])
        self.assertEqual('rate_limited', login.coded(merged))
        self.assertIn('429 原文', merged.diagnostic)

    def test_nothing_recognisable_is_still_undecided(self):
        """另一边也要钉住:**判不出来的照旧落「别再判了」,不许顺手升一档。**

        留空的话 `coded` 会拿那段拼起来的消息重新分类,本机的 `permission denied`
        和模型服务的 429 混在里面,又会被说成「读不出 cookie」「出口被限流」。
        """
        merged = login.merge(RuntimeError('两条都没法归类'),
                             [failure('unknown', '一句看不出来的话'), failure('', '另一句')])
        self.assertEqual(login.UNDECIDED, merged.code)
        self.assertEqual('unknown', login.coded(merged), '它的意思是「别再猜了」')

    def test_an_upstream_that_is_not_youtube_never_becomes_a_youtube_wall(self):
        """再钉一边:**白名单放开不许把别的上游一起放进来。**

        本机的 `permission denied` 和模型服务的 429 进了这套判据就成了 YouTube 的墙,
        面板让人去钥匙串放行或者换出口,而问题在磁盘上、在模型服务那头。
        """
        for origin in (LOCAL, MODEL):
            with self.subTest(origin=origin):
                merged = login.merge(RuntimeError('本机那一侧失败'),
                                     [failure('cookie_unreadable', 'permission denied', origin)])
                self.assertEqual(origin, merged.origin)
                # 判据是**后果**:`collector.wall` 按 `AUTH_CODES` 收,所以只要它不落进
                # 那几档,这条失败就不会被说成 YouTube 的墙。
                self.assertNotIn(login.coded(merged), login.AUTH_CODES, 'YouTube 的分档认了它')
                summary = {}
                collector.wall(summary, ['1'], merged)
                self.assertEqual({}, summary, '别的上游的失败进了 YouTube 的失败归并')

    def test_no_caution_ever_claims_the_content_arrived(self):
        """**并列警告只许说得住的那半截。**

        同一句话也挂在失败路的摘要上(`collector.heeded`),所以一句「这一轮只拿到元数据
        和字幕」会出现在一条字幕都没有的链上(审查实测:没有字幕 → 去下音频 → 撞上挑战)。
        警告证明的是「请求发出去了」,不是产物 —— 这是 `CAUTIONS` 表头那条规矩,
        **整张表一起钉**,不是只盯新加的那一档。
        """
        for code, wording in login.CAUTIONS.items():
            for claim in ('只拿到', '取到了', '已经取到', '已拿到'):
                with self.subTest(code=code, claim=claim):
                    self.assertNotIn(claim, wording, '这一档断言了内容已经到手')


class AChallengeReachesTheFailureDetail(YouTubeFixture):
    """**真实调用点上的后果**:没有字幕、去下音频撞上那道挑战的那一轮。

    走的是 `media.obtain` 里字幕与本地转录共用的那道归并出口,不是直接调 `merge`。
    """

    def setUp(self):
        super().setUp()
        self.isolate()
        self.row = {'video_id': '12345678901',
                    'entry': {'url': 'https://www.youtube.com/watch?v=12345678901'}}
        self.metadata = {'id': self.row['video_id'], 'duration': 600, 'language': 'en'}

    def test_the_detail_keeps_the_classification_and_the_full_text(self):
        with patch.object(media, 'captions', return_value=None), \
                patch.object(media, 'asr', side_effect=failure('js_challenge')):
            with self.assertRaises(Exception) as caught:
                media.obtain(self.row, self.metadata, {'youtube': {}},
                             time.monotonic() + 5, lambda: False)
        raised = caught.exception
        self.assertEqual('js_challenge', login.coded(raised), '详情里又变成「没能归类」了')
        self.assertIn('Requested format is not available', login.evidence(raised),
                      '人唯一的线索是这一行,它在尾部')
        # **后果的下一站**:失败归并按这个分档收,收不到的话这一类失败在面板上并不成组。
        summary = {}
        collector.wall(summary, ['1'], raised)
        self.assertEqual(['1'], summary.get('walls', {}).get('js_challenge'), summary)


class TheVideoRowKeepsTheRealReason(YouTubeFixture):
    """**下半段:落库再读回页面。** 判据一律在重新读出来的那一份里。"""

    def setUp(self):
        super().setUp()
        self.source = self.channel()
        storage.discover(self.source, [self.entry(1)])

    def listed(self):
        """页面真正拿到的那一份:走真实接口,不读异常对象、也不直接读表。"""
        response = self.client.get('/api/intake/youtube/ranking')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()['items'][0]

    def test_every_verdict_reaches_the_video_row(self):
        """**集合级那一条:`VERDICTS` 里每一档的决定性原文都要到得了视频行。**

        只钉 `js_challenge` 那一条触发路径等于把同一个缺陷留给下一档 —— 这个末端
        丢的不是某一档,是「权威诊断」这件事本身。
        """
        for code in login.VERDICTS:
            with self.subTest(code=code):
                error = carried(code)
                for mark in (HEAD, TAIL):
                    self.assertNotIn(mark, str(error),
                                     '摘要里本来就没有诊断那两行,否则这条用例什么都没验')
                self.assertNotIn(TRIED, error.diagnostic,
                                 '诊断里本来就没有这句话,否则下面那条断言什么都没验')
                storage.failed(storage.rows()[0], error)
                item = self.listed()
                self.assertEqual('failed', item['state'])
                self.assertIn(TAIL, item['reason'], '页面上那一行又只剩开头那截普通工具输出了')
                # **诊断的开头也要在。** 摘要堆长之后,合起来裁一次就把它挤进省略的中段
                # —— 而分档常常正是开头那一行定的(429 那一幕)。
                self.assertIn(HEAD, item['reason'], '诊断开头那一行被摘要挤掉了')
                # **另一半:摘要不许被权威诊断顶掉。** 「这一轮试了哪几步、最后停在哪」
                # 只有它记得 —— 只给诊断的话,「先前那条原因不许被顶掉」当场破
                # (`tests/test_youtube_diagnosis.py` 在订阅那一侧钉着同一件事)。
                self.assertIn(TRIED, item['reason'], '前面那几步的原因被顶掉了')

    def test_the_whole_chain_from_the_real_call_site_ends_up_on_the_page(self):
        """**真实调用点一路到页面**:没有字幕 → 去下音频 → 撞上那道挑战。

        `media.obtain` 归并 → `collector.wall` 记账 → `storage.failed` 落库 →
        真实 ranking 接口读回来。中间任何一段把权威诊断换成摘要,这里就红。
        """
        row = {'video_id': f'{1:011d}', 'entry': self.entry(1)}
        with patch.object(media, 'captions', return_value=None), \
                patch.object(media, 'asr', side_effect=failure('js_challenge')):
            with self.assertRaises(Exception) as caught:
                media.obtain(row, {'id': row['video_id'], 'duration': 600, 'language': 'en'},
                             {'youtube': {}}, time.monotonic() + 5, lambda: False)
        storage.failed(storage.rows()[0], caught.exception)
        item = self.listed()
        self.assertIn('Requested format is not available', item['reason'], item['reason'])
        self.assertIn('challenge solver', item['reason'], '那道挑战也得看得见')
        # 这一轮**只有一条原因**:字幕是干干净净地回了空(不是失败),所以没有「前面那几步」
        # 要保。两条原因并存那一半由上面那条集合级用例和 `tests/test_youtube_diagnosis.py`
        # 的真实同步钉 —— 在这儿顺手加一句就成了对这个场景的错断言。

    def test_the_same_line_is_never_printed_twice(self):
        """**同一句话不许在面板上印两遍。**

        这是「摘要自己留一份」那一步的判据(`ToolFailure.summary`):异常消息本身是
        「摘要 + 裁过的诊断」拼出来的,拿它去和未裁的诊断再拼一次,决定性那一行就出现两次,
        还白占掉一半字数预算 —— 真正该省略的中段反而被挤掉了。
        """
        short = failure('js_challenge', 'WARNING: [jsc] challenge solver skipped\n'
                                        'ERROR: Requested format is not available')
        storage.failed(storage.rows()[0], short)
        told = self.listed()['reason']
        self.assertIn('yt-dlp 执行失败', told, '摘要那一截没了')
        self.assertEqual(1, told.count('Requested format is not available'), told)

    def test_a_plain_short_failure_is_not_mangled(self):
        """反过来钉一边:**没有完整诊断、本来就短的那种,原话照旧一字不动。**

        提取器只在超长时两头留中间省,短消息不许被它改样子;空消息退回类名,
        否则面板上那一行只剩一个状态词。
        """
        storage.failed(storage.rows()[0], RuntimeError('字幕语言不对'))
        self.assertEqual('字幕语言不对', self.listed()['reason'])
        storage.failed(storage.rows()[0], ValueError(''))
        self.assertEqual('ValueError', self.listed()['reason'], '空消息要退回类名')


class AnEarlyLineIsNotSqueezedOut(YouTubeFixture):
    """**审查实测那一幕原样复现**:决定性那一行印在诊断**开头**。

    人工字幕失败 → 自动字幕 en 失败 → en-orig 报 `HTTP Error 429`、后面跟着长重试上下文
    和末尾的机器人验证。`code` 和 `walls` 都是 `rate_limited`,而页面上那一行只有普通上下文
    和 `Sign in to confirm you are not a bot` —— **把「请求过多」显示成了「机器人验证」**,
    照它的提示去重新登录没有用。判据落在页面拿到的那一份里。
    """

    LIMIT = 'ERROR: HTTP Error 429: Too Many Requests'
    BOT = 'ERROR: Sign in to confirm you are not a bot'

    def setUp(self):
        super().setUp()
        self.source = self.channel()
        storage.discover(self.source, [self.entry(1)])

    def test_the_line_that_decided_the_verdict_reaches_the_page(self):
        diagnostic = (self.LIMIT + '\n' + 'WARNING: [youtube] retrying player client\n' * 30 + self.BOT)
        broken = [ValueError('人工字幕失败' * 20), ValueError('自动字幕 en 失败' * 20),
                  ToolFailure(STAGE, diagnostic, 'rate_limited', YOUTUBE)]
        error = login.merge(RuntimeError('；'.join(str(item)[:300] for item in broken)), broken)
        self.assertEqual('rate_limited', login.coded(error), '这一幕的前提是分档判对了')
        self.assertNotIn('429', str(error), 'carrier 里本来就没有它,否则这条用例什么都没验')
        storage.failed(storage.rows()[0], error)
        told = self.client.get('/api/intake/youtube/ranking').json()['items'][0]['reason']
        self.assertIn('429', told, '「请求过多」又被显示成了「机器人验证」')
        self.assertIn(self.BOT, told, '次级错误照旧要留着,它是上下文')
        # 订阅失败明细那一处同样的预算更紧,一起钉。
        self.assertIn('429', login.evidence(error, 500), '失败明细里也丢了')


class TheChannelExitGoesThroughTheSameContract(YouTubeFixture):
    """**频道那一侧的末端**:它交给主干那个共用出口,不许绕开统一口径。

    审查实测:详细路径报超时、后路 flat 末尾报 `challenge solver` 和
    `Requested format is not available`,分档和完整诊断都对(`js_challenge`),而写进库的
    `last_error` 恰好 1200 字、决定性那两行一个字都没留下 —— 主干那一刀是**从头裁**,
    而 `discovery.merge` 的消息是两份各约 800 字的摘要拼起来的、轻松超过它。
    """

    def setUp(self):
        super().setUp()
        self.source = reg.subscription('youtube', self.channel()['id'])
        self.summary = {'errors': [], 'failed_ids': []}

    def merged(self):
        """`discovery.merge` 的原样:两条原因、各自的完整消息拼进 carrier。"""
        slow = ToolFailure('逐条解析失败', 'WARNING: ordinary context\n' * 30 + 'ERROR: timed out', '', YOUTUBE)
        wall = failure('js_challenge')
        return login.merge(discovery.FeedError('；'.join([str(slow), str(wall)])), [slow, wall])

    def test_the_decisive_line_survives_the_shared_exit(self):
        error = self.merged()
        self.assertEqual('js_challenge', login.coded(error), '这一幕的前提是分档判对了')
        self.assertGreater(len('；'.join([str(error)])), FAILURE_CAP,
                           '原消息要真的超过那道上限,否则这条用例什么都没验')
        self.assertNotIn(TAIL, str(error)[:FAILURE_CAP], '从头裁本来就会丢掉它')
        # **机制本身也要钉。** 上面那条端到端断言在这个形状上还有第二条满足路径:
        # `discovery.merge` 的消息尾部本来就带着诊断的尾巴,两头都留的摘要那一半会把它
        # 带回来 —— 所以它证得了「用户看得到」,证不了「那一刀真成了冗余带」。
        # 这一条直接问那件事:交过去的文本必须已经压到主干那道上限之内。
        handed = collector._Told(error)
        self.assertLessEqual(len(str(handed)), FAILURE_CAP, '主干那一刀还会从头裁一次')
        _save_failure(self.source, handed, self.summary)
        stored = reg.subscription('youtube', self.source['id'])['last_error']
        self.assertIn(TAIL, stored, '频道失败详情里又只剩先前那次超时了')
        self.assertIn(TAIL, self.summary['errors'][0]['reason'], '同步汇总那一份也丢了')
        self.assertLessEqual(len(stored), FAILURE_CAP, '主干那一刀只许是冗余带')

    def test_the_conversion_never_drops_the_unavailable_notice(self):
        """反过来钉一边:**「这几条取不到」那条提示不许被转换丢掉。**

        换一个异常对象去交付时最容易顺手丢的就是这类附加属性 —— 丢了之后那条提示
        从面板上整条消失,而它跟失败原文是两件事。
        """
        error = self.merged()
        error.unavailable = ['视频 A', '视频 B']
        error.notice = '有 2 条视频已经取不到了'
        _save_failure(self.source, collector._Told(error), self.summary)
        self.assertEqual([{'title': self.source['name'], 'reason': '有 2 条视频已经取不到了',
                           'count': 2, 'items': ['视频 A', '视频 B']}], self.summary['notices'])


class NoFailureExitTruncatesOnItsOwn(unittest.TestCase):
    """**结构级那一条:裁字只许发生在统一提取器里。**

    上一条钉的是今天这一个末端的后果。这一条钉的是「以后新开一个末端」——
    谁再写一次 `reason=str(error)[:N]`,这里当场红,不必等下一轮审查拿真实页面复现。
    判据是**有没有自己裁一刀**,不是「有没有调某个函数」:摘要和权威诊断的区别在
    「裁没裁过」,而末端把上限从 300 调到 1000、1200 都补不回尾部那一行。
    """

    def test_nothing_slices_the_text_it_writes_to_those_two_columns(self):
        ours = sorted(Path(login.__file__).parent.glob('*.py'))
        self.assertGreater(len(ours), 5, '一个文件都没扫到,下面全是空转')
        sliced = []
        for path in ours:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg not in TOLD:
                        continue
                    if any(isinstance(inner, ast.Subscript) and isinstance(inner.slice, ast.Slice)
                           for inner in ast.walk(keyword.value)):
                        sliced.append(f'{path.name}:{keyword.value.lineno} {keyword.arg}=')
        self.assertEqual([], sliced, '这些末端自己裁了一刀,权威诊断在它们那儿变回了摘要')

    def test_every_handoff_to_the_shared_exit_fixes_the_text_first(self):
        """**包外那个共用出口也要全覆盖。** 上面那条只扫得到本包里的关键字实参。

        `_save_failure` 住在主干、自己从头裁一刀,所以 YouTube 这一侧每一个交付点都必须
        先过 `_Told`(它把文本压到 `FAILURE_CAP` 之内)。漏一个就是那一条频道失败的
        决定性原文静默消失 —— 而分档、`walls`、同步汇总全都正常。
        """
        raw = []
        for path in sorted(Path(login.__file__).parent.glob('*.py')):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if not (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == '_save_failure'):
                    continue
                handed = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(handed, ast.Call) and getattr(handed.func, 'id', '') == '_Told'):
                    raw.append(f'{path.name}:{node.lineno}')
        self.assertEqual([], raw, '这些交付点把原始异常直接交给了主干那一刀')
        self.assertGreater(len([1 for line in Path(collector.__file__).read_text(encoding='utf-8')
                                .splitlines() if '_save_failure(' in line and '_Told(' in line]), 2,
                           '一个交付点都没扫到,上面那条断言恒绿')


if __name__ == '__main__':
    unittest.main()
