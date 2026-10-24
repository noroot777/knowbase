# uv 在这台机器上的位置 —— **只有这一份**,`kb.ps1`(经 win_py.ps1 的 `Get-Uv`)和
# `collector/setup.ps1` 两个入口共用。
#
# **为什么值得单独一份。** 这两个入口彼此独立:`collector/setup.ps1` 要能在还没有任何
# venv 的新工作树上单跑(它就是去建那个 venv 的那一步),所以它不 source kb.ps1 那一族。
# 于是「uv 在哪」原先各写了一遍,而两份**已经不一样了** —— setup.ps1 那份只看 PATH、
# `.local\bin` 和 WinGet Links。后果是这台机器上实测的那一种:uv 是
# `pip install --user` 装的、落在 `%APPDATA%\Python\Python3XX\Scripts`,于是
# `.\kb.ps1 start` 找得到它,`collector\setup.ps1` 偏说「没有 venv 也没有 uv」——
# 而人刚亲手装过 uv,只会认为这个脚本坏了。元规则:口径只有一份,抽成函数,
# 所有消费方都过它。
#
# **这一份不许 Die / Fail,也不许打字。** 两个消费方找不到 uv 时要说的话不一样
# (一个让你 `winget install astral-sh.uv`,一个还得提 `KB_COLLECTOR_VENV_PATH`),
# 所以这里只回答「在哪」,找不到就返回 `$null`,话留给调用方。

function Find-UvPath {
    <#  uv 的完整路径;找不到返回 `$null`(不打字、不退出)。

        PATH 上没有就找几个常见的安装位置:winget、官方安装脚本、cargo、
        `pip install --user` 都装在用户目录下,而那几个目录**不一定在当前 shell 的
        PATH 上**(winget 装完不开新终端就还是旧的 PATH)。

        `pip install --user uv` 那一处不能漏:它落在
        `%APPDATA%\Python\Python3XX\Scripts`,版本号写在目录名里,所以只能 glob。  #>
    $c = Get-Command 'uv' -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $cands = @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
               (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'),
               (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'))
    # pip install --user:%APPDATA%\Python\Python3XX\Scripts\uv.exe(版本号在目录名里)
    if ($env:APPDATA) {
        $cands += @(Get-ChildItem -Path (Join-Path $env:APPDATA 'Python') -Filter 'Python3*' `
                                  -Directory -ErrorAction SilentlyContinue |
                    ForEach-Object { Join-Path $_.FullName 'Scripts\uv.exe' })
    }
    foreach ($p in $cands) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}
