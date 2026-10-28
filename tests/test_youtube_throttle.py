"""撞上「请求过多」就当场收手:**限的是出口,不是这个频道;而且得是真的不再发请求。**

**为什么红要落在「后面那些请求还发不发」上,而不是「有没有认出 429」**:认出来那一档
(`login.classify` 的 `rate_limited`)早有自己的回归,而真正花钱的是认出来之后那一步 ——
原先这一轮会接着把剩下 47 个频道查完,每一个都再去撞一次同样的墙:惩罚被这一轮自己加深、
拖长,面板上多出几十条不是它们自己毛病的失败,它们还一起进了退避,**下一轮想试都试不了**。

**只在两个外层 `except` 上挡是不够的**(审查实测的三种漏法一条都不经过那个 `except`):
① 逐条解析撞 429、后路 flat 照旧起一个进程,它要是成功了限流只剩一句被丢弃的说明文字;
② 同一个进程先印出一条正常视频再 429,转成「部分成功」,`walls` 是空的;
③ 发现阶段退出之后处理阶段无条件开跑,去探前一个**发现成功**的频道的视频,把那条本来
正常的视频也带成 `failed` 和退避。所以后半边这些用例一律**从真子进程跑起**,断言的是
「哪几次调用真的发生了」和「库里那几条的末态」。

反过来的几边少一边就各错一头:普通失败(地址坏了、网络抖一下)**不许**收手,否则一个频道
就能把整轮同步掐掉,后路也不该被顺手关掉;没轮到的那些频道**一个字都不许写**(不记失败、
不推退避);模型服务的 429 **不许**被说成 YouTube 出口限流 —— 状态码相同不能证明请求来自
同一个服务,照那句话去换出口对模型配额一点用都没有。
"""
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import (collector, discovery, llm_request, login, media, notes, ranking, storage,
                        throttle)
from kb.youtube.diagnosis import LOCAL, ToolFailure
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_tool_stub import FakeToolCase

#: 真机形态:429 的原话。分档由 `login.coded` 在完整诊断上算,这里直接给它结论。
THROTTLED = ToolFailure('列频道失败', 'ERROR: HTTP Error 429: Too Many Requests', 'rate_limited')
BROKEN = ToolFailure('列频道失败', 'ERROR: Unable to download webpage: timed out', 'unknown')
#: 模型服务那一侧的原话(Anthropic/OpenAI 的 SDK 都是这个形状)。**同一个 429,另一个上游。**
MODEL_LIMIT = ("Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', "
               "'message': 'Number of requests has exceeded your per-minute rate limit'}}")


class StopOnThrottle(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.sources = [self.channel(number) for number in range(1, 5)]
        self.settings = reg.get_settings('youtube')
        self.asked = []

    def fetching(self, error):
        """第一个频道撞上 `error`,后面的频道都正常 —— 看它还问不问得到。"""
        def fetch(source, *rest):
            self.asked.append(source['id'])
            if len(self.asked) == 1:
                raise error
            return []
        return fetch

    def collect(self, fetch):
        with patch.object(discovery, 'fetch', side_effect=fetch), \
                patch.object(collector.time, 'sleep'), \
                patch.object(notes, 'available', return_value=(False, '本轮不处理视频')):
            return collector.collect(FetchScope())

    def retry_times(self):
        """按名字取退避时刻。**不许按下标对** —— `subscriptions` 里还有被 `isolate`
        停用的那些默认订阅,下标对不上(照下标写的那一版读到的是别人的 0)。"""
        return {item['name']: item['retry_at'] for item in reg.subscriptions('youtube')}

    def test_a_throttled_round_stops_asking_right_there(self):
        """撞上 429:后面三个频道一次都不许再问。

        接着问下去的那一版在这条用例上会问满 4 次 —— 每一次都是朝着一个已经在罚时里的
        出口再打一发。
        """
        summary = self.collect(self.fetching(THROTTLED))
        self.assertEqual(1, len(self.asked), '撞上 429 之后还接着查下一个频道')
        self.assertEqual(1, summary['checked'])
        self.assertEqual(3, summary['skipped'], '没轮到的要如实报')
        self.assertIn('429', summary['message'])
        self.assertIn('提前收手', summary['message'])

    def test_the_channels_never_reached_keep_a_clean_slate(self):
        """没轮到的那三个:不记失败、不推退避。

        连坐之后下一轮它们还在退避里 —— 一次出口限流就把整份订阅名单按下去了,
        而面板上看不出这跟它们没关系。
        """
        self.collect(self.fetching(THROTTLED))
        after = self.retry_times()
        self.assertGreater(after['频道 1'], time.time(), '撞上的那一个照常进退避')
        self.assertEqual([0, 0, 0], [after[f'频道 {number}'] for number in (2, 3, 4)],
                         '没轮到的被连坐了')
        self.assertEqual(1, len(summarized := [item for item in reg.subscriptions('youtube')
                                               if item['last_error']]), summarized)

    def test_a_channel_refused_at_the_request_entry_keeps_a_clean_slate(self):
        """循环头那道检查**之后**、请求真正启动**之前**才熔断:这个频道一个字都不许写。

        真机形态是并发 —— 同步跑着的时候点一次「检查登录态」,它和这一轮共用同一本账
        (同一个出口),于是这一轮的循环头检查早已通过,轮到下一个频道时请求入口才拒绝。
        它一次请求都没发出去,不记 `last_error`、不推退避,算「没轮到」。记成失败的那一版
        把一条好订阅推进了半小时退避,而面板上写着「连接失败」。
        """
        def fetch(source, *rest, **keywords):
            self.asked.append(source['id'])
            if len(self.asked) > 1:
                # 另一侧(并发的检查登录态)刚把这一轮的 429 记上,再走真正的请求入口。
                throttle.trip(throttle.YOUTUBE, 'ERROR: HTTP Error 429: Too Many Requests')
                throttle.guard()
            return []

        summary = self.collect(fetch)
        standing = {item['name']: item for item in reg.subscriptions('youtube')}
        self.assertEqual(2, len(self.asked), '请求入口拒绝之后还在往下问')
        self.assertEqual('', standing['频道 2']['last_error'], '没发出去的那一次被记成失败')
        self.assertEqual(0, standing['频道 2']['retry_at'], '没发出去也推了退避')
        self.assertEqual([], summary['errors'], summary['errors'])
        self.assertEqual(1, summary['checked'], '被拒的那个不该算查过')

    def test_an_ordinary_failure_does_not_stop_the_round(self):
        """反过来钉一边:普通失败照旧往下查。

        收手这件事只认 429 那一档。认宽了的话,一个频道地址写错就能把整轮同步掐掉,
        而用户只会看到「同步完成」和一堆没更新的频道。
        """
        summary = self.collect(self.fetching(BROKEN))
        self.assertEqual(4, len(self.asked), '普通失败不该收手')
        self.assertEqual(4, summary['checked'])
        self.assertEqual(0, summary['skipped'])

    def test_a_merge_of_only_local_failures_is_never_a_cookie_problem(self):
        """归并的输入里**一条 YouTube 的都没有时,输出绝不许默认成 YouTube**。

        审查实测:把工具输出目录改成不可写,直接调用时 `origin=local` 判得对,同一个错误
        过一次两路归并就变成没有来源的失败,`coded` 按 YouTube 重判 —— `permission denied`
        在 `login._COOKIE` 里,于是面板让人去钥匙串放行,而问题在磁盘上。
        **反过来钉一边**:混着一条 YouTube 失败的那种照旧按 YouTube 算 —— 里面确实有一条,
        标成本机会把 cookie 那一档的线索一起丢掉。
        """
        local = ToolFailure('建不了工具输出目录：permission denied', 'permission denied', '', LOCAL)
        alone = login.merge(RuntimeError('带登录态逐条解析：permission denied'), [local])
        self.assertEqual('unknown', login.coded(alone))
        mixed = login.merge(RuntimeError('带登录态逐条解析：permission denied'),
                            [local, ToolFailure('列频道失败', 'cannot decrypt v10 cookies')])
        self.assertEqual('cookie_unreadable', login.coded(mixed))

    def test_a_mixed_output_reports_the_limit_as_the_wall(self):
        """同一份输出里 cookie 和 429 都在时,**对外说的那一档是限流**。

        展示分档(`login.classify`)把 cookie 排在前面 —— 它确实是更靠前的原因。可这一轮
        是因为 429 停的:`walls` 说「读不出 cookie」、面板让人去钥匙串放行,而放行完再跑
        还是撞同一道墙(审查点名:停止原因、`walls` 分档、给用户的下一步必须是同一件事)。
        **反过来钉一边**:cookie 那条证据照旧留在给人看的原文里,它才是钥匙串那一步的依据。
        """
        error = ToolFailure('列频道失败', 'WARNING: cannot decrypt v10 cookies: no key found'
                            ' | ERROR: HTTP Error 429: Too Many Requests', 'cookie_unreadable')
        self.assertEqual('rate_limited', login.coded(error))
        self.assertIn('cookie', login.evidence(error, 500).lower())

    def process(self, error=None, build=None, count=3):
        """排名之后那一段:第一条候选探元数据时撞上 `error`(或者 `build` 自己抛)。

        **在一轮里跑**(`throttle.opened`):熔断账是本轮共享的,`collect` 在生产里已经
        开好了这一层,直接调 `process` 的这里要自己开 —— 不开的话这段压根没有熔断。
        """
        for number, source in enumerate(self.sources[:count], start=1):
            storage.discover(source, [self.entry(number)])
        ranked = ranking.score(storage.rows(), collector.of(self.settings), FetchScope())
        self.assertEqual(count, len(ranked))
        probed = []

        def probe(row, *rest, **keywords):
            probed.append(row['video_id'])
            if error is not None and len(probed) == 1:
                raise error
            return {'id': row['video_id'], 'duration': 600, 'language': 'en'}

        summary = {'added': 0, 'processed': 0, 'errors': [], 'message': '', 'stopped': False, 'walls': {}}
        with throttle.opened(), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', side_effect=probe), \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=build or self.note):
            collector.process(ranked, self.settings, FetchScope(), summary, time.monotonic() + 30)
        return probed, summary

    def test_a_throttled_probe_stops_the_processing_too(self):
        """处理阶段同一道闸:剩下的候选不再探,而且**留得住**。

        原先会一路探到预算用完(`max_videos * 3`),每一次都是再撞一堵同样的墙。
        剩下的要落 `deferred` —— 那是「这一轮没轮到」,下一轮照旧回得来。
        """
        probed, summary = self.process(THROTTLED)
        self.assertEqual(1, len(probed), '撞上 429 之后还在探下一条')
        self.assertIn('429', summary['message'])
        left = [row for row in storage.rows() if row['video_id'] != probed[0]]
        self.assertEqual(['deferred', 'deferred'], [row['state'] for row in left])

    def test_an_ordinary_probe_failure_keeps_going(self):
        """反过来钉一边:普通失败照旧处理下一条候选。"""
        probed, _ = self.process(BROKEN)
        self.assertEqual(3, len(probed), '普通失败不该掐掉这一轮的处理')

    def modelled(self, message):
        """内容笔记那一步撞上模型服务自己的错误。**从真正的那道边界造**
        (`llm_request.failed`):上游标记就是在那儿打上去的,自己手搓一条异常等于
        把要验的东西绕过去。"""
        def build(*arguments):
            raise llm_request.failed(message)
        return build

    def test_a_model_rate_limit_is_never_called_a_youtube_one(self):
        """只有模型服务说「请求过多」:面板不许说成 YouTube 出口限流。

        审查实测的那一轮里所有 YouTube 请求都成功了,面板却说「YouTube 对本机出口返回了
        429、网页根本没下下来、换出口也有用」,还把一个无辜的频道记进了登录态提醒 ——
        照那句话去换出口对模型配额一点用都没有。**状态码相同不能证明请求来自同一个服务。**
        """
        probed, summary = self.process(build=self.modelled(MODEL_LIMIT))
        self.assertEqual({}, summary['walls'], '模型的 429 被记成了 YouTube 的墙')
        self.assertIn('内容模型', summary['message'])
        self.assertNotIn('出口被 YouTube 限流', summary['message'])
        self.assertNotIn('换出口也有用', summary['message'], 'YouTube 那一档的下一步被搬过来了')

    def test_a_model_rate_limit_stops_asking_the_model_again(self):
        """同一道闸也要对模型成立:这一轮不再调它,剩下的候选留着。

        接着问下去的那一版会把每一条候选都送去撞同一道配额 —— 换成几条失败和退避,
        而配额那一头一分钟后自己就好了。
        """
        probed, summary = self.process(build=self.modelled(MODEL_LIMIT))
        self.assertEqual(1, len(probed), '模型撞上配额之后还在探下一条视频')
        left = [row for row in storage.rows() if row['video_id'] != probed[0]]
        self.assertEqual(['deferred', 'deferred'], [row['state'] for row in left])
        self.assertTrue(all('内容模型' in row['reason'] for row in left), [row['reason'] for row in left])

    def test_the_model_boundary_records_the_limit_before_the_next_call(self):
        """模型那道边界**自己**就要把限流记下来,不许只靠外层接住异常之后补记。

        一条视频要调好几次模型(逐段翻译、再成稿),而外层那一道(`throttle.caught`)只在
        异常离开 `process` 之后才看得见 —— **同一条视频里剩下那几次调用在那之前就发出去
        了**,每一次都是再撞同一道配额。所以这里断言的是「第二次调用压根没起进程」。
        """
        started = []
        child = ('import json, sys; json.load(sys.stdin); '
                 'print(json.dumps({"error": "Error code: 429 - rate_limit_error"}))')
        original = subprocess.Popen
        selected = SimpleNamespace(provider='openai', model_id='fixture')

        def launch(arguments, **values):
            started.append(arguments)
            return original([sys.executable, '-c', child], **values)

        with throttle.opened(), patch.object(llm_request.subprocess, 'Popen', side_effect=launch):
            for attempt in range(2):
                with self.assertRaises(RuntimeError):
                    llm_request.request('prompt', selected, time.monotonic() + 30, lambda: False)
        self.assertEqual(1, len(started), '撞上配额之后还起了第二次模型请求')

    def test_a_model_process_that_cannot_start_is_not_a_cookie_problem(self):
        """起不了模型子进程是**本机**的事:不许被判进 cookie 那一档,也不算撞上限流。

        `permission denied` 在 `login._COOKIE` 里 —— 不标上游的话一次句柄用尽就成了
        「读不出 chrome 的 cookie」,面板让人去钥匙串放行,而问题在本机。
        """
        selected = SimpleNamespace(provider='openai', model_id='fixture')
        with throttle.opened(), patch.object(llm_request.subprocess, 'Popen',
                                             side_effect=PermissionError('permission denied')):
            with self.assertRaises(RuntimeError) as caught:
                llm_request.request('prompt', selected, time.monotonic() + 30, lambda: False)
            self.assertEqual('unknown', login.coded(caught.exception))
            self.assertFalse(throttle.tripped(), '本机起不了进程不该算撞上限流')

    def test_an_ordinary_model_failure_keeps_going(self):
        """反过来钉一边:模型的普通失败不许掐掉这一轮。

        空回复、译文校验没过这些是**这一条**的事,下一条照旧该试 —— 认宽了的话一条视频
        的译文不合格就能让整轮一条都不出。
        """
        probed, summary = self.process(build=self.modelled('模型返回空内容，未生成中文成品'))
        self.assertEqual(3, len(probed), '模型的普通失败不该收手')
        self.assertEqual({}, summary['walls'])

class ThrottleIsRequestLevel(FakeToolCase):
    """熔断与拒绝这一半:**断言的是「那几次请求压根没发生」。**

    假工具和跑一轮的夹具在 `tests.youtube_tool_stub`(两份用例文件共用);诊断怎么
    运输、几条失败怎么归并在 `tests/test_youtube_diagnostics.py`。
    """

    def test_the_flat_fallback_never_starts_after_a_rate_limited_detail_pass(self):
        """逐条解析撞上 429:后路 flat **一个进程都不许起**。

        同一个出口刚说了「请求过于频繁」,换掉一个参数再问一次只是把惩罚加深。原先这条
        后路照旧跑,它要是成功了,限流就只留在被丢弃的原因列表里 —— `walls` 是空的,
        这一轮接着查下一个频道,面板上看不出自己正在挨罚(审查实测)。
        """
        summary = self.sync([('--flat-playlist', 'ok'), ('--dump-json', 'limit')])
        self.assertEqual([], self.ran('--flat-playlist'), '后路还是起了进程')
        self.assertEqual(1, len(self.ran('--dump-json')), '只该有那一次逐条解析')
        self.assertEqual((1, 1), (summary['checked'], summary['skipped']))
        self.assertIn('提前收手', summary['message'])
    def test_an_ordinary_detail_failure_still_falls_back_to_flat(self):
        """反过来钉一边:普通失败照旧走后路。

        把后路一并关掉的话,一次网络抖动就等于这个频道这一轮颗粒无收 —— 而后路本来
        就是为这种情况留的。
        """
        summary = self.sync([('--flat-playlist', 'ok'), ('--dump-json', 'broken')])
        self.assertEqual(2, len(self.ran('--flat-playlist')), '两个频道的后路都该跑')
        self.assertEqual((2, 0), (summary['checked'], summary['skipped']))
        self.assertEqual(2, summary['discovered'], '后路带回来的条目要入库')
    def test_a_partial_listing_that_ends_in_a_rate_limit_still_stops_the_round(self):
        """先印出一条正常视频、再 429:**条目收下,这一轮收手。**

        这条路走的是成功分支(`PartialOutput` 按条目收),所以没有任何异常落进 `walls` ——
        只按 `except` 判的那一版在这条用例上照旧查完两个频道(审查实测 `checked=2`)。
        保留已经取得的条目本身是对的,但不该连「本轮须停止发请求」一起吞掉。
        """
        summary = self.sync([('--dump-json', 'partial')])
        self.assertEqual(1, summary['discovered'], '已经整行印出来的那条要收下')
        self.assertEqual((1, 1), (summary['checked'], summary['skipped']), '还接着查下一个频道')
        self.assertEqual(1, len(storage.rows()), '收下的条目要留在库里')
        self.assertEqual([], self.ran('--dump-single-json'), '收手之后还在探视频元数据')
        # 这一轮没有任何异常落进 `walls`,所以「为什么还有频道没轮到」那句话**只能按熔断账
        # 判** —— 按 `walls` 判会说成单纯的「未轮到」,听起来像预算用完了。
        self.assertIn('提前收手', summary['message'])
    def test_a_rate_limit_that_is_only_a_warning_still_stops_the_round(self):
        """429 只是一条警告、条目也列出来了:同样收手。

        进程退 0、东西也拿到了,可出口已经在挨罚(`login.CAUTIONS` 里「这一轮算侥幸过的」
        说的就是它)。接着发下一个请求多半就过不去了,而那一条会被记成那个频道自己的失败。
        """
        summary = self.sync([('--dump-json', 'warned')])
        self.assertEqual(1, summary['discovered'])
        self.assertEqual((1, 1), (summary['checked'], summary['skipped']))
    def test_the_processing_stage_never_probes_after_a_discovery_rate_limit(self):
        """发现阶段撞上 429:处理阶段**一次元数据都不许探**,前一个频道的视频留得住。

        审查实测的那一次:频道 1 发现成功、频道 2 撞上 429,退出发现循环之后处理阶段
        无条件开跑,去探频道 1 的视频 —— 那一条本来好好的,结果带着一条不是自己的理由
        落了 `failed` 和退避。首次 429 之后还多起了两个进程。
        """
        summary = self.sync([('UC' + f'{2:022d}', 'limit')])
        self.assertEqual(1, summary['discovered'], '频道 1 的那条该入库')
        self.assertEqual([], self.ran('--dump-single-json'), '收手之后还在探视频元数据')
        self.assertEqual(['deferred'], [row['state'] for row in storage.rows()],
                         '本来正常的那条视频被带成了失败')
        standing = {item['name']: (item['last_error'], item['retry_at'])
                    for item in reg.subscriptions('youtube')}
        self.assertEqual(('', 0), standing['频道 1'], '发现成功的频道被连坐了')
        self.assertTrue(standing['频道 2'][0], '撞上的那一个照常留原文')
    def captioning(self, **status):
        """字幕那一段:人工和自动字幕都撞 429,本机转录的工具状态由用例钉死。

        **不许读真实机器的状态**:装着 whisper 模型的机器走到音频下载(被请求入口挡住),
        没装的机器在那之前就报「缺工具」—— 同一条断言在两台机器上验的是两件事。
        """
        override = patch.object(media, 'tools', return_value={
            'yt_dlp': True, 'ffmpeg': True, 'whisper': True, 'model': True,
            'model_path': '', 'asr_enabled': True, **status})
        override.start()
        self.addCleanup(override.stop)
        self.sync([('bestaudio', 'ok'), ('--write-subs', 'limit'),
                   ('--write-auto-subs', 'limit')], transcript=False)
        failed = [row for row in storage.rows() if row['last_error']]
        self.assertEqual(1, len(failed), [row['state'] for row in storage.rows()])
        self.assertEqual(['deferred'], [row['state'] for row in storage.rows()
                                        if not row['last_error']], '剩下那条要留得住')
        return failed[0]['last_error']
    def test_a_caption_rate_limit_is_not_covered_by_the_asr_off_notice(self):
        """字幕撞上 429、本机转录又是关着的:报出来的仍须是限流。

        这条链原先是:人工字幕 429 → 换别的语言 → 自动字幕 → 最后走本机转录,而那一步
        第一句话是「没有可用字幕,且本地语音转录已关闭」—— **原始的限流到这儿就传不出去
        了**,而外层正要靠它决定收不收手(审查按代码推断、未实测的那一行)。
        """
        reported = self.captioning(asr_enabled=False)
        # **按证据断言,不按提示文案**:字幕那一次是真发出去、真撞上的,所以留下来的
        # 该是它的原话(`throttle.NotAttempted` 的文案只属于「压根没发出去」那一种)。
        self.assertIn('429', reported)
        self.assertNotIn('语音转录已关闭', reported, '限流被另一句话盖掉了')
        self.assertNotIn('没有再发起新的请求', reported, '真发出去的那一次被说成没发出去')
    def test_a_caption_rate_limit_never_starts_the_audio_download(self):
        """本机转录开着、工具也齐:那就更不许去下音频了。

        音频是这条链上最贵的一个请求(整段媒体),而出口刚说了「请求过于频繁」。
        """
        reported = self.captioning()
        self.assertEqual([], self.ran('bestaudio'), '收手之后还在下音频')
        self.assertIn('429', reported)
        self.assertNotIn('没有再发起新的请求', reported, '真发出去的那一次被说成没发出去')
    def test_a_broken_first_language_still_tries_the_next_one(self):
        """第一种语言的字幕坏了,**另一种完整的照旧要试** —— 不许连坐。

        审查实测的形态:视频同时挂着 en 和 zh 两份作者字幕,en 下回来的 json3 是截断的,
        同步只起了 en 那一次子进程就收手,视频记成「没有可用字幕,且本地语音转录已关闭」、
        连频道一起推了半小时退避 —— 而 zh 那份是完整可读的。根因是给熔断加专用分支时
        把**逐语言的普通失败收集**一起删了,于是 `broken` 永远是空的、第一种语言的
        `ValueError` 直接掀翻整个语言循环。

        **断言落在后果上**:zh 那次调用真的发生了、成品是 zh、视频没进退避。
        """
        self.sync([('--dump-single-json', 'twolangs'), ('uploaded-0', 'badsub'),
                   ('uploaded-1', 'goodsub')], channels=1, transcript=False)
        self.assertEqual(1, len(self.ran('uploaded-1')), '第二种语言一次都没试过')
        rows = [row for row in storage.rows() if row['state'] == 'ready']
        self.assertEqual(1, len(rows), [(row['state'], row['last_error']) for row in storage.rows()])
        self.assertEqual('', rows[0]['last_error'])
        self.assertEqual(0, rows[0]['retry_at'], '拿到字幕了还在推退避')

    def test_a_successful_call_that_only_warned_about_429_still_records_it(self):
        """**退 0 那一次也要记这一笔。** 记的地方是所有 yt-dlp 调用共用的完成边界。

        429 很多时候只是一条警告:进程退 0、产物也齐,而出口已经在挨罚。字幕和音频那两条
        路拿到产物之后**当场把日志删了**,挂在各个调用点上的那一版在这儿一笔都记不下来
        (审查实测:第一次字幕 429 之后又发起了两次新的读取)。
        `mixed` 那一档单列,是因为它钉的是另一件事:**事实不许从分档的返回值反推**。
        `login.classify` 只能给出一种结论,而 cookie 那一档排在限流前面 —— 照分档判的
        那一版在这份输出上一笔都记不下来,而这里没有任何异常,外层那道冗余带也接不住。
        **反过来钉一边**:干净的成功输出不许让这一轮收手,否则整轮同步会被一句无关的警告掐掉。
        """
        for name, lines, expected in (
                ('warned', ['WARNING: ERROR: HTTP Error 429: Too Many Requests'], True),
                ('mixed', ['WARNING: cannot decrypt v10 cookies: no key found',
                           'WARNING: ERROR: HTTP Error 429: Too Many Requests'], True),
                ('clean', ['WARNING: Falling back on generic information extractor'], False)):
            script = self.root / f'{name}.py'
            script.write_text(''.join(f'print({line!r})\n' for line in lines), encoding='utf-8')
            with self.subTest(name=name), throttle.opened():
                media.run([sys.executable, str(script)], self.root / f'{name}.log',
                          time.monotonic() + 30, lambda: False)
                self.assertEqual(expected, throttle.tripped())
    def test_a_mixed_cookie_and_429_output_still_stops_the_round(self):
        """同一份输出既报 cookie 解密失败又报 429:**两件事都要成立**。

        「这一次撞上限流没有」是个独立事实,不许从展示分档的返回值里反推 —— 分档只能
        给出一种结论,而 cookie 那一档排在限流前面(它是更靠前的原因)。照分档判的那一版
        在这种输出上一笔都记不下来,这一轮照旧把剩下的频道查完(审查实测:两个频道的详细
        列举都带着这两条,`checked=2`)。**反过来钉一边**:cookie 那条证据照旧留在原文里,
        不许为了说限流把它擦掉 —— 用户真要去钥匙串放行的话,靠的就是它。
        """
        summary = self.sync([('--dump-json', 'mixed'), ('--flat-playlist', 'mixed')])
        self.assertEqual([], self.ran('UC' + f'{2:022d}'), '撞上之后还在查下一个频道')
        self.assertEqual(['rate_limited'], list(summary['walls']), summary['message'])
        standing = {item['name']: item['last_error'] for item in reg.subscriptions('youtube')}
        self.assertIn('cookie', standing['频道 1'].lower(), 'cookie 那条证据被擦掉了')
    def test_an_unconfigured_model_round_still_says_why_it_stopped(self):
        """模型还没配的那一轮,**停下来的真正原因照旧要说出来**。

        审查实测:发现阶段带着 429 警告过去了,视频全部 `deferred`、理由里有 429,而面板上
        只有一句「入选视频待配置翻译和内容笔记模型」,`walls` 还是空的 —— 看着像只差个
        模型配置,用户配完回来发现还是不动。两件事可以同时成立,就并列着说。
        """
        summary = self.sync([('--dump-json', 'warned')], channels=2, model=False)
        self.assertIn('出口被 YouTube 限流', summary['message'])
        self.assertIn('待配置', summary['message'], summary['message'])
    def test_a_request_that_never_went_out_does_not_fail_the_video(self):
        """**被本轮熔断挡住的那一次不是这条视频的失败。**

        真机形态:元数据已经取到(进程退 0),只是同时带了一条 429 警告;接下来的字幕请求
        被入口挡住 —— 一个进程都没起。混成一种结果的那一版把这条候选记成「处理失败,
        待重试」并推了半小时退避,而 `last_error` 自己写着「没有再发起新的请求」
        (审查实测,页面截图在案)。该写的是 `deferred`:它没毛病,下一轮回得来。
        **反过来钉一边**:真发出去并撞上的那一次照旧记失败、照旧推退避 —— 它确实没
        拿到东西,原文也得留得住。
        """
        for action, state, failed in (('warned', 'deferred', False), ('limit', 'failed', True)):
            with self.subTest(action=action):
                self.setUp()
                self.sync([('--dump-single-json', action)], channels=1, transcript=False)
                rows = storage.rows()
                self.assertEqual([state], [row['state'] for row in rows], rows[0]['reason'])
                self.assertEqual(failed, rows[0]['attempts'] > 0, '没发出去的那次不该记一次尝试')
                self.assertEqual(failed, rows[0]['retry_at'] > 0, '没发出去的那次不该推退避')
                standing = [item for item in reg.subscriptions('youtube') if item['name'] == '频道 1'][0]
                self.assertEqual(failed, bool(standing['last_error']), standing['last_error'])
                self.assertEqual(failed, standing['retry_at'] > 0, '频道被连坐推进了退避')
    def test_a_channel_that_never_sent_a_request_keeps_a_clean_slate(self):
        """发现阶段同一道口径:**一次请求都没发出去的频道一个字都不写。**

        频道 1 列举成功、同时报了 429;轮到频道 2 时入口直接拒绝,它一个进程都没起。
        那它就是「没轮到」(`skipped`),不记 `last_error`、不推退避 —— 记成失败的那一版
        把一条好订阅推进了半小时退避,而它这一轮压根没被访问过。
        **反过来钉一边**:频道 2 自己真发出去并撞上时照旧记失败(那一组里频道 1 是干净的,
        不然入口会先把频道 2 拒掉 —— 这两种情形在同一个计划里凑不出来)。
        """
        for plan, failed, skipped in (
                ([('UC' + f'{1:022d}', 'warned')], False, 1),
                ([('UC' + f'{1:022d}', 'ok'), ('UC' + f'{2:022d}', 'limit')], True, 0)):
            with self.subTest(failed=failed):
                self.setUp()
                summary = self.sync(plan, channels=2)
                standing = {item['name']: item for item in reg.subscriptions('youtube')}
                self.assertEqual(failed, bool(standing['频道 2']['last_error']),
                                 standing['频道 2']['last_error'])
                self.assertEqual(failed, standing['频道 2']['retry_at'] > 0, '没发请求也推了退避')
                self.assertEqual(skipped, summary['skipped'], summary['message'])
