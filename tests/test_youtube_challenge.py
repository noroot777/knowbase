# -*- coding: utf-8 -*-
"""两件「明明登录着、却时好时坏」的事:**这一轮带没带上登录态**、**那道 JS 挑战解没解开**。

两条都是实测复现过的真机形态,而且都表现成下游那道墙,所以**红要落在后果上**:
这一轮发出去的 cookie 库是哪一份、元数据到底回来没有、面板上那句话说的是哪一档。

1. **cookie 库被扩展分区顶掉。** yt-dlp 自己的做法是在整个浏览器目录里
   `max(所有叫 Cookies 的文件, key=mtime)`(`yt_dlp/cookies.py` 的 `_find_files` / `_newest`),
   本意是「多个 profile 就用最近用过的那个」。但 Chrome 130+ 把**每个扩展的 cookie 分区**
   也命名为 `Cookies`,摆在 `<profile>/Storage/ext/<id>/<n>/` 下 —— 哪个扩展刚活动过,它就
   成了「最近用过的 profile」。实测本机:主库 `Default/Cookies` 983 KB、19:33:24,内置扩展
   那份 32 KB、19:32:09,**只差 75 秒**;同一分钟里跑两次,一次读到 1843 条并打出
   `Found YouTube account cookies`,一次只读到 30 条、当场撞
   `Sign in to confirm you're not a bot`。那一轮根本是以未登录身份出去的,而面板上只看得见
   下游那道墙,于是人被送去反复登录。

2. **JS 挑战解不开,元数据跟着一起失败。** YouTube 会对一部分请求发一道 JS 挑战,yt-dlp 要
   用本地解题脚本(`yt-dlp-ejs`)配一个 JS 运行时(deno 这类)去算;pip 装的那份不带脚本,
   于是 `n challenge solving failed` → `Only images are available for download` → 默认格式
   选择器一个都没匹配上 → `Requested format is not available` **当场抛错**。实测:cookie
   完全正常(1843 条、`Found YouTube account cookies`)也一样撞。**元数据、列频道、字幕压根
   不需要任何格式**,却跟着一起失败 —— 连「检查登录态」都报错。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.youtube import login, media, toolchain
from tests.youtube_fixture import YouTubeFixture

#: 复刻 yt-dlp 挑 cookie 库的那两行(`_find_files` + `_newest`)。**断言落在这儿**:
#: 问的是「我们交出去之后,yt-dlp 会读到哪一份」,不是「我们那个函数返回了什么」。
def resolved(search_root: Path) -> Path | None:
    found = [path for path in search_root.rglob('Cookies') if path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime, default=None)


class CookieStorePinned(unittest.TestCase):
    """钉住主 cookie 库:**扩展分区再新也不许赢**,而「最近用过的 profile」照旧要赢。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.browser = self.root / 'browser'
        (self.browser / 'Default').mkdir(parents=True)
        (self.browser / 'Local State').write_text('{}', encoding='utf-8')
        for attribute, value in (('_HOME', self.root),
                                 ('DATA_DIR', self.root / 'data'),
                                 ('_ROOTS', {'chrome': {login.platform_key(): ['browser']},
                                             'firefox': {login.platform_key(): ['firefox']}})):
            override = patch.object(login, attribute, value)
            override.start()
            self.addCleanup(override.stop)

    def store(self, relative: str, age: float) -> Path:
        """在浏览器目录下放一份 cookie 库。`age` 是「多少秒以前被写过」。"""
        path = self.browser / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'SQLite format 3\x00')
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        # **两边都 `resolve()` 之后再比**(macOS 的 `/var` 本身就是条链接,比原始字符串必红;
        # 口径同 CLAUDE.md 操作入口那节的 realpath)。
        return path.resolve()

    def handed(self, spec='chrome') -> Path:
        """我们交给 yt-dlp 的那条路径,按它自己的算法解出来的那一份库。"""
        arguments = login.arguments(spec)
        self.assertEqual('--cookies-from-browser', arguments[0])
        _, _, profile = arguments[1].partition(':')
        self.assertTrue(profile, f'没钉住,交出去的还是浏览器名：{arguments[1]}')
        found = resolved(Path(profile))
        # 视图里那一份是指向真文件的链接,**比的是它最终落在哪儿**(我们一个字节都不复制)。
        return found if found is None else found.resolve()

    def test_an_extension_partition_never_wins_the_cookie_store(self):
        """**真机形态**:扩展那份比主库新 75 秒,yt-dlp 就只读到扩展里那几条。

        后果是这一轮以未登录身份出去,而失败表现成 `Sign in to confirm you're not a bot`
        —— 人照着去重新登录完全没用。
        """
        main = self.store('Default/Cookies', age=200)
        partition = self.store('Default/Storage/ext/glic/66A834677761/Cookies', age=1)
        self.assertEqual(partition, resolved(self.browser).resolve(),
                         '前提没立住:yt-dlp 原本的算法本该挑中扩展那一份')
        self.assertEqual(main, self.handed(), '扩展的 cookie 分区又把主库顶掉了')

    def test_the_most_recently_used_profile_still_wins(self):
        """**反过来钉一边:yt-dlp 的本意要留着。**

        多 profile 时它挑「最近用过的那个」—— 这一点不是 bug,写死 `Default` 会把一个
        早就不用、而且没登录的 profile 交出去。所以只排除扩展分区,不排除别的 profile。
        """
        self.store('Default/Cookies', age=500)
        recent = self.store('Profile 1/Cookies', age=5)
        self.store('Profile 1/Storage/ext/abc/1/Cookies', age=1)
        self.assertEqual(recent, self.handed(), '最近用过的那个 profile 没被挑中')

    def test_a_named_profile_is_the_only_one_considered(self):
        """设置里点名了 profile,就只认它那一份 —— 哪怕别的 profile 刚被写过。"""
        named = self.store('Profile 1/Cookies', age=500)
        self.store('Default/Cookies', age=1)
        self.assertEqual(named, self.handed('chrome:Profile 1'))

    def test_the_modern_layout_under_network_is_found_too(self):
        """现代 Chromium 把主库挪进了 `<profile>/Network/` —— 两种布局都要认得出来。"""
        inner = self.store('Default/Network/Cookies', age=100)
        self.store('Default/Storage/ext/abc/1/Cookies', age=1)
        self.assertEqual(inner, self.handed())
        view = Path(login.arguments('chrome')[1].partition(':')[2])
        self.assertTrue((view.parent / 'Local State').exists(),
                        'Windows 的解密密钥从视图根的 Local State 取,少了它就解不出 cookie')

    def test_a_browser_without_partitions_is_handed_over_untouched(self):
        """**Firefox / Safari 不钉**:它们的 cookie 全在一个单独的文件里,没有同名分区。

        不该为了「统一」去动它们 —— 交出去的照旧是浏览器名,`pinned` 如实报 False。
        """
        (self.root / 'firefox').mkdir()
        view = login.cookie_view('firefox')
        self.assertEqual(['--cookies-from-browser', 'firefox'], view['args'])
        self.assertFalse(view['pinned'])
        self.assertEqual('', login.pin_note('firefox'), 'Firefox 没有这个问题,不该惊动用户')

    def test_a_view_that_cannot_be_built_degrades_and_says_so(self):
        """**搭不起来就如实降级**(Windows 上建符号链接要特权):照旧只交浏览器名,
        并且在面板上说清这一轮没钉住 —— 悄悄退回去等于让人继续猜。
        """
        self.store('Default/Cookies', age=10)
        with patch('os.symlink', side_effect=OSError('operation not permitted')), \
                patch('os.link', side_effect=OSError('cross-device link')):
            view = login.cookie_view('chrome')
            note = login.pin_note('chrome')
        self.assertEqual(['--cookies-from-browser', 'chrome'], view['args'])
        self.assertFalse(view['pinned'])
        # 两样都试过了,`reason` 带的是最后那一次的原话 —— 要紧的是「为什么没钉住」说出来了。
        self.assertIn('建不了 cookie 视图', view['reason'], '为什么没钉住要说出来')
        self.assertIn('cross-device link', view['reason'], '原话要带上,那是唯一的线索')
        self.assertIn('未登录身份', note, '面板上要说清这一轮可能是以未登录身份出去的')

    def test_pinning_never_raises_out_of_the_call_path(self):
        """**它挂在每一次 yt-dlp 调用的前面,一个字都不许往上抛。**

        读不动浏览器目录(权限、目录被换成文件)时返回「挑不出来」,由调用方降级。
        **`iterdir` 和 `stat` 一起打桩**:判据是「挑选这一步出任何 OSError 都不外溢」,
        不是「用了哪一个系统调用」—— 只打一个的话,换种实现方式这条就变成假红。
        """
        self.store('Default/Cookies', age=10)
        with patch.object(Path, 'iterdir', side_effect=OSError('permission denied')), \
                patch.object(Path, 'stat', side_effect=OSError('permission denied')):
            self.assertIsNone(login.cookie_store('chrome'))
            self.assertEqual(['--cookies-from-browser', 'chrome'], login.arguments('chrome'))


class ChallengeVerdict(unittest.TestCase):
    """那道挑战自己一档:**不许报成「没能归类」,也不许抢了别人的档。**"""

    def test_the_challenge_shape_is_its_own_verdict(self):
        """用户那一次的原话(实测复现过同一串)。"""
        text = ('WARNING: [youtube] [jsc] Remote components challenge solver script (deno) and NPM '
                'package (deno) were skipped. These may be required to solve JS challenges. '
                'WARNING: Only images are available for download. use --list-formats to see them '
                'ERROR: [youtube] C4Sq3Rot8FU: Requested format is not available.')
        self.assertEqual('js_challenge', login.classify(text))
        self.assertIn('JS 运行时', login.describe('js_challenge', 'chrome'))
        self.assertNotIn('登录', login.describe('js_challenge', 'chrome').split('这不是登录')[0],
                         '这一档不许把人往登录那头引')

    def test_a_bare_format_error_stays_unknown(self):
        """**反过来钉一边:光有「格式不可用」不算。**

        那也可能是这条视频真的没有音轨 —— 那时让人去装 JS 运行时是帮倒忙。判不准就落
        `unknown` 原样交出原文(口径同 `classify` 的末档)。
        """
        self.assertEqual('unknown', login.classify(
            'ERROR: [youtube] abc: Requested format is not available. Use --list-formats'))

    def test_the_causes_upstream_of_it_still_win(self):
        """429、机器人验证、cookie 读不出来都排在它前面 —— 它们才是人要去处理的那件事。"""
        for mark, expected in (('ERROR: HTTP Error 429: Too Many Requests', 'rate_limited'),
                               ('ERROR: Sign in to confirm you are not a bot', 'bot_wall'),
                               ('WARNING: cannot decrypt v10 cookies: no key found', 'cookie_unreadable')):
            with self.subTest(expected):
                self.assertEqual(expected, login.classify(
                    mark + ' WARNING: [jsc] challenge solver skipped. Only images are available'))

    def test_the_verdict_merges_instead_of_repeating_itself(self):
        """一个出口、一次全中:48 个频道会一字不差各报一遍,所以它要进归并那个集合。"""
        self.assertIn('js_challenge', login.AUTH_CODES)


class SolverShipped(unittest.TestCase):
    """解题脚本跟 yt-dlp 一起装,**而且装没装上要如实报**。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'ytdlp'
        (self.root / 'bin').mkdir(parents=True)
        override = patch.object(toolchain, 'home', return_value=self.root)
        override.start()
        self.addCleanup(override.stop)
        self.real_run = toolchain._run

    def installer(self, solver_code=0):
        """假的 `_run`:venv 那一步真把布局建出来,pip 那一步按参数成功或失败,
        **自检那一步交回真的 `_run`**(同 `tests/test_youtube_toolchain.py` 的口径)。
        """
        self.asked = []

        def run(arguments, timeout):
            if 'venv' in arguments:
                env = Path(arguments[-1])
                (env / 'bin').mkdir(parents=True, exist_ok=True)
                python = env / 'bin' / 'python'
                python.write_text('#!/bin/sh\n')
                python.chmod(0o755)
                return 0, ''
            if 'pip' in arguments:
                self.asked.append(arguments)
                if toolchain.SOLVER in arguments and solver_code:
                    return solver_code, 'pip: 装不上 yt-dlp-ejs'
                entry = Path(arguments[0]).parent / 'yt-dlp'
                entry.write_text('#!/bin/sh\necho 2026.10.26\n')
                entry.chmod(0o755)
                return 0, ''
            return self.real_run(arguments, timeout)
        return run

    def test_the_install_asks_for_the_solver_alongside_ytdlp(self):
        with patch.object(toolchain, '_run', side_effect=self.installer()):
            toolchain.upgrade()
        self.assertEqual(1, len(self.asked), '一条 pip 命令就该把两样一起装上')
        self.assertIn(toolchain.PACKAGE, self.asked[0])
        self.assertIn(toolchain.SOLVER, self.asked[0])

    def test_a_solver_that_cannot_be_installed_still_publishes_ytdlp(self):
        """**解题脚本装不上不许连 yt-dlp 一起不装。**

        它只影响「下音频」那一步,元数据、列频道、字幕照旧能跑;一起失败的话用户连版本都
        更新不了,而他点这颗按钮多半正是因为抓不到东西。
        """
        with patch.object(toolchain, '_run', side_effect=self.installer(solver_code=1)):
            result = toolchain.upgrade()
        self.assertEqual('2026.10.26', result['installed'], '退一步也要把 yt-dlp 装上')
        self.assertEqual(2, len(self.asked), '第二条命令只装 yt-dlp')
        self.assertNotIn(toolchain.SOLVER, self.asked[1])

    def test_the_panel_hears_which_of_the_two_is_missing(self):
        """缺脚本和缺运行时让用户做的事不同(点一次更新 / 自己装个 deno),所以分开报。"""
        header = ('[debug] Optional libraries: sqlite3-3.53.2, yt_dlp_ejs-0.8.0\n'
                  '[debug] JS runtimes: deno-2.9.0')
        with patch.object(toolchain, '_run', return_value=(2, header)):
            self.assertEqual({'script': 'yt_dlp_ejs-0.8.0', 'runtime': 'deno-2.9.0'},
                             toolchain.challenge())
        bare = ('[debug] Optional libraries: sqlite3-3.53.2\n'
                '[debug] JS runtimes: none')
        with patch.object(toolchain, '_run', return_value=(2, bare)):
            self.assertEqual({'script': '', 'runtime': ''}, toolchain.challenge())

    def test_the_status_keys_the_panel_renders_are_all_there(self):
        """面板那一行读的就是这几个键 —— 少一个就在页面上显示成「缺」。"""
        entry = self.root / 'envs' / 'v1' / 'bin' / 'yt-dlp'
        entry.parent.mkdir(parents=True)
        entry.write_text('#!/bin/sh\necho 2026.10.26\n')
        entry.chmod(0o755)
        toolchain._publish('v1')
        with patch.object(toolchain, 'latest_version', return_value='2026.10.26'):
            status = toolchain.status()
        for key in ('installed', 'origin', 'latest', 'outdated', 'latest_error', 'script', 'runtime'):
            self.assertIn(key, status)


class MetadataSurvivesTheChallenge(YouTubeFixture):
    """挑战解不开那一轮:**元数据照旧要回来,而那道挑战照旧要说出来。**"""

    #: 照 yt-dlp 自己那段逻辑写的假工具(`YoutubeDL.process_video_result`:一个格式都没选中时,
    #: 带 `--ignore-no-formats-error` 就降级成 warning 往下走,不带就当场抛)。
    SCRIPT = (
        'import json, sys\n'
        'args = sys.argv[1:]\n'
        "print('[youtube] [jsc:deno] Solving JS challenges using deno')\n"
        "print('WARNING: [youtube] [jsc] Remote components challenge solver script (deno) "
        "were skipped. These may be required to solve JS challenges')\n"
        "print('WARNING: n challenge solving failed: Some formats may be missing')\n"
        "print('WARNING: Only images are available for download. use --list-formats to see them')\n"
        "if '--ignore-no-formats-error' not in args:\n"
        "    print('ERROR: Requested format is not available. "
        "Use --list-formats for a list of available formats')\n"
        '    sys.exit(1)\n'
        "print(json.dumps({'id': '12345678901', 'duration': 600, 'language': 'en',\n"
        "                  'upload_date': '20261027', 'view_count': 1000,\n"
        "                  'subtitles': {'en': [{'url': 'https://example.invalid/s'}]}}))\n"
    )

    def setUp(self):
        super().setUp()
        self.isolate()
        self.row = {'video_id': '12345678901',
                    'entry': {'url': 'https://www.youtube.com/watch?v=12345678901'}}
        self.deadline = time.monotonic() + 30
        override = patch.object(toolchain, 'command', return_value=[sys.executable, '-c', self.SCRIPT])
        override.start()
        self.addCleanup(override.stop)

    def test_metadata_still_comes_back_when_only_images_are_available(self):
        """**不下载的那几条路压根不需要格式**,不该跟着格式选择一起失败。

        这一条红的时候,面板上「检查登录态」会直接报错,而那一轮的 cookie 好着
        (实测:`Found YouTube account cookies`、1843 条)。
        """
        noise = []
        metadata = media.probe(self.row, {'youtube': {}}, self.deadline, lambda: False,
                               force=True, warnings=noise)
        self.assertEqual(600, metadata['duration'], '元数据没回来')
        self.assertEqual(['en'], list(metadata['subtitles']), '字幕清单也该在')
        # **那道挑战照旧要说出来**:退 0 不等于什么都没发生,面板上并列显示那一句。
        self.assertEqual('js_challenge', login.classify(' | '.join(noise)), noise)

    def test_the_audio_download_still_fails_loudly(self):
        """**反过来钉一边:下音频那一步「没有可用格式」是真失败。**

        那里带上这个开关的话,yt-dlp 会退 0、却一个音频文件都没下下来,我们只好报一句
        「未取得音频」—— 真正的原因(那道挑战)就此丢掉,人照着去查磁盘和 ffmpeg。
        """
        with self.assertRaises(RuntimeError) as caught:
            media.ytdlp(['-f', 'bestaudio', self.row['entry']['url']],
                        self.root / 'audio.log', self.deadline, lambda: False, downloads=True)
        self.assertIn('Requested format is not available', str(caught.exception))
        self.assertEqual('js_challenge', login.coded(caught.exception),
                         '真失败那一次的分档也要是挑战这一档,不是「没能归类」')


if __name__ == '__main__':
    unittest.main()
