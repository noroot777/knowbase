"""yt-dlp 自己那条工具链:版本比较,以及「装新版」怎么做到不碰正在用的那一份。

**为什么从 `test_youtube_fallback.py` 搬出来**:那份文件长到 724 行,过了本项目 700 行
的硬上限(CLAUDE.md 第一节)。按职责切:发现的几条路留在那边,工具链在这里。

**判据一律落在后果上**:失败之后原来那份还跑不跑得出版本号、指针有没有动、半截那份
有没有留在 envs 下 —— 不是「锁取了没有」「标志打上没有」。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.youtube import toolchain


class Toolchain(unittest.TestCase):
    def test_outdated_only_speaks_when_both_sides_parse(self):
        self.assertTrue(toolchain.outdated('2026.06.09', '2026.10.26'))
        self.assertTrue(toolchain.outdated('stable@2026.06.09', '2026.10.26'))
        self.assertFalse(toolchain.outdated('2026.10.26', '2026.10.26'))
        self.assertFalse(toolchain.outdated('2026.10.26', '2026.06.09'))
        # 解析不了的一律不敢说旧 —— 否则 nightly 会被永远标成「可以更新」。
        for installed, latest in (('2026.10.26.dev0', '2026.10.27'), ('', '2026.10.26'),
                                  ('2026.10.26', ''), ('nightly', '2026.10.26')):
            with self.subTest(installed=installed, latest=latest):
                self.assertFalse(toolchain.outdated(installed, latest))

    def test_bundled_copy_wins_over_the_one_on_path(self):
        """自备那份优先。用户点过更新之后就该用上新版,而 PATH 里那份可能还是旧的。"""
        with patch.object(toolchain, 'binary') as binary, \
                patch('shutil.which', return_value='/usr/bin/yt-dlp'):
            binary.return_value.is_file.return_value = True
            with patch('os.access', return_value=True):
                self.assertEqual('bundled', toolchain.origin())
                self.assertEqual([str(binary.return_value)], toolchain.command())
            binary.return_value.is_file.return_value = False
            self.assertEqual('path', toolchain.origin())
            self.assertEqual(['/usr/bin/yt-dlp'], toolchain.command())
        with patch.object(toolchain, 'binary') as binary, patch('shutil.which', return_value=None):
            binary.return_value.is_file.return_value = False
            self.assertEqual('', toolchain.origin())
            self.assertEqual([], toolchain.command())


class ToolchainPublish(unittest.TestCase):
    """**新版装完自检过了才生效;任何一步没过,正在用的那一份一个字节都不许动。**

    原地 `pip install --upgrade` 的事故形态有三种:卸载和安装之间那段窗口里采集起了
    yt-dlp(更新那把锁只有更新自己拿)、pip 被超时杀掉留下半装的环境、`command()` 只看
    入口文件在不在所以看不出里头缺东西。所以判据落在**后果**上:失败之后原来那份还跑不跑
    得出版本号、指针有没有动、半截那份有没有留在 envs 下。
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'ytdlp'
        (self.root / 'bin').mkdir(parents=True)
        patcher = patch.object(toolchain, 'home', return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_run = toolchain._run

    def plant(self, name, version):
        """在 `envs/<name>` 下放一份真跑得出版本号的假 yt-dlp。"""
        entry = self.root / 'envs' / name / 'bin' / 'yt-dlp'
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(f'#!/bin/sh\necho {version}\n')
        entry.chmod(0o755)
        return entry

    def envs(self):
        return sorted(item.name for item in (self.root / 'envs').iterdir() if item.is_dir())

    def installer(self, pip_code=0, version='2026.10.27'):
        """假的 `_run`:建 venv 那一步真把布局建出来,pip 那一步按参数成功或失败,
        **自检那一步交回真的 `_run`** —— 自检必须真起一次新装那份进程,否则这几条用例
        验的只是「有没有调过一个函数」。

        `version=''` = pip 回 0 但入口压根没写出来;`version=None` = 入口在、但跑起来
        就报错(半装那一份最常见的形态)。
        """
        def run(arguments, timeout):
            if 'venv' in arguments:
                env = Path(arguments[-1])
                (env / 'bin').mkdir(parents=True, exist_ok=True)
                python = env / 'bin' / 'python'
                python.write_text('#!/bin/sh\n')
                python.chmod(0o755)
                return 0, ''
            if 'pip' in arguments:
                entry = Path(arguments[0]).parent / 'yt-dlp'
                if pip_code == 0 and version:
                    entry.write_text(f'#!/bin/sh\necho {version}\n')
                    entry.chmod(0o755)
                elif pip_code == 0 and version is None:
                    entry.write_text('#!/bin/sh\necho "ImportError: no module named yt_dlp" >&2\nexit 1\n')
                    entry.chmod(0o755)
                return pip_code, 'pip: 装不上' if pip_code else ''
            return self.real_run(arguments, timeout)
        return run

    def test_a_failed_install_never_touches_the_copy_in_use(self):
        self.plant('v1', '2026.01.01')
        toolchain._publish('v1')
        with patch.object(toolchain, '_run', side_effect=self.installer(pip_code=1)):
            with self.assertRaises(RuntimeError) as caught:
                toolchain.upgrade()
        self.assertIn('安装 yt-dlp 失败', str(caught.exception))
        self.assertEqual('2026.01.01', toolchain.installed_version(), '原来那份照旧跑得出版本号')
        self.assertEqual([str(self.root / 'envs' / 'v1' / 'bin' / 'yt-dlp')], toolchain.command())
        self.assertEqual(['v1'], self.envs(), '半截那一份不许留在 envs 下占着')

    def test_a_copy_that_cannot_report_its_version_is_never_published(self):
        """**pip 回 0 不等于装好了。** 自检没过就当这次更新失败,指针不动。"""
        self.plant('v1', '2026.01.01')
        toolchain._publish('v1')
        for label, version in (('入口压根没写出来', ''), ('入口在但跑起来就报错', None)):
            with self.subTest(label), patch.object(toolchain, '_run',
                                                   side_effect=self.installer(version=version)):
                with self.assertRaises(RuntimeError) as caught:
                    toolchain.upgrade()
            self.assertIn('跑不出版本号', str(caught.exception))
            self.assertIn('仍然可用', str(caught.exception), '要说清原来那份没被动过')
            self.assertEqual('v1', (self.root / 'current.txt').read_text().strip())
            self.assertEqual('2026.01.01', toolchain.installed_version())
            self.assertEqual(['v1'], self.envs())

    def test_the_pointer_moves_only_after_the_self_check_passes(self):
        """反过来钉一边:自检过了,指针就要真指到新那份,而上一份留着兜底。"""
        self.plant('v1', '2026.01.01')
        toolchain._publish('v1')
        with patch.object(toolchain, '_run', side_effect=self.installer(version='2026.10.27')), \
                patch.object(toolchain, 'latest_version', return_value='2026.10.27'):
            result = toolchain.upgrade()
        self.assertEqual('2026.10.27', result['installed'])
        self.assertFalse(result['created'], '本来就有一份,这次不算首装')
        pointed = (self.root / 'current.txt').read_text().strip()
        self.assertNotEqual('v1', pointed)
        self.assertEqual('2026.10.27', toolchain.installed_version())
        self.assertEqual([str(self.root / 'envs' / pointed / 'bin' / 'yt-dlp')], toolchain.command())
        self.assertIn('v1', self.envs(), '发布那一刻可能有一轮采集已经解析过旧路径,上一份要留着')
        self.assertFalse(list(self.root.glob('current.txt.*')), '临时指针文件不许留下')

    def test_a_missing_or_dangling_pointer_falls_back_to_the_old_layout(self):
        """存量库里 venv 直接装在 `home()`,没有指针 —— 那份要一直认。

        反过来也钉一边:指针指到一份不存在的环境、或者里头被写进了路径分隔符,
        都退回旧布局,**不许返回一条不存在的路径**(那会让面板报「没有找到 yt-dlp」,
        而机器上明明还有一份能用的)。
        """
        legacy = self.root / 'bin' / 'yt-dlp'
        legacy.write_text('#!/bin/sh\necho 2025.12.31\n')
        legacy.chmod(0o755)
        self.assertEqual(legacy, toolchain.binary())
        self.assertEqual('2025.12.31', toolchain.installed_version())
        for junk in ('v9', '../../etc', '', 'a/b'):
            with self.subTest(junk=junk):
                (self.root / 'current.txt').write_text(junk)
                self.assertEqual(legacy, toolchain.binary())
        self.plant('v2', '2026.10.27')
        toolchain._publish('v2')
        self.assertEqual('2026.10.27', toolchain.installed_version(), '指针有效时就该用它那份')

    def test_upgrade_answers_in_the_same_keys_the_panel_renders(self):
        """**装完那一行和平时那一行是同一个函数渲染的,所以键集合必须一样。**

        它认 `installed`;只回 `version` 的话,装成功之后面板上是红字「没有找到
        yt-dlp」—— 装上了却报没找到。比的是**集合**而不是「有没有 installed 这个键」:
        后者挡不住以后给 `status()` 加一行而忘了这边(那一行在面板上就是空的)。
        """
        with patch.object(toolchain, '_run', side_effect=self.installer(version='2026.10.27')), \
                patch.object(toolchain, 'latest_version', return_value='2026.10.27'):
            installed = toolchain.upgrade()
            expected = set(toolchain.status())
        self.assertEqual(expected, set(installed) - {'created'})
        self.assertEqual('2026.10.27', installed['installed'])
        self.assertTrue(installed['created'], '原先一份都没有,这次是首装')
        self.assertFalse(installed['outdated'], '刚装的那一版不该当场被标成旧的')


if __name__ == '__main__':
    unittest.main()
