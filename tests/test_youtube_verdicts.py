# -*- coding: utf-8 -*-
"""**一个已经判出来的分档,穿过公共归并层之后还得是它。**

事故形态(审查用真实子进程复现):`login.classify` 认得出 `js_challenge`,但归并层
(`login.merge`)原先拿按**证据强度**排的 `_STRENGTH` 兼着当**白名单** —— 新加一档时
没人记得同步第二份。于是同步一条没有字幕的视频、去下音频撞上那道挑战时,分档一过归并
就变成 `undecided`、完整原文跟着清空:视频失败详情里只剩开头那截普通工具输出,而
`collector.wall` 也因此收不到这一档、进不了失败归并。

所以这一份的判据是**集合**:`login.VERDICTS` 里每一档都要穿得过去。只补 `js_challenge`
那一条触发路径等于等下一档再来一遍 —— 排序名单不许兼任白名单,因为谁都会忘记同步第二份。
"""
import time
import unittest
from unittest.mock import patch

from kb.youtube import collector, login, media
from kb.youtube.diagnosis import LOCAL, MODEL, YOUTUBE, ToolFailure
from tests.youtube_fixture import YouTubeFixture

#: 那一轮真实输出的形状:决定性的一行在**尾部**,前面一长串普通上下文。
#: 原文丢掉之后面板上留下的恰恰只有开头那截。
NOISE = ('WARNING: [youtube] player client fallback\n' * 20 +
         'WARNING: [youtube] [jsc] challenge solver script was skipped\n'
         'ERROR: Requested format is not available')


def failure(code, diagnostic=NOISE, origin=YOUTUBE):
    return ToolFailure('yt-dlp 执行失败', diagnostic, code, origin)


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


if __name__ == '__main__':
    unittest.main()
