# 解释器的位置(Windows)。`kb.ps1` 里 BOOT_PY / proj_py / uv 那一族,由 kb.ps1 source。
#
# **拆成单独一份是因为 kb.ps1 顶到了 700 行那条线**(全局约定)。这一族本来就是一件
# 独立的事:「这台机器上用哪个 python、哪个 uv」—— 它有自己的坑(Microsoft Store 那个
# 占位 python、winget 装完 PATH 还是旧的),也有自己的回归。
#
# **双环境的分工别记错**(口径同 kb.sh):`Get-BootPython` 是给**只用标准库**的引导脚本
# 用的(win_daemon.py、grab_cli.py),它指向采集器那个 venv;要 import kb.* 的脚本一律
# 走 `Invoke-ProjectPython`。弄反了的症状是 ModuleNotFoundError,而且只在正常的双环境
# 安装上才复现 —— 开发机恰好没有 collector/.venv 时两条路看着一样。

# ── 跑原生程序 ────────────────────────────────────────────────────────────────

function Invoke-Native {
    <#  跑一个原生程序,拿回**退出码和输出**,不让它的 stderr 把脚本掀翻。

        **为什么必须有这一层(实测,这条是真进去过的)。** `kb.ps1` 顶上设了
        `$ErrorActionPreference = 'Stop'`,而 PowerShell 一旦看到原生程序往 stderr
        写了字,就把那几行包成 `NativeCommandError` 错误记录 —— 在 `Stop` 下那是
        **终止性**错误。于是「跑一下、看退出码」这种探测会在**它本来要探测的那种
        情形上**当场崩掉,而报出来的话指向 PowerShell 自己的内部机制,跟业务毫无关系。
        实测形态:`.\kb.ps1 start` 在首次建 venv 时死在
        `uv.exe : Using CPython 3.11.17` 上 —— 那根本不是错误,是 uv 的进度信息,
        uv 把进度写在 stderr 上。

        **`2>$null` 挡不住,这一点反直觉、别踩第二次。** 实测在 `Stop` 下
        `2>&1` 和 `2>$null` **都抛**;只有「完全不重定向」和「把
        `$ErrorActionPreference` 降回 `Continue`」不抛。而完全不重定向的话那些
        stderr 会直接漏到用户终端上(uv 的进度、git 的「不是仓库」),所以这里选
        降级 + 捕获。反过来钉一边:程序在 stderr 上一个字都没写时,三种写法都不抛
        —— 触发条件是**stderr 有内容**,不是重定向本身。

        **降级只在本函数作用域内生效**(实测确认过不外泄):出了这个函数,调用方
        那份 `Stop` 照旧管着 cmdlet —— 那是我们要保留的纪律,别为了这件事把整份
        脚本降成 `Continue`。

        **找不到可执行文件照旧抛**(`CommandNotFoundException`),这是故意的:
        那是真·配置错,不该被翻译成一个「退出码非 0」悄悄混进判据里。

        返回 `@{ Code; Text }`。只关心成败的调用方看 `Code`,要认输出的
        (`Get-BootPython` 认 `Python 3`)看 `Text`。

        **`-StdoutOnly` 决定 stderr 去哪,选错会静默污染判据。** 默认把 stderr 并进
        `Text`(探测「它说了什么」时要的就是这个:Store 占位 python 把话写在 stderr
        上)。而**从 stdout 上解析一个值**的调用方必须加 `-StdoutOnly`:curl 的
        `%{http_code}`、git 的路径、采集器那行 JSON —— 并进来的话,一句
        `curl: (7) Failed to connect` 会让 `"000"` 变成一段解析不出数字的文本,
        或者让 `ConvertFrom-Json` 拿到最后一行 stderr。两种模式都不抛,区别只在
        stderr 是留下还是丢掉。  #>
    param([Parameter(Mandatory)][string]$Exe,
          [string[]]$Arg = @(),
          [switch]$StdoutOnly)
    $ErrorActionPreference = 'Continue'
    if ($StdoutOnly) {
        $out = & $Exe @Arg 2>$null
    } else {
        $out = & $Exe @Arg 2>&1
    }
    return @{ Code = $LASTEXITCODE
              Text = (@($out | ForEach-Object { "$_" }) -join "`n") }
}

function Invoke-NativeLive {
    <#  跑一个原生程序,**输出照原样流到用户终端**,退出码留在 `$LASTEXITCODE` 里。

        `Invoke-Native` 的另一半:那个要把输出攥在手里(探测、解析),这个是给
        **人要看过程**的命令用的 —— 图谱构建、回归、采集器 cli。两者的共同点是
        都把 `$ErrorActionPreference` 降回 `Continue`,理由一模一样(见上)。

        **这一半尤其要紧,因为很多程序把正常输出写在 stderr 上。** `unittest`
        把全部进度和结果都写 stderr,所以 `.\kb.ps1 test` 在 `Stop` 下**一开跑就被
        掀翻**;`uv` 的进度、git 的提示同理。把这些当成错误来终止,等于「凡是会说话
        的程序都不许调用」。

        **不要在这里判成败、也不要 Die。** 调用方的口径各不相同(`accounts` 要把
        退出码原样透给 shell,图谱那边要接着往下走),在这儿统一翻译会把那个信号吃掉
        —— 口径同 `Invoke-ProjectPython` 那条「不替调用方 die」。

        赋值给变量时拿到的是 stdout(stderr 照旧流到终端),`Invoke-CollectorCli`
        问 `status` 那一处要的正是这个形状。  #>
    param([Parameter(Mandatory)][string]$Exe,
          [string[]]$Arg = @())
    $ErrorActionPreference = 'Continue'
    & $Exe @Arg
}

# ── 解释器 ────────────────────────────────────────────────────────────────────

function Get-BootPython {
    <#  `BOOT_PY` 的对等物:**给只用标准库的引导脚本准备的**(win_daemon.py、
        grab_cli.py)。先用采集器那个 venv,取不到就退到系统上的 python。

        **必须真跑一次 `--version` 才算找到。** Windows 10/11 预装的那个
        `python` / `python3` 是 Microsoft Store 的占位程序:它在 PATH 上、
        Get-Command 找得到,一跑却只打印一句「去商店装」并以非 0 退出(实测 rc=49)。
        只看 Get-Command 的话,服务会死在一句跟知识库毫无关系的话上。  #>
    if ($null -ne $script:BootPyCache) { return $script:BootPyCache }
    if (Test-Path -LiteralPath $script:Py -PathType Leaf) {
        $script:BootPyCache = @{ Exe = $script:Py; Pre = @() }
        return $script:BootPyCache
    }
    foreach ($c in @(@{ Exe = 'py'; Pre = @('-3') },
                     @{ Exe = 'python3'; Pre = @() },
                     @{ Exe = 'python'; Pre = @() })) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        # Store 那个占位程序把「去商店装」写在 stderr 上,所以这一句必须走
        # Invoke-Native —— 直接 `2>&1` 的话,**正是在它要识别的那台机器上**
        # 脚本会被 NativeCommandError 掀翻(见 Invoke-Native 的说明)。
        $r = Invoke-Native $c.Exe @($c.Pre + @('--version'))
        if ($r.Code -eq 0 -and ($r.Text -match 'Python 3')) {
            $script:BootPyCache = $c
            return $c
        }
    }
    Die @"
找不到可用的 python 3。装一个(两条都行):
       winget install astral-sh.uv        # 顺带把项目环境也能装起来
       winget install Python.Python.3.12
     PATH 上那个 python/python3 如果是 Microsoft Store 的占位程序,它不算。
"@
}
$script:BootPyCache = $null

function Invoke-BootPython { param([string[]]$Arg)
    $p = Get-BootPython
    Invoke-NativeLive $p.Exe @($p.Pre + $Arg)
}

function Get-Uv {
    <#  uv 的位置。PATH 上没有就找几个常见的安装位置(winget / 官方安装脚本 /
        `pip install --user uv` 都装在用户目录下,而那几个目录不一定在当前 shell 的
        PATH 上)。

        **`pip install --user uv` 那一处不能漏**:它落在
        `%APPDATA%\Python\Python3XX\Scripts`,版本号写在目录名里,所以只能 glob ——
        实测这台机器上 uv 就装在那儿,少了这一条的后果是「uv 明明装好了,入口却说
        找不到」,而人刚亲手装过、只会认为是脚本坏了。

        **别拿 `Get-BootPython` 去推解释器的 Scripts 目录。** 它返回的是
        `@{Exe;Pre}` 不是路径,而且找不到 python 时**自己就 Die 了** —— 于是一次
        「找 uv」会打出一句叫人去装 python 的话。两件事不许在这里缠在一起。  #>
    if ($null -ne $script:UvCache) { return $script:UvCache }
    $c = Get-Command 'uv' -ErrorAction SilentlyContinue
    if ($c) { $script:UvCache = $c.Source; return $script:UvCache }
    $cands = @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
               (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'),
               (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'))
    # pip install --user:%APPDATA%\Python\Python3XX\Scripts\uv.exe(版本号在目录名里)
    if ($env:APPDATA) {
        $cands += @(Get-ChildItem -Path (Join-Path $env:APPDATA 'Python') -Filter 'Python3*' `
                                  -Directory -ErrorAction SilentlyContinue |
                    ForEach-Object { Join-Path $_.FullName 'Scripts\uv.exe' })
    }
    foreach ($p in $cands) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { $script:UvCache = $p; return $p }
    }
    Die @"
找不到 uv —— 项目环境和起服务那条命令都要它。装一个:
       winget install astral-sh.uv
     (或者 pip install --user uv,或 https://docs.astral.sh/uv/ 上那条安装命令;
     装完**开一个新终端**,不然 PATH 还是旧的)
"@
}
$script:UvCache = $null

function Invoke-ProjectPython {
    <#  `proj_py` 的对等物。**要 import kb.* 的脚本一律走这里,不许用 BootPython**
        —— BootPython 指向采集器那个 venv(只声明 lxml / requests / zstandard),
        而主项目的脚本要 bs4 / httpx / feedparser。实测在正常的双环境安装上
        `held` 直接 ModuleNotFoundError,而开发机恰好没有 collector/.venv、系统
        python 恰好装着那几个包,于是单跑脚本全绿、经过入口的那条真实用户路径没人走过。

        **不清环境**(CleanEnv 那一族是起服务用的):那会把 `KB_DATA_DIR` 一起清掉,
        于是这条命令**静默**去读仓库内的 `data/` —— 显式隔离过数据目录的人看到的是
        「没有挂起」,而他那个库里躺着一堆。

        **不替调用方 die。** 有些调用方拿非 0 当信号(Show-EgressGap 就是),
        在这儿一律翻成「环境没装好」会把那个信号吃掉。  #>
    param([string[]]$Arg)
    $venv = Join-Path $script:Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv -PathType Leaf) {
        Invoke-NativeLive $venv $Arg
        return
    }
    Push-Location $script:Root
    try { Invoke-NativeLive (Get-Uv) (@('run', '--offline', 'python') + $Arg) }
    finally { Pop-Location }
}

