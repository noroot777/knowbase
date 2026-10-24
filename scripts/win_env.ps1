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

function Show-EgressLines {
    <#  起服务 / 看状态时那两三行:可信解析开没开、端点是什么、出口代理走谁。

        **整段转交给 `kb.feeds.egress.summary()`,这一侧一个字都不判**(口径同
        `scripts/web_env.sh` 的 `egress_lines`:它也是整段交给同一个函数)。原先这儿
        自己读环境变量、再用一条正则把端点剪到主机名,两处都错:

          - **那条正则不脱敏凭证。** `^([a-z]+://[^/?]+).*` 留下的是整个 netloc,于是
            `https://user:s3cret@doh.example/x` 原样进终端、进 runtime_logs —— 而这一行
            存在的全部理由就是别把凭证写进日志。`egress.mask_endpoint` 是**从解析结果
            重拼**的,`user:pass@` 一律剪掉,连没有协议头、也没有 `/` 的
            `doh.example?token=…` 都剪(那一类恰恰最常带 token)。
          - **只认环境变量就会答错。** 端点也可以配在设置里(面板「出口与解析」那块),
            那时环境变量是空的、服务其实开着,而这边照旧打一行「关」。
            `egress.endpoint()` 两处都认,还顺带说清这一项来自哪里。

        原先那个单独的「配了 DoH 却探不到代理」提示也一并收掉了:那句判断本来就在
        `summary()` 里(而且只在端点开着时打),在这边再写一遍就是同一个问题判两处。

        **跟 bash 唯一真的不一样的地方:读不到服务进程自己那份环境。**
        `egress_lines "$pid"` 在 POSIX 上拿的是跑着那个进程的环境;Windows 上要
        NtQueryInformationProcess + ReadProcessMemory 才办得到。所以这几行按
        **设置文件 + 当前 shell** 算 —— 跟 bash 读不到进程环境时那条降级路径一个
        口径(它那时也只说「下面只按设置文件算」)。后果是:起服务时临时设了
        `$env:KB_FEED_DOH`、之后在一个新终端看 status,这一行会说「关」。

        **输出必须落文件、按 UTF-8 读回来。** PowerShell 解码原生程序的 stdout 用的是
        `[Console]::OutputEncoding`(这台机器上是 936),而这几行全是中文 —— 直接接
        stdout 会把它们拆成乱码(同 `Invoke-Api` 那条,病根一样)。

        **先看退出码再决定要不要说话:** 只挡 stderr 不够,一个半路失败的解释器照旧
        可能在 stdout 上留下半行东西,原样打出来像是出口配置出了问题。一行都没打是
        正常末态(口径同 web_env.sh 末尾那两处)。  #>
    $out = [IO.Path]::GetTempFileName()
    try {
        # **这一段 python 里不许出现双引号。** PowerShell 5.1 把原生程序参数里的 `"`
        # 吞掉:实测 `open(sys.argv[1], "w", encoding="utf-8")` 到了 python 手里成了
        # `open(sys.argv[1], w, encoding=utf-8)`,报一句 SyntaxError —— 而上面那个
        # `2>$null` 正好把它藏起来,表现成**这几行一声不响地没了**(本轮真踩了一次)。
        # 所以字符串一律单引号(PS 里写成两个单引号),换行用 `chr(10)` 而不是 `\n`。
        $code = 'import sys; from kb.feeds import egress; ' +
                'open(sys.argv[1], ''w'', encoding=''utf-8'').' +
                'write(chr(10).join(egress.summary()))'
        # **`$LASTEXITCODE` 在「这个会话里还没成功跑过原生程序」时是未定义的**,而
        # StrictMode 下读未定义变量会抛。于是一个**起不来**的解释器(半途建坏的
        # .venv 就是这样,本任务里真遇到过)会让 `status` 死在一句指着
        # `$LASTEXITCODE` 的红上 —— 跟出口配置毫无关系,也跟那个坏掉的 venv 不沾边。
        # 实测:解释器压根没启动时 PowerShell 连 `$LASTEXITCODE` 都不设(不是设成
        # 非 0)。所以先把它摆成 `$null`,再按「有没有被设上」判。
        $global:LASTEXITCODE = $null
        $null = Invoke-ProjectPython @('-c', $code, $out) 2>$null
        if ($null -eq $LASTEXITCODE -or $LASTEXITCODE -ne 0) { return }
        $utf8 = New-Object System.Text.UTF8Encoding $false
        foreach ($line in ([IO.File]::ReadAllText($out, $utf8) -split "`n")) {
            $t = ([string]$line).Trim()
            if ($t) { Write-Note "[web] $t" }
        }
    } finally {
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
    }
}
