# -*- coding: utf-8 -*-
"""跑着的 kb-web 端着旧模块时,`./kb.sh` 要说一句 —— 而且说的得是真因。

事故形态(2026-10-10 实测,这组用例就是照它写的):kb-web 10-09 21:23 起,那一刻
`kb.feeds.x_channels` 还没有 `fill_bridge`;07:08 git 把源码换成新版;08:03 第一次
跑同步时 `kb.feeds.sync` 才被**延迟 import**、从磁盘读到新版并调用
`x_channels.fill_bridge` —— 而 `x_channels` 早以旧版驻留在 sys.modules 里。于是
同一个进程里新旧两个版本混跑,七条 RSS 订阅一起报
`module 'kb.feeds.x_channels' has no attribute 'fill_bridge'`、各自被推进最长
24 小时的退避,而面板上写的是「连接失败」。

**判据一律是后果**(那句提示出没出现、里面有没有可执行的下一步),不是"函数定义
在不在":一个定义对了却没人调的实现,只验定义的话照样全绿 —— 所以挂载点那两条
真跑 `web_start` / `web_status` 的那段代码,只把"进程在不在跑"这个外部事实替掉。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_SH = ROOT / 'kb.sh'
# 522 行那句 `[ $# -gt 0 ] || { usage; exit 0; }` 之后是 case 分发:source 函数段
# 要在它之前截断,否则 source 一次就把命令跑了。按内容找,不写死行号。
_CUT = 'case "$ACTION" in'
HINT = '代码比跑着的进程新'


class CodeStaleCase(unittest.TestCase):
    """一个假 ROOT:kb/ 下几个 .py、kb/static/ 下几个 js/css、一个 pidfile。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='kbstale-'))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / 'kb' / 'feeds').mkdir(parents=True)
        (self.dir / 'kb' / 'static' / 'js').mkdir(parents=True)
        (self.dir / '.run').mkdir()
        (self.dir / 'kb' / 'config.py').write_text('x = 1\n')
        (self.dir / 'kb' / 'feeds' / 'sync.py').write_text('y = 2\n')
        (self.dir / 'kb' / 'static' / 'js' / 'core.js').write_text('// js\n')
        (self.dir / 'kb' / 'static' / 'base.css').write_text('/* css */\n')
        self.pid = self.dir / '.run' / 'web-8787.pid'
        self.pid.write_text('4242')
        shutil.copy2(KB_SH, self.dir / 'kb.sh')
        self.funcs = self.dir / 'funcs.sh'
        text = (KB_SH).read_text()
        self.assertIn(_CUT, text)
        self.funcs.write_text(text.split(_CUT)[0])
        # 基准:代码都停在 t=500,进程起于 t=1000 —— 也就是"刚重启过"的常态,
        # 默认安静。要触发就把某个文件推到 t=2000。时间用 utime 摆死,不睡。
        for f in self.dir.rglob('*'):
            if f.is_file():
                self.touch(f, 500)
        self.touch(self.pid, 1000)

    def touch(self, path: Path, when: float):
        os.utime(path, (when, when))

    def run_sh(self, body: str) -> str:
        """跑一段 bash:source kb.sh 的函数段(真代码),再执行 body。返回 stderr。"""
        script = (
            'set -- status\n'
            f'. {self.funcs}\n'
            'set +e\n'
            f'ROOT={self.dir}\n'
            f'WEB_PID={self.pid}\n'
            f'{body}\n'
        )
        out = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                             timeout=60, cwd=self.dir)
        return out.stderr


class HintFiresTests(CodeStaleCase):
    def test_a_py_newer_than_the_process_is_reported(self):
        self.touch(self.dir / 'kb' / 'feeds' / 'sync.py', 2000)
        err = self.run_sh('code_stale')
        self.assertIn(HINT, err, f'代码换过了却一声不响:\n{err}')

    def test_the_hint_names_the_next_step(self):
        """只说"旧了"没用 —— 人要知道下一步敲什么。"""
        self.touch(self.dir / 'kb' / 'config.py', 2000)
        err = self.run_sh('code_stale')
        self.assertIn('restart', err, f'提示里没有可执行的下一步:\n{err}')

    def test_a_nested_module_counts_too(self):
        """`kb.feeds.sync` 正是这次出事的那个,它在子目录里。"""
        deep = self.dir / 'kb' / 'feeds' / 'x_channels.py'
        deep.write_text('z = 3\n')
        self.touch(deep, 2000)
        err = self.run_sh('code_stale')
        self.assertIn(HINT, err, f'子目录里的模块没被算进来:\n{err}')


class HintStaysQuietTests(CodeStaleCase):
    def test_right_after_a_restart_nothing_is_said(self):
        self.touch(self.dir / 'kb' / 'feeds' / 'sync.py', 2000)
        self.touch(self.pid, 3000)          # 重启:进程比代码新
        err = self.run_sh('code_stale')
        self.assertNotIn(HINT, err, f'刚重启就催重启:\n{err}')

    def test_touching_static_js_and_css_says_nothing(self):
        """反过来钉一边:`kb/static/` 每次请求从磁盘读(免重启),算进来就是
        每改一行样式催一次 —— 那种提示两天就没人看了。"""
        self.touch(self.dir / 'kb' / 'static' / 'js' / 'core.js', 2000)
        self.touch(self.dir / 'kb' / 'static' / 'base.css', 2000)
        err = self.run_sh('code_stale')
        self.assertNotIn(HINT, err, f'改样式也催重启:\n{err}')

    def test_without_a_pidfile_nothing_is_said(self):
        """服务没跑就没有"端着旧模块"这回事。

        如实记一笔:今天这道守卫是**冗余带** —— `find -newer <不存在的文件>` 在
        系统 find 和 bfs 上都是报错 + 输出空,于是后面那句判断自己也会安静。留着
        它的理由是构造上的:"服务没跑"该由我们自己回答,不该押在某个 find 实现
        对缺失参照物的处理上。
        """
        self.touch(self.dir / 'kb' / 'config.py', 2000)
        self.pid.unlink()
        err = self.run_sh('code_stale')
        self.assertNotIn(HINT, err, f'服务都没跑还催重启:\n{err}')

    def test_it_never_breaks_the_command_it_is_attached_to(self):
        """提示而不代劳:它挂在 `web_start` 的返回路径上,返回非 0 会把整条命令带掉。"""
        self.touch(self.dir / 'kb' / 'config.py', 2000)
        err = self.run_sh('code_stale; echo "rc=$?" >&2')
        self.assertIn('rc=0', err, f'它挡住了调用方:\n{err}')


class WiredUpTests(CodeStaleCase):
    """光有函数没人叫它等于没有。这两条真跑那两处的代码,只把"进程在不在跑"替掉。"""

    def wired(self, call: str) -> str:
        self.touch(self.dir / 'kb' / 'feeds' / 'sync.py', 2000)
        return self.run_sh(
            # service_pids 要 lsof 真端口、比对进程名;这里只需要它说"有一个在跑"。
            'service_pids() { echo 4242; }\n'
            # graph_stale 会去跑采集器那个 python(假 ROOT 里没有),它自己 `|| true`
            # 兜着;换成空操作是为了让 stderr 只剩我们要看的东西。
            'graph_stale() { :; }\n'
            f'{call}'
        )

    def test_web_start_says_it_when_the_service_is_already_up(self):
        """`sync` / `collect` / `export` / `index` / `extract` 全都先过 `web_start`
        的这条"已在"分支 —— 这个 bug 发作的那条路正是它。"""
        err = self.wired('web_start')
        self.assertIn(HINT, err, f'web_start 没说:\n{err}')

    def test_kb_sh_status_says_it(self):
        """`./kb.sh status` 是日常两条命令之一,一屏看全就该看得见这件事。"""
        err = self.wired('web_status >/dev/null')
        self.assertIn(HINT, err, f'web_status 没说:\n{err}')


if __name__ == '__main__':
    unittest.main()
