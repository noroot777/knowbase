# kb-web 这个服务自己的起停(Windows)。`kb.sh` 里 web_prepare_env / web_start /
# web_stop / web_status 那一节的对等物,由 kb.ps1 source。
#
# **拆成单独一份是因为 kb.ps1 顶到了 700 行那条线**(全局约定)。分界线是:
# `win_proc.ps1` 管的是「进程和端口这类事情通用怎么问、怎么停」,这一份管的是
# 「**我们这一个**服务怎么起、什么时候不许起、停之前要先问谁」—— 后者全是业务判据
# (管道在不在跑、环境装没装、端口被谁占着),前者一条业务都不认识。
#
# 这一节里每一处「问不出来」都单独有一档,不许压成「没在跑」:口径和事故形态写在
# win_proc.ps1 开头那节,完整语境见 docs/decisions/windows-entry.md。

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

function Get-WebState {
    $state = @{ Name = 'unknown'; Why = ''; Pids = @(); LegacyPid = 0; LegacyToken = '' }
    try { $service = Get-ServiceState $script:WebPid $script:Port }
    catch { $state.Why = $_.Exception.Message; return $state }
    $state.Pids = @($service.Pids)
    $state.LegacyPid = $service.LegacyPid
    $state.LegacyToken = $service.LegacyToken
    if (@($service.Owners).Count -gt 0 -and -not $service.Owned) {
        $state.Name = 'conflict'
        $state.Why = "端口 $($script:Port) 被未登记的进程占着"
        return $state
    }
    if ($state.Pids.Count -eq 0) {
        $state.Name = 'stopped'
        $state.Why = '受管理进程已全部结束'
        return $state
    }
    $state.Name = if ($service.RootAlive) { 'starting' } else { 'residual' }
    $state.Why = if ($service.RootAlive) { '进程仍在，但服务尚未就绪' } else { '根进程已退出，仍有受管理的后代' }
    if (-not $service.Owned) { return $state }
    try {
        $response = Invoke-Api -Path '/api/health'
        if ($response.Code -ne 200) { $state.Why = '端口已监听，但健康检查未通过'; return $state }
        $health = $response.Body | ConvertFrom-Json
        if ((Get-Prop $health 'status' '') -cne 'ok') {
            $state.Why = '健康检查未返回 status=ok'
            return $state
        }
    } catch { $state.Why = "健康检查失败:$($_.Exception.Message)"; return $state }
    try { $confirmed = Get-ServiceState $script:WebPid $script:Port }
    catch { $state.Name = 'unknown'; $state.Why = $_.Exception.Message; return $state }
    if (-not $confirmed.Owned) {
        $state.Name = 'unknown'
        $state.Why = '健康检查后端口归属发生变化'
        return $state
    }
    $state.Name = 'ready'
    $state.Why = ''
    return $state
}

function Wait-WebReady {
    param([int]$TimeoutSeconds = 20)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $state = Get-WebState
        if ($state.Name -ne 'starting' -or [DateTime]::UtcNow -ge $deadline) { return $state }
        Start-Sleep -Milliseconds 500
    } while ($true)
}

function Start-Web {
    param([int]$TimeoutSeconds = 20)
    $state = Get-WebState
    try {
        for ($attempt = 0; $attempt -lt 3 -and $state.Name -eq 'stopped' -and $state.LegacyPid -gt 0; $attempt++) {
            $null = Invoke-LegacyMigration $script:WebPid $script:Port $state.LegacyToken
            $state = Get-WebState
        }
        if ($state.Name -eq 'stopped' -and $state.LegacyPid -gt 0) {
            throw '旧实例记录持续变化，未启动新实例；请稍后重试'
        }
    } catch { Die $_.Exception.Message }
    if ($state.Name -eq 'ready') {
        Write-Note "[web] 已在 $($script:Api)"
        Show-CodeStale; Show-EgressLines; Show-GraphStale
        return
    }
    if ($state.Name -in @('unknown', 'conflict', 'residual')) {
        Die "kb-web 未就绪:$($state.Why)。未启动新实例，已有记录和进程保留；请按具体原因处理后重试。"
    }
    if (-not (Test-Path -LiteralPath $script:Curl -PathType Leaf)) {
        Die "找不到 $($script:Curl) —— 这个入口靠系统自带的 curl 跟面板说话(Win10 1803+ 都有)"
    }
    if ($state.Name -eq 'stopped') {
        Initialize-WebEnv
        $envPairs = @(Build-WebEnv) + @("PORT=$($script:Port)")
        $spawn = @($script:WinDaemon, 'spawn', $script:WebPid,
                   (Join-Path $script:LogDir 'web.log'), $script:Root)
        foreach ($e in $envPairs) { $spawn += @('--env', $e) }
        $spawn += @('--', (Get-Uv), 'run', '--offline', 'kb-web')
        $null = Invoke-BootPython $spawn
        if ($LASTEXITCODE -ne 0) { Die 'win_daemon.py spawn 没起成(上面有原因)' }
    }
    $state = Wait-WebReady -TimeoutSeconds $TimeoutSeconds
    if ($state.Name -ne 'ready') {
        Die "kb-web 未就绪:$($state.Why)。看 $(Join-Path $script:LogDir 'web.log')；实例记录和进程已保留，未自动强杀或另起一份。"
    }
    Write-Note "[web] $($script:Api)"
    Show-EgressLines
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
    try { Stop-Service_ 'kb-web' $script:WebPid $script:Port $script:WebPat }
    catch { Die $_.Exception.Message }
}

function Get-WebStatus {
    $state = Get-WebState
    switch ($state.Name) {
        'ready' {
            Write-Output "kb-web    运行中 ($($script:Api))"
            Show-CodeStale
            Show-EgressLines
        }
        'unknown' { Write-Output "kb-web    问不出来($($state.Why))" }
        'stopped' { Write-Output 'kb-web    未运行' }
        'starting' { Write-Output "kb-web    未就绪($($state.Why)，实例记录已保留)" }
        'residual' { Write-Output "kb-web    残留进程($($state.Why)，实例记录已保留)" }
        'conflict' { Write-Output "kb-web    未就绪($($state.Why)，未接管或清理)" }
    }
}
