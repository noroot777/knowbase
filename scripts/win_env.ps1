# kb-web 进程拿到的那份环境(Windows)。`scripts/web_env.sh` 的对等物,由 kb.ps1 source。
#
# **拆成单独一份的理由同 web_env.sh**:kb.ps1 顶着 700 行那条线(全局约定),而
# 「服务进程的环境怎么拼」本来就是一件独立的事 —— 它有自己的名单、自己的理由、
# 自己的回归(tests/test_win_entry.py 直接读本文件)。
#
# 透传这一族的理由跟 web_env.sh 一字不差(别记错):
# - `KB_FEED_DOH` 是**硬需求**:不透传的话服务里永远读不到它,可信解析那一层等于没开关。
# - 代理那几个是**让出口变显式**。web_env.sh 里说 macOS 上 `env -i` 并不会让抓取失去
#   代理(httpx 的 trust_env 走 `getproxies()`,它在 macOS 上读系统代理设置)——
#   **Windows 跟 macOS 这一点上是同一侧**:`getproxies()` 在 Windows 上是
#   `getproxies_environment() or getproxies_registry()`,环境变量清光了还会去读注册表里
#   IE/系统那份代理。所以这里透传同样是「让出口显式、可排查」,不是「否则就没出口」。
# - 清掉继承变量防的是外层(尤其嵌套 claude)的登录态变量,那条理由对出口配置不成立。
#
# **名单只有一份**:谁再手写第二遍循环,这件事就又变成两处各判一遍。
# **只透传出口这一族,不是整份 KB_ 开头的变量**:`KB_DATA_DIR` 跟进去会静默换掉服务读
# 的那个库,是另一件事(口径同 web_env.sh)。**空值不透传**:服务进程的环境该只有真正
# 配过的东西,空壳变量会让「到底配没配」在排查时多一层噪声。
# 完整形态与实测见 docs/decisions/dns-egress.md。
$script:EgressVars = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                       'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                       'KB_FEED_DOH', 'KB_FEED_DOH_SECONDS')

# **Windows 上没有 `env -i`,所以「干净环境」要正着拼:一份白名单。**
#
# kb.sh 那边是 `env -i HOME=… PATH=…` —— 把一切都扔掉、只留两样。Windows 上照着扔会
# 让子进程压根起不来:CreateProcess 和 CRT 要 `SystemRoot` 才找得到系统 DLL,python 要
# `TEMP` 落临时文件,uv 的缓存在 `LOCALAPPDATA`。所以换成等价的做法 —— 列出「Windows
# 和 uv/python 跑起来必须有的那些」,**其余一概不继承**,于是外层那些登录态变量
# (`ANTHROPIC_*`、`CLAUDE_*` 之类)同样进不来,那正是 env -i 要防的东西。
#
# **不要往这个名单里加 `KB_*`**(除了出口那一族,它们由 EgressVars 单独透传)。
$script:CleanEnvKeep = @(
    'SystemRoot', 'windir', 'SystemDrive', 'ComSpec', 'OS',
    'PATH', 'PATHEXT', 'TEMP', 'TMP',
    'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER',
    # HOME 的对等物 + uv/pip/python 的缓存和用户目录
    'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'USERNAME', 'APPDATA', 'LOCALAPPDATA',
    'ProgramData', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432',
    'CommonProgramFiles', 'CommonProgramFiles(x86)'
)

#: **Windows 上必须强制打开的那些。这不是用户配置,是平台校正 —— 所以一律覆盖。**
#:
#: `PYTHONUTF8=1`(Python UTF-8 Mode,3.7+):**不开这个,Windows 上的 kb 是坏的,而且
#: 坏法是静默的。** Python 的 `open()` 不带 `encoding=` 时用的是**本地代码页**
#: (中文机器上是 cp936/GBK),而这个项目的事实来源全是 UTF-8 —— vault 里的 markdown、
#: `defaults.json`、`secrets.json`、提取结果。于是服务会用 GBK 去读写 UTF-8 的正文:
#: 读进来是乱码、写回去把原文**替换成**乱码,而一路零报错。那正是元规则里
#: 「静默丢数据、而且不可恢复」那一类。
#:
#: 实测(这台机器,cp936):同一批 179 条回归,不开 171 条错;开了 **179 条全绿**。
#: 病根是同一个 —— 「不写 encoding 就跟着系统代码页走」。它和 `Invoke-Api` 那个
#: `ConvertFrom-Json` 崩掉的 bug 是一件事的两面(一个在 PowerShell 侧,一个在 Python 侧)。
#:
#: **覆盖而不是「没设才设」**:有人把 `PYTHONUTF8=0` 留在环境里(或者某个工具给设了),
#: 拿到的就是上面那个静默损坏。这件事不该由环境投票。
$script:ForcedEnv = @{ PYTHONUTF8 = '1' }

function Set-ForcedEnv {
    <#  把 ForcedEnv 应用到**当前进程**,于是从这个入口起的每个子进程都继承到它:
        `uv run`、项目 python(held / grab / graph)、采集器 cli、回归。

        服务进程走的是另一条路(`Build-WebEnv` → win_daemon.py 的 --env),那边
        **也从同一份 `ForcedEnv` 取** —— 名单只有一处,别在两边各写一遍。  #>
    foreach ($k in $script:ForcedEnv.Keys) {
        Set-Item -Path "env:$k" -Value $script:ForcedEnv[$k]
    }
}

function Build-WebEnv {
    <#  服务进程那份环境,`NAME=VALUE` 的数组(交给 scripts/win_daemon.py)。  #>
    $out = @()
    foreach ($k in $script:CleanEnvKeep) {
        $v = [Environment]::GetEnvironmentVariable($k)
        if ($null -ne $v -and $v -ne '') { $out += "$k=$v" }
    }
    foreach ($k in $script:EgressVars) {
        $v = [Environment]::GetEnvironmentVariable($k)
        if ($null -ne $v -and $v -ne '') { $out += "$k=$v" }      # 空值不透传
    }
    foreach ($k in $script:ForcedEnv.Keys) { $out += "$k=$($script:ForcedEnv[$k])" }
    return $out
}

function Show-EgressGap {
    <#  配了 DoH 却看不到出口时提一句。**提示而不代劳**(口径同 Show-CodeStale)。

        **判据是真探测,不是「环境变量里有没有」。** web_env.sh 里记着:按变量判在
        macOS 上是误报,因为那里 `getproxies()` 读的是系统代理设置。**Windows 同理**
        —— 它会去读注册表里 IE/系统那份代理,一个 `*_proxy` 都没设也可能有出口。
        所以直接问一次 `urllib.request.getproxies()`:那正是 httpx 的 trust_env 走的
        那一份,口径只有一份,而且跨平台自动对。

        文案是**陈述**:说清「我们这边探到什么」,不断言对方连不上。  #>
    if (-not (Get-Setting 'KB_FEED_DOH')) { return }
    $code = 'import sys, urllib.request; sys.exit(0 if urllib.request.getproxies() else 1)'
    $null = Invoke-ProjectPython @('-c', $code) 2>$null
    if ($LASTEXITCODE -eq 0) { return }
    Write-Note @"
[web] KB_FEED_DOH 配着,但服务那份环境里探不到代理(环境变量和系统代理都没有)
     —— DoH 端点要是在墙外,解析会报「域名解析服务不可用」
"@
}

function Show-DohLine {
    <#  启动/状态各打一行:DoH 开没开、端点是什么。**端点脱敏到主机名** —— 有人把
        token 写在 path 或 query 里(NextDNS、AdGuard 那类带 profile id 的端点),
        日志不该留。少了这一行的后果 web_env.sh 里实测过:服务重启了却没生效,
        只能靠手工比 pid 和进程环境才看出来。  #>
    $doh = Get-Setting 'KB_FEED_DOH'
    if ($doh) {
        $ep = ($doh -replace '^([a-z]+://[^/?]+).*', '$1')
        Write-Note "[web] 可信解析:开(端点 $ep)"
    } else {
        Write-Note '[web] 可信解析:关(走系统解析;要开:$env:KB_FEED_DOH=''…'',再 .\kb.ps1 restart)'
    }
}
