#!/usr/bin/env pwsh
<#
把采集器跑起来所需的、不进版本库的两样东西链好:数据目录与 venv。
`collector/setup.sh` 的 Windows 对等物 —— 两边的判据和提示一字对着翻。

为什么需要这一步:data/(文章库)和 .venv/ 都在 .gitignore 里,所以
`git worktree add` 出来的新工作树只有源码。跑一次本脚本即可,它是幂等的。

两者都指向仓库外的同一份真实目录——采集是有状态的,多个 worktree 共用同一个库
才对:各自持有一份几百 M 的副本既浪费,又会让「哪份是最新的」变成新问题。
要独立数据就设 KB_COLLECTOR_DATA_PATH 指到别处 —— 和 compose.yaml / .env 里那个
同名变量是同一个意思(「宿主机上的 collector 数据目录在哪」),故意共用一个名字。

**Windows 这边唯一真的不一样的地方:用目录联接(junction)而不是符号链接。**
`New-Item -ItemType SymbolicLink` 在 Windows 上要管理员权限或者开「开发人员模式」,
而 junction 对目录来说不要任何特权、对所有读写程序都透明(python 的 open、sqlite
都照常)。所以这里一律 junction —— 否则这一步会在普通用户的机器上直接失败,
而报出来的是一句「客户端没有所需的特权」,跟采集器没有任何关系。
#>

Set-StrictMode -Version Latest

$HereDir = $PSScriptRoot
# `$XDG_DATA_HOME/knowbase/collector` 的 Windows 对等物。LOCALAPPDATA 才是
# 「本机、不随漫游配置走」的那一份 —— 库有几百 M,不该跟着域账户漫游。
$StateBase = if ($env:XDG_DATA_HOME) { $env:XDG_DATA_HOME } else { $env:LOCALAPPDATA }
$State = Join-Path $StateBase 'knowbase\collector'

function Note { param([string]$T) [Console]::Error.WriteLine($T) }
function Fail { param([string]$T) [Console]::Error.WriteLine("[setup] 错误:$T"); exit 1 }

# 旧变量名 GZH_DATA_HOME / GZH_VENV_HOME 必须继续认 —— 这个脚本改名之前读的是它们,
# 用它们显式指过目录的人,换的是「打开哪个库」而不是一个链接的写法:静默忽略等于让人
# 升级之后打开另一份(或者一份空的)订阅和文章,而脚本照旧报成功。新名字优先,两个都
# 设了打一行说明;只设旧的就沿用并提一句新名字。
function Select-Path {
    param([string]$New, [string]$Old, [string]$NewName, [string]$OldName, [string]$Fallback)
    if ($New) {
        if ($Old -and $New -ne $Old) {
            Note "[setup] $NewName 和 $OldName 都设了,按 $NewName 来:$New"
        }
        return @{ Path = $New; Explicit = $true }
    }
    if ($Old) {
        Note "[setup] 沿用旧变量 ${OldName}:$Old(新名字是 $NewName)"
        return @{ Path = $Old; Explicit = $true }
    }
    return @{ Path = $Fallback; Explicit = $false }
}

$d = Select-Path $env:KB_COLLECTOR_DATA_PATH $env:GZH_DATA_HOME `
                 'KB_COLLECTOR_DATA_PATH' 'GZH_DATA_HOME' (Join-Path $State 'data')
$DataHome = $d.Path; $DataExplicit = $d.Explicit
$v = Select-Path $env:KB_COLLECTOR_VENV_PATH $env:GZH_VENV_HOME `
                 'KB_COLLECTOR_VENV_PATH' 'GZH_VENV_HOME' (Join-Path $State 'venv')
$VenvHome = $v.Path; $VenvExplicit = $v.Explicit

# 存量回退:这个采集器原来住在 WeChatOA_Aggregation 里,库和 venv 也在那边。没显式指定、
# 默认位置还空着、而老位置确实有东西时就接着用老的 —— 搬几百 M 的库是用户自己的决定,
# 不该由一个「链好链接」的脚本代劳。
$Legacy = Join-Path $env:USERPROFILE 'code\gongzhonghao\WeChatOA_Aggregation'
if (-not $DataExplicit -and -not (Test-Path -LiteralPath $DataHome) -and
    (Test-Path -LiteralPath (Join-Path $Legacy 'data') -PathType Container)) {
    $DataHome = Join-Path $Legacy 'data'
    Note "[setup] 沿用原位置的数据目录:$DataHome"
    Note "[setup]   要搬到 $(Join-Path $State 'data') 就把目录移过去,或设 KB_COLLECTOR_DATA_PATH"
}
if (-not $VenvExplicit -and -not (Test-Path -LiteralPath (Join-Path $VenvHome 'Scripts\python.exe')) -and
    (Test-Path -LiteralPath (Join-Path $Legacy '.venv\Scripts\python.exe'))) {
    $VenvHome = Join-Path $Legacy '.venv'
    Note "[setup] 沿用原位置的 venv:$VenvHome"
}

# 空目录也行:库和表由 src/db 在首次连接时建出来
$null = New-Item -ItemType Directory -Force -Path $DataHome

function Get-Uv {
    $c = Get-Command 'uv' -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
                     (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'))) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}

if (-not (Test-Path -LiteralPath (Join-Path $VenvHome 'Scripts\python.exe') -PathType Leaf)) {
    $uv = Get-Uv
    if (-not $uv) {
        Fail '没有 venv 也没有 uv。装一个 uv(winget install astral-sh.uv),或用 KB_COLLECTOR_VENV_PATH 指到现成的 venv'
    }
    Note "[setup] 建 venv:$VenvHome"
    $null = & $uv 'venv' '--project' $HereDir $VenvHome
    if ($LASTEXITCODE -ne 0) { Fail 'uv venv 没建成(原因在上面)' }
    $savedVe = $env:VIRTUAL_ENV
    try {
        $env:VIRTUAL_ENV = $VenvHome
        $null = & $uv 'sync' '--project' $HereDir '--active'
        if ($LASTEXITCODE -ne 0) { Fail 'uv sync 没装成(原因在上面)' }
    } finally {
        if ($null -eq $savedVe) { Remove-Item env:VIRTUAL_ENV -ErrorAction SilentlyContinue }
        else { $env:VIRTUAL_ENV = $savedVe }
    }
}

function Set-Junction {
    <#  `relink` 的对等物:把 $Link 做成指向 $Target 的目录联接。

        setup.sh 那边记着一个踩过的坑:`ln -sfn A link` 在 link 已经是真目录时会把
        链接建进那个目录里(变成 link/A),而不是替换它 —— 于是 collector/data 下多出
        一个 data/,库却照 DATA_DIR 建在 collector/data 本身,采集器拿一个空库跑、
        `cli status` 报 0 个号而不是报错。

        Windows 上形态不同但同样要防:`New-Item -ItemType Junction` 在目标已存在时
        直接失败。所以照 setup.sh 的判据来 —— **已经是链接就换掉;是空目录就收掉;
        非空的真目录一律停下让人看一眼**(里面可能是上一次真的库)。  #>
    param([string]$Target, [string]$Link)
    if (Test-Path -LiteralPath $Link) {
        $item = Get-Item -LiteralPath $Link -Force
        $isLink = ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if ($isLink) {
            # 已经是链接:直接拆掉重建(Remove-Item 对 reparse point 只删链接本身)
            Remove-Item -LiteralPath $Link -Force -Recurse -ErrorAction SilentlyContinue
        } elseif (-not $item.PSIsContainer) {
            Fail "$Link 已经存在而且不是目录,先自己挪开"
        } else {
            if (@(Get-ChildItem -LiteralPath $Link -Force -ErrorAction SilentlyContinue).Count -gt 0) {
                Fail "$Link 是个非空目录,不敢替换成链接:先确认里面的东西还要不要"
            }
            Remove-Item -LiteralPath $Link -Force -ErrorAction SilentlyContinue
        }
    }
    $null = New-Item -ItemType Junction -Path $Link -Target $Target -ErrorAction Stop
}

Set-Junction $DataHome (Join-Path $HereDir 'data')
Set-Junction $VenvHome (Join-Path $HereDir '.venv')

Write-Output "[setup] data -> $DataHome"
Write-Output "[setup] .venv -> $VenvHome"

# `-m cli` 要在 collector 目录里跑才找得到模块
Push-Location $HereDir
try {
    $null = & (Join-Path $HereDir '.venv\Scripts\python.exe') '-m' 'cli' 'status' 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Output '[setup] 自检通过:python -m cli status 可用' }
    else { Note '[setup] 警告:cli status 跑不通,检查上面两个路径' }
} finally { Pop-Location }
