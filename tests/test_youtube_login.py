"""登录态这一块的回归:浏览器规格的校验、异常的归类、两步检查的分工、整轮的闸门。

**为什么要有「两步」那几条**:列频道不需要登录态(实测裸跑可通),取单条视频元数据才会撞
登录墙。所以第一步失败只说明连不上,把它报成「没登录」会让人反复去浏览器里登录,而问题
在另一头。这里逐条钉住那条分界。

**为什么有一条集合级的文案用例**:X 那边把一档挪到首项时,它那句「风险同上」当场指向了
空气。逐档断言「有没有文案」是钉不住这类错的 —— 那条用例要比的是**值**:两两不同、
都自带主语。
"""
import unittest
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, login, media, storage, toolchain
from tests.youtube_fixture import YouTubeFixture


class BrowserSpec(unittest.TestCase):
    def test_accepts_profile_form_and_rejects_everything_else(self):
        self.assertEqual('chrome', login.validate(''))
        self.assertEqual('chrome', login.validate('  chrome '))
        self.assertEqual('chrome:Profile 1', login.validate('chrome:Profile 1'))
        self.assertEqual('firefox', login.validate('firefox'))
        for bad in ('netscape', 'chrome;rm -rf /', 'chrome|cat', '../chrome', 'chrome:a:b',
                    'chrome$(id)', 'x' * 80, 'chrome\nfirefox'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                login.validate(bad)

    def test_arguments_only_carry_validated_values(self):
        """**钉不住 cookie 主库那条路上,交出去的仍然只能是校验过的那个串。**

        `cookie_store` 在这儿打成「挑不出来」:它读的是真机上那个浏览器目录,留着不打桩的话
        这条用例在「本机恰好有 Profile 1」的机器上红,还会往仓库数据目录里建视图。
        钉住那一半由 `tests/test_youtube_challenge.py` 按后果验(yt-dlp 会读到哪一份库)。
        """
        with patch.object(login, 'cookie_store', return_value=None):
            self.assertEqual(['--cookies-from-browser', 'chrome:Profile 1'],
                             login.arguments('chrome:Profile 1'))
            with self.assertRaises(ValueError):
                login.arguments('netscape')


class Classification(unittest.TestCase):
    def test_cookie_trouble_never_reads_as_a_login_problem(self):
        """**cookie 读不出来优先于一切墙。** 两类特征同时出现时,报的必须是 cookie 那一类
        —— 它是因,墙是果;报成「没登录」就把人送去做一件没用的事。"""
        self.assertEqual('cookie_unreadable', login.classify(
            'ERROR: could not find chrome cookies database'))
        self.assertEqual('cookie_unreadable', login.classify(
            'Failed to decrypt with DPAPI. Sign in to confirm you are not a bot'))
        # yt-dlp 只把「解不开 cookie」报成一条 warning,所以这一句必须认得出来 ——
        # 认不出就只剩下游那道墙,而那会被归成「去登录」(我们自己压掉过这条警告)。
        self.assertEqual('cookie_unreadable', login.classify(
            'WARNING: cannot decrypt v10 cookies: no key found'))

    def test_only_the_real_login_signals_say_go_log_in(self):
        """**机器人验证和「这一条没授权」都不蕴含未登录。**

        前者是出口 IP 级的(实测两个 yt-dlp 版本撞同一道、换版本无效),已经登录的人
        照样会撞上;后者是单条内容的授权 —— 而第一个启用频道的最新一条恰好受限,就会
        被当成整个账号的状态。两者归成「没登录」都是把人送去反复登录,而问题在别处。
        """
        self.assertEqual('bot_wall', login.classify(
            'ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser'))
        self.assertEqual('no_access', login.classify('ERROR: [youtube] abc: This video is private'))
        self.assertEqual('no_access', login.classify('ERROR: Join this channel to get access'))
        # 真正指向登录态的只剩这一组。
        self.assertEqual('not_logged_in', login.classify('Account cookies are no longer valid'))
        self.assertEqual('not_logged_in', login.classify('ERROR: Please sign in to view this video'))
        # 这几档都是「一个出口、一次全中」,归并同因失败时按整个集合判。
        self.assertEqual({'not_logged_in', 'bot_wall', 'cookie_unreadable', 'rate_limited',
                          'js_challenge'}, set(login.AUTH_CODES))
        self.assertNotIn('no_access', login.AUTH_CODES, '单条没授权不是这一摊的事')

    def test_a_throttled_exit_is_not_a_bot_wall_and_not_a_login_problem(self):
        """**429 要排在机器人验证前面。** 真机形态(用户截图):YouTube 先回 429,yt-dlp 换
        别的 player 客户端再试,最后报出「Sign in to confirm you're not a bot」。两句话一起
        回来时照 bot 那一档说「带上登录态通常能过」是错的 —— 他本来就带着 cookie,照着做
        只会再登一次,而该做的是等一段时间或把一轮的量调小。

        反过来钉两边:单独的 bot 那句照旧是 `bot_wall`(没有 429 就不是限流);cookie 读不出来
        照旧压过 429(它更靠前、而且是本机就能解决的那一头)。
        """
        throttled = ('YouTube 元数据无效，未开始下载或转录；WARNING: [youtube] Unable to '
                     'download webpage: HTTP Error 429: Too Many Requests (caused by <HTTPError 429>)')
        self.assertEqual('rate_limited', login.classify(throttled))
        self.assertEqual('rate_limited', login.classify(
            throttled + " | ERROR: [youtube] abc: Sign in to confirm you're not a bot."))
        self.assertEqual('bot_wall', login.classify(
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot."))
        self.assertEqual('cookie_unreadable', login.classify(
            'WARNING: cannot decrypt v10 cookies: no key found | HTTP Error 429: Too Many Requests'))
        # 版本旧那条警告每次都在,不许把它拖成「限流」之外的别的结论。
        self.assertEqual('rate_limited', login.classify(
            'WARNING: Your yt-dlp version (2026.06.09) is older than 90 days! | '
            'WARNING: [youtube] Unable to download webpage: HTTP Error 429: Too Many Requests'))

    def test_unfamiliar_and_empty_output_admit_ignorance(self):
        """**判不准一律 unknown。** 猜一个具体原因比承认不知道更坏:用户会照着那条错的做。"""
        for text in ('', None, 'ERROR: HTTP Error 503: Service Unavailable', '随便一句没见过的话'):
            with self.subTest(text=text):
                self.assertEqual('unknown', login.classify(text))

    def test_every_message_stands_alone_and_differs(self):
        """每一档的说明都要能单独读懂。**比值,不比有无。**"""
        codes = ('metadata_ok', 'yt_dlp_missing', 'no_browser', 'no_channel', 'unreachable',
                 'cookie_unreadable', 'not_logged_in', 'bot_wall', 'rate_limited', 'no_access',
                 'js_challenge', 'output_cap', 'unknown')
        self.assertEqual(set(codes), set(login.MESSAGES), '有档位没有自己的说明,或多了一档没人用')
        spoken = [login.describe(code, 'chrome') for code in codes]
        self.assertEqual(len(spoken), len(set(spoken)), '有两档说的是同一句话')
        for code, text in zip(codes, spoken):
            with self.subTest(code=code):
                self.assertNotIn('{', text, '占位符没有被填上')
                for pointer in ('同上', '见上', '同前', '如上'):
                    self.assertNotIn(pointer, text, '说明在指代别的档位,挪动顺序就会指向空气')
        for code in ('no_browser', 'cookie_unreadable', 'not_logged_in'):
            with self.subTest(code=code):
                self.assertIn(login.LABELS['chrome'], login.describe(code, 'chrome'))
                self.assertIn(login.LABELS['firefox'], login.describe(code, 'firefox'))


class Readiness(YouTubeFixture):
    def test_cheap_pass_never_claims_the_login_works(self):
        """便宜那一道只回答「配没配」。**它不许说登录态有效** —— cookie 过期在文件上看不出来。"""
        settings = reg.get_settings('youtube')
        with patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(login, 'installed', return_value=True):
            result = login.readiness(settings)
        self.assertTrue(result['ready'])
        self.assertEqual('unverified', result['code'])
        self.assertIn('检查登录态', result['message'])

    def test_missing_tool_and_missing_browser_are_separate_answers(self):
        settings = reg.get_settings('youtube')
        with patch.object(toolchain, 'command', return_value=[]):
            missing = login.readiness(settings)
        with patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(login, 'installed', return_value=False):
            absent = login.readiness(settings)
        self.assertEqual(('yt_dlp_missing', False), (missing['code'], missing['ready']))
        self.assertEqual(('no_browser', False), (absent['code'], absent['ready']))
        self.assertNotEqual(missing['message'], absent['message'])


class TwoStepCheck(YouTubeFixture):
    def setUp(self):
        super().setUp()
        # `defaults.json` 会幂等补种 48 个默认频道,不先停用它们,下面断言的频道数
        # 和「第一个启用的频道」都不是自己建的那一条(同其余 youtube 回归的起手式)。
        self.isolate()
        self.source = self.channel()
        self.ready = patch.object(toolchain, 'command', return_value=['yt-dlp'])
        self.ready.start(); self.addCleanup(self.ready.stop)
        self.here = patch.object(login, 'installed', return_value=True)
        self.here.start(); self.addCleanup(self.here.stop)

    def run_check(self, listing=None, probing=None):
        with patch.object(media, 'channel_videos', **listing), patch.object(media, 'probe', **probing):
            return login.check(reg.get_settings('youtube'))

    def test_listing_failure_is_never_reported_as_a_login_problem(self):
        """**第一步不需要登录态,所以它失败不是登录问题。** 唯一有把握的归类是 cookie。"""
        plain = self.run_check(listing={'side_effect': RuntimeError('HTTP Error 500')},
                               probing={'return_value': {}})
        cookie = self.run_check(listing={'side_effect': RuntimeError('could not find chrome cookies database')},
                                probing={'return_value': {}})
        # 429 在这一步同样判得准:它说的是出口被限,和「要不要登录」无关,
        # 所以不该被兜底成笼统的「连不上」—— 那句话会把人支去查网络。
        throttled = self.run_check(listing={'side_effect': RuntimeError('HTTP Error 429: Too Many Requests')},
                                   probing={'return_value': {}})
        self.assertEqual('unreachable', plain['code'])
        self.assertEqual('cookie_unreadable', cookie['code'])
        self.assertEqual('rate_limited', throttled['code'])
        for result in (plain, cookie, throttled):
            self.assertFalse(result['ready'])
            self.assertNotEqual('not_logged_in', result['code'])

    def test_second_step_reports_which_wall_it_hit(self):
        listed = [{'video_id': '12345678901', 'title': 'x', 'duration': 10.0,
                   'url': 'https://www.youtube.com/watch?v=12345678901'}]
        walled = self.run_check(
            listing={'return_value': listed},
            probing={'side_effect': RuntimeError('Sign in to confirm you’re not a bot')})
        # **不是「没登录」**:这道墙是出口 IP 级的,说成没登录会把人送去反复登录。
        self.assertEqual('bot_wall', walled['code'])
        self.assertFalse(walled['ready'])
        self.assertIn('Sign in', walled['detail'])
        stale = self.run_check(listing={'return_value': listed},
                               probing={'side_effect': RuntimeError('Account cookies are no longer valid')})
        self.assertEqual('not_logged_in', stale['code'])
        # 单条视频本身不可访问,也不是登录态的问题 —— 第一个启用频道的最新一条恰好
        # 是会员专属,就会把整个账号判成未登录。
        limited = self.run_check(
            listing={'return_value': listed},
            probing={'side_effect': RuntimeError(
                "HTTP Error 429: Too Many Requests | Sign in to confirm you're not a bot")})
        self.assertEqual('rate_limited', limited['code'], '限流被说成 bot 墙,人就去白登一次')
        self.assertIn('429', limited['detail'])
        locked = self.run_check(listing={'return_value': listed},
                                probing={'side_effect': RuntimeError('This video is private')})
        self.assertEqual('no_access', locked['code'])
        self.assertFalse(locked['ready'])
        # 反过来钉一边:两步都过就算就绪,而且带着「检的是哪个浏览器」回去。
        fine = self.run_check(listing={'return_value': listed},
                              probing={'return_value': {'id': '12345678901', 'duration': 10}})
        self.assertEqual(('metadata_ok', True, 'chrome'),
                         (fine['code'], fine['ready'], fine['browser']))

    def test_passing_both_steps_does_not_claim_the_login_is_valid(self):
        """**两步都过只证明工具链和出口通,不证明那份 cookie 还有效。**

        取元数据公开视频也过得去 —— 真正用到登录态的是字幕和音频,那要等同步才知道。
        所以这一档必须带着 `confirmed=False` 回去(面板据它把「已就绪」降成中性提示),
        文案里也不许出现「登录态有效」:拿更弱的证据冒充结论,会让人在同步仍然失败时
        先去怀疑别处。
        """
        listed = [{'video_id': '12345678901', 'title': 'x', 'duration': 10.0,
                   'url': 'https://www.youtube.com/watch?v=12345678901'}]
        fine = self.run_check(listing={'return_value': listed},
                              probing={'return_value': {'id': '12345678901', 'duration': 10}})
        self.assertTrue(fine['ready'])
        self.assertFalse(fine['confirmed'], '没验过字幕/音频就不算确认')
        self.assertIn('还不能证明', fine['message'], '这一句就是「别把它当结论」那道提醒')
        self.assertIn('字幕', fine['message'], '要说清真正用得上登录态的是哪一步')
        # 便宜的那一道证据更弱,同样不许自称确认。
        with patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(login, 'installed', return_value=True):
            cheap = login.readiness(reg.get_settings('youtube'))
        self.assertEqual((True, False), (cheap['ready'], cheap['confirmed']))

    def test_no_enabled_channel_says_so_instead_of_guessing(self):
        self.isolate()  # 连 setUp 建的那条一起停用
        with patch.object(media, 'channel_videos') as listing:
            result = login.check(reg.get_settings('youtube'))
        listing.assert_not_called()
        self.assertEqual('no_channel', result['code'])


class RoundGate(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()

    def test_unready_login_stops_the_round_without_punishing_the_channels(self):
        """**缺的是本机配置,不是这些订阅的毛病。** 所以不写 `last_error`、不推退避 ——
        配好之后该立刻能跑,不该先等一轮退避过去。"""
        with patch.object(toolchain, 'command', return_value=[]), \
                patch.object(discovery, 'fetch') as fetch:
            summary = collector.collect(FetchScope())
        fetch.assert_not_called()
        self.assertEqual(1, summary['unconfigured'])
        self.assertIn('yt-dlp', summary['message'])
        self.assertEqual([], summary['errors'])
        after = next(item for item in reg.subscriptions('youtube') if item['id'] == self.source['id'])
        self.assertEqual('', after['last_error'])
        self.assertEqual(0, after['retry_at'])
        self.assertEqual(0, after['failures'])
        self.assertEqual([], storage.rows())

    def test_ready_login_lets_the_round_through(self):
        """反过来钉一边:就绪时闸门不许拦 —— 否则这道闸就是把来源整个关掉了。"""
        with patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(login, 'installed', return_value=True), \
                patch.object(discovery, 'fetch', return_value=[self.entry(1)]), \
                patch.object(collector.notes, 'available', return_value=(False, '模型未配置')):
            summary = collector.collect(FetchScope())
        self.assertEqual(1, summary['checked'])
        self.assertEqual(1, summary['discovered'])

    def enabled_channels(self, total):
        """把启用的频道补到 `total` 个。一个用例里可能连跑两档,所以建过就不再建
        (重复链接会被订阅层拒掉),而 `isolate()` 停用的那 48 条还在表里、不能按总数算。"""
        live = [item for item in reg.subscriptions('youtube') if item['enabled']]
        for number in range(len(live) + 1, total + 1):
            self.channel(number)

    def walled_round(self, text):
        self.enabled_channels(4)
        with patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(login, 'installed', return_value=True), \
                patch.object(discovery, 'fetch', side_effect=RuntimeError(text)), \
                patch.object(collector.notes, 'available', return_value=(True, '')):
            return collector.collect(FetchScope())

    def test_repeated_wall_failures_collapse_into_one_sentence(self):
        """48 个频道各报一遍同一句话,要逐条读完才明白只有一个原因。归并那句要带频道数。

        **归并按整个 `AUTH_CODES` 集合判,不是只看 `not_logged_in`** —— 实测最常撞的
        恰恰是机器人验证那一档,只认一个档等于让最常见的那一类退回逐条重复。
        """
        summary = self.walled_round('ERROR: [youtube] abc: Sign in to confirm you’re not a bot')
        self.assertEqual(4, len(summary['errors']), '逐条明细仍要留着')
        self.assertIn('4 个频道卡在同一处', summary['message'])
        self.assertIn('机器人', summary['message'])

    def test_the_collapsed_sentence_says_which_browser_when_that_is_the_fix(self):
        """**归并不许把要做的那件事归并掉。** 真·登录失效那一档,人要去的是某个具体
        浏览器 —— 那句话里没有浏览器名就等于没说要做什么。反过来钉住上一条:bot 墙
        那一档跟浏览器无关(换浏览器没用),所以那里不该出现浏览器名。"""
        summary = self.walled_round('ERROR: Account cookies are no longer valid')
        self.assertIn('4 个频道卡在同一处', summary['message'])
        self.assertIn(login.LABELS['chrome'], summary['message'])
        bot = self.walled_round('ERROR: Sign in to confirm you’re not a bot')
        self.assertNotIn(login.LABELS['chrome'], bot['message'])

    def test_two_different_walls_are_not_collapsed_into_one(self):
        """**同因才并,不同因不并。** 两档并成一句会让其中一半人去做没用的事。"""
        self.channel(2)
        errors = iter([RuntimeError('ERROR: Account cookies are no longer valid'),
                       RuntimeError('ERROR: Sign in to confirm you’re not a bot')])

        def failing(*arguments, **keywords):
            raise next(errors)

        with patch.object(toolchain, 'command', return_value=['yt-dlp']), \
                patch.object(login, 'installed', return_value=True), \
                patch.object(discovery, 'fetch', side_effect=failing), \
                patch.object(collector.notes, 'available', return_value=(True, '')):
            summary = collector.collect(FetchScope())
        self.assertEqual(2, len(summary['errors']))
        self.assertEqual(2, summary['message'].count('个频道卡在同一处'), summary['message'])
        self.assertIn(login.LABELS['chrome'], summary['message'])
        self.assertIn('机器人', summary['message'])


if __name__ == '__main__':
    unittest.main()
