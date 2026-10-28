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

反过来的三边少一边就各错一头:干净成功时不许凭空造出一条警告;带警告的成功**不许**被
升级成「登录态无效」(人刚刚明明取到了元数据);命中缓存那一次没起过进程,不许拿上一轮的
警告冒充这一轮的事实。
"""
import json
import sys
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, login, media, storage, toolchain
from kb.youtube.diagnosis import ToolFailure, excerpt
from tests.youtube_fixture import YouTubeFixture

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
        self.assertIn('没带登录态', result.get('warning', ''))
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
        warnings = []
        with self.faketool([COOKIE]):
            media.probe(row, self.settings, __import__('time').monotonic() + 30, lambda: False,
                        warnings=warnings)
        self.assertEqual([], warnings)

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
