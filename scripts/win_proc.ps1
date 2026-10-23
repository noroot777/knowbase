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

# ── 进程 ──────────────────────────────────────────────────────────────────────

function Get-ProcTable {
    <#  全部进程的 pid → @{Parent; Name; Cmd}。

        **一次取全表而不是逐个问。** `Get-CimInstance` 单次调用约 100ms,而
        collect_tree / top_owner 要走十几个 pid —— 逐个问就是十几次 WMI 往返,
        停一次服务要等好几秒。另一个好处是这一份是**同一时刻的快照**:逐个问的话
        父子关系可能在中间变了,树就收不全。  #>
    param([switch]$Fresh)
    if (-not $Fresh -and $null -ne $script:ProcTable) { return $script:ProcTable }
    $t = @{}
    foreach ($p in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $t[[int]$p.ProcessId] = @{
            Parent = [int]$p.ParentProcessId
            Name   = [string]$p.Name
            Cmd    = [string]$p.CommandLine          # 受保护的进程这里会是空
        }
    }
    $script:ProcTable = $t
    return $t
}

function Test-Alive { param([int]$Id)
    if ($Id -le 0) { return $false }
    return ($null -ne (Get-Process -Id $Id -ErrorAction SilentlyContinue))
}

function Read-PidFile {
    <#  pidfile 里那个号。读不到、不是数字就返回空 —— 口径同 kb.sh 的 read_pid。  #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    if (-not $raw) { return @() }
    $d = ($raw -replace '[^0-9]', '')
    if (-not $d) { return @() }
    return @([int]$d)
}

function Get-PortOwners { param([int]$P)
    if ($P -le 0) { return @() }
    try {
        return @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction Stop |
                 Select-Object -ExpandProperty OwningProcess -Unique |
                 ForEach-Object { [int]$_ })
    } catch { return @() }
}

# **`@()` 不能省。** 空数组从函数返回时 PowerShell 会把它拆成 `$null`,而
# StrictMode 下对 `$null` 取 `.Count` 当场报错 —— 实测 `status` 因此打一行红、
# 接着**拿一个错的答案往下走**(报「kb-web 未运行」,其实是没问出来)。
function Test-PortBusy { param([int]$P) return (@(Get-PortOwners $P).Count -gt 0) }

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
        请求 POST 给一个根本不是 kb-web 的进程,而这边一切看着正常。  #>
    param([string]$PidFile, [int]$Port, [string]$Pattern)
    $raw = @()
    if ($Port -gt 0) { $raw = @(Get-PortOwners $Port) }
    if ($raw.Count -eq 0) { $raw = @(Read-PidFile $PidFile) }
    if ($raw.Count -eq 0) { return @() }
    $null = Get-ProcTable -Fresh
    $out = @()
    foreach ($p in ($raw | Sort-Object -Unique)) {
        if (-not (Test-Alive $p)) { continue }
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
        所以中间那一档省了,等待时间按 kb.sh 的 INT 那一档给(5 秒)。  #>
    param([int]$Root)
    if (-not (Test-Alive $Root)) { return }
    $null = Get-ProcTable -Fresh
    $pids = @(Get-ProcTree $Root)
    # 送给服务那个控制台上的所有进程 = kb.sh 里对整棵树逐个 kill -INT
    $null = Invoke-BootPython @($script:WinDaemon, 'ctrlbreak', "$Root") 2>$null
    $n = 0
    while ($n -lt 25 -and (Test-Alive $Root)) { Start-Sleep -Milliseconds 200; $n++ }
    if (-not (Test-Alive $Root)) { return }
    # 还在。**后代先走**:先杀父进程的话,它可能在死前又拉起一个我们已经不认识的子进程
    foreach ($p in ($pids | Select-Object -Last ($pids.Count) | Sort-Object -Descending)) {
        if ($p -eq $Root) { continue }
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $Root -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

function Stop-Service_ {
    <#  `stop_service` 的对等物。  #>
    param([string]$Label, [string]$PidFile, [int]$Port, [string]$Pattern)
    $pids = @(Get-ServicePids $PidFile $Port $Pattern)
    if ($pids.Count -eq 0) {
        if ($Port -gt 0 -and (Test-PortBusy $Port)) {
            # 端口有人占,但不是我们的进程。**不动它**:这条路径在 kb.sh 的历史上
            # 曾是无差别 kill -9,而占着 8787 的完全可能是另一个人的东西。
            Write-Note "[$Label] 没在跑。端口 $Port 被别的进程占着,没动它:"
            foreach ($o in (Get-PortOwners $Port)) {
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
    while ($Port -gt 0 -and (Test-PortBusy $Port) -and $n -lt 20) {
        Start-Sleep -Milliseconds 200; $n++
    }
    if ($Port -gt 0 -and (Test-PortBusy $Port)) {
        # 还占着就再查一遍是不是我们的残留;是才强杀,不是就放着(见上)
        $pids = @(Get-ServicePids '' $Port $Pattern)
        if ($pids.Count -gt 0) {
            Write-Note "[$Label] 端口 $Port 仍被占用,强杀"
            foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
        }
    }
}

