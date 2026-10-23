# -*- coding: utf-8 -*-
"""Windows 入口的**行为**判据:问不出来算哪一档、pid 怎么核身份、参数怎么校验。

跟 `tests/test_win_entry.py` 的分工:那一份钉结构(BOM、转义、命令面、行数),
纯文本检查、哪台机器上都跑;这一份每条都要**真跑一次 PowerShell**,因为要验的
正是 PowerShell 自己的那几样行为 —— 异常分类、参数绑定、进程起跑时间。
所以在没有 powershell 的机器上整份 skip(日常门禁在 macOS 上过)。

**为什么这几条值得单独一份。** 它们钉的都是「答错了也不像答错了」:

1. **问不出来 ≠ 确定没有。** `Get-NetTCPConnection` 的失败原先一律 `catch { @() }`,
   于是 CIM 坏掉那一刻 `Get-PipelineState` 答「空闲」—— `stop` 把正跑着的同步砍在
   半路,而停机保护那三态看着好好的。这一条两头都要钉:真·无匹配必须转成「空着」
   (否则 start 一律起不来),别的失败必须留在「问不出来」。
2. **pid 是会被回收的号。** pidfile 那条路只核命令行的话,号被回收给另一份 kb-web
   实例时命令行一模一样 —— stop 杀掉别人那一份。判据是起跑时间对不对得上 pidfile
   的写入时间。
3. **取值参数要在副作用之前校验完。** `--targets` 后面什么都不给,原先起了服务、
   还发出一次**全量**同步请求(几十分钟),而人以为自己限定了范围。
4. **`f @(a,b) + $c` 不是一次调用。** PowerShell 在参数模式下把 `+` 和 `$c` 当成
   另外两个位置参数,于是用户给的链接一条都没传进去 —— 而命令照常返回 0。
5. **找解释器要把项目自己那个 venv 算上。** 少了它,PATH 上只有 Microsoft Store
   占位程序的机器上会说「找不到可用的 python 3」,而那台机器上 `uv run` 跑得好好的。
6. **根进程退干净了,后代也得扫。** uvicorn 收到 CTRL_BREAK 会从容退出(最常见的
   那条路),而它拉起的 chrome / 向量子进程不跟着走:Windows 没有进程组连坐。

每条都带反向的那一边。写法上统一:把探针脚本落成临时 `.ps1` 用 `-File` 跑
(理由同 test_win_entry 里 `run_ps` 的 docstring:`-Command` 会把参数拼进命令行,
实测**真的执行过一次 kb.ps1**),dot-source 真文件、只替掉要注入的那一处。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_win_entry import REPO, _powershell

#: 每个探针的开头:装好 kb.ps1 里那几个被 win_* 依赖的东西,再 dot-source 真文件。
#: **`Die` 换成 throw** —— 真的 `Die` 会 `exit 1`,那样探针拿不到「它拦住了哪一条」。
_PRELUDE = r'''
# **这一行必须是第一行,别挪走、别删。** 探针的判据全是中文(`Die` 的话、出口那几
# 行),而 PowerShell 写 stdout 用的是 `[Console]::OutputEncoding` —— 这台机器上是
# 936。于是 Python 那边按 UTF-8 解码时撞上 0xb2 一类字节,而**失败的样子不是报错**:
# Windows 上 `Popen._communicate` 的读管子是两个线程,线程里抛出的
# UnicodeDecodeError 被 threading 的 excepthook 打掉,`communicate()` 把那一股原样
# 返回 `None` —— 于是探针拿到 `rc=0` + `stdout=None`,红在三帧之外的一句
# TypeError 上,跟它要验的事情毫无关系(本轮真踩了一次:4 条参数校验探针连一次
# REFUSED 都没比对过,却一直显示「红了」)。病根同 `Invoke-Api` 和
# `Show-EgressLines`:不写 encoding 就跟着代码页走。
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Root = $env:KB_PROBE_ROOT
$script:Collector = Join-Path $script:Root 'collector'
$script:Py = Join-Path $script:Collector '.venv\Scripts\python.exe'
$script:RunDir = Join-Path $script:Root '.run'
$script:WinDaemon = Join-Path $script:Root 'scripts\win_daemon.py'
$script:ProcTable = $null
$script:Port = 8787
$script:WebPat = 'kb-web'
function Write-Note { param([string]$Text) [Console]::Error.WriteLine($Text) }
function Die { param([string]$Text) throw "DIE:$Text" }
function Get-Setting {
    param([string]$Name)
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($v) { return $v }
    return ''
}
. (Join-Path $script:Root 'scripts\win_uv.ps1')
. (Join-Path $script:Root 'scripts\win_py.ps1')
. (Join-Path $script:Root 'scripts\win_args.ps1')
. (Join-Path $script:Root 'scripts\win_proc.ps1')
. (Join-Path $script:Root 'scripts\win_env.ps1')
'''


class _Probe(unittest.TestCase):
    """探针的公共壳子。"""

    maxDiff = None

    def setUp(self) -> None:
        self.ps = _powershell()
        if not self.ps:
            self.skipTest('这台机器上没有 powershell / pwsh —— 这一份验的就是 '
                          'PowerShell 自己的行为(异常分类、参数绑定、进程起跑时间),'
                          '换个语言复述一遍证明不了任何事。Windows 上跑一次 '
                          '.\\kb.ps1 test 就覆盖到了。')

    def ps_run(self, body: str, timeout: int = 180,
               extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """`_PRELUDE + body` 落成临时 .ps1 跑一遍,返回 CompletedProcess。

        **环境是当前环境的副本,不是一份白名单。** 探针里有要真跑项目解释器的
        (出口那几行),而 venv 里的 python 在一个被剥干净的环境里**压根起不来**
        —— 实测连 `$LASTEXITCODE` 都不设,于是那一条红在一个跟它要验的事情毫无
        关系的地方。要隔离的那几项逐个显式覆盖。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'probe.ps1'
            path.write_text(_PRELUDE + body, encoding='utf-8-sig')
            env = dict(os.environ)
            env.update({'KB_PROBE_ROOT': str(REPO), 'TEMP': tmp, 'TMP': tmp,
                        # 本机真设着的话会把出口那几条探针带偏
                        'KB_FEED_DOH': '', 'KB_FEED_DOH_SECONDS': ''})
            env.update(extra_env or {})
            return subprocess.run([self.ps, '-NoProfile', '-NonInteractive',
                                   '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                                  capture_output=True, timeout=timeout, env=env,
                                  # 显式 UTF-8(配 _PRELUDE 开头那一行);`replace`
                                  # 是第二道 —— 真漏进一个野字节时,红应该落在
                                  # 判据那一句上(看见乱码),不是落在一股 None 上。
                                  encoding='utf-8', errors='replace')

    def ps_lines(self, body: str, timeout: int = 180,
                 extra_env: dict | None = None) -> list[str]:
        r = self.ps_run(body, timeout=timeout, extra_env=extra_env)
        # 上面两道都失手时就在这儿停下:`None` 往下走会变成一句指不到任何地方的
        # TypeError,而那条红看着跟判据有关、其实是解码塌了。
        self.assertIsNotNone(r.stdout, '探针的 stdout 解不出来(多半是编码那条)')
        self.assertIsNotNone(r.stderr, '探针的 stderr 解不出来(多半是编码那条)')
        self.assertEqual(r.returncode, 0,
                         f'探针本身炸了,这一条就什么都没验到:\n{r.stdout}\n{r.stderr}')
        # 这几行是 Write-Note 打的(stderr),所以两股都要收
        return [x.strip() for x in (r.stdout + '\n' + r.stderr).splitlines() if x.strip()]


class PortQueryIsThreeStateTest(_Probe):
    """端口那一问:**无法确定**不许塌成「确定没有」。"""

    #: 注入点只有一处 —— 用一个同名函数把那个 cmdlet 盖掉(函数优先于 cmdlet)。
    _FAIL = '''
function Get-NetTCPConnection { throw [UnauthorizedAccessException]::new('拒绝访问') }
"state=$(Get-PortState 8787)"
$q = Get-PortOwners 8787
"known=$($q.Known) owners=$(@($q.Owners).Count)"
'''

    _NOMATCH = '''
function Get-NetTCPConnection {
    $e = [Management.Automation.ErrorRecord]::new(
            [Exception]::new('没有匹配'), 'CmdletizationQuery_NotFound,Get-NetTCPConnection',
            [Management.Automation.ErrorCategory]::ObjectNotFound, $null)
    throw $e
}
"state=$(Get-PortState 8787)"
'''

    def test_a_failed_query_is_unknown(self) -> None:
        """查询失败 → unknown。塌成 free 的后果是 stop 砍掉正在跑的同步。"""
        out = self.ps_lines(self._FAIL)
        self.assertIn('state=unknown', out)
        self.assertIn('known=False owners=0', out)

    def test_a_real_no_match_is_free(self) -> None:
        """反过来钉一边:**真·无匹配**必须答「空着」。

        不钉这一边的话,一个「宁可全算 unknown」的实现也能让上一条绿 ——
        而那样 `start` 在任何一台机器上都起不来(端口明明空着却说问不出来)。
        """
        self.assertIn('state=free', self.ps_lines(self._NOMATCH))

    def test_pipeline_state_carries_the_unknown_up(self) -> None:
        """`Get-PipelineState` 要把 unknown 原样往上递,不许就地翻成 idle。

        这一条单独验**传导**:上面两条管适配层答得对不对,这条管 kb.ps1 里那个
        真函数有没有把它压掉。所以从 kb.ps1 里按 AST 取出真函数体执行,只替掉
        `Get-PortState`(端口适配层由上面两条管)。
        """
        body = '''
$src = Get-Content -LiteralPath (Join-Path $script:Root 'kb.ps1') -Raw
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$null, [ref]$null)
$fn = $ast.FindAll({ $args[0] -is
        [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $args[0].Name -eq 'Get-PipelineState' }, $true)
if (@($fn).Count -ne 1) { throw '没从 kb.ps1 里取到 Get-PipelineState —— 这一条已经不起作用了' }
Invoke-Expression $fn[0].Extent.Text
function Get-PortState { param([int]$P) return 'unknown' }
function Invoke-Api { throw '问不出端口时压根不该发这个请求' }
"state=$(Get-PipelineState)"
'''
        self.assertIn('state=unknown', self.ps_lines(body))


class PidFileIdentityTest(_Probe):
    """pidfile 那条路:号对不上身份就不许认领(认下来的后果是杀)。"""

    _BODY = '''
$f = Join-Path $env:TEMP 'probe.pid'
$me = $PID
Set-Content -LiteralPath $f -Value "$me" -Encoding ascii
# 这份记录「写在」一小时前:那时本进程还没起跑 —— 典型的号被回收形态
$old = (Get-Date).AddHours(-1)
(Get-Item -LiteralPath $f).LastWriteTime = $old
"recycled=$(Test-RecordedProc $me (Get-Item -LiteralPath $f).LastWriteTime)"
# 反过来:记录是现在写的,那就是真身
(Get-Item -LiteralPath $f).LastWriteTime = (Get-Date)
"genuine=$(Test-RecordedProc $me (Get-Item -LiteralPath $f).LastWriteTime)"
# 两行数字不许被接成一个号
Set-Content -LiteralPath $f -Value "123`n456" -Encoding ascii
"firstline=$(@(Read-PidFile $f).Pids -join ',')"
# 写坏成满屏数字:要返回空,不许抛 OverflowException
Set-Content -LiteralPath $f -Value ('9' * 40) -Encoding ascii
"overflow=$(@((Read-PidFile $f).Pids).Count)"
'''

    def test_identity(self) -> None:
        out = self.ps_lines(self._BODY)
        self.assertIn('recycled=False', out,
                      'pidfile 比进程起跑时间早 —— 这个号已经被回收给别人了,不许认领')
        self.assertIn('genuine=True', out,
                      '反过来钉一边:记录确实是这个进程起来之后写的,就得认 —— '
                      '不认的话服务还在启动、端口还没开时 stop 一律说「没在跑」')
        self.assertIn('firstline=123', out, '只认第一行,两行数字不许接成 123456')
        self.assertIn('overflow=0', out)

    def test_get_service_pids_actually_uses_it(self) -> None:
        """**上一条只证明那个判据自己对,不证明有人用它。**

        原先的事故就在这条接线上:`Get-ServicePids` 从 pidfile 拿到号之后只核命令行
        (另一份 kb-web 的命令行一模一样),身份那一关压根没有。所以这里走真实入口 ——
        `Port` 给 0 把端口那条路关掉,只留 pidfile 这一条。
        """
        body = '''
$f = Join-Path $env:TEMP 'wire.pid'
Set-Content -LiteralPath $f -Value "$PID" -Encoding ascii
(Get-Item -LiteralPath $f).LastWriteTime = (Get-Date).AddHours(-1)
"stale=$(@(Get-ServicePids $f 0 'powershell').Count)"
(Get-Item -LiteralPath $f).LastWriteTime = (Get-Date)
"fresh=$(@(Get-ServicePids $f 0 'powershell').Count)"
'''
        out = self.ps_lines(body)
        self.assertIn('stale=0', out,
                      '这份记录写在本进程起跑之前 —— 号被回收了,不该认领(认了就杀)')
        self.assertIn('fresh=1', out,
                      '反过来钉一边:记录是新的就得认出来,否则 stop 什么都停不掉')


class OptionValidationTest(_Probe):
    """取值参数:四种写错法都要在**副作用之前**被拦住。"""

    _KEYS = "@('--since', '--until', '--targets', '--channel')"

    def _one(self, args: str) -> str:
        body = f'''
try {{
    $o = Read-OptionMap {args} {self._KEYS}
    "OK:" + (($o.Keys | Sort-Object | ForEach-Object {{ "$_=$($o[$_])" }}) -join ' ')
}} catch {{ "REFUSED:$($_.Exception.Message)" }}
'''
        return '\n'.join(self.ps_lines(body))

    def test_missing_value_is_refused(self) -> None:
        """`--targets` 后面什么都没给。原先:起服务 + 发出一次全量同步。"""
        self.assertIn('REFUSED', self._one("@('--targets')"))

    def test_empty_value_is_refused(self) -> None:
        """`--targets ""` 同样静默变成全量 —— bash 那边这一种至今也是空转。"""
        self.assertIn('REFUSED', self._one('@(\'--targets\', \'\')'))
        self.assertIn('REFUSED', self._one('@(\'--targets\', \'   \')'))

    def test_a_following_option_is_not_a_value(self) -> None:
        """`--since --targets x`:原先把 `--targets` 当日期发走,再报「不认识 x」。

        **这一条不能只看「拦住了没有」。** 把那道闸拿掉,它照样会拦 —— 只是晚一步、
        而且指错地方:`--targets` 被当成 `--since` 的取值收下,下一轮轮到 `x`,于是
        报一句「不认识的参数 x」。`x` 没写错,写错的是 `--since` 后面那一格。所以
        判据落在**它说的是哪一处**上:必须点到 `--since`,不许是那句「不认识」。
        (实测:只断言 REFUSED 时,定点注入是绿的 —— 用例不敏感,不是注入无效。)
        """
        got = self._one("@('--since', '--targets', 'x')")
        self.assertIn('REFUSED', got)
        self.assertIn('--since', got)
        self.assertNotIn('不认识', got)

    def test_unknown_key_is_refused(self) -> None:
        self.assertIn('REFUSED', self._one("@('--nope', 'x')"))

    def test_the_good_forms_still_pass(self) -> None:
        """反过来钉一边:正常写法照旧放行,**而且取值原样**。

        不钉这一边的话,一个「一律 Die」的实现也能让上面四条全绿。
        """
        got = self._one("@('--since', '2026-01-02', '--targets', 'a,b')")
        self.assertIn('OK:', got)
        self.assertIn('--since=2026-01-02', got)
        self.assertIn('--targets=a,b', got)
        self.assertIn('OK:', self._one('@()'))


class SplatIsOneCallTest(_Probe):
    """`f @(a,b) + $c` 会静默变成三个参数 —— 这一条钉的是「括起来了没有」。"""

    def test_the_parse_hazard_is_real(self) -> None:
        """先证明这个坑真的在(不然下一条钉的是空气)。"""
        body = '''
function Probe { param([string[]]$Arg) "arg=$($Arg -join '|') rest=$($args -join '|')" }
$extra = @('x')
Probe @('p', 'q') + $extra
Probe (@('p', 'q') + $extra)
'''
        out = self.ps_lines(body)
        self.assertEqual(out[0], 'arg=p|q rest=+|System.Object[]',
                         '这个坑的形态变了,下面那条静态检查要重新想')
        self.assertEqual(out[1], 'arg=p|q|x rest=')

    def test_no_unparenthesised_splat_survives(self) -> None:
        """源码里不许再出现 `Invoke-* @(…) + $Arg` 这种写法。

        静态检查,因为真实调用点(grab)会起服务、发请求 —— 在回归里跑一次它
        本身就是事故。形态由上一条钉住。
        """
        import re
        bad = []
        for rel in ('kb.ps1', 'scripts/win_py.ps1', 'scripts/win_proc.ps1',
                    'scripts/win_web.ps1', 'scripts/win_env.ps1'):
            for n, line in enumerate((REPO / rel).read_text(encoding='utf-8-sig')
                                     .splitlines(), 1):
                if re.search(r'^\s*\S*Invoke-\S+\s+@\([^)]*\)\s*\+', line):
                    bad.append(f'{rel}:{n}: {line.strip()}')
        self.assertEqual(bad, [], '这些调用会把 + 右边当成另一个位置参数:'
                                  '整个表达式要用一对括号包起来')


class EgressLineDoesNotLeakCredentialsTest(_Probe):
    """那几行出口状态:端点里的凭证**一个字都不许进终端和日志**。

    原先是在 PowerShell 这边用一条正则把端点剪到 `scheme://host`,而那条正则保留的
    是整个 netloc —— `user:pass@` 原样留在里头。日志是落盘的
    (`runtime_logs/web.log`),于是一次 start 就把 DoH 端点上的 token 写了进去。
    现在整段交给 `kb.feeds.egress.summary()`(bash 那边也是它),脱敏在 Python 一侧
    只有一份实现。
    """

    #: 这种形状是真实存在的:NextDNS / AdGuard 那类带 profile id 或 token 的端点。
    _DOH = 'https://user:s3cret-token@doh.example.net/dns-query?key=k9'

    def test_the_secret_never_shows_up(self) -> None:
        out = '\n'.join(self.ps_lines('Show-EgressLines',
                                     extra_env={'KB_FEED_DOH': self._DOH}))
        self.assertIn('doh.example.net', out,
                      '主机名还是要打出来的 —— 这一行的用处就是「现在生效的是哪个端点」')
        for secret in ('s3cret-token', 'user:', 'key=k9', 'dns-query'):
            self.assertNotIn(secret, out, f'{secret} 不该出现在终端和日志里')


class BootPythonFindsProjectVenvTest(_Probe):
    """找解释器:项目自己那个 venv 也算,不然 PATH 上只有 Store 占位程序时就瘸了。"""

    def test_root_venv_is_a_candidate(self) -> None:
        body = '''
$script:Py = Join-Path $env:TEMP 'no-such-collector-venv\\python.exe'
$script:BootPyCache = $null
$want = Join-Path $script:Root '.venv\\Scripts\\python.exe'
if (-not (Test-Path -LiteralPath $want -PathType Leaf)) { "SKIP"; exit 0 }
$p = Get-BootPython
"exe=$($p.Exe)"
'''
        out = self.ps_lines(body)
        if 'SKIP' in out:
            self.skipTest('这个检出里没有 .venv —— 那就没有可验的东西了')
        want = str(REPO / '.venv' / 'Scripts' / 'python.exe')
        self.assertIn(f'exe={want}', out,
                      '采集器 venv 不在时要退到项目 venv,而不是直接去 PATH 上赌运气')


class StopTreeSweepsSurvivorsTest(_Probe):
    """根进程退干净了,后代照样要扫 —— 那恰恰是最常见的那条路。"""

    def test_a_child_outliving_the_root_is_killed(self) -> None:
        """注入点:把「送 CTRL_BREAK」那一步换成「根当场退出」。

        这正是 uvicorn 收到 CTRL_BREAK 之后那条路的形状(从容退出、退得很快),
        原先的实现到这里就 `return` 了,子进程留在后台 —— 向量子进程、CDP 起的
        chrome 都是这么漏出去的。
        """
        body = '''
$root = Start-Process -FilePath 'powershell.exe' -PassThru -WindowStyle Hidden `
    -ArgumentList '-NoProfile', '-NonInteractive', '-Command',
                  'Start-Process powershell.exe -ArgumentList @(''-NoProfile'',''-Command'',''Start-Sleep 120'') -WindowStyle Hidden; Start-Sleep 120'
# **不许叫 $root。** Stop-ProcTree 的参数就是 `$Root`,而 PowerShell 的变量名不分
# 大小写、查找又是按调用栈来的 —— 注入的函数里 `$root` 会拿到那个 int 参数,
# 报的是一句「找不到属性 Id」(本轮真踩了一次)。
$script:ProbeRootPid = $root.Id
$n = 0
$kid = $null
while ($n -lt 50 -and -not $kid) {
    Start-Sleep -Milliseconds 200
    $null = Get-ProcTable -Fresh
    $t = Get-ProcTable
    # **必须按名字挑,不能拿「第一个子进程」。** 隐藏窗口的那个根会带一个
    # conhost.exe 子进程,而 conhost 是跟着根一起走的 —— 挑中它的话,这一条在
    # 「不扫后代」的实现上也会绿(本轮真这么飘过一次:同一条注入一次红一次绿)。
    $kid = @($t.Keys | Where-Object {
                 $t[$_].Parent -eq $script:ProbeRootPid -and
                 $_ -ne $script:ProbeRootPid -and
                 $t[$_].Name -eq 'powershell.exe' }) |
           Select-Object -First 1
    $n++
}
if (-not $kid) {
    Stop-Process -Id $script:ProbeRootPid -Force -EA SilentlyContinue
    "NOCHILD"; exit 0
}
"child=$kid"
# 「送控制台事件」换成「根从容退出」
function Invoke-BootPython { param([string[]]$Arg)
    Stop-Process -Id $script:ProbeRootPid -Force -EA SilentlyContinue
}
Stop-ProcTree $script:ProbeRootPid
Start-Sleep -Milliseconds 500
"root_alive=$(Test-Alive $script:ProbeRootPid)"
"child_alive=$(Test-Alive $kid)"
Stop-Process -Id $kid -Force -EA SilentlyContinue
'''
        out = self.ps_lines(body, timeout=240)
        if 'NOCHILD' in out:
            self.skipTest('没能造出父子进程(这台机器上 Start-Process 被拦了?)'
                          '—— 不假装验过')
        self.assertIn('root_alive=False', out)
        self.assertIn('child_alive=False', out,
                      '根退了就返回的话,这个子进程会留在后台:它不跟着父进程走')


if __name__ == '__main__':
    unittest.main()
