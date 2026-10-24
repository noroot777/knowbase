function Test-NoSuchObject {
    param($Err)
    try {
        return ([string]$Err.FullyQualifiedErrorId -eq
                'CmdletizationQuery_NotFound,Get-NetTCPConnection' -and
                [string]$Err.CategoryInfo.Category -eq 'ObjectNotFound')
    } catch { return $false }
}

function Get-PortOwners {
    param([int]$P)
    if ($P -le 0) { return @{ Known = $true; Owners = @(); Why = '' } }
    try {
        $owners = @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction Stop |
                    Select-Object -ExpandProperty OwningProcess -Unique |
                    ForEach-Object { [int]$_ })
        return @{ Known = $true; Owners = $owners; Why = '' }
    } catch {
        if (Test-NoSuchObject $_) { return @{ Known = $true; Owners = @(); Why = '' } }
        return @{ Known = $false; Owners = @(); Why = [string]$_.Exception.Message }
    }
}

function Get-PortState {
    param([int]$P)
    $query = Get-PortOwners $P
    if (-not $query.Known) { return 'unknown' }
    if (@($query.Owners).Count -gt 0) { return 'busy' }
    return 'free'
}

function Invoke-Instance {
    param([string]$Action, [string]$PidFile, [string[]]$Extra = @())
    $python = Get-BootPython
    $result = Invoke-Native $python.Exe (@($python.Pre) + @('-X', 'utf8', $script:WinDaemon, $Action, $PidFile) + $Extra)
    if ($result.Code -ne 0) { throw $result.Text }
    try { return ($result.Text | ConvertFrom-Json) }
    catch { throw "实例操作没有返回有效 JSON，保留记录:$PidFile" }
}

function Get-ServiceState {
    param([string]$PidFile, [int]$Port)
    $query = Get-PortOwners $Port
    if (-not $query.Known) { throw "问不出端口 $Port 上有没有人:$($query.Why)" }
    $members = @()
    $rootAlive = $false
    $legacyPid = 0
    $legacyToken = ''
    if ($PidFile -and (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        $instance = Invoke-Instance 'state' $PidFile
        $members = @(Get-Prop $instance 'pids' @())
        $rootAlive = Get-Prop $instance 'root_alive' $null
        if ($rootAlive -isnot [bool]) { throw '实例状态缺少有效的根进程状态，记录已保留' }
        $legacyPid = Get-Prop $instance 'legacy_pid' 0
        if ($legacyPid -gt 0) {
            $legacyToken = Get-Prop $instance 'legacy_token' ''
            if ($legacyToken -cnotmatch '^[0-9a-f]{64}$') { throw '旧记录缺少有效内容标识，记录已保留' }
            $legacyAlive = Get-Prop $instance 'legacy_alive' $null
            if ($legacyAlive -isnot [bool]) { throw '无法确认旧 PID 是否已退出，记录已保留' }
            if ($legacyAlive -or @($query.Owners).Count -gt 0) {
                $listeners = if (@($query.Owners).Count -gt 0) { @($query.Owners) -join ', ' } else { '无' }
                throw "$($PidFile): 旧版 PID 记录（PID $legacyPid；端口 $Port 的监听 PID: $listeners），不能凭裸 PID 接管或终止。请先关闭旧服务（原窗口按 Ctrl+C；无窗口时可重启 Windows），再运行 .\kb.cmd restart；确认旧 PID 已退出且端口空闲后会自动备份旧记录。--force 不会绕过身份检查。"
            }
        }
    }
    $owned = @($query.Owners).Count -gt 0
    foreach ($owner in @($query.Owners)) {
        if ($members -notcontains $owner) { $owned = $false }
    }
    return @{ Pids = $members; Owners = @($query.Owners); Owned = $owned; RootAlive = $rootAlive; LegacyPid = $legacyPid; LegacyToken = $legacyToken }
}

function Invoke-LegacyMigration {
    param([string]$PidFile, [int]$Port, [string]$Expected)
    $result = Invoke-Instance 'migrate' $PidFile @($Expected, [string]$Port)
    $changed = Get-Prop $result 'changed' $null
    if ($changed -isnot [bool]) { throw '迁移结果缺少有效的记录变更状态，请重新查询实例' }
    $backup = Get-Prop $result 'legacy_backup' ''
    if (-not $changed -and -not $backup) { throw '迁移结果缺少备份记录，请重新查询实例' }
    if ($backup) { Write-Note "已备份退出实例的旧 PID 记录:$backup" }
    return $changed
}

function Stop-Service_ {
    param([string]$Label, [string]$PidFile, [int]$Port, [string]$Pattern)
    $service = Get-ServiceState $PidFile $Port
    if ($service.LegacyPid -gt 0) {
        if (Invoke-LegacyMigration $PidFile $Port $service.LegacyToken) {
            throw '实例记录已改变，未停止任何新实例；请重新查询后重试'
        }
        return
    }
    if (@($service.Pids).Count -gt 0) {
        Write-Note "[$Label] 停止实例 ($(@($service.Pids) -join ' '))"
    } elseif (@($service.Owners).Count -gt 0) {
        throw "[$Label] 端口 $Port 被未登记的进程占用，不能凭命令名认领，没动它，记录已保留"
    } else {
        Write-Note "[$Label] 本来就没在跑"
    }
    $stopped = Invoke-Instance 'stop' $PidFile
    if (@($stopped.pids).Count -ne 0) { throw "[$Label] 受管理进程未结束，记录已保留" }
}
