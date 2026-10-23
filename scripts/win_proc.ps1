# 进程那一族(Windows)。`kb.sh` 里 alive / port_busy / top_owner / service_pids /
# collect_tree / kill_tree / stop_service 那一整段的对等物,由 kb.ps1 source。
#
# **拆成单独一份是因为 kb.ps1 顶到了 700 行那条线**(全局约定)。这一段也正是整个
# Windows 入口里**唯一需要换实现而不是换写法**的地方:POSIX 那边靠 lsof / ps / pgrep /
# kill,这边靠 Get-NetTCPConnection / Win32_Process / 控制台事件 / TerminateProcess。
#
# 判据一条都没改,只换了问法。每个函数头上那段注释写的就是它防的那个事故形态 ——
# 跨端口误杀、从子进程动手把父进程剩下、端口被别人占着却报「已在运行」。
# 完整语境见 docs/decisions/windows-entry.md。
#
# ## 这一份的总纪律:问不出来 ≠ 确定没有
#
# 本族每个「问一下系统」的函数只许答三种话:**确定没有 / 成功有结果 / 无法确定**。
# 把第三种压成第一种是本项目最不能要的那一类(元规则「判不准一律抛,不许返回确定
# 没有」),而它在这一族里格外贵 —— 停机保护、端口冲突、要不要杀一个进程,全押在
# 这几个答案上。病根只要一句 `catch { return @() }`(CIM 在高负载下偶发失败、装了
# 别家网络过滤驱动时 Get-NetTCPConnection 直接报错),后果就是:
#   - `Get-PipelineState` 落到「空闲」,`stop` 把正在跑的同步砍在半路(保护全失效);
#   - `start` 以为端口空着,往一个已被占用的端口上再起一份;
#   - `stop` 以为「本来就没在跑」,顺手把 pidfile 删了 —— 连下次找回来的线索也没了。
# 所以**能转成空的只有「问到了、确实没有匹配」那一种**,别的失败一律 `Known = $false`
# 或者抛。谁要把这几个函数重新包成一个返回布尔的 `Test-XXX`,这条纪律就又没了 ——
# 布尔只装得下两种话。

# ── 问系统:三态 ──────────────────────────────────────────────────────────────

function Test-NoSuchObject {
    <#  这个错误记录是不是「问到了,确实没有」。

        `Get-NetTCPConnection` 在**一个匹配都没有**时不返回空,而是抛:实测
        `FullyQualifiedErrorId = CmdletizationQuery_NotFound,Get-NetTCPConnection`、
        `CategoryInfo.Category = ObjectNotFound`。只有这一种可以转成空集 ——
        权限不足、CIM 坏了、驱动拦下来,都属于「无法确定」。  #>
    param($Err)
    try {
        if ([string]$Err.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*') { return $true }
        return ([string]$Err.CategoryInfo.Category -eq 'ObjectNotFound')
    } catch { return $false }
}

# ── 进程 ──────────────────────────────────────────────────────────────────────

function Get-ProcTable {
    <#  全部进程的 pid → @{Parent; Name; Cmd; Created}。

        **一次取全表而不是逐个问。** `Get-CimInstance` 单次调用约 100ms,而
        collect_tree / top_owner 要走十几个 pid —— 逐个问就是十几次 WMI 往返,
        停一次服务要等好几秒。另一个好处是这一份是**同一时刻的快照**:逐个问的话
        父子关系可能在中间变了,树就收不全。

        **问不出来就抛,不返回空表。** 空表会让上面的判断答出「这个号不存在」
        (`Get-ProcTree` 收出一棵空树、`Test-PidMatches` 答「不像」)—— 于是 stop
        什么都没杀却报成功。抛出去的话,话里写着「问不出进程表」,指得到真因。

        **查成了的话表里一定有本进程自己**(实测;正在问的这个 PowerShell 也是一个
        进程)。没有就说明这一份不完整,照样当「无法确定」—— 这是反过来钉的那一边:
        不止防「报错」,也防「没报错、却答出一份空的或残的」。  #>
    param([switch]$Fresh)
    if (-not $Fresh -and $null -ne $script:ProcTable) { return $script:ProcTable }
    $rows = @()
    try {
        $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        $script:ProcTable = $null
        throw "问不出进程表(Win32_Process):$($_.Exception.Message)"
    }
    $t = @{}
    foreach ($p in $rows) {
        $t[[int]$p.ProcessId] = @{
            Parent  = [int]$p.ParentProcessId
            Name    = [string]$p.Name
            Cmd     = [string]$p.CommandLine          # 受保护的进程这里会是空
            Created = $p.CreationDate                 # 同上;核身份用,见 Test-SameProc
        }
    }
    if (-not $t.ContainsKey($PID)) {
        $script:ProcTable = $null
        throw '进程表里连本进程都没有 —— 这一份不完整,不敢拿它当「某个号不存在」的依据'
    }
    $script:ProcTable = $t
    return $t
}

function Test-Alive { param([int]$Id)
    if ($Id -le 0) { return $false }
    return ($null -ne (Get-Process -Id $Id -ErrorAction SilentlyContinue))
}

function Get-ProcStart {
    <#  一个进程的起跑时间。**问不出来返回 $null** —— 那是「无法确定」,不是「没有」,
        两个调用方都据此**拒绝动手**(见 Test-SameProc / Test-RecordedProc)。  #>
    param([int]$Id)
    if ($Id -le 0) { return $null }
    $t = Get-ProcTable
    if (-not $t.ContainsKey($Id)) { return $null }
    $c = $t[$Id].Created
    if ($c -is [datetime]) { return $c }
    return $null
}

function Test-SameProc {
    <#  那个号上现在的进程,还是不是快照里的那一个 —— 动手之前最后一道。

        **pid 是会被回收的号。** 收树和真正动手之间隔着一次 CTRL_BREAK 和最多 5 秒
        的等待,这中间那些号完全可能已经被系统分给别人(服务自己的子进程退得最快)。
        只按号杀的话,杀的是一个跟我们毫无关系的进程 —— 跨端口误杀的同一个病根,
        只是换成了跨时间。

        起跑时间有一头问不出来就**不认**(于是不杀):判不准一律拒,因为认下来的
        后果是 TerminateProcess,不可挽回。  #>
    param([int]$Id, $Created)
    if ($Id -le 0 -or $null -eq $Created) { return $false }
    $live = $null
    try { $live = (Get-Process -Id $Id -ErrorAction Stop).StartTime } catch { return $false }
    if ($null -eq $live) { return $false }
    # CIM 的 CreationDate 只到微秒、Process.StartTime 到 100ns(实测差 5e-07 秒),
    # 所以不能按相等比。1 秒的容差盖得住这点误差,又分得开「号被回收」—— 新主必然
    # 起跑在原主死掉之后,而原主在快照那一刻还活着。
    return ([math]::Abs(($live - $Created).TotalSeconds) -lt 1)
}

function Read-PidFile {
    <#  pidfile 里那个号,外加**这份记录是什么时候写下的**(核身份要用,见
        Get-ServicePids)。读不到、不是数字就返回空 —— 口径同 kb.sh 的 read_pid。
        返回 `@{ Pids; At }`。

        **只认第一行。** 原先是把整份内容里的非数字字符全删掉再转数字:哪天这份
        记录多写一行,两行的数字就被**接成一个号**。那个号大概率不存在,于是 stop
        一声不响地什么都没杀,而 pidfile 照旧被删掉。

        **先 TryParse 再判范围,不许直接 `[int]` / `[int64]` 转。** 一份被写坏的
        记录(满屏数字)直接转会抛 OverflowException / InvalidCastFromStringToInteger,
        而顶上是 `Stop` —— 一句 status 整条红掉,话里是「值对于 Int64 太大或太小」,
        指不到 pidfile。实测:40 个 9 连 `[int64]` 都装不下(回归钉着这一条)。  #>
    param([string]$Path)
    $none = @{ Pids = @(); At = $null }
    if (-not $Path) { return $none }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $none }
    $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    if (-not $raw) { return $none }
    $first = ([string](($raw -split "`n")[0])).Trim()
    if ($first -notmatch '^[0-9]+$') { return $none }
    $n = [int64]0
    if (-not [int64]::TryParse($first, [ref]$n)) { return $none }
    if ($n -le 0 -or $n -gt [int]::MaxValue) { return $none }
    $at = $null
    try { $at = (Get-Item -LiteralPath $Path -Force).LastWriteTime } catch { $at = $null }
    return @{ Pids = @([int]$n); At = $at }
}

function Test-RecordedProc {
    <#  这个号上现在的进程,是不是 pidfile 写下的那一个。

        判据:**它的起跑时间不晚于那份记录的写入时间**。守护进程是先把服务拉起来、
        再写下这份记录的,所以真身必然满足;号被回收之后的新主必然破(它起跑在原主
        死掉之后,而原主在写记录那一刻还活着)。

        宽 5 秒:pidfile 的 mtime 在某些文件系统上只有 2 秒精度,而「拉起服务」到
        「写下记录」之间只隔几毫秒。号被回收是分钟量级的事,这点宽容造不出巧合。
        两头有一个问不出来就不认 —— 同 Test-SameProc,认下来的后果是杀。  #>
    param([int]$Id, $At)
    if ($null -eq $At) { return $false }
    $born = Get-ProcStart $Id
    if ($null -eq $born) { return $false }
    return ($born -le $At.AddSeconds(5))
}

# ── 端口 ──────────────────────────────────────────────────────────────────────

function Get-PortOwners {
    <#  监听这个端口的进程号。返回 `@{ Known; Owners; Why }` —— **三态,不是布尔**
        (为什么见本文件开头那节)。`Known = $false` 时 `Why` 是问不出来的原因,
        给调用方往话里带。  #>
    param([int]$P)
    if ($P -le 0) { return @{ Known = $true; Owners = @(); Why = '' } }
    try {
        $o = @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction Stop |
               Select-Object -ExpandProperty OwningProcess -Unique |
               ForEach-Object { [int]$_ })
        return @{ Known = $true; Owners = $o; Why = '' }
    } catch {
        if (Test-NoSuchObject $_) { return @{ Known = $true; Owners = @(); Why = '' } }
        return @{ Known = $false; Owners = @(); Why = [string]$_.Exception.Message }
    }
}

function Get-PortState {
    <#  `'busy'` | `'free'` | `'unknown'`。只要状态不要原因的地方用它(循环条件)。

        **`@()` 不能省**(下面那句 `.Count`)。空数组从函数返回时 PowerShell 会把它
        拆成 `$null`,而 StrictMode 下对 `$null` 取 `.Count` 当场报错 —— 实测
        `status` 因此打一行红、接着**拿一个错的答案往下走**(报「kb-web 未运行」,
        其实是没问出来)。那正是本文件开头那条纪律要防的形态。  #>
    param([int]$P)
    $q = Get-PortOwners $P
    if (-not $q.Known) { return 'unknown' }
    if (@($q.Owners).Count -gt 0) { return 'busy' }
    return 'free'
}

#: 碰到这些就停着往上走:交互 shell 和终端的命令行里可能正好带着服务的字样
#: (比如你在一条命令里提到 kb-web),再往上走就会把自己的终端当成服务杀掉。
$script:ShellNames = @('powershell.exe', 'pwsh.exe', 'cmd.exe', 'conhost.exe',
                       'WindowsTerminal.exe', 'OpenConsole.exe', 'explorer.exe',
                       'bash.exe', 'sh.exe', 'mintty.exe', 'git-bash.exe')

function Get-TopOwner {
    <#  从一个进程往上找到这个服务的「头」。

        端口给的常常是子进程(`uv run kb-web` 起的实际 server 就是),而 Stop-ProcTree
        只往下走,从子进程动手会把父进程剩在那儿(端口没释放,restart 于是失败)。
        一路向上,直到父进程不再长得像这个服务为止。

        **父进程读不到就停在当前这个。** win_daemon.py 那个引导进程写完 pidfile 就退了,
        于是服务的 ParentProcessId 指着一个已经没了的号 —— 读不到就停,正好停在服务
        自己身上(kb.sh 那边 `ps -o command= -p <死号>` 返回空、grep 失败、break,
        是同一个形态)。  #>
    param([int]$Id, [string]$Pattern)
    $t = Get-ProcTable
    $cur = $Id
    for ($hop = 0; $hop -lt 32; $hop++) {       # 上限兜一下环(pid 复用能造出环)
        if (-not $t.ContainsKey($cur)) { break }
        $parent = $t[$cur].Parent
        if ($parent -le 4) { break }            # 0 / 4 = Idle / System
        if (-not $t.ContainsKey($parent)) { break }
        if (-not (Test-Alive $parent)) { break }
        $pcmd = $t[$parent].Cmd
        if (-not $pcmd -or $pcmd -notlike "*$Pattern*") { break }
        if ($script:ShellNames -contains $t[$parent].Name) { break }
        $cur = $parent
    }
    return $cur
}

function Test-PidMatches {
    <#  这个 pid 的命令行像不像目标服务。  #>
    param([int]$Id, [string]$Pattern)
    $t = Get-ProcTable
    if (-not $t.ContainsKey($Id)) { return $false }
    $c = $t[$Id].Cmd
    return ($c -and ($c -like "*$Pattern*"))
}

function Get-ServicePids {
    <#  一个服务当前的进程。

        刻意【不】用「按名字找全部进程」兜底:名字是全局匹配的,分不清端口 ——
        8799 上做一次 restart,会把 8787 上的生产实例一起杀了。端口是唯一能把实例
        分开的东西。

        所以端口说了算:有人占着这个端口,就以占着的那个为准,pidfile 不插话 ——
        它可能是上次异常退出留下的陈迹,号早被系统回收给了别的进程。只有端口上一个人
        都没有时才回头看 pidfile:那说明服务要么还在启动、要么挂住了没释放端口,
        那份记录(名字里带端口,只可能是本端口这一份写的)才有用。

        两条路来的 pid 都要核命令行,而且核的是**往上走到头**的那个。不核则更糟:
        8787 被别的东西占着时 start 会说「已在运行」却什么都没起,接着 sync 把同步
        请求 POST 给一个根本不是 kb-web 的进程,而这边一切看着正常。

        **pidfile 那条路还要再核一次身份,光核命令行不够。** 号被回收给另一份 kb-web
        实例时命令行一模一样(就是 `kb-web`),于是 stop 杀掉别人那一份 —— 跨端口误杀
        的同一个病根,换成了跨时间。判据见 Test-RecordedProc。**端口那条路不用核**:
        「正占着我们这个端口」是系统现在就答出来的事实,号的新旧无从错位。

        **问不出端口状态时抛,不返回空。** 返回空会被每个调用方读成「没在跑」:
        start 再起一份、stop 删掉 pidfile 还报「本来就没在跑」。  #>
    param([string]$PidFile, [int]$Port, [string]$Pattern)
    $raw = @()
    $fromFile = $false
    $at = $null
    if ($Port -gt 0) {
        $q = Get-PortOwners $Port
        if (-not $q.Known) { throw "问不出端口 $Port 上有没有人:$($q.Why)" }
        $raw = @($q.Owners)
    }
    if ($raw.Count -eq 0 -and $PidFile) {
        $rec = Read-PidFile $PidFile
        $raw = @($rec.Pids)
        $at = $rec.At
        $fromFile = $true
    }
    if ($raw.Count -eq 0) { return @() }
    $null = Get-ProcTable -Fresh
    $out = @()
    foreach ($p in ($raw | Sort-Object -Unique)) {
        if (-not (Test-Alive $p)) { continue }
        if ($fromFile -and -not (Test-RecordedProc $p $at)) { continue }
        $owner = Get-TopOwner $p $Pattern
        if (Test-PidMatches $owner $Pattern) { $out += $owner }
    }
    return @($out | Sort-Object -Unique)
}

function Get-ProcTree {
    <#  先序收集一个进程及其全部后代。**必须趁父进程还活着时调用**:它一旦先死,
        后代的 ParentProcessId 就指着一个没了的号,再也追不回来了。  #>
    param([int]$Id, [int]$Depth = 0)
    if ($Depth -gt 16) { return @() }
    $t = Get-ProcTable
    $out = @($Id)
    foreach ($k in @($t.Keys)) {
        if ($k -ne $Id -and $t[$k].Parent -eq $Id) {
            $out += (Get-ProcTree $k ($Depth + 1))
        }
    }
    return $out
}

function Stop-ProcTree {
    <#  CTRL_BREAK → 硬杀。先送控制台事件是给一次收拾自己的机会:服务可能正跑着
        同步,直接硬杀会把 sqlite 的写事务和子进程甩在那儿。

        **Windows 上只有两级**(kb.sh 那边是 INT → TERM → KILL 三级):没有可捕获的
        SIGTERM 这种中间档,`Stop-Process -Force` 就是 TerminateProcess、不可捕获。
        所以中间那一档省了,等待时间按 kb.sh 的 INT 那一档给(5 秒)。

        **根退干净了也要把后代扫一遍。** 原先是「根没了就直接返回」,而那恰恰是最
        常见的那条路:uvicorn 收到 CTRL_BREAK 自己走 shutdown、从容退出,于是返回得
        特别早。可它拉起的东西不跟着走 —— 向量子进程、CDP 起的 chrome-headless-shell、
        采集器子进程都是独立进程,父进程退了只是被挂到别的父亲下面(Windows 没有
        进程组连坐)。留下来的后果是 chrome 常驻吃内存、下一轮同步撞上一个还握着
        kb.db 的残留,而 `status` 看着干干净净 —— 这正是 kb.sh 那边逐个 kill 整棵树
        要防的事,这一侧不该比它少做。

        **每个号动手之前核一遍身份**(Test-SameProc):从收树到这里隔了最多 5 秒,
        号可能已经被回收给别人了。  #>
    param([int]$Root)
    if (-not (Test-Alive $Root)) { return }
    $null = Get-ProcTable -Fresh
    $t = Get-ProcTable
    $pids = @(Get-ProcTree $Root)
    # 快照里每个号的起跑时间:动手前拿它核身份(见上)。趁现在记下来 —— 等下那个
    # 进程可能已经没了,那时谁也答不出它当初是什么时候起的。
    $born = @{}
    foreach ($p in $pids) { if ($t.ContainsKey($p)) { $born[$p] = $t[$p].Created } }
    # 送给服务那个控制台上的所有进程 = kb.sh 里对整棵树逐个 kill -INT
    $null = Invoke-BootPython @($script:WinDaemon, 'ctrlbreak', "$Root") 2>$null
    $n = 0
    while ($n -lt 25 -and (Test-Alive $Root)) { Start-Sleep -Milliseconds 200; $n++ }
    # **后代先走**:先杀父进程的话,它可能在死前又拉起一个我们已经不认识的子进程
    $rest = @($pids | Where-Object { $_ -ne $Root } | Sort-Object -Unique -Descending)
    foreach ($p in $rest) {
        if (-not (Test-Alive $p)) { continue }
        if (-not ($born.ContainsKey($p) -and (Test-SameProc $p $born[$p]))) { continue }
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }
    if (Test-Alive $Root) {
        if ($born.ContainsKey($Root) -and (Test-SameProc $Root $born[$Root])) {
            Stop-Process -Id $Root -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }
}

function Stop-Service_ {
    <#  `stop_service` 的对等物。

        **问不出端口状态时什么都不做,连 pidfile 都不删。** 那一档既不是「在跑」也
        不是「没在跑」—— 照「没在跑」处理的后果是:报一句「本来就没在跑」、把 pidfile
        删掉,于是服务还在后台跑着,而下一次连找回它的线索都没有了。  #>
    param([string]$Label, [string]$PidFile, [int]$Port, [string]$Pattern)
    # **局部名不许只靠大小写跟参数区分。** PowerShell 的变量名大小写不敏感:写
    # `$port` 拿到的就是上面那个 `[int]$Port`,于是 `'busy'` 被往 int 里塞,
    # `stop`/`restart` 当场抛 —— 而 `rc` 还是 0(实测:服务一动没动、照旧跑着,
    # 外面看着像成功)。结构守卫在 tests/test_win_entry.py。
    $state = if ($Port -gt 0) { Get-PortState $Port } else { 'free' }
    if ($state -eq 'unknown') {
        Write-Note "[$Label] 问不出端口 $Port 上有没有人,没敢动(pidfile 也留着):$((Get-PortOwners $Port).Why)"
        return
    }
    $pids = @(Get-ServicePids $PidFile $Port $Pattern)
    if ($pids.Count -eq 0) {
        if ($state -eq 'busy') {
            # 端口有人占,但不是我们的进程。**不动它**:这条路径在 kb.sh 的历史上
            # 曾是无差别 kill -9,而占着 8787 的完全可能是另一个人的东西。
            Write-Note "[$Label] 没在跑。端口 $Port 被别的进程占着,没动它:"
            foreach ($o in (Get-PortOwners $Port).Owners) {
                $p = Get-Process -Id $o -ErrorAction SilentlyContinue
                if ($p) { Write-Note ("       {0,-8} {1}" -f $p.Id, $p.ProcessName) }
            }
        } else {
            Write-Note "[$Label] 本来就没在跑"
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return
    }
    Write-Note "[$Label] 停止 $($pids -join ' ')"
    foreach ($p in $pids) { Stop-ProcTree $p }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    $n = 0
    while ($Port -gt 0 -and $n -lt 20 -and (Get-PortState $Port) -eq 'busy') {
        Start-Sleep -Milliseconds 200; $n++
    }
    # 还占着就再查一遍是不是我们的残留;是才强杀,不是就放着(见上)。问不出来的
    # 那一档同样什么都不做 —— 强杀是不可挽回的那一侧。
    if ($Port -gt 0 -and (Get-PortState $Port) -eq 'busy') {
        $pids = @(Get-ServicePids '' $Port $Pattern)
        if ($pids.Count -gt 0) {
            Write-Note "[$Label] 端口 $Port 仍被占用,强杀"
            foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
        }
    }
}
