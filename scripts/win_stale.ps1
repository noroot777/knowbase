# 「提示而不代劳」那一族(Windows)。`scripts/stale.sh` 的对等物,由 kb.ps1 source。
#
# **拆成单独一份的理由同 stale.sh**:kb.ps1 顶着 700 行那条线(全局约定)。这两个函数
# 本来就是同一件事的两面:一个看服务进程端着的代码,一个看 vault 里那三张图谱页;
# 口径也一样 —— **永远不抛、不返回失败**(抛出去会把它挂着的那条命令整个带掉),
# 读不到东西就安静。
#
# 由 kb.ps1 顶层 source 进来,所以它自己也在 Show-CodeStale 的比较范围里(被 source 的
# .ps1 决定服务进程拿到哪一份环境)。改了它要重启服务才生效 —— 那一条正是 code_stale
# 在说的事。回归:tests/test_win_entry.py。

function Show-CodeStale {
    <#  跑着的进程端着的是它启动那一刻的代码,而 kb 里有一批模块是延迟 import 的。

        Python 不热加载,所以服务运行期间一次 `git checkout` 就能让同一个进程里新旧两个
        版本的模块混跑:`kb.feeds.sync` 要等管道真跑一轮 crawl 才被 import、从磁盘读到
        **新版**,而它依赖的 `x_channels` 早以**旧版**驻留在 sys.modules 里。报出来的是
        一句指不到真因的 AttributeError,而面板上显示的是「连接失败」—— 那句话指不到
        任何跟订阅、跟网络有关的东西。完整形态见 docs/decisions/code-stale.md。

        判据是 **pidfile 比 `kb/` 下任何一个 .py 旧**:win_daemon.py 写 pidfile 的那一刻
        就是进程启动那一刻,而别处只删它、不写它。**只看 .py** —— `kb/static/` 下的
        js/css 每次请求都从磁盘读(免重启),算进来就是每改一行样式催一次重启;
        `scripts/` 和 `collector/` 同理不算,它们每次都是新起的子进程,拿的本来就是
        磁盘上那一版。**但被 source 的 .ps1 要算**:那几份决定服务进程拿到哪一份环境
        (出口变量、DoH 开关),改完不重启,跑着的进程端的还是旧那份。

        口径同 Show-GraphStale:**提示而不代劳**,也不拦。重启会把正在跑的同步断在
        半路,什么时候重启得由人定;而且新旧混跑未必每次都撞上(两个版本之间没有互相
        引用的新符号时一切照旧),拿它挡住命令比提示一句更坏。

        **「没有 pidfile 就安静」那道守卫是冗余带,别删** —— 「服务没跑」该由我们自己
        回答,不该押在某个遍历实现对缺失参照物的处理上。  #>
    try {
        if (-not (Test-Path -LiteralPath $script:WebPid -PathType Leaf)) { return }
        $ref = (Get-Item -LiteralPath $script:WebPid -Force).LastWriteTimeUtc
        $newer = $false
        # Python 侧:kb/ 下的 .py
        foreach ($f in (Get-ChildItem -LiteralPath (Join-Path $script:Root 'kb') -Recurse `
                        -File -Filter '*.py' -Force -ErrorAction SilentlyContinue)) {
            if ($f.FullName -match '__pycache__') { continue }
            if ($f.LastWriteTimeUtc -gt $ref) { $newer = $true; break }
        }
        # shell 侧:kb.ps1 自己和它 source 进来的 .ps1(顶层那一层,不递归)
        if (-not $newer) {
            foreach ($d in @($script:Root, (Join-Path $script:Root 'scripts'))) {
                foreach ($f in (Get-ChildItem -LiteralPath $d -File -Filter '*.ps1' -Force `
                                -ErrorAction SilentlyContinue)) {
                    if ($f.LastWriteTimeUtc -gt $ref) { $newer = $true; break }
                }
                if ($newer) { break }
            }
        }
        if (-not $newer) { return }
        Write-Note @"
[web] 代码比跑着的进程新(kb/ 下的 .py,或者 kb.ps1 / scripts 下的 .ps1 改过)。
     Python 不热加载,而延迟 import 的模块会读到新版 —— 同一个进程里新旧混跑,报出来
     的是一句指不到真因的 AttributeError;那几份 .ps1 决定服务拿到哪份环境(出口变量、
     DoH 开关),改完不重启照旧是旧的。要让它生效:.\kb.ps1 restart
"@
    } catch { }
}

# ── 图谱页 ────────────────────────────────────────────────────────────────────
# 三张图谱是「CSS/JS 内联进单文件 HTML」的产物,住在 vault 里,kb-web 只是把文件原样
# 发出去。所以改了 catgraph.css/js 或那几个构建脚本,**重启不会让页面变样**,得重新
# 生成。跑同步时 wiki 阶段会顺手带上;`.\kb.ps1 graph` 是「只想立刻看到新样式」时的
# 快捷方式(--no-llm 只吃已有归类缓存,零模型调用)。
#
# **故意没做成 restart 的一部分**(同 stale.sh):restart 是纯进程操作,失败了顶多服务
# 没起来,磁盘上一个字节不动;塞进去之后每次重启都在**原子替换 vault 里三张正式页面**,
# 而 build_catgraph 里那一整套两阶段提交 / 发布 CAS / 质量验收,存在的理由就是这个动作
# 有风险 —— 重启一天十次就是对正式产物发布十次,还会跟正在跑的 wiki 阶段抢发布锁。
$script:GraphSrc = @('scripts/wiki/assets/catgraph.css', 'scripts/wiki/assets/catgraph.js',
                     'scripts/wiki/catgraph_page.py', 'scripts/wiki/build_catgraph.py',
                     'scripts/wiki/graph_data.py', 'scripts/wiki/taxo_tables.py',
                     'scripts/wiki/build_graph_html.py')

function Show-GraphStale {
    <#  源文件比图谱页新就提示一句。永远不抛:它只是提示,不能挡住启动。  #>
    try {
        $vault = Get-Setting 'KB_VAULT_ROOT'
        if (-not $vault) { $vault = Join-Path $env:USERPROFILE 'obs\new-start' }
        $pages = @('graph.html', 'graph-projects.html', 'graph-skills.html') |
                 ForEach-Object { Join-Path $vault $_ }
        # 还没生成过就不催:跑一次 wiki 阶段就有了
        foreach ($p in $pages) { if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return } }
        $oldest = ($pages | ForEach-Object { (Get-Item -LiteralPath $_ -Force).LastWriteTimeUtc } |
                   Measure-Object -Minimum).Minimum
        $newest = $null
        foreach ($rel in $script:GraphSrc) {
            $p = Join-Path $script:Root $rel
            if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
            $t = (Get-Item -LiteralPath $p -Force).LastWriteTimeUtc
            if ($null -eq $newest -or $t -gt $newest) { $newest = $t }
        }
        if ($newest -and $oldest -and $newest -gt $oldest) {
            Write-Note '[graph] 图谱页比样式/构建源旧了 —— .\kb.ps1 graph 重建(约 6 秒,零模型调用)'
        }
    } catch { }
}
