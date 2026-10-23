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
        $out = & $c.Exe @($c.Pre + @('--version')) 2>&1
        if ($LASTEXITCODE -eq 0 -and (($out -join ' ') -match 'Python 3')) {
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
    & $p.Exe @($p.Pre + $Arg)
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
    if (Test-Path -LiteralPath $venv -PathType Leaf) { & $venv @Arg; return }
    Push-Location $script:Root
    try { & (Get-Uv) 'run' '--offline' 'python' @Arg } finally { Pop-Location }
}

