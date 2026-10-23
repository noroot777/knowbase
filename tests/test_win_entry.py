# -*- coding: utf-8 -*-
"""Windows 操作入口(`kb.ps1` 那一套)的结构回归。

`kb.ps1` 是 `kb.sh` 的对等物:同一组命令、同一套安全语义,只是换成 PowerShell。
**这一份钉的全是「坏了也不像坏了」的那几条** —— 它们的共同点是开发机(macOS)上
一个字都看不出来,而 Windows 上的症状又指不到真因:

1. **UTF-8 BOM**。PowerShell 5.1(Windows 10/11 自带的那个)读没有 BOM 的 .ps1
   时按**系统 ANSI 代码页**解码。于是整份脚本里的中文提示全变成乱码 —— 连
   `错误:找不到 uv` 这种话都读不出来,而脚本本身照常执行。谁用编辑器重存一次
   文件就可能把 BOM 丢掉,`git diff` 里也看不见。
2. **双引号串里误生效的反引号**。`@"…"@` 会插值,反引号在里头是转义符:照 markdown
   习惯写 `` `accounts discover `` 的话,PowerShell 把 `` `a `` 读成 BEL ——
   打出来是「ccounts discover」(**少一个字母**)外加一声响,解析期一个警告都没有。
   实测就是这么进去的。合法的只有 `` `$ ``(故意转义,让 $env:… 原样印出来)。
3. **命令面不许单边漂移**。`kb.sh` 加了一条命令而 `kb.ps1` 没跟,Windows 上的人
   得到的是「不认识的命令」,而文档和同事都说有这条。两边的 dispatch 现算出来对比。
4. **dot-source 的文件得在**。kb.ps1 按 700 行上限拆出了四份,少一份则一加载就炸,
   而报出来的是一句找不到某个函数 —— 指不到「少拷/少提交了一个文件」。
   (同一个形态在 bash 那边的留痕见 `tests/kb_sh_fixture.py` 的 docstring。)
5. **700 行上限**(全局约定),顺手钉住。

前三条里第 2 条要 PowerShell 的 AST 才判得准,所以它在没有 powershell 的机器上
显式 skip;其余几条是纯字节/纯文本检查,**在哪台机器上都跑** —— 这很要紧,因为
日常提交门禁是在 macOS 上过的。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 这一套的全部 .ps1(仓库内相对路径)。新拆一份出来要加进这里 —— 漏登记的后果
#: 是它的 BOM 和转义没人看着,而那两样坏起来都不像坏了。
PS_FILES = ('kb.ps1', 'scripts/win_py.ps1', 'scripts/win_proc.ps1',
            'scripts/win_env.ps1', 'scripts/win_stale.ps1', 'collector/setup.ps1')

BOM = b'\xef\xbb\xbf'
LINE_LIMIT = 700

#: kb.sh 的 `case "$ACTION" in` 里那些分支标签。
_SH_CASE = re.compile(r'^\s*((?:-?-?[a-z]+\|)*-?-?[a-z]+)\)\s', re.M)
#: kb.ps1 的 `switch ($Action)` 里那些 'xxx' { … }
_PS1_CASE = re.compile(r"^\s{4}'([^']+)'\s*\{", re.M)
#: kb.ps1 顶层的 `. (Join-Path $script:Root 'scripts\xxx.ps1')`
_PS1_SOURCE = re.compile(r"^\.\s+\(Join-Path\s+\$script:Root\s+'([^']+)'\)", re.M)


def _sh_actions() -> set[str]:
    """`kb.sh` 分发的命令名。只取最后那个 `case` 块(分发在文件末尾)。"""
    src = (REPO / 'kb.sh').read_text(encoding='utf-8')
    tail = src[src.rindex('case "$ACTION" in'):]
    out: set[str] = set()
    for label in _SH_CASE.findall(tail):
        out |= set(label.split('|'))
    return out


def _ps1_actions() -> set[str]:
    """`kb.ps1` 分发的命令名(顶层 switch 的 case,缩进 4 格那一层)。"""
    src = (REPO / 'kb.ps1').read_text(encoding='utf-8')
    tail = src[src.rindex('switch ($Action)'):]
    return set(_PS1_CASE.findall(tail))


class EncodingTest(unittest.TestCase):
    """没有 BOM 的 .ps1 在 PowerShell 5.1 上按 ANSI 解码:中文提示全成乱码。"""

    def test_all_ps1_have_utf8_bom(self) -> None:
        for rel in PS_FILES:
            with self.subTest(rel):
                head = (REPO / rel).read_bytes()[:3]
                self.assertEqual(head, BOM,
                                 f'{rel} 少了 UTF-8 BOM —— PowerShell 5.1 会按系统'
                                 f'代码页解码,里面每一句中文提示都变成乱码,'
                                 f'而脚本照常执行')

    def test_cmd_wrapper_stays_ascii(self) -> None:
        """`kb.cmd` 反过来:**不许有 BOM、不许有中文**。

        cmd.exe 按活动代码页读 .bat/.cmd,BOM 的三个字节会被当成命令的一部分
        (`'' 不是内部或外部命令`),中文注释在非中文代码页上同样是乱码。
        所以这一份只用 ASCII —— 它的全部职责就是把参数转给 kb.ps1。
        """
        raw = (REPO / 'kb.cmd').read_bytes()
        self.assertNotEqual(raw[:3], BOM, 'kb.cmd 不许有 BOM:cmd.exe 会把它当命令')
        try:
            raw.decode('ascii')
        except UnicodeDecodeError as exc:
            self.fail(f'kb.cmd 里有非 ASCII 字符(偏移 {exc.start}),'
                      f'在非中文代码页的 cmd.exe 上会是乱码')


class LineLimitTest(unittest.TestCase):
    """700 行上限(全局约定)。超了就按职责再拆一份,别往下堆。"""

    def test_under_limit(self) -> None:
        for rel in PS_FILES + ('scripts/win_daemon.py', 'kb/filelock.py'):
            with self.subTest(rel):
                n = len((REPO / rel).read_text(encoding='utf-8').splitlines())
                self.assertLessEqual(n, LINE_LIMIT, f'{rel} 有 {n} 行,超过 {LINE_LIMIT}')


class SourcedFilesTest(unittest.TestCase):
    """kb.ps1 dot-source 的那几份必须在:少一份则一加载就炸,而话指不到原因。"""

    def test_sourced_files_exist(self) -> None:
        src = (REPO / 'kb.ps1').read_text(encoding='utf-8')
        rels = _PS1_SOURCE.findall(src)
        self.assertTrue(rels, '一条 dot-source 都没认出来 —— 多半是写法变了,'
                              '那这条用例已经不起作用了,先修正则')
        for rel in rels:
            with self.subTest(rel):
                path = REPO / rel.replace('\\', '/')
                self.assertTrue(path.is_file(), f'kb.ps1 source 了 {rel},但文件不在')

    def test_sourced_files_are_registered(self) -> None:
        """source 进来的每一份都得在 PS_FILES 里 —— 否则它的 BOM 没人看着。"""
        src = (REPO / 'kb.ps1').read_text(encoding='utf-8')
        known = {r.replace('\\', '/') for r in PS_FILES}
        for rel in _PS1_SOURCE.findall(src):
            with self.subTest(rel):
                self.assertIn(rel.replace('\\', '/'), known,
                              f'{rel} 没登记进本文件的 PS_FILES')


class CommandParityTest(unittest.TestCase):
    """两边的命令面一一对应 —— 单边加命令的后果是「文档说有、Windows 上说没有」。"""

    def test_same_actions(self) -> None:
        sh, ps1 = _sh_actions(), _ps1_actions()
        self.assertTrue(len(sh) > 10, f'kb.sh 只认出 {len(sh)} 条命令,正则该修了')
        missing = sorted(sh - ps1)
        extra = sorted(ps1 - sh)
        self.assertEqual(missing, [], f'kb.sh 有而 kb.ps1 没有:{missing}')
        self.assertEqual(extra, [], f'kb.ps1 有而 kb.sh 没有:{extra}')


# PowerShell 的 AST 扫一遍:双引号串(含 here-string)里**误生效**的转义。
#
# 判据按串的种类分两档,因为这两处写的本来就是两种东西:
#
# - **here-string**(`@"…"@`)装的全是给人看的多行文案:用法、错误提示。那里
#   **一个转义都不需要** —— 要换行直接按回车。所以只许 `$`(故意转义,让 $env:…
#   原样印出来)和反引号本身,别的一概算误生效。照 markdown 习惯写
#   `accounts discover` 就是这么被 BEL 吃掉一个字母的(实测:打出来是
#   「ccounts discover」外加一声响,解析期毫无提示)。
# - **单行双引号串**里 `n` / `t` 这类是正常写法(`-join` 拼换行就得用),放行;
#   但 a / b / v / f 这几个控制字符在这个项目里没有任何正当用途,照旧算误生效
#   —— 它们正是 markdown 反引号最容易撞上的那几个开头字母。
#
# **为什么要借 PowerShell 的 AST 而不是自己用正则切**:要先知道哪一段是双引号串、
# 哪一段是注释。注释里的反引号是正常的 markdown 写法(这几份文件的注释里有上百个),
# 文本正则分不开两者,只会得到一百条假红。
_SCAN = r'''
$ErrorActionPreference = 'Stop'
# here-string 里放行的:只有这两个
$HereOk = @('$', '`')
# 单行双引号串里额外放行的:真的会用到的那几个转义
$LineOk = @('$', '`', 'n', 't', 'r', '0')
$hits = @()
foreach ($f in $args) {
  $ast = [System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$null)
  $strs = $ast.FindAll({ param($n)
      $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
      $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true)
  foreach ($s in $strs) {
    $kind = $s.StringConstantType.ToString()
    if ($kind -notlike 'DoubleQuoted*') { continue }
    $ok = if ($kind -like '*Here*') { $HereOk } else { $LineOk }
    $raw = $s.Extent.Text
    if ($raw -notmatch '`') { continue }
    $why = @()
    for ($i = 0; $i -lt $raw.Length - 1; $i++) {
      if ($raw[$i] -eq '`') {
        $next = [string]$raw[$i + 1]
        if ($ok -notcontains $next) { $why += ('backtick-' + $next) }
        $i++        # 成对吃掉，别把转义过的反引号数成两处
      }
    }
    if ($why.Count) {
      $hits += [pscustomobject]@{ file = (Split-Path $f -Leaf)
                                  line = $s.Extent.StartLineNumber
                                  kind = $kind
                                  why  = (($why | Select-Object -Unique) -join ',') }
    }
  }
}
ConvertTo-Json -InputObject @($hits) -Compress
'''


def _powershell() -> str | None:
    for exe in ('pwsh', 'powershell'):
        found = shutil.which(exe)
        if found:
            return found
    return None


def _project_python() -> str | None:
    """项目 venv 里的 python。`NativeCallTest` 要一个**真会往 stderr 说话**的
    原生程序,拿它当被试最省事(而且这几条用例只在 Windows 上有意义)。"""
    exe = REPO / '.venv' / 'Scripts' / 'python.exe'
    return str(exe) if exe.is_file() else None


class _NeedsPowerShell(unittest.TestCase):
    """要真跑 PowerShell 的那几条共用这道闸门。"""

    def setUp(self) -> None:
        self.ps = _powershell()
        if not self.ps:
            self.skipTest('这台机器上没有 powershell / pwsh —— 这几条要 PowerShell '
                          '自己跑一遍才算验上(AST 的字符串边界、以及那份环境到底'
                          '拼出了什么)。Windows 上跑一次 .\\kb.ps1 test 就覆盖到了。')

    def run_ps(self, script: str, *args: str) -> subprocess.CompletedProcess:
        """脚本落成临时 .ps1 用 `-File` 跑。

        **不能走 `-Command <脚本> <参数>`**:-Command 把它后面的全部参数用空格拼成
        **一条命令**再求值。实测扫描脚本和六个文件路径粘在一起,第一个路径被当成
        命令**真的执行了一次 `kb.ps1`**(报的是 kb.ps1 自己那句「不认识的命令
        …\\win_py.ps1」)—— 那条命令要是恰好是 `stop`,它就真把服务停了。
        只有 `-File` 才把余下的参数填进 `$args`。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'probe.ps1'
            path.write_text(script, encoding='utf-8-sig')
            return subprocess.run([self.ps, '-NoProfile', '-NonInteractive',
                                   '-ExecutionPolicy', 'Bypass',
                                   '-File', str(path), *args],
                                  capture_output=True, text=True, timeout=180)


# `Build-WebEnv` 到底拼出了什么。只 dot-source `win_env.ps1`(它不依赖 kb.ps1 里的
# 任何东西),所以这一条验的是那一份自己的名单,不是整个入口。
_ENV_PROBE = r'''
param([string]$WinEnv)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. $WinEnv
Build-WebEnv | ForEach-Object { $_ }
'''


class WebEnvTest(_NeedsPowerShell):
    """服务进程那份环境:出口这一族必须进去,`KB_DATA_DIR` 必须进不去。

    两头的后果都不可见、都很贵(口径同 `tests/test_egress_env.py` 给 bash 那边钉的):
    - `KB_FEED_DOH` 不透传 → 服务里永远读不到开关,可信解析那一层**等于没做**,
      而面板上「可信解析:开」照样打出来。
    - `KB_DATA_DIR` 透传进去 → **静默换掉服务读的那个库**。显式隔离过数据目录的人
      看到的是一个空的知识库,零报错。
    """

    def _built(self, **env) -> list[str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith('KB_')}
        base.update(env)
        win_env = str(REPO / 'scripts' / 'win_env.ps1')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'probe.ps1'
            path.write_text(_ENV_PROBE, encoding='utf-8-sig')
            out = subprocess.run([self.ps, '-NoProfile', '-NonInteractive',
                                  '-ExecutionPolicy', 'Bypass', '-File', str(path),
                                  win_env],
                                 capture_output=True, text=True, env=base, timeout=180)
        self.assertEqual(out.returncode, 0, f'Build-WebEnv 自己炸了:{out.stderr}')
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]

    def test_resolver_switch_is_passed_through(self) -> None:
        built = self._built(KB_FEED_DOH='https://doh.example/q')
        self.assertIn('KB_FEED_DOH=https://doh.example/q', built,
                      '不透传它,可信解析那一层在服务里就永远读不到开关 —— 等于没做')

    def test_proxy_vars_are_passed_through(self) -> None:
        built = self._built(HTTPS_PROXY='http://127.0.0.1:7890')
        self.assertIn('HTTPS_PROXY=http://127.0.0.1:7890', built)

    def test_data_dir_is_not_passed_through(self) -> None:
        """反过来钉一边:`KB_DATA_DIR` 不许跟进去。"""
        built = self._built(KB_DATA_DIR='C:\\somewhere\\else',
                            KB_FEED_DOH='https://doh.example/q')
        leaked = [l for l in built if l.startswith('KB_DATA_DIR=')]
        self.assertEqual(leaked, [], 'KB_DATA_DIR 跟进服务进程会静默换掉它读的那个库')

    def test_empty_values_are_not_passed_through(self) -> None:
        """空壳变量不透传 —— 排查时「到底配没配」不该多一层噪声。"""
        built = self._built(KB_FEED_DOH='')
        leaked = [l for l in built if l.startswith('KB_FEED_DOH')]
        self.assertEqual(leaked, [], '空值透传进去了')

    def test_login_state_vars_do_not_leak(self) -> None:
        """白名单的正事:外层那些登录态变量进不来(`env -i` 要防的就是这个)。

        实测过的形态在 CLAUDE.md 里:继承嵌套 claude 的环境变量会让服务
        「Not logged in」。
        """
        built = self._built(ANTHROPIC_API_KEY='sk-should-not-leak',
                            CLAUDE_CODE_SOMETHING='x')
        leaked = [l for l in built if l.startswith(('ANTHROPIC_', 'CLAUDE_'))]
        self.assertEqual(leaked, [], f'登录态变量漏进了服务进程:{leaked}')

    def test_essentials_are_kept(self) -> None:
        """反过来的另一边:白名单不能收得太紧,否则子进程压根起不来。"""
        built = self._built()
        names = {l.split('=', 1)[0] for l in built}
        for need in ('SystemRoot', 'PATH', 'TEMP', 'USERPROFILE', 'LOCALAPPDATA'):
            with self.subTest(need):
                self.assertIn(need, names,
                              f'{need} 没进去 —— CreateProcess/CRT 找不到系统 DLL、'
                              f'python 落不了临时文件、uv 找不到缓存')


    def test_utf8_mode_is_forced_into_the_service(self) -> None:
        """`PYTHONUTF8=1` 必须进服务进程 —— 不开的话 Windows 上是**静默损坏**。

        Python 的 `open()` 不带 `encoding=` 时跟着本地代码页走(这台机器 cp936),
        而这个项目的事实来源全是 UTF-8。于是服务用 GBK 读写 UTF-8 的正文:读进来
        乱码、写回去把原文**替换成**乱码,一路零报错。实测同一批 179 条回归,
        不开 171 条错、开了全绿。
        """
        built = self._built()
        self.assertIn('PYTHONUTF8=1', built,
                      'UTF-8 Mode 没进服务进程 —— vault 里的正文会被按 GBK 读写')

    def test_utf8_mode_overrides_an_inherited_zero(self) -> None:
        """反过来钉一边:环境里留着 `PYTHONUTF8=0` 也要被覆盖掉。

        这不是用户配置、是平台校正 —— 让环境投票的结果就是上面那个静默损坏。
        """
        built = self._built(PYTHONUTF8='0')
        self.assertIn('PYTHONUTF8=1', built)
        self.assertNotIn('PYTHONUTF8=0', built,
                         '继承来的 PYTHONUTF8=0 没被覆盖')

    def test_forced_env_has_one_definition(self) -> None:
        """名单只有一处:`Set-ForcedEnv` 和 `Build-WebEnv` 都从 `$script:ForcedEnv` 取。

        两边各写一遍的话,加一个变量时漏掉一边 —— 而漏掉的那一边(服务进程)
        坏起来是静默的。
        """
        src = (REPO / 'scripts' / 'win_env.ps1').read_text(encoding='utf-8-sig')
        self.assertEqual(len(re.findall(r'\$script:ForcedEnv\s*=', src)), 1,
                         'ForcedEnv 被赋值了不止一次')
        for consumer in ('Set-ForcedEnv', 'Build-WebEnv'):
            with self.subTest(consumer):
                body = src.split(f'function {consumer}', 1)[1]
                self.assertIn('ForcedEnv', body.split('function ')[0],
                              f'{consumer} 没从 ForcedEnv 取,多半是又手写了一遍')


class EscapeTest(_NeedsPowerShell):
    """双引号串里不许有误生效的转义(here-string 比单行串更严,见 _SCAN 上的说明)。"""

    def test_no_misfiring_escapes(self) -> None:
        args = [str(REPO / rel.replace('/', '\\')) for rel in PS_FILES]
        out = self.run_ps(_SCAN, *args)
        self.assertEqual(out.returncode, 0, f'扫描本身失败了:{out.stderr}')
        hits = json.loads(out.stdout.strip() or '[]')
        pretty = '; '.join(f"{h['file']}:{h['line']}({h['why']})" for h in hits)
        self.assertEqual(hits, [],
                         '双引号串里有误生效的反引号转义 —— 打出来会少字符、'
                         f'还可能响一声,而解析期毫无提示:{pretty}')


#: 原生程序往 stderr 写一行、退出码 0 —— uv 建 venv 时那句 `Using CPython 3.11.17`
#: 就是这个形状。**不带引号**:PowerShell 5.1 传原生参数时会吃掉嵌入的双引号,
#: 探针自己先踩进那个坑的话,红的原因就不是我们要验的那一条了。
_STDERR_RC0 = 'import sys; sys.stderr.write(chr(88)); sys.exit(0)'

#: 在 `$ErrorActionPreference = 'Stop'` 下跑一句「会往 stderr 说话」的原生命令。
#: 判据是**它有没有抛**,不是输出长什么样。
_NATIVE_PROBE = r'''
param([string]$WinPy, [string]$Py)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'          # kb.ps1 顶上就是这个,照搬
$script:Root = (Get-Location).Path
function Die { param([string]$m) throw $m }
. $WinPy

$code = 'import sys; sys.stderr.write(chr(88)); sys.exit(0)'
try {
    $r = Invoke-Native $Py @('-c', $code)
    "NATIVE_OK rc=$($r.Code)"
} catch {
    "NATIVE_THROW $($_.FullyQualifiedErrorId)"
}
try {
    $null = Invoke-NativeLive $Py @('-c', $code)
    "LIVE_OK rc=$LASTEXITCODE"
} catch {
    "LIVE_THROW $($_.FullyQualifiedErrorId)"
}
# 反过来钉一边:**降级不许外泄**。出了那两个函数,调用方的 Stop 还得管事 ——
# 否则这道修复会顺手把整份脚本的报错纪律关掉,那比原来的 bug 更难查。
try {
    $null = & $Py '-c' $code 2>&1
    'LEAK'
} catch {
    'CALLER_STILL_STRICT'
}
'''


class NativeCallTest(_NeedsPowerShell):
    """原生程序往 stderr 说一句话,不许把入口掀翻。

    **这条是真进去过的,而且是「`.\\kb.ps1 start` 直接用不了」那一级。**
    `kb.ps1` 顶上设 `$ErrorActionPreference = 'Stop'`(那是有意的:cmdlet 出错就
    停,免得像 `status` 那样打一行红再接着给出一个错答案)。但 PowerShell 一看到
    **原生程序**往 stderr 写了字,就把那几行包成 `NativeCommandError` 错误记录,
    而在 `Stop` 下那是**终止性**的。

    于是 uv 建 venv 时打的那句进度 `Using CPython 3.11.17` —— 一句**不是错误的
    话** —— 让 `start` 当场死掉,报出来的是一段 PowerShell 内部机制的话,跟知识库
    毫无关系。更坏的是它**留下半个 `.venv`**(只有 `CACHEDIR.TAG`),之后 uv 既
    拒绝用它也不会自己修,于是**后面每一次重试都失败,而且换了个理由** ——
    人完全看不出这几次失败是同一个根。

    **`2>$null` 挡不住**(反直觉,所以这里连它一起钉):实测 `Stop` 下 `2>&1` 和
    `2>$null` 都抛。所以口径是统一走 `Invoke-Native` / `Invoke-NativeLive`,它们
    把 `$ErrorActionPreference` 降回 `Continue`、**只在自己的作用域里**。
    """

    def setUp(self) -> None:
        super().setUp()
        self.py = _project_python()
        if not self.py:
            self.skipTest('找不到项目 python(.venv) —— 这一条要一个真能往 stderr '
                          '写字的原生程序才验得了')

    def _run(self):
        out = self.run_ps(_NATIVE_PROBE,
                          str(REPO / 'scripts' / 'win_py.ps1'), self.py)
        self.assertEqual(out.returncode, 0, f'探针本身失败了:{out.stderr}')
        return out.stdout

    def test_capturing_helper_survives_stderr(self) -> None:
        text = self._run()
        self.assertIn('NATIVE_OK rc=0', text,
                      'Invoke-Native 被 stderr 掀翻了 —— `.\\kb.ps1 start` 会死在 '
                      'uv 的进度信息上,还会留下半个 .venv 让后续重试全失败')

    def test_streaming_helper_survives_stderr(self) -> None:
        text = self._run()
        self.assertIn('LIVE_OK rc=0', text,
                      'Invoke-NativeLive 被 stderr 掀翻了 —— unittest 把全部输出都'
                      '写 stderr,`.\\kb.ps1 test` 会一开跑就炸')

    def test_the_relaxation_does_not_leak_to_the_caller(self) -> None:
        """反过来钉一边:降级只在函数内,出来之后 `Stop` 照旧管事。"""
        text = self._run()
        self.assertIn('CALLER_STILL_STRICT', text,
                      '两个 helper 把调用方的 $ErrorActionPreference 也改了 —— '
                      '那等于顺手关掉整份脚本的报错纪律')


class NoRawRedirectTest(unittest.TestCase):
    """别处不许再写 `2>&1` / `2>$null` 直接套在原生命令上。

    这是上面那条的**结构版**,不需要 PowerShell 就能跑(日常门禁在 macOS 上过)。
    判据只盯「`&` 调用 + 重定向」这一种写法:套在 **PowerShell 函数**调用上的
    `2>$null` 是另一件事(那是压这个函数的错误流),照旧放行。
    """

    #: `& $something … 2>&1` / `… 2>$null` —— 原生调用自带重定向。
    _BAD = re.compile(r'^\s*(?:\$\w+\s*=\s*)?&\s*[\$(].*?2>(?:&1|\$null)', re.M)

    #: **唯一允许写重定向的地方就是 `Invoke-Native` 自己的函数体** —— 口径只有
    #: 一份,那一处就是这一份。整段挖掉再扫,所以这里不需要维护行号。
    _OWNER = re.compile(r'(?ms)^function Invoke-Native \{.*?^\}\n')

    def test_no_native_call_carries_its_own_redirect(self) -> None:
        offenders = []
        for rel in PS_FILES:
            src = (REPO / rel).read_text(encoding='utf-8-sig')
            # 挖掉时用等长的空行替换,行号才还对得上
            def _blank(m):
                return '\n' * m.group(0).count('\n')
            src = self._OWNER.sub(_blank, src)
            for m in self._BAD.finditer(src):
                line = src[:m.start()].count('\n') + 1
                offenders.append(f'{rel}:{line}')
        self.assertEqual(offenders, [],
                         '原生调用自带 2>&1 / 2>$null:在 $ErrorActionPreference='
                         "'Stop' 下,对端往 stderr 写一个字就会终止整条命令。"
                         '改走 Invoke-Native / Invoke-NativeLive')

    def test_the_owner_really_was_exempted(self) -> None:
        """反过来钉一边:那段豁免真的命中了 `Invoke-Native`。

        正则哪天对不上(函数改名、括号换行),上面那条会**悄悄变回全文扫描**然后
        永远红;或者更糟,`_BAD` 一起失效而它永远绿。所以这里确认两件事:
        豁免段落存在,且它里面确实有那两句重定向。
        """
        src = (REPO / 'scripts/win_py.ps1').read_text(encoding='utf-8-sig')
        m = self._OWNER.search(src)
        self.assertIsNotNone(m, 'Invoke-Native 的函数体没匹配上 —— 先修这条正则')
        self.assertTrue(self._BAD.search(m.group(0)),
                        'Invoke-Native 里居然没有重定向了 —— 要么实现换了写法,'
                        '要么这条豁免已经不需要了,两种都该把它删掉')


if __name__ == '__main__':
    unittest.main()
