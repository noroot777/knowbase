"""诊断证据的运输:**分类在完整文本上做,裁字数只许发生在展示那一端。**

**为什么红要落在「面板上那一行说的是哪一档」上,而不是 `classify()` 的返回值**:分类函数
本身一直是对的,丢证据发生在它两边 —— 上游先把诊断按尾部裁成 800 字(开头那行 429 就此
消失),下游又按开头裁成 400 字给人看(结尾那句最终错误就此消失)。两次都不是分类的错,
而只测 `classify('http error 429')` 的用例对这两处**恒绿**(审查点名:缺的正是整条输出链
与成功警告的覆盖)。所以这里一律从**真子进程**跑起,断言的是 `login.check` 的回包和
`collect` 的归并说明。

成功那一半同样是丢证据:cookie 解密失败在 yt-dlp 那边只是一条 warning,进程退 0、公开
视频照旧列得出来,于是那条铁证跟着临时输出一起被删 —— 面板上只剩「取到了元数据」,
而这一轮其实压根没带上登录态。

失败那一半同形,而且更难看出来:诊断**已经收到了**,后面那一步(字幕解析、完整性校验、
本机转录、成品落库)随便哪一条失败,原先都能让它整段蒸发 —— 面板上只说「没有可用字幕」。
**「这条诊断有没有发生」不许由「后面那个产物成没成」推断。** 同一条还钉着那句话的措辞:
它在失败路上也要说,所以不许自称「取到了内容」。

另一头是来源:几条失败归并之后,**判不出来就是结论,不许回去拿拼起来的文本再猜一次** ——
本机的 `permission denied`、模型服务的 `429` 混在里面,猜一次就成了「读不出 cookie」
「出口被限流」,而该做的是 `chmod` 或等配额。

反过来的三边少一边就各错一头:干净成功时不许凭空造出一条警告;带警告的成功**不许**被
升级成「登录态无效」(人刚刚明明取到了元数据);命中缓存那一次没起过进程,不许拿上一轮的
警告冒充这一轮的事实。
"""
import json
import sys
import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, diagnosis, discovery, login, media, storage, throttle, toolchain
from kb.youtube.diagnosis import LOCAL, MODEL, YOUTUBE, ToolFailure, excerpt
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_tool_stub import FakeToolCase

#: 真机形态:429 不在最前面(前面是 yt-dlp 自己的前言),中间一长串重试诊断,
#: 最后才是那句机器人验证 —— 单端裁切丢的就是中段那一行。
#: **故意不用 `[youtube:tab] …` 那种前言**:`media.diagnostics` 会把 `[` 开头的行一并
#: 滤掉(JSON 数组也以它开头,而那一行可能带着 cookie 串)。那道滤是「宁可多拒」的凭据
#: 保护,不是这条红要动的东西 —— 要是哪天想放宽它,先想清楚裁半截的 JSON 怎么办。
PREAMBLE = 'WARNING: Falling back on generic information extractor for the channel page'
FILLER = ' | '.join(f'WARNING: [youtube] retrying player client fallback #{number}' for number in range(40))
BOTLINE = 'ERROR: Sign in to confirm you are not a bot. Use --cookies-from-browser'
COOKIE = 'WARNING: cannot decrypt v10 cookies: no key found'


class DiagnosticEvidence(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.settings = reg.get_settings('youtube')
        installed = patch.object(login, 'installed', return_value=True)
        installed.start()
        self.addCleanup(installed.stop)

    def faketool(self, lines=(), code=0):
        """装一份假的 yt-dlp:按参数决定印哪种 JSON,并把 `lines` 当成它的警告/错误印出来。

        **从真子进程跑起**:`run` 的输出泵、脱敏、`diagnostics` 和 `ToolFailure` 都在
        这条链上,打桩在 `media.run` 之上就把要验的东西全绕过去了。
        """
        script = self.root / 'faketool.py'
        script.write_text(
            'import json, sys\n'
            'args = sys.argv[1:]\n'
            'for line in %r:\n'
            '    sys.stdout.write(line + "\\n")\n'
            'single = "--dump-single-json" in args\n'
            'sys.stdout.write(json.dumps({"id": "12345678901", "title": "AI agent coding tutorial",\n'
            '                             "duration": 600, "language": "en", "view_count": 10,\n'
            '                             "upload_date": "20261027"}) + "\\n")\n'
            'sys.exit(%d)\n' % (list(lines), code), encoding='utf-8')
        return patch.object(toolchain, 'command', return_value=[sys.executable, str(script)])

    def staged(self, listing=(), listing_code=0, probing=(), probing_code=0):
        """两步各一种行为的假 yt-dlp。**检查是两段**(先列频道、再取元数据),
        而丢证据正发生在两段之间 —— 一份输出印同样的话验不出跨段的事。
        """
        script = self.root / 'staged.py'
        script.write_text(
            'import json, sys\n'
            'args = sys.argv[1:]\n'
            'single = "--dump-single-json" in args\n'
            'for line in (%r if single else %r):\n'
            '    sys.stdout.write(line + "\\n")\n'
            'sys.stdout.write(json.dumps({"id": "12345678901", "title": "AI agent coding tutorial",\n'
            '                             "duration": 600, "language": "en", "view_count": 10,\n'
            '                             "upload_date": "20261027"}) + "\\n")\n'
            'sys.exit(%d if single else %d)\n'
            % (list(probing), list(listing), probing_code, listing_code), encoding='utf-8')
        return patch.object(toolchain, 'command', return_value=[sys.executable, str(script)])

    def checked(self, lines=(), code=0):
        with self.faketool(lines, code):
            return login.check(self.settings)

    def test_a_cookie_warning_survives_an_otherwise_successful_check(self):
        """工具报了「读不出 cookie」而进程退 0:那条警告必须到达用户。

        丢掉它的后果不是报错,是**面板上一切正常**:用户看到「能带着登录态取到元数据」,
        而这一轮取到的其实是公开内容,字幕和音频到了同步才会全军覆没。
        """
        result = self.checked([COOKIE])
        self.assertEqual('cookie_unreadable', result.get('warning_code'))
        self.assertIn('没带上登录态', result.get('warning', ''))
        self.assertIn('no key found', result.get('detail', ''), '证据原文要看得到')

    def test_a_cookie_warning_is_not_escalated_into_a_failed_login(self):
        """反过来钉一边:那条警告**不许**被说成「登录态无效」。

        它和失败是两件事:这一轮确实取到了元数据(ready),只是那份成功可能不算数
        (confirmed 照旧 False)。升级成失败的话,面板会让人去重新登录,而他刚刚才取到东西 ——
        审查点名要的是「并列显示证据和未确认状态」。
        """
        result = self.checked([COOKIE])
        self.assertEqual((True, False, 'metadata_ok'),
                         (result['ready'], result['confirmed'], result['code']))

    def test_a_later_failure_never_erases_the_earlier_evidence(self):
        """列频道时明说「读不出 cookie」、随后取元数据普通超时:**那条铁证还得在**。

        审查实测:列表子进程正常返回一条视频并印了 cookie 解密警告,probe 报普通连接
        超时 —— 结论只按后一条异常算,面板上只剩「yt-dlp 报了一个没能归类的错误」,
        先前那件事一个字都没有,人去查网络而根因是这一轮压根没带上登录态。
        **两件事并列**:这一步为什么没成照旧说(`code` 不许被警告顶掉),警告另挂一行。
        """
        with self.staged(listing=[COOKIE], probing=['ERROR: Unable to download webpage: timed out'],
                         probing_code=1):
            result = login.check(self.settings)
        self.assertEqual('cookie_unreadable', result.get('warning_code'), result)
        self.assertIn('没带上登录态', result.get('warning', ''))
        self.assertEqual(False, result['ready'], '警告不许把失败说成通过')
        self.assertEqual('unknown', result['code'], '这一步为什么没成被警告顶掉了')
        self.assertIn('timed out', result.get('detail', ''), '失败自己的原文是人唯一的线索')

    def test_a_clean_success_invents_no_warning(self):
        """反过来钉一边:什么警告都没有的那一轮,不许凭空挂一条。

        面板上一条空的告警比没有更坏 —— 它会让人以为每次检查都有问题。
        """
        result = self.checked([])
        self.assertEqual((True, 'metadata_ok'), (result['ready'], result['code']))
        self.assertNotIn('warning', result)
        self.assertNotIn('warning_code', result)

    def test_a_long_failure_still_reports_the_rate_limit_not_the_bot_wall(self):
        """429 夹在中段、机器人验证在尾部:结论必须是限流那一档。

        这是用户报过的那一次真机形态:YouTube 先回 429,yt-dlp 换 player 客户端重试,
        最后印出机器人验证。按尾部裁 800 字之后 429 不见了,于是面板说「带上登录态通常能
        过这一道」—— 他本来就带着 cookie,照着做只会再登一次。
        """
        result = self.checked([PREAMBLE, FILLER, 'ERROR: HTTP Error 429: Too Many Requests',
                               FILLER, BOTLINE], code=1)
        self.assertEqual('rate_limited', result['code'])
        self.assertIn('429', result['message'])
        self.assertNotIn('机器人', result['message'])

    def test_the_shown_原文_keeps_both_ends(self):
        """展示那一份两头都留:开头和结尾都要能看见。

        从一头裁就必丢另一头的证据:429 在前、最终错误在后,这两行恰恰是最有信息的两行。
        """
        result = self.checked([PREAMBLE, 'ERROR: HTTP Error 429: Too Many Requests', FILLER, BOTLINE],
                              code=1)
        detail = result['detail']
        self.assertLessEqual(len(detail), 420, '展示那一份照旧要有字数上限')
        self.assertIn('Falling back', detail, '开头那一行没了')
        self.assertIn('429', detail, '决定性那一行没了')
        self.assertIn('not a bot', detail, '结尾那句最终错误没了')

    def test_a_cached_probe_does_not_replay_an_old_warning(self):
        """反过来钉一边:命中缓存那一次压根没起进程,不许报出警告。

        拿上一轮的警告冒充这一轮的事实,和丢掉它一样是假话。
        """
        row = {'video_id': '12345678901', 'entry': {'url': 'https://www.youtube.com/watch?v=12345678901'}}
        media.save_json(media.cache_dir(row['video_id']) / 'metadata.json',
                        {'id': row['video_id'], 'duration': 600, 'language': 'en',
                         'subtitles': {'en': [{'url': 'x'}]}})
        with diagnosis.heard() as ledger, self.faketool([COOKIE]):
            media.probe(row, self.settings, time.monotonic() + 30, lambda: False)
        self.assertEqual([], ledger, '没起过进程却记下了一笔')

    def test_the_merge_speaks_the_code_seen_when_the_failure_happened(self):
        """归并那句话要按**失败当时**算出的档说,不是拿存过库、裁过的文本重算。

        同步那一侧的那一半:理由文本一路被裁三道(`process` 的 500、`_save_failure` 的
        1200、异常消息自己的摘要),中段那一行 429 到归并时早已不在 —— 于是 48 个频道
        被归并成「机器人验证」。两份实现各要有自己的红,这一条是 `collect` 那一份。
        """
        long_one = ' | '.join([PREAMBLE, FILLER, 'ERROR: HTTP Error 429: Too Many Requests',
                               FILLER, BOTLINE])
        self.assertNotIn('429', excerpt(long_one), '前提:裁过之后 429 真的不在里面了')
        self.assertIn('not a bot', excerpt(long_one), '而 bot 那一行还在 —— 这正是误判的来源')
        with patch.object(discovery, 'fetch',
                          side_effect=ToolFailure('列频道失败', long_one, 'rate_limited')), \
                patch.object(collector.notes, 'available', return_value=(False, '本轮不处理视频')), \
                patch.object(toolchain, 'command', return_value=['yt-dlp']):
            summary = collector.collect(FetchScope())
        self.assertIn('1 个频道卡在同一处', summary['message'])
        self.assertIn('429', summary['message'])
        self.assertNotIn('机器人', summary['message'])
        self.assertEqual(['rate_limited'], list(summary['walls']))
        self.assertEqual([], storage.rows(), '失败那一轮不许留下候选')


class DiagnosticsTravel(FakeToolCase):
    """同一族的另一半:**从真子进程跑一整轮**,断言落在摘要、订阅提醒、分档上。

    上面那个类打桩在工具命令那一层验单次调用;这里要的是「一轮下来人在面板上看到
    的是哪件事」—— 警告产生在一步、交账在另一步,只有整轮跑得起来才看得见中间断掉。
    假工具和跑一轮的夹具在 `tests.youtube_tool_stub`(和 `test_youtube_throttle`
    共用一份);熔断与拒绝那一半在 `tests/test_youtube_throttle.py`。
    """

    def test_a_long_diagnostic_keeps_its_verdict_through_the_fallback_merge(self):
        """两条发现路合并成一条失败时,**分档要跟着走**,不许让上层回去猜。

        真机形态:YouTube 先回 429,yt-dlp 换 player 客户端重试一长串,最后印出机器人验证。
        合并后那条 `FeedError` 只剩文字,而文字为了给人看已经两头裁过 —— 中段那一行 429
        正好在被省略的那一段里,于是归并那句话说成「机器人验证」,照它的提示去重新登录
        完全没用(用户本来就带着 cookie)。
        """
        summary = self.sync([('--dump-json', 'long')], channels=1)
        self.assertEqual(['rate_limited'], list(summary['walls']), summary['message'])
        self.assertIn('429', summary['message'])
        self.assertNotIn('机器人', summary['message'])
    def test_the_merge_picks_the_stronger_evidence_not_the_first_one(self):
        """两条发现路各报一档时,**按证据强度挑,不按到达先后**。

        真机形态:逐条解析先报机器人验证,后路 flat 随后报 429。按先后挑的那一版对外说
        「带上登录态通常能过这一道」,而用户本来就带着登录态 —— 这一轮是因为 429 停的,
        而那句话让他去重新登录,一点用都没有。**停止原因、`walls` 分档、给的下一步必须
        是同一件事。**
        """
        # 清单**按自己的顺序**匹配,而后路那次 argv 里两个标记都在(`--flat-playlist`
        # 和 `--dump-json`)—— flat 要写在前面,否则后路也走成了逐条解析那一档。
        summary = self.sync([('--flat-playlist', 'limit'), ('--dump-json', 'bot')], channels=1)
        self.assertEqual(['rate_limited'], list(summary['walls']), summary['message'])
        self.assertIn('429', summary['message'])
        self.assertNotIn('带上登录态通常能过', summary['message'], 'bot 那一档的下一步盖掉了限流')
    def test_every_call_hands_its_diagnostic_to_the_open_ledger(self):
        """**每一次调用在完成边界交一次账**:退 0、退非 0、本机工具,都交。

        交账原先挂在各个调用方自己的成功分支上(列表 parse 之后、probe 写完缓存之后、
        字幕拿到产物之后),于是「工具报了话」这件事要等后面那一步成了才算发生 ——
        审查三轮各实测到一种组合。**反过来钉三边**:干净的输出一个字都不记(否则面板上
        每轮都挂着一条空提醒,要紧的那条反被埋掉);**退非 0 的那次照旧要记**(真实失败
        的证据正是从这儿走);本机工具记在**它自己的上游名下**(ffmpeg 的
        `permission denied` 进了 YouTube 那套判据就成了「读不出 cookie」)。
        """
        for name, line, code, origin, expected in (
                ('cookie', 'WARNING: cannot decrypt v10 cookies: no key found', 0, YOUTUBE, 1),
                ('failed', 'ERROR: Sign in to confirm you are not a bot', 1, YOUTUBE, 1),
                ('local', 'ERROR: permission denied', 1, LOCAL, 0),
                ('clean', '', 0, YOUTUBE, 0)):
            script = self.root / f'heard-{name}.py'
            script.write_text(f'import sys\nprint({line!r})\nsys.exit({code})\n'
                              if line else 'pass\n', encoding='utf-8')
            with self.subTest(name=name), throttle.opened(), diagnosis.heard() as ledger:
                try:
                    media.heed(media.run([sys.executable, str(script)], self.root / f'heard-{name}.log',
                                         time.monotonic() + 30, lambda: False, origin=origin))
                except Exception:
                    pass
                heard = diagnosis.witnessed(ledger)
                self.assertEqual(expected, len(heard), heard)
                if expected:
                    self.assertIn(line.split(': ', 1)[-1][:12], heard[0])
                if origin == LOCAL:
                    self.assertEqual(1, len(diagnosis.witnessed(ledger, LOCAL)), '本机那一份也要记,只是记在它名下')
    def test_a_cookie_warning_on_a_successful_round_reaches_the_summary(self):
        """取成功、但工具同时报了读不出 cookie:**那句话要走到摘要和订阅提醒上**。

        审查实测:同步发现了 1 条视频,`errors`/`partial`/`notices`/`walls` 全空、订阅
        `last_error` 也空 —— cookie 这件事一个字都没有,而同一个工具行为点「检查登录态」
        却看得见。这一轮很可能是没带登录态拿到的公开内容,而真正用得上登录态的字幕和
        音频到同步时才会全军覆没。**不许升级成失败**:取到的照旧收下、照旧出成品。
        """
        summary = self.sync([('--dump-json', 'cookie')], channels=1)
        self.assertIn('cookie', summary['message'].lower(), summary['message'])
        self.assertEqual({}, summary['walls'], '一条警告被说成了整个频道卡住')
        self.assertEqual([], summary['errors'], [row['reason'] for row in summary['errors']])
        standing = [item for item in reg.subscriptions('youtube') if item['name'] == '频道 1'][0]
        self.assertEqual('', standing['last_error'], '警告被升级成了失败')
        self.assertIn('cookie', standing['last_notice'].lower(), standing['last_notice'])
        self.assertEqual(1, summary['discovered'], '警告让这一轮颗粒无收')
    def test_a_cookie_warning_while_probing_also_reaches_the_summary(self):
        """探元数据这一端同形:**它报的那句警告也要走到摘要上**。

        发现和处理是两条独立的运输路径,各有自己的出参 —— 只修发现那一条的话,
        「频道列举干净、单视频读取报了 cookie」那一轮照旧一个字都不说。
        """
        summary = self.sync([('--dump-single-json', 'cookie')], channels=1)
        self.assertIn('cookie', summary['message'].lower(), summary['message'])
        self.assertEqual([], summary['errors'], summary['errors'])
    def test_an_unrelated_warning_is_not_reported(self):
        """反过来钉一边:**认不出的那些警告一个字都不说。**

        yt-dlp 平时的退路警告(换 player 客户端、通用提取器)每一轮都有好几条,一律照搬
        会把真正要紧的那条 cookie 警告埋掉,面板上还显示「已同步（有跳过）」。
        """
        summary = self.sync([('--dump-json', 'noise')], channels=1)
        self.assertEqual('', summary['message'], summary['message'])
        standing = [item for item in reg.subscriptions('youtube') if item['name'] == '频道 1'][0]
        self.assertEqual('', standing['last_notice'], standing['last_notice'])
        self.assertEqual(1, summary['discovered'])
    def test_a_local_tool_saying_429_does_not_trip_the_youtube_breaker(self):
        """本机工具的输出里出现 429 时,**不许**把 YouTube 那边的闸拉下来。

        ffmpeg、whisper-cli 的输出和 YouTube 一点关系都没有,它们报什么都不该让这一轮
        不再读 YouTube。**反过来钉一边**:同一条命令按 YouTube 算时照旧要熔断 —— 否则
        这条红也可能是因为限流压根没认出来。
        """
        script = self.root / 'noisy.py'
        script.write_text('import sys\nsys.stdout.write("ERROR: HTTP Error 429\\n")\nsys.exit(1)\n',
                          encoding='utf-8')
        for origin, expected in ((LOCAL, False), ('youtube', True)):
            with self.subTest(origin=origin), throttle.opened():
                with self.assertRaises(Exception):
                    media.run([sys.executable, str(script)], self.root / f'{origin}.log',
                              time.monotonic() + 30, lambda: False, origin=origin)
                self.assertEqual(expected, throttle.tripped())
    def test_a_local_tool_failure_is_not_read_as_a_youtube_wall(self):
        """反过来钉一边:本机工具(ffmpeg、whisper-cli)的报错不许进 YouTube 那套判据。

        `permission denied` 在 `login._COOKIE` 里 —— 按文本一判,一次 ffmpeg 权限错误就
        成了「读不出 chrome 的 cookie」,面板让人去钥匙串放行,而问题在磁盘上。
        **两份实现各有自己的红**:同一条命令不标上游时照旧判成 cookie 那一档。
        """
        script = self.root / 'noisy.py'
        script.write_text('import sys\nsys.stdout.write("ERROR: permission denied\\n")\nsys.exit(1)\n',
                          encoding='utf-8')
        command = [sys.executable, str(script)]
        for origin, expected in ((LOCAL, 'unknown'), ('youtube', 'cookie_unreadable')):
            with self.subTest(origin=origin):
                with self.assertRaises(Exception) as caught:
                    media.run(command, self.root / 'tool.log', time.monotonic() + 30,
                              lambda: False, origin=origin)
                self.assertEqual(expected, login.coded(caught.exception))
                # 分档按上游门控,**原文不分上游都要留**:ffmpeg 为什么失败,人得看得见。
                self.assertIn('permission denied', caught.exception.diagnostic)
    def test_a_warning_before_a_failure_still_reaches_the_summary(self):
        """**警告已经收到了,后面那一步失败不许把它吞掉。**

        审查实测的形态:读字幕时工具明说 `cannot decrypt v10 cookies: no key found`,
        随后字幕不完整、本机转录又关着 —— 面板上只剩「没有可用字幕」,`cautions` 空、
        `last_notice` 空,人照着提示去查字幕,而根因是这一轮压根没带上登录态。
        根因是交账押在「整段读取正常返回」上,解析不过、校验不过、转录失败、落库失败
        任何一条都能绕过它。

        **这一条不依赖「有没有另一种语言可试」**:元数据只声明一份 en 字幕。
        """
        override = patch.object(media, 'tools', return_value={
            'yt_dlp': True, 'ffmpeg': True, 'whisper': True, 'model': True,
            'model_path': '', 'asr_enabled': False})
        override.start()
        self.addCleanup(override.stop)
        summary = self.sync([('uploaded-0', 'cookie')], channels=1, transcript=False)
        failed = [row for row in storage.rows() if row['last_error']]
        self.assertEqual(1, len(failed), [row['state'] for row in storage.rows()])
        self.assertIn('没有可用字幕', failed[0]['last_error'], '这一条本该是「字幕没拿到」')
        # 失败这件事照旧说,**cookie 那件事也得说** —— 两件事并列,不是二选一。
        self.assertIn('cookie', summary['message'].lower(), summary['message'])
        self.assertIn('cookie_unreadable', summary.get('cautions', {}), summary.get('cautions'))

    def test_the_summary_line_never_claims_the_content_arrived(self):
        """**反过来钉一边**:那句话不许自称「取到了内容」。

        同一句话在成功路和失败路上都要说(见上一条),而失败路上内容恰恰没拿到 ——
        写成「这一轮取到了内容」的话,面板上就变成「取到了内容」紧挨着「没有可用字幕」,
        自相矛盾。说得住的只有半截:请求确实发出去了。
        """
        override = patch.object(media, 'tools', return_value={
            'yt_dlp': True, 'ffmpeg': True, 'whisper': True, 'model': True,
            'model_path': '', 'asr_enabled': False})
        override.start()
        self.addCleanup(override.stop)
        summary = self.sync([('uploaded-0', 'cookie')], channels=1, transcript=False)
        self.assertNotIn('取到了内容', summary['message'], summary['message'])
        self.assertNotIn('取到的公开内容', summary['message'], summary['message'])

    def test_a_merge_with_no_youtube_verdict_never_guesses_one(self):
        """几条失败里**一条可用的 YouTube 分档都没有**时,结论是「判不出来」,不是去猜。

        触发例:详细读取那一路是本机写盘失败(`permission denied`,`LOCAL`),后路 flat
        恢复之后遇到普通网络超时(YouTube,`unknown`)。两条都排不上名次,原先 `carrier`
        既不标分档也不标来源 —— 于是 `coded` 拿**拼起来的那段文本**重判一次,本机那句
        `permission denied` 又被说成「读不出 cookie」,面板让人去钥匙串放行。

        **反过来钉一边**:里面有一条真的 YouTube 分档时,照旧挑它、照旧说那一档。
        """
        local = ToolFailure('写盘失败：permission denied', 'permission denied', '', LOCAL)
        timeout = ToolFailure('读取超时', 'Read timed out', '', YOUTUBE)
        mixed = login.merge(RuntimeError('写盘失败：permission denied；读取超时'), [local, timeout])
        self.assertEqual('unknown', login.coded(mixed), '本机那句话又被当成 cookie 问题了')
        self.assertIn('permission denied', str(mixed), '原话不许丢')
        wall = ToolFailure('机器人验证', 'Sign in to confirm you are not a bot', 'bot_wall', YOUTUBE)
        picked = login.merge(RuntimeError('x'), [local, wall])
        self.assertEqual('bot_wall', login.coded(picked), '真有结论时不许也说判不出来')

    def test_a_model_limit_inside_a_merge_is_still_not_a_youtube_wall(self):
        """同形的另一头:模型服务那条限流混在里面时,归并之后**不许**成了出口限流。

        `coded` 认限流那一道在分档之前(它要盖住「分档被 cookie 占住」那一半),于是
        `carrier` 不标结论时,拼起来的文本里只要有一句 `Too Many Requests` 就说成
        「YouTube 出口被限流、换出口也有用」—— 而那是模型配额,换出口一点用都没有。

        **措辞是这条用例的要害**:网关那一侧回的正是 HTTP 那句原话(`login._LIMIT` 认的
        就是它)。换成 SDK 自己那种 `rate_limit_error` 的写法,这条红就恒绿了 ——
        那串字 `_LIMIT` 压根不认,无论修没修都判成 `unknown`。
        """
        quota = ToolFailure('内容模型调用失败：Error code: 429 - Too Many Requests',
                            'Error code: 429 - Too Many Requests', '', MODEL)
        timeout = ToolFailure('读取超时', 'Read timed out', '', YOUTUBE)
        merged = login.merge(RuntimeError('；'.join([str(quota), str(timeout)])), [quota, timeout])
        self.assertEqual('unknown', login.coded(merged), '模型的限流被说成了 YouTube 出口限流')

    def walled_captions(self, **status):
        """字幕那一次**真撞上了墙**(cookie 警告 + 机器人验证、退 1),随后本机转录普通失败。

        **本机转录的状态由用例钉死**,不许读真实机器:装着 whisper 的机器和没装的机器
        在这条链上验的是两件事。
        """
        override = patch.object(media, 'tools', return_value={
            'yt_dlp': True, 'ffmpeg': True, 'whisper': True, 'model': True,
            'model_path': '', 'asr_enabled': True, **status})
        override.start()
        self.addCleanup(override.stop)
        summary = self.sync([('uploaded-0', 'walled')], channels=1, transcript=False)
        failed = [row for row in storage.rows() if row['last_error']]
        self.assertEqual(1, len(failed), [row['state'] for row in storage.rows()])
        return summary, failed[0]['last_error']

    def verdict(self, summary, reported):
        """两条原因并列,对外那一档按证据强度挑的是字幕那次真失败。"""
        self.assertIn('没有可用字幕', reported)
        self.assertIn('cookie', reported.lower(), reported)
        self.assertEqual(['cookie_unreadable'], list(summary['walls']), summary['message'])
        self.assertIn('钥匙串', summary['message'])

    def test_a_real_caption_failure_is_not_replaced_by_the_asr_off_notice(self):
        """字幕真撞上了墙、本机转录是关着的:**先前那条原因不许被顶掉。**

        审查实测:字幕子进程印着 `cannot decrypt v10 cookies` 和机器人验证、退 1,
        随后本机转录关着 —— 面板上只剩「没有可用字幕,且本地语音转录已关闭」,
        `walls` 空、`cautions` 空、订阅 `last_error` 也只有那一句,人照着它去开转录,
        而根因是这一轮没带上登录态。根因在 `obtain`:本机转录这一步原先只接住了
        「没发出去」那一种,普通失败直接替换掉字幕那几条。
        """
        self.verdict(*self.walled_captions(asr_enabled=False))

    def test_a_real_caption_failure_is_not_replaced_by_a_missing_tool_notice(self):
        """**两份实现各有自己的红**:换成「缺 FFmpeg」那一句,结论照旧是字幕那次真失败。

        只补「关着」那一条分支的话,缺工具、模型语言不对、转码失败、取不到音频
        这几种照旧会把证据顶掉 —— 那一档罩的是本机转录的**所有**普通失败。
        """
        self.verdict(*self.walled_captions(ffmpeg=False))

    def test_a_failed_call_on_a_round_that_ends_well_still_reports_it(self):
        """**这一轮最后成了,照样要说。** 失败那一次说过的话不许因为末态是成功就作废。

        形态:视频同时挂着 en 和 zh 两份字幕,en 那一次退 1、印着 `cannot decrypt v10
        cookies`,zh 那一次完整可读 —— 成品照旧出,这个频道一条失败都没有。`errors` 和
        `walls` 都是从**异常**上取的,这一轮没有异常,所以「压根没带上登录态」这件事
        **只剩账本一条路**:交账挂在成功分支上的那一版在这儿一个字都留不下,而面板上
        看起来一切正常,直到哪天字幕和音频全军覆没。
        """
        summary = self.sync([('--dump-single-json', 'twolangs'), ('uploaded-0', 'walled'),
                             ('uploaded-1', 'goodsub')], channels=1, transcript=False)
        ready = [row for row in storage.rows() if row['state'] == 'ready']
        self.assertEqual(1, len(ready), [(row['state'], row['last_error']) for row in storage.rows()])
        self.assertEqual({}, summary.get('walls', {}), '这一轮并没有失败')
        self.assertIn('cookie_unreadable', summary.get('cautions', {}), summary)
        self.assertIn('cookie', summary['message'].lower(), summary['message'])

    def test_a_local_tool_warning_never_becomes_a_cookie_caution(self):
        """账上那几份**按上游分**:本机工具说的话不进登录态那套判据。

        ffmpeg、whisper-cli 的 `permission denied` 在 `login._COOKIE` 里 —— 不按上游
        过滤的话,一次转码权限错误就在面板上变成「读不出浏览器的 cookie、去钥匙串放行」,
        而问题在磁盘上。**反过来钉一边**:同一本账里 YouTube 那句照旧要说。
        """
        summary = {}
        ledger = [{'origin': LOCAL, 'noise': 'ERROR: permission denied'},
                  {'origin': YOUTUBE, 'noise': COOKIE}]
        collector.caution(summary, [7], ledger)
        self.assertEqual({'cookie_unreadable': ['7']}, summary.get('cautions'), summary)
        self.assertEqual(1, len(summary['cautions']), '本机那句话也被记成了登录态问题')

    def test_a_probe_that_cannot_cache_still_reports_what_the_tool_said(self):
        """工具报了 cookie 警告、随后**写缓存失败**:那句话照旧要到账上。

        记账原先排在 `save_json` 之后,于是磁盘这一头一出问题,工具明明说过的话就
        一个字都没留下。**顺带钉另一半**:写盘失败是**本机**的事,不许被当成
        「读不出 cookie」—— 它和工具报的那条警告是两件事,一件进 `cautions`,
        一件老老实实说自己判不出来。
        """
        # 把元数据缓存的落点先占成一个目录:`os.replace` 必然失败,而工具照旧跑完。
        (media.cache_dir('00000000001') / 'metadata.json').mkdir(parents=True)
        summary = self.sync([('--dump-single-json', 'cookie')], channels=1, transcript=False)
        self.assertIn('cookie_unreadable', summary.get('cautions', {}), summary.get('cautions'))
        self.assertIn('cookie', summary['message'].lower(), summary['message'])
        self.assertEqual({}, summary['walls'], '本机写盘失败被算成了登录态的墙')

    def test_a_listing_that_parses_into_nothing_still_reports_the_warning(self):
        """列频道印了 cookie 警告、一条合法条目都没解析出来:**那句话照旧要到账上。**

        记账原先排在 `parse` 之后(列表那一端),解析一抛就什么都没留下 ——
        而这一轮的原话正是唯一的线索。
        """
        summary = self.sync([('--dump-json', 'cookieonly')], channels=1)
        self.assertIn('cookie_unreadable', summary.get('cautions', {}), summary.get('cautions'))
        self.assertEqual(['cookie_unreadable'], list(summary['walls']), summary['message'])
        # **同一个频道的同一档不说两遍**:已经是它的失败结论了,不再并列说一句「但报了…」。
        self.assertEqual(1, summary['message'].lower().count('钥匙串'), summary['message'])

    def test_a_listing_without_partial_output_is_still_a_youtube_verdict(self):
        """**反过来钉一边**:没有半截输出的那一半,照旧按解析错误和 YouTube 算。

        真机形态:列频道那次退 0、只印了一条 429 警告,一条合法条目都没有 —— 这时
        `PartialOutput` 压根没发生,底下没有「真实失败」可借。半截输出那条新口径
        (按 `cause` 的上游和分档抛)**只许管有 cause 的那一半**:把没有 cause 的那一半
        也顺手改掉(比如上游默认值写成本机),每一次普通的列举失败就都不再是 YouTube 的墙,
        面板上「同步完成」而 `walls` 空的。
        """
        summary = self.sync([('--dump-json', 'warnedonly')], channels=1)
        self.assertEqual(['rate_limited'], list(summary['walls']), summary['message'])
        self.assertIn('429', summary['message'])

    def test_a_tool_that_cannot_start_is_a_local_problem(self):
        """工具在、但起不来(没有执行权限):**这是本机的事。**

        `Popen` 原先只接 `FileNotFoundError`,`PermissionError` 裸着往上走 —— 不带来源,
        `login.coded` 按 YouTube 算,`permission denied` 落进 cookie 那一档,面板让人去
        钥匙串放行,而真正该做的是 `chmod`。口径同模型那一侧的 `OSError → LOCAL`。
        """
        blocked = self.root / 'blocked.sh'
        blocked.write_text('#!/bin/sh\necho hi\n', encoding='utf-8')
        blocked.chmod(0o644)
        with self.assertRaises(Exception) as caught:
            media.run([str(blocked)], self.root / 'blocked.log', time.monotonic() + 30, lambda: False)
        self.assertEqual(LOCAL, getattr(caught.exception, 'origin', YOUTUBE),
                         '起不了本机工具被算成了 YouTube 的问题')
        self.assertEqual('unknown', login.coded(caught.exception))
        self.assertIn(str(blocked), str(caught.exception), '是哪条命令起不来,人得看得见')

    def test_a_partial_listing_keeps_the_real_failure_underneath(self):
        """半截输出要**把底下那条真实失败带着**,不只留一句话。

        列表路失败时不删产物、改抛 `PartialOutput` 把已经印出来的那几条交出去 ——
        而原先只装了 `str(error)`,上游和在完整诊断上算好的分档就地丢了:本机写盘失败
        (真机形态:磁盘满、输出目录只读)先转成半截输出,一条合法条目都没有之后再包一层,
        再过一次归并,最后按文本猜成「读不出 cookie」,面板让人去钥匙串放行。

        **断言落在归并之后那一档上** —— 那是人在面板上读到的那句话的来源。
        """
        script = self.root / 'halfway.py'
        script.write_text('import sys\nprint("{}")\n'
                          'sys.stdout.write("ERROR: permission denied\\n")\nsys.exit(1)\n',
                          encoding='utf-8')
        with self.assertRaises(media.PartialOutput) as caught:
            media.run([sys.executable, str(script)], self.root / 'halfway.log',
                      time.monotonic() + 30, lambda: False, partial=True, origin=LOCAL)
        caught.exception.path.unlink(missing_ok=True)
        self.assertEqual(LOCAL, getattr(caught.exception.cause, 'origin', YOUTUBE),
                         '真实失败的上游跟着半截输出一起丢了')
        self.assertIn('permission denied', getattr(caught.exception.cause, 'diagnostic', ''))
        # 消费方这一端同形:一条合法条目都没有时,抛出去的那条要**带着真因的上游**。
        half = self.root / 'listing.txt'
        half.write_text('ERROR: permission denied\n', encoding='utf-8')
        failure = ToolFailure('写不了工具输出：permission denied', 'permission denied', '', LOCAL)
        with patch.object(media, 'ytdlp', side_effect=media.PartialOutput(half, str(failure), failure)):
            with self.assertRaises(ToolFailure) as wrapped:
                media._collect_listing(['yt-dlp'], half, '', time.monotonic() + 30, lambda: False,
                                       lambda *_: (_ for _ in ()).throw(ValueError('没有可解析的条目')),
                                       'https://www.youtube.com/channel/UC0000000001', None)
        self.assertEqual(LOCAL, getattr(wrapped.exception, 'origin', YOUTUBE))
        self.assertEqual('unknown', login.coded(discovery._combined(['详细读取失败'], [wrapped.exception])),
                         '本机写盘失败过一次归并又被说成 cookie 问题')

    def test_a_cleanup_that_cannot_delete_is_a_local_problem(self):
        """删不掉临时日志是**本机**的毛病,不许变成「读不出 cookie」。

        清理那一侧原先散着七八处裸 `path.unlink(missing_ok=True)`:`missing_ok` 只挡「文件
        不在」,挡不住权限不足、目录只读、父目录被别人占着。抛出来的 `OSError` 没有上游,
        按 `coded` 的兜底算 YouTube,而它那句 `permission denied` 正在 `login._COOKIE` 的
        特征串里 —— 面板于是让人去钥匙串放行,该做的是改权限或腾磁盘。所以清理**只走
        `media.heed` 一个出口**,由它标 `LOCAL`。
        """
        log = self.root / 'undeletable.log'
        log.mkdir()  # 占住这个名字:`unlink` 删不掉目录,而且 root 跑也一样删不掉
        (log / 'keep').write_text('x', encoding='utf-8')
        with self.assertRaises(ToolFailure) as caught:
            media.heed(log)
        self.assertEqual(LOCAL, getattr(caught.exception, 'origin', YOUTUBE),
                         '删不掉临时文件被算成了 YouTube 的问题')
        self.assertEqual('unknown', login.coded(caught.exception),
                         '本机的清理失败被当成了登录凭据问题')
        self.assertIn(log.name, str(caught.exception), '删不掉哪一个,人得看得见')

    def test_a_cache_write_that_fails_is_a_local_problem(self):
        """写缓存失败是**本机**的事,不许被当成「读不出浏览器的 cookie」。

        `no such file or directory` 和 `permission denied` 两个串都在 `login._COOKIE`
        里,而裸 `OSError` 没有上游、按 `coded` 的兜底算 YouTube —— 于是数据目录没建起来
        或者只读的那一轮,面板上说的是「去钥匙串放行」,人去折腾浏览器,而该做的是看磁盘。
        口径只有一份(`diagnosis.on_disk`),建缓存目录、写缓存、清字幕目录都过它。
        """
        with self.assertRaises(ToolFailure) as caught:
            media.save_json(self.root / 'never-created' / 'metadata.json', {'id': 'x'})
        self.assertIn('no such file or directory', str(caught.exception).lower(),
                      '裸 OSError 的原话正是 `_COOKIE` 里那一串,这一条才有意义')
        self.assertEqual(LOCAL, getattr(caught.exception, 'origin', YOUTUBE),
                         '写盘失败被算成了 YouTube 的问题')
        self.assertEqual('unknown', login.coded(caught.exception),
                         '本机写盘失败被当成了登录凭据问题')

    def test_a_cleanup_failure_never_replaces_the_failure_being_raised(self):
        """**正在抛的那条才是人要看的原因** —— 清理失败不许顶掉它。

        `heed` 挂在 `run` 的 `except` 和两处 `finally` 上。标了 `LOCAL` 之后它会抛,而那
        几处恰恰是**已经有异常在飞**的时刻:一抛就把上游那句真实失败(读不出 cookie、
        机器人验证)换成一条「删不掉某个日志」,面板上的原因整句变了样 —— 口径同 `obtain`
        「后面这一步只许追加、不许抹掉」。所以有异常在飞时 `heed` 只闭嘴。
        """
        log = self.root / 'undeletable-while-failing.log'
        log.mkdir()
        (log / 'keep').write_text('x', encoding='utf-8')
        with self.assertRaises(ToolFailure) as caught:
            try:
                raise ToolFailure('yt-dlp 执行失败', 'WARNING: cannot decrypt v10 cookies: no key found',
                                  'cookie_unreadable', YOUTUBE)
            finally:
                media.heed(log)
        self.assertEqual('cookie_unreadable', login.coded(caught.exception),
                         '上游那条真实失败被一句「删不掉日志」顶掉了')
        self.assertTrue(log.is_dir(), '清理那一下该是无害的闭嘴,不是别的副作用')
