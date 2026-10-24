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
    param([string]$Action, [string]$PidFile)
    $python = Get-BootPython
    $result = Invoke-Native $python.Exe (@($python.Pre) + @('-X', 'utf8', $script:WinDaemon, $Action, $PidFile))
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
    if ($PidFile -and (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        $instance = Invoke-Instance 'state' $PidFile
        $members = @(Get-Prop $instance 'pids' @())
        $rootAlive = Get-Prop $instance 'root_alive' $null
        if ($rootAlive -isnot [bool]) { throw '实例状态缺少有效的根进程状态，记录已保留' }
    }
    $owned = @($query.Owners).Count -gt 0
    foreach ($owner in @($query.Owners)) {
        if ($members -notcontains $owner) { $owned = $false }
    }
    return @{ Pids = $members; Owners = @($query.Owners); Owned = $owned; RootAlive = $rootAlive }
}

function Stop-Service_ {
    param([string]$Label, [string]$PidFile, [int]$Port, [string]$Pattern)
    $query = Get-PortOwners $Port
    if (-not $query.Known) {
        throw "[$Label] 问不出端口 $Port 状态，没敢动，实例记录已保留:$($query.Why)"
    }
    $instance = Invoke-Instance 'state' $PidFile
    if (@($instance.pids).Count -gt 0) {
        Write-Note "[$Label] 停止实例 ($(@($instance.pids) -join ' '))"
    } elseif (@($query.Owners).Count -gt 0) {
        throw "[$Label] 端口 $Port 被未登记的进程占用，不能凭命令名认领，没动它，记录已保留"
    } else {
        Write-Note "[$Label] 本来就没在跑"
    }
    $stopped = Invoke-Instance 'stop' $PidFile
    if (@($stopped.pids).Count -ne 0) { throw "[$Label] 受管理进程未结束，记录已保留" }
}
