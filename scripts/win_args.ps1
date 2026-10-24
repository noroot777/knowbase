# 外面给进来的东西的归一化 —— 命令行上的取值参数,和路径。由 kb.ps1 source。
#
# 两件事放在一起,是因为它们是同一条纪律的两头:**副作用之前先把外面给的东西变成
# 可信的值**。一个防「参数写错了却照跑」,一个防「同一个目录的两种写法被判成两个
# 目录」。两类错的共同点是**不报错**:命令照常返回 0,而做的不是你要的那件事。

function Read-OptionMap {
    <#  `--键 值` 这种参数表。认识的键在 `$Keys` 里给,返回 键 → 值 的哈希表。
        有一条不对就 Die —— 而且是**在调用方做任何副作用之前**。

        **四种写错法都要当场拦住**,一条都不能漏成「照跑」:
          - 不认识的键(原先就拦了);
          - 键在末尾、后面没有取值:原先补一个空串往下走,于是
            `.\kb.ps1 sync --targets` 这条**起了服务、还发出了一次全量同步请求**
            (实测:请求体 `{"stages":["crawl"]}`,一个 accounts 都没带),
            而人以为自己限定了范围 —— 一次全量采集几十分钟,没有任何话提醒他;
          - 取值是空串或全是空白(`--targets ""`、`--targets " "`):同上,
            静默变成全量;
          - 取值其实是下一个参数(`--since --targets x`):原先把 `--targets`
            当成日期塞进 `since_date` 发给服务端,而 `x` 成了下一轮的键,
            报的是「不认识的参数 x」—— 指不到真正写错的那一处。

        kb.sh 那边这几条是 `shift 2` 兜住的(取值缺失时它自己就报错退出),
        所以这不是「Windows 这边多加的严格」,是把那一侧的行为补齐。  #>
    param([string[]]$Arg, [string[]]$Keys)
    $out = @{}
    if (-not $Arg) { return $out }
    $i = 0
    while ($i -lt $Arg.Count) {
        $k = [string]$Arg[$i]
        if ($Keys -notcontains $k) { Die "不认识的参数 $k" }
        if ($i + 1 -ge $Arg.Count) { Die "$k 后面少了取值" }
        $v = [string]$Arg[$i + 1]
        if (-not $v.Trim()) { Die "$k 的取值是空的" }
        if ($Keys -contains $v -or $v -match '^--') {
            Die "$k 后面跟的是另一个参数($v),不是它的取值"
        }
        $out[$k] = $v
        $i += 2
    }
    return $out
}

function Resolve-RealPath {
    <#  一条路径的「真实」写法:`..` 去掉、符号链接和目录联接一路解开。
        算不出来返回**空串**(不是原路返回 —— 调用方据此拒绝往下判)。

        **`Resolve-Path` 不是这件事的对等物,这一点反直觉。** 实测 PS 5.1 下它对
        目录联接原样返回链接自己那条路(`…\link` → `…\link`),而 Windows 上建链接
        的默认手段**就是** junction:符号链接要管理员权限或开发者模式,
        `collector/setup.ps1` 正因此一律用 junction。`[IO.Directory]::ResolveLinkTarget`
        在 .NET Framework 4.x 上压根不存在(实测 MethodNotFound)。于是元规则里
        「realpath 之后再比,不能比原始字符串」在这一侧**静默地没有生效**:把
        `data\` 联接到别处的人,`graph` 那道 worktree 闸门当场被绕过去,后果是拿一个
        0 节点的页面原子替换掉 vault 里那张好的。

        所以这里**转交给 python 的 `os.path.realpath`**:它走
        GetFinalPathNameByHandle,junction、符号链接、以及「中间某一段是链接」都解得开
        (实测 `link\inner` → `real\inner`)。顺带口径也就只有一份了 ——
        `kb/config.py` 和 `scripts/wiki/taxonomy.py` 判数据目录用的是 `Path.resolve`,
        bash 那边 `realpath` 是同一个语义,这一侧现在也是同一个。

        路径不存在也照样归一化(`realpath` 不要求存在),这是要的:worktree 里
        `data\` 本来就可能不在,而那恰恰是闸门要管的情形。  #>
    param([string]$Path)
    if (-not $Path) { return '' }
    $code = 'import os,sys; sys.stdout.write(os.path.realpath(sys.argv[1]))'
    $result = Invoke-BootPythonCapture @('-X', 'utf8', '-c', $code, $Path)
    if ($result.Code -ne 0) { return '' }
    return ([string](($result.Text -split "`n")[0])).Trim().TrimEnd('\', '/')
}
