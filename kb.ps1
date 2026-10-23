#!/usr/bin/env pwsh
<#
Knowbase 的 Windows 操作入口 —— `kb.sh` 的对等物,一条命令一条命令对着翻过来的。

原则同 kb.sh:每条命令都是跑完就还你终端。服务起成后台守护进程,耗时的活(采集、
导出、索引、打标)交给已经在后台的 kb-web 去跑,命令本身立刻返回。

**跟 kb.sh 的对应关系**(改这个文件之前先读 docs/decisions/windows-entry.md 第一节):

  kb.sh                           kb.ps1
  ─────────────────────────────── ──────────────────────────────────────────────
  daemonize()  fork + setsid      scripts/win_daemon.py spawn(自己的隐藏控制台)
  lsof -ti tcp:N                  Get-NetTCPConnection -LocalPort N -State Listen
  ps -o ppid=/command=  pgrep -P  Get-CimInstance Win32_Process(一次取全表)
  kill -INT                       win_daemon.py ctrlbreak(CTRL_BREAK → SIGBREAK)
  kill -TERM / -KILL              Stop-Process -Force(TerminateProcess)
  env -i HOME=… PATH=…            白名单拼环境,见 scripts/win_env.ps1
  collector/.venv/bin/python      collector\.venv\Scripts\python.exe
  . scripts/web_env.sh            . scripts/win_env.ps1
  . scripts/stale.sh              . scripts/win_stale.ps1

**只有一处语义真的少了一档**:kb.sh 的停机是 SIGINT → SIGTERM → SIGKILL 三级,
Windows 只有「送控制台事件」和「TerminateProcess」两级(没有 SIGTERM 这种可捕获的
中间档)。所以这边是 CTRL_BREAK → 等 → 硬杀。补偿是停机保护那三态照搬过来了,
而且它比在 macOS 上更要紧 —— 见 Stop-Web。
#>

Set-StrictMode -Version Latest
# `set -euo pipefail` 的对等物里**最要紧的是这一条**。PowerShell 默认
# `$ErrorActionPreference = 'Continue'`:cmdlet 报了错它打一行红、然后**接着往下走**
# —— 于是一个读不出来的状态会被当成「确定没有」。实测形态:`Test-PortBusy` 里一处
# StrictMode 报错让 `status` 打了一行红,紧接着照常输出「kb-web 未运行」,而真相是
# 「没问出来」。这正是本项目最不能要的那一类(「判不准一律抛,不许返回确定没有」)。
# 预期之内的失败一律就地显式放行(`-ErrorAction SilentlyContinue` 或 try/catch),
# 而不是把全局开关放松。
#
# **原生命令的非 0 退出不受它管**(PowerShell 5.1 只对 cmdlet 错误生效),所以底下
# 那些 `$LASTEXITCODE` 的判断照旧有效、也照旧必须写。
$ErrorActionPreference = 'Stop'

# ── 配置(对着 kb.sh 顶上那一段)────────────────────────────────────────────────

$script:Root = $PSScriptRoot
$script:Collector = Join-Path $script:Root 'collector'
$script:Py = Join-Path $script:Collector '.venv\Scripts\python.exe'
$script:RunDir = Join-Path $script:Root '.run'
$script:LogDir = Join-Path $script:Root 'runtime_logs'
$script:WinDaemon = Join-Path $script:Root 'scripts\win_daemon.py'

$script:Port = if ($env:KB_WEB_PORT) { [int]$env:KB_WEB_PORT } else { 8787 }
# pidfile 名字里带端口。脚本自己在端口冲突时就会劝你换个端口跑,要是所有端口共用一个
# .run/web.pid,临时端口起过一次之后,默认 8787 的 status/stop/restart 就会把临时端口
# 那份也当成自己的 —— 跨端口误杀是同一个病根。
$script:WebPid = Join-Path $script:RunDir "web-$($script:Port).pid"
$script:Api = "http://127.0.0.1:$($script:Port)"
$script:WebPat = 'kb-web'

# 所有 curl 都带超时。没有超时的话,旧实例端口还在监听但 HTTP 卡住时,stop/restart 会
# 跟着一起卡死 —— 恰好是最需要它把旧实例杀掉的场景。
#
# **`--noproxy *` 是 Windows 这边多出来的一条,不能省。** 这儿的人多半挂着全局代理
# (系统代理 / 各种客户端),而 curl 在 Windows 上会读环境变量里的 `*_proxy`。本机
# 回环被代理吃掉之后,`Get-PipelineState` 永远落在「问不出来」那一档:stop 不敢停、
# sync 投不出去,而面板在浏览器里开着好好的 —— 报出来的话一点都不指向代理。
$script:Curl = Join-Path $env:SystemRoot 'System32\curl.exe'
$script:CurlArgs = @('-s', '--connect-timeout', '2', '--max-time', '5', '--noproxy', '*')

$script:Force = $false
$script:ProcTable = $null

function Write-Note { param([string]$Text) [Console]::Error.WriteLine($Text) }
function Die { param([string]$Text) [Console]::Error.WriteLine("错误:$Text"); exit 1 }

$null = New-Item -ItemType Directory -Force -Path $script:RunDir, $script:LogDir -EA SilentlyContinue

. (Join-Path $script:Root 'scripts\win_py.ps1')
. (Join-Path $script:Root 'scripts\win_proc.ps1')
. (Join-Path $script:Root 'scripts\win_env.ps1')
. (Join-Path $script:Root 'scripts\win_stale.ps1')

# **立刻生效,排在任何命令之前。** 这一句开 Python 的 UTF-8 Mode,于是从这个入口起的
# 每个子进程都继承到它。不开的话 Python 的 open() 跟着本地代码页走(cp936),而这个
# 项目的事实来源全是 UTF-8 —— 服务会用 GBK 读写 UTF-8 的正文,一路零报错地把原文
# 替换成乱码。名单和完整理由在 win_env.ps1 的 ForcedEnv 上。
Set-ForcedEnv

function Get-Setting {
    <#  一个环境变量的取值(空串当没设)。win_env/win_stale 两份都用它。  #>
    param([string]$Name)
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ($v) { return $v }
    return ''
}

# ── kb-web:知识库面板(耗时的活都在它进程里跑)──────────────────────────────

function Initialize-WebEnv {
    <#  `web_prepare_env` 的对等物。三条硬约束在 CLAUDE.md「操作入口」那节,
        这里只记它们在这一侧的样子:

        **起服务那条命令一律 --offline。** pyproject.toml 一动,`uv run` 就要重建
        editable 包、去 pypi 问一次 hatchling 的最新版;网络一抖就挂在
        「No solution found when resolving: hatchling」上,而那发生在 daemonize
        之后的后台进程里 —— 用户只看到「kb-web 起不来,看 web.log」,日志尾巴是
        一段 tls handshake eof。

        **所以「要不要联网装东西」挪到前台问**:离线确认不成才联网装一次。前台做
        有两个好处:失败当场说得清;以及用得上当前 shell 的代理设置(清环境那一步
        把 `*_proxy` 一起清掉了)。

        **不用 `uv sync`**:它默认 exact,会卸掉 venv 里旧项目名那份 editable
        残留,而两份 dist-info 的 RECORD 记着**同一组** kb / kb-web / kb-mcp 入口。  #>
    $uv = Get-Uv
    Push-Location $script:Root
    try {
        # **探测用的那句写 `pass`,不许写空串。** kb.sh 那边是 `python -c ""`,而
        # PowerShell 调原生程序时会把 `''` 整个丢掉 —— python 于是收到一个光秃秃的
        # `-c`,报 `Argument expected for the -c option` 并以非 0 退出。后果不是
        # 报错了事:这一步**永远失败**,于是每次起服务都去联网装一次。`pass` 同样是空操作。
        # **两句都必须走 Invoke-Native**(理由见该函数):uv 把进度写在 stderr 上,
        # 而顶上是 `Stop` —— 实测 `.\kb.ps1 start` 死在 `Using CPython 3.11.17`
        # 这句**不是错误的进度信息**上。`2>$null` 也挡不住。
        $probe = @('run', '--offline', 'python', '-c', 'pass')
        if ((Invoke-Native $uv $probe).Code -eq 0) { return }
        Write-Note '[web] 环境要补一下(依赖有变动,或 uv 缓存不全),联网装一次'
        $r = Invoke-Native $uv @('run', 'python', '-c', 'pass')
        if ($r.Code -ne 0) {
            # **把 uv 说的话原样带上** —— 这一段挪到前台的理由就是「失败当场说得
            # 清」,只打一句「装不上」等于没说(实测真因是上一次被掀翻的运行留下
            # 了一个空 .venv,而那句话会让人一直往网络上找)。
            Die @"
依赖装不上,kb-web 起不来。uv 说:
$($r.Text)
     上面要是提到 .venv 不是有效环境,把那个目录删掉再跑(半途被打断的首次
     安装会留下一个空壳);否则等网络通了再跑一次 .\kb.ps1 web start
"@
        }
    } finally { Pop-Location }
}

function Start-Web {
    if (@(Get-ServicePids $script:WebPid $script:Port $script:WebPat).Count -gt 0) {
        Write-Note "[web] 已在 $($script:Api)"
        Show-CodeStale; Show-GraphStale
        return
    }
    # 端口有人占但不是 kb-web:说清楚,别等它「起完」再报一个莫名其妙的超时
    if (Test-PortBusy $script:Port) {
        Die @"
端口 $($script:Port) 被别的进程占着,kb-web 起不来。
     换个端口:`$env:KB_WEB_PORT=8788; .\kb.ps1 web start
"@
    }
    if (-not (Test-Path -LiteralPath $script:Curl -PathType Leaf)) {
        Die "找不到 $($script:Curl) —— 这个入口靠系统自带的 curl 跟面板说话(Win10 1803+ 都有)"
    }
    Initialize-WebEnv
    Show-EgressGap
    $envPairs = @(Build-WebEnv) + @("PORT=$($script:Port)")
    $spawn = @($script:WinDaemon, 'spawn', $script:WebPid,
               (Join-Path $script:LogDir 'web.log'), $script:Root)
    foreach ($e in $envPairs) { $spawn += @('--env', $e) }
    $spawn += @('--', (Get-Uv), 'run', '--offline', 'kb-web')
    $null = Invoke-BootPython $spawn
    if ($LASTEXITCODE -ne 0) { Die 'win_daemon.py spawn 没起成(上面有原因)' }
    $n = 0
    while (-not (Test-PortBusy $script:Port) -and $n -lt 40) { Start-Sleep -Milliseconds 500; $n++ }
    if (-not (Test-PortBusy $script:Port)) {
        Die "kb-web 起不来,看 $(Join-Path $script:LogDir 'web.log')"
    }
    Write-Note "[web] $($script:Api)"
    Show-DohLine
    # 图谱页是 vault 里的产物,不是 kb-web 现渲染的:样式改了得重新生成,提示一句
    Show-GraphStale
}

function Stop-Web {
    <#  管道跑在 kb-web 进程里,杀了它这一轮同步就断在半路(采集到一半的文章要重抓)。
        所以默认拦一下,给一句怎么强来 —— 而不是二话不说砍掉一个可能跑了半小时的活。

        **这道保护在 Windows 上比在 macOS 上更要紧**:那边停机的第一下是 SIGINT,
        uvicorn 收到之后会走正常 shutdown;这边第一下是 CTRL_BREAK(也能走 shutdown),
        但万一它没收到,第二下就直接是 TerminateProcess —— 没有中间那一档可以缓。  #>
    if (-not $script:Force) {
        $state = Get-PipelineState
        if ($state -eq 'running') {
            Die @"
同步正在跑,现在停会把它断在半路。
     等它跑完(.\kb.ps1 status 看进度),或者确实要停就加 --force
"@
        }
        if ($state -eq 'unknown') {
            Die @"
问不出同步在不在跑($($script:Api) 没应答),不敢就这么停 —— 它可能正跑到一半。
     过一会儿再试,或者确实要停就加 --force
"@
        }
    }
    Stop-Service_ 'kb-web' $script:WebPid $script:Port $script:WebPat
}

function Get-WebStatus {
    $pids = @(Get-ServicePids $script:WebPid $script:Port $script:WebPat)
    if ($pids.Count -gt 0) {
        Write-Output "kb-web    运行中 ($($script:Api))"
        Show-CodeStale
        Show-DohLine
    } elseif (Test-PortBusy $script:Port) {
        Write-Output "kb-web    未运行(端口 $($script:Port) 被别的进程占着)"
    } else {
        Write-Output 'kb-web    未运行'
    }
}

# ── 管道:把耗时的活丢给 kb-web,命令立刻返回 ─────────────────────────────────

function Invoke-Api {
    <#  一次 HTTP。返回 @{Code; Body};连不上时 Code=0。

        **响应体必须落到文件、按 UTF-8 读回来,不许让 PowerShell 去接 curl 的 stdout。**
        PowerShell 解码原生程序的输出用的是 `[Console]::OutputEncoding`,默认是系统
        OEM 代码页(这台机器是 936)。而接口返回的是 UTF-8,于是每个中文字的多字节
        序列都被按 GBK 拆错 —— **连带把 JSON 的引号吃掉**,`ConvertFrom-Json` 当场抛
        `ArgumentException`。实测形态:`/api/pipeline/status` 的
        `"message":"采集器还没装好"` 变成 `"message":"采集器还没装�?,`,整份 JSON 报废。

        **后果不止是 status 少打一行。** `Get-PipelineState` 读的是同一份 JSON:解析
        不了就落在「问不出来」那一档,于是 `stop` / `restart` **一律拒绝停**(怕砍断
        正在写 kb.db 的那一步),而面板在浏览器里一切正常 —— 人只会觉得这个入口坏了。
        这正是 `--noproxy *` 那一条防的同一类事故,只是病根换成了编码。

        请求体同样走文件(`--data-binary @file`):PowerShell 传给原生程序的参数用的是
        ANSI 代码页,`--targets 某个中文号` 直接写在命令行上会在半路被改写。  #>
    param([string]$Path, [string]$Method = 'GET', [string]$Body = '')
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $out = [IO.Path]::GetTempFileName()
    $payload = $null
    try {
        $a = @() + $script:CurlArgs
        if ($Method -eq 'POST') {
            $payload = [IO.Path]::GetTempFileName()
            [IO.File]::WriteAllText($payload, $Body, $utf8)
            $a += @('-X', 'POST', '-H', 'Content-Type: application/json',
                    '--data-binary', "@$payload")
        }
        # http_code 只有三位 ASCII,它走 stdout 是安全的;响应体走 -o
        # `-StdoutOnly`:curl 把连不上那类话写在 stderr 上,并进来这个数字就解析不出。
        $a += @('-o', $out, '-w', '%{http_code}', "$($script:Api)$Path")
        $codeText = (Invoke-Native $script:Curl $a -StdoutOnly).Text.Trim()
        $code = 0
        $null = [int]::TryParse($codeText, [ref]$code)
        $body = ''
        if (Test-Path -LiteralPath $out -PathType Leaf) {
            $body = [IO.File]::ReadAllText($out, $utf8)
        }
        return @{ Code = $code; Body = $body }
    } finally {
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
        if ($payload) { Remove-Item -LiteralPath $payload -Force -ErrorAction SilentlyContinue }
    }
}

function Get-Prop {
    <#  `d.get("name", default)` 的对等物。

        **JSON 的字段一律走这里取。** StrictMode 下直接 `$d.stages` / `$x.detail`
        在字段缺席时会**抛**(PropertyNotFoundStrict),而接口少一个可选字段是完全
        正常的事(kb.sh 那边写的就是 `s.get("detail", "")`)—— 抛出去的后果是
        `status` 整条命令红掉,而它只是想打一行进度。  #>
    param($Obj, [string]$Name, $Default = $null)
    if ($null -eq $Obj) { return $Default }
    try {
        $p = $Obj.PSObject.Properties[$Name]
        if ($null -eq $p) { return $Default }
        if ($null -eq $p.Value) { return $Default }
        return $p.Value
    } catch { return $Default }
}

function Get-PipelineState {
    <#  'running' | 'idle' | 'unknown'

        **三态是必须的。** 停机保护只有在「确定空闲」时才该放行。以前问不出来一律当
        空闲,于是状态接口慢一点,stop 就把正在跑的同步砍了 —— 而它天然就会慢,因为
        要顺带探采集器(冷缓存时几十秒),恰好超过 curl 那 5 秒。所以这里还要
        `source=false`:只问「在不在跑」,那是内存里现成的一个布尔值,不该被来源探测
        拖下水。  #>
    if (-not (Test-PortBusy $script:Port)) { return 'idle' }
    $r = Invoke-Api -Path '/api/pipeline/status?source=false'
    if ($r.Code -ne 200 -or -not $r.Body) { return 'unknown' }
    try { $d = $r.Body | ConvertFrom-Json } catch { return 'unknown' }
    if ($null -eq $d -or ($d.PSObject.Properties.Name -notcontains 'running')) { return 'unknown' }
    if (Get-Prop $d 'running' $false) { return 'running' }
    return 'idle'
}

function Invoke-Pipeline {
    <#  组装 /api/pipeline/run 的请求体。stages 为空 = 全流程。  #>
    param([string[]]$Stages, [string[]]$Arg)
    $since = ''; $until = ''; $targets = ''; $channel = ''
    $i = 0
    while ($i -lt $Arg.Count) {
        $k = [string]$Arg[$i]
        $v = if ($i + 1 -lt $Arg.Count) { [string]$Arg[$i + 1] } else { '' }
        switch ($k) {
            '--since'   { $since = $v }
            '--until'   { $until = $v }
            '--targets' { $targets = $v }
            '--channel' { $channel = $v }
            default     { Die "不认识的参数 $k" }
        }
        $i += 2
    }
    Start-Web
    if ((Get-PipelineState) -eq 'running') {
        Die "管道已经在跑了(看 $($script:Api) 管道 tab,或 .\kb.ps1 status)"
    }
    $b = @{}
    if ($Stages -and $Stages.Count -gt 0) { $b['stages'] = $Stages }
    if ($since)   { $b['since_date'] = $since }
    if ($until)   { $b['until_date'] = $until }
    if ($targets) { $b['accounts'] = @($targets.Split(',') | Where-Object { $_ }) }
    if ($channel) { $b['channel'] = $channel }
    $r = Invoke-Api -Path '/api/pipeline/run' -Method POST -Body ($b | ConvertTo-Json -Compress)
    if ($r.Code -eq 200) {
        Write-Note "已在后台开始。进度看 $($script:Api) 管道 tab,或跑 .\kb.ps1 status"
    } else {
        Die "启动失败:$($r.Body)"
    }
}

# ── 手动收录 ──────────────────────────────────────────────────────────────────

function Invoke-Grab {
    <#  网页端那颗「存入知识库」的命令行版。**判断一个都不在这儿**:抓取、查重、
        重抓替换、起后半条链全在服务端(kb/web/grab_api.py,硬约束见
        docs/decisions/link-grab.md 第一节),这里只负责「确认服务在」,再把参数原样
        交给 scripts/grab_cli.py。

        先 Start-Web 的理由同 sync:**管道跑在 kb-web 进程内**,服务不在就只能把内容
        留在 feeds.db 里、检索不到。

        **worktree 里只许借用已经跑着的那份服务。** 端口上还没人时 Start-Web 会起一份
        用**这个检出自己** `data/` 的服务 —— 真文章于是收进一个测试库,而终端上写着
        「已写入知识库」。那是谎报。出路给两条,都不代劳。  #>
    param([string[]]$Arg)
    if ($Arg.Count -eq 0) {
        Die '给一条链接:.\kb.ps1 grab <链接>(多条用空格隔开;- 表示从标准输入读,一行一条)'
    }
    $cli = Join-Path $script:Root 'scripts\grab_cli.py'
    # **帮助自己就是终点,打完直接走。** argparse 的 `--help` 让子进程以 0 退出,和
    # 「参数校验通过」一模一样 —— 光靠下面那句分不开它们,于是帮助打完之后还会往下走:
    # 起服务(worktree 里更糟,先撞上那道闸门、连帮助都拿不到)。
    foreach ($a in $Arg) {
        if ($a -eq '-h' -or $a -eq '--help') {
            Invoke-BootPython @($cli, '--help')
            exit $LASTEXITCODE
        }
    }
    # 本机 API 不走代理:grab_cli 用的是 urllib,而它在 Windows 上会去读注册表里的系统
    # 代理设置 —— 挂着全局代理的人,投递会被代理吃掉。理由同 CurlArgs 里那个
    # `--noproxy`。只在本进程里设,跑完还原,不污染调用者的 shell。
    $saved = $env:no_proxy
    try {
        $env:no_proxy = '127.0.0.1,localhost'
        # **余下的参数校验也排在任何副作用之前。** `--parse-only` 只解析就退出:不读
        # 标准输入、不发一个请求、不起服务。它顺带把最终生效的那个 kb-web 地址打回来。
        $want = ([string](Invoke-BootPython @($cli, '--parse-only', '--api', $script:Api) + $Arg |
                          Select-Object -First 1)).Trim()
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        # **只有目标就是本机托管那份服务时,才替它起/拦。** `--api` 指到别处(另一台
        # 机器、自己另起的一份)时,下面那道闸门和 Start-Web 管的是**另一个**服务。
        # **明确认出「目标是另一个地址」才跳过** —— `$want` 空着一律照旧按本机走。
        $remote = ($want -match '^https?://') -and ($want -ne $script:Api.TrimEnd('/'))
        if (-not $remote) {
            if (-not (Test-PortBusy $script:Port)) {
                $main = Get-MainCheckout
                if ($main) {
                    Die @"
这是 worktree,而 $($script:Port) 上还没有服务 —— 这儿起的那份会用本检出的 data/,
     收进去的文章进的是测试库,而命令行会说「已写入知识库」。
     去主检出跑:cd $main; .\kb.ps1 grab $($Arg -join ' ')
     确实要在这儿收(数据目录是显式挂的):先 .\kb.ps1 start,再跑这条。
"@
                }
            }
            Start-Web
        }
        Invoke-BootPython @($cli, '--api', $script:Api) + $Arg
        exit $LASTEXITCODE
    } finally { $env:no_proxy = $saved }
}

# ── 图谱 ──────────────────────────────────────────────────────────────────────

function Get-MainCheckout {
    <#  如果**当前是 worktree**,返回主检出的路径;否则返回 $null。
        判据用 git 的 common-dir:worktree 的 .git 是个文件,指向主检出那一份。  #>
    # `-StdoutOnly`:不是仓库时 git 把 fatal 写在 stderr 上,并进来会被当成路径。
    $g = @('-C', $script:Root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    $r = Invoke-Native 'git' $g -StdoutOnly
    if ($r.Code -ne 0 -or -not $r.Text) { return $null }
    $common = ([string](($r.Text -split "`n")[0])).Trim()
    if ($common -notmatch '[\\/]\.git[\\/]?$') { return $null }
    $full = [IO.Path]::GetFullPath($common).TrimEnd('\', '/')
    $here = [IO.Path]::GetFullPath((Join-Path $script:Root '.git')).TrimEnd('\', '/')
    if ($full -ieq $here) { return $null }
    return (Split-Path $full -Parent)
}

function Invoke-Graph {
    param([string[]]$Arg)
    # 数据目录跟 kb/config.py 和 scripts/wiki/taxonomy.py 一个口径:KB_DATA_DIR 优先,
    # 没设才回退仓库内的 data/。两个构建器本来就按这个环境变量找库和归类缓存,前置检查
    # 要是只认 $ROOT/data,外置数据目录下就会在进构建器之前误报「缺文件」。
    #
    # **算出来的路径要规范化之后再跟仓库 data/ 比。** 拿原始字符串比的话,
    # `data\..\data` 和一条指回 data\ 的链接都是同一个目录的另一种写法,却跟
    # `$ROOT\data` 不相等,下面那道 worktree 闸门当场被绕过去。
    # `GetFullPath` 去得掉 `..`;符号链接/junction 还要再 Resolve 一次。
    $raw = Get-Setting 'KB_DATA_DIR'
    if (-not $raw) { $raw = Join-Path $script:Root 'data' }
    if (-not [IO.Path]::IsPathRooted($raw)) { $raw = Join-Path $script:Root $raw }
    $dataDir = [IO.Path]::GetFullPath($raw).TrimEnd('\', '/')
    $repoData = [IO.Path]::GetFullPath((Join-Path $script:Root 'data')).TrimEnd('\', '/')
    foreach ($v in @('dataDir', 'repoData')) {
        $p = Get-Variable $v -ValueOnly
        $r = (Resolve-Path -LiteralPath $p -ErrorAction SilentlyContinue)
        if ($r) { Set-Variable $v -Value ($r.ProviderPath.TrimEnd('\', '/')) }
    }
    # **仓库内的 data/ 只许在主检出跑。** 它在 .gitignore 里、只有主检出有;worktree
    # 里要么没有、要么是跑测试留下的空壳 —— 照跑的后果实测过:话题图谱拿测试库建出一张
    # 0 节点的页面,**原子替换掉 vault 里那张好的**(它自己不做质量验收)。
    # KB_DATA_DIR 指到仓库外时这条前提不成立(数据是显式挂的、跟检出无关),那时只靠
    # 下面那道归类缓存检查把空目录挡住。
    if ($dataDir -ieq $repoData) {
        $main = Get-MainCheckout
        if ($main) {
            Die @"
这是 worktree,data\(库 + 归类缓存)只有主检出有,照跑会拿空库覆盖掉好页面。
     去主检出跑:cd $main; .\kb.ps1 graph
"@
        }
    }
    foreach ($f in @('taxonomy_projects.json', 'taxonomy_skills.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $dataDir $f) -PathType Leaf)) {
            Die @"
缺 $(Join-Path $dataDir $f) —— 归类缓存不在,--no-llm 会把几千条判成「待归类」。
     先跑一次完整同步(wiki 阶段会重建缓存),或者 .\kb.ps1 graph 之前确认数据目录挂对了
"@
        }
    }
    # **按项目时区算,不按宿主机**(CLAUDE.md 的元规则)。Asia/Shanghai 1991 年之后
    # 没有夏令时,偏移恒定 +08:00,所以直接加 8 小时就是它 —— 不绕 tzdata,这条命令
    # 在依赖还没装好时也算得对。
    $since = [datetime]::UtcNow.AddHours(8).AddDays(-14).ToString('yyyy-MM-dd')
    Write-Note '[graph] 重建三张图谱(--no-llm:只吃已有归类缓存,不调模型)…'
    $uv = Get-Uv
    Push-Location $script:Root
    try {
        Invoke-NativeLive $uv @('run', 'python', 'scripts/wiki/build_graph_html.py',
                                '--since', $since)
        if ($LASTEXITCODE -ne 0) { Die '话题图谱没建成(原因在上面)' }
        # 分类图谱会在「新图明显不如旧图」时拒绝发布并非零退出 —— 那是保护不是故障,
        # 确实该缩的那次(比如故意收紧分类规则)加 --allow-shrink。
        Invoke-NativeLive $uv (@('run', 'python', 'scripts/wiki/build_catgraph.py',
                                 '--kind', 'all', '--no-llm') + $Arg)

        if ($LASTEXITCODE -ne 0) {
            Die '分类图谱没发布(原因在上面)。拒绝发布是保护:确实该缩就加 --allow-shrink'
        }
    } finally { Pop-Location }
    Write-Note '[graph] 好了。刷新 /graph、/graph/projects、/graph/skills'
}

# ── 组合动作 ──────────────────────────────────────────────────────────────────

function Invoke-CollectorCli {
    <#  `cli()` 的对等物:`-m cli` 要在 collector 目录里跑才找得到模块。  #>
    param([string[]]$Arg)
    if (-not (Test-Path -LiteralPath $script:Py -PathType Leaf)) {
        Die @"
采集器环境还没装好(找不到 $($script:Py))。
     跑一次:.\kb.ps1 setup
"@
    }
    Push-Location $script:Collector
    try { Invoke-NativeLive $script:Py (@('-m', 'cli') + $Arg) } finally { Pop-Location }
}

function Get-Status {
    Write-Output '── 服务 ──'
    Get-WebStatus
    Write-Output ''
    Write-Output '── 同步进度 ──'
    if (Test-PortBusy $script:Port) {
        $r = Invoke-Api -Path '/api/pipeline/status'
        $d = $null
        if ($r.Code -eq 200 -and $r.Body) { try { $d = $r.Body | ConvertFrom-Json } catch { } }
        if ($null -eq $d) { Write-Output '(读不到)' }
        elseif (-not (Get-Prop $d 'running' $false)) { Write-Output '空闲' }
        else {
            $stages = Get-Prop $d 'stages'
            foreach ($p in @(if ($stages) { $stages.PSObject.Properties } else { @() })) {
                $st = Get-Prop $p.Value 'status' ''
                if ($st -in @('running', 'error')) {
                    Write-Output ("{0}: {1} {2}" -f $p.Name, $st, (Get-Prop $p.Value 'detail' ''))
                }
            }
        }
    } else {
        Write-Output '(面板没开)'
    }
    Write-Output ''
    Write-Output '── 公众号来源 ──'
    # **这一条在 Windows 上注定报接不上,那是对的、不是故障。** 发现文章那一步要读
    # Mac 绿泡泡客户端的库,Windows 上压根没有那个库。采集器自己会把这件事说清楚,
    # 所以照原样问它一句、原样打出来,不在这儿替它编一句话。
    $ok = $false
    if (Test-Path -LiteralPath $script:Py -PathType Leaf) {
        $out = Invoke-CollectorCli @('status') 2>$null
        $last = @($out | Where-Object { $_ }) | Select-Object -Last 1
        if ($last) {
            try {
                $d = $last | ConvertFrom-Json
                Write-Output $d.message
                if (($d.PSObject.Properties.Name -contains 'hint') -and $d.hint) {
                    Write-Output "  $($d.hint)"
                }
                $ok = $true
            } catch { }
        }
    }
    if (-not $ok) { Write-Output '(采集器起不来)' }
}

function Get-Logs {
    param([string[]]$Arg)
    $n = 40
    foreach ($a in $Arg) {
        if ($a -match '^\d+$') { $n = [int]$a }
        elseif ($a -eq 'web') { }                 # kb.sh 那边第一个参数是日志名
        else { Die '只有 web 一份日志(用法:.\kb.ps1 logs [行数])' }
    }
    $f = Join-Path $script:LogDir 'web.log'
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Write-Note '还没有 web 的日志'; return }
    Get-Content -LiteralPath $f -Tail $n
}

function Invoke-Test {
    <#  分层口径在 tests/tiers.py,这里只做两件事:给一个**全新的临时数据目录**
        (打桩范围泄漏那类事故写的就是 `$KB_DATA_DIR/kb.db` —— 不隔离,一次全量就可能
        把夹具文档写进真实库,实测发生过),以及把 TZ / 离线口径钉成和 CI 一致。

        临时目录跑完一定收走:长回归常被中断,残留会在 TEMP 里越积越多。PowerShell 的
        `finally` 在 Ctrl-C 时也会走到,所以放在那儿。  #>
    param([string[]]$Arg)
    $tier = if ($Arg.Count -gt 0) { [string]$Arg[0] } else { 'fast' }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("kb-test-data-" + [IO.Path]::GetRandomFileName())
    $null = New-Item -ItemType Directory -Force -Path $tmp
    $saved = @{}
    foreach ($k in @('KB_DATA_DIR', 'TZ', 'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE')) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k)
    }
    $uv = Get-Uv
    Push-Location $script:Root
    try {
        $env:KB_DATA_DIR = $tmp
        $env:TZ = 'Asia/Shanghai'
        $env:HF_HUB_OFFLINE = '1'
        $env:TRANSFORMERS_OFFLINE = '1'
        # unittest 把进度和结果全写 stderr —— 必须走 Invoke-NativeLive。
        Invoke-NativeLive $uv @('run', '--offline', 'python', '-m', 'tests.tiers', $tier)
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
        foreach ($k in $saved.Keys) {
            if ($null -eq $saved[$k]) { Remove-Item "env:$k" -ErrorAction SilentlyContinue }
            else { Set-Item "env:$k" $saved[$k] }
        }
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit $rc
}

function Show-Usage {
    # **这段里不许出现反引号。** `@"…"@` 是会插值的 here-string,反引号在里头是转义符:
    # 写成 markdown 那样的 `accounts discover`,PowerShell 把 `` `a `` 读成 BEL ——
    # 打出来是「ccounts discover」(少一个字母)外加一声响,而解析期一个警告都没有。
    # 端口那一行的 `` `$ `` 是反过来的用法:故意转义,让 $env:… 原样印出来而不是插值。
    # 回归 tests/test_win_entry.py 用 AST 扫所有双引号串里误生效的转义。
    [Console]::Error.WriteLine(@"
Knowbase 操作入口(Windows)—— 每条命令跑完就还你终端,不用挂着
(这是 ./kb.sh 的 Windows 版;两边命令一一对应)

  日常
    .\kb.ps1 start                  起面板(后台守护进程)
    .\kb.ps1 sync                   跑全流程(采集→导出→索引→打标),后台跑
    .\kb.ps1 sync --channel rss     只跑某一个来源(wechat_local|social|rss)
    .\kb.ps1 status                 服务 / 同步进度 / 来源 一屏看全

  服务(后台守护进程;restart 会先杀掉在跑的那份,不管是谁起的)
    .\kb.ps1 start | stop | restart
    .\kb.ps1 web   start|stop|restart|status      面板 $($script:Port)
    .\kb.ps1 logs [行数]
    (同步正在跑时 stop/restart 会拦一下,加 --force 强来)

  单跑某一步(都是后台跑,立刻返回)
    .\kb.ps1 collect [--since 2026-08-01 --until 2026-08-26] [--targets a,b]
                     [--channel wechat_local|social|rss]
    .\kb.ps1 export | index | extract
    .\kb.ps1 graph [--allow-shrink]  重建三张图谱页(前台约 6 秒,零模型调用)
                   图谱是 vault 里的单文件 HTML,样式改了要重新生成——重启不管用。

  手动收录(网页端那颗按钮的命令行版;抓不到正文的一律不入库)
    .\kb.ps1 grab <链接> [<链接>...]  抓进来,并等它真写进知识库再返回
    .\kb.ps1 grab --state <链接>      只查这一条此刻的状态(只读,不抓不写)
    .\kb.ps1 grab --refetch <链接>    库里那一份不对时重抓,确认得了就把它换掉
                   Get-Clipboard | .\kb.ps1 grab -        从剪贴板那段文字里挑出链接
                   Get-Content links.txt | .\kb.ps1 grab -  一行一条地址

  原文补抓里「看不出是不是正文」的那几条(挂起等人看一眼;只读库,不起服务)
    .\kb.ps1 held [来源]              列出挂起的:每条带着「上一次为什么没收下」
    .\kb.ps1 held --release <key>...  把这几条放回队列,下一轮 sync 再抓
    .\kb.ps1 held [来源] --release-all 列出来的全部放回队列

  回归(前台跑,数据目录自动换成全新临时目录,不碰真实库)
    .\kb.ps1 test                   提交前的快速层:纯逻辑全量,浏览器回归显式拦下
    .\kb.ps1 test probes            只跑浏览器回归 —— 改了 kb/static/ 或阅读页之后
    .\kb.ps1 test full              真全量,push 前至少一次

  订阅(瞬间返回)
    .\kb.ps1 accounts list|discover|add|remove|enable|disable [名字]
    .\kb.ps1 setup                  首次准备(装环境、链好采集器的 data 和 .venv)

**公众号采集这一条在 Windows 上做不到**:发现文章那一步要读 Mac 绿泡泡客户端的库,
Windows 上没有那个库,accounts discover 和 --channel wechat_local 会如实报接不上。
其余阶段(RSS / X / 手动收录 / 导出 / 索引 / 打标 / wiki / 问答)都照常。
"@)
}

# ── 分发(对着 kb.sh 最后那一段)──────────────────────────────────────────────

if ($args.Count -eq 0) { Show-Usage; exit 0 }

$Action = [string]$args[0]
# --force 可以出现在任何位置,择出来后剩下的参数照常传
$Rest = @()
for ($n = 1; $n -lt $args.Count; $n++) {
    $a = [string]$args[$n]
    if ($a -eq '--force' -or $a -eq '-Force') { $script:Force = $true } else { $Rest += $a }
}

switch ($Action) {
    'status'  { Get-Status }
    'logs'    { Get-Logs $Rest }

    'start'   { Start-Web }
    'stop'    { Stop-Web }
    'restart' { Stop-Web; Start-Web }
    'web'     {
        $sub = if ($Rest.Count -gt 0) { [string]$Rest[0] } else { 'status' }
        switch ($sub) {
            'start'   { Start-Web }
            'stop'    { Stop-Web }
            'restart' { Stop-Web; Start-Web }
            'status'  { Get-WebStatus }
            default   { Die 'web 只接 start|stop|restart|status' }
        }
    }

    'sync'    { Invoke-Pipeline @() $Rest }
    'collect' { Invoke-Pipeline @('crawl') $Rest }
    'export'  { Invoke-Pipeline @('export') @() }
    'index'   { Invoke-Pipeline @('index') @() }
    'extract' { Invoke-Pipeline @('extract') @() }
    'graph'   { Invoke-Graph $Rest }
    'grab'    { Invoke-Grab $Rest }

    # **只读库、不起服务、不发请求**,所以直接跑脚本(口径同 accounts:瞬间返回的
    # 那一类不该把 kb-web 拽起来)。判据只有一份,在 fulltext.held / release 上。
    'held'    {
        Invoke-ProjectPython (@((Join-Path $script:Root 'scripts\feed_held_cli.py')) + $Rest)
        exit $LASTEXITCODE
    }
    'test'    { Invoke-Test $Rest }

    'accounts' { Invoke-CollectorCli (@('accounts') + $Rest); exit $LASTEXITCODE }
    'setup'    {
        $ps1 = Join-Path $script:Collector 'setup.ps1'
        if (-not (Test-Path -LiteralPath $ps1 -PathType Leaf)) { Die "找不到 $ps1" }
        & $ps1
        exit $LASTEXITCODE
    }

    '-h'      { Show-Usage }
    '--help'  { Show-Usage }
    'help'    { Show-Usage }
    default   { Die "不认识的命令 $Action(跑 .\kb.ps1 看列表)" }
}
