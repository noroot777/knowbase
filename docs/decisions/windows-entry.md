# Windows 操作入口(`kb.ps1`、`kb.cmd`、`scripts/win_*`、`kb/filelock.py`)

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是事故形态、
> 实测证据、回归清单和反向验证的注入点。CLAUDE.md 只留跨块的元规则和一张索引。
> 硬约束和下面的展开**说的是同一条口径,要改规则两边一起改**。

`kb.ps1` 是 `kb.sh` 的对等物:**同一组命令、同一套安全语义**,只是换成 PowerShell + Win32。
它不是「简化版」,也不是容器驱动壳 —— `kb.sh` 里每一条用事故换来的判据(端口写进 pidfile
名字、按端口认服务、往上走到顶层属主、不跨端口杀、不碰别人占着的端口、三态管道闸门、起服务
一律 `--offline`、副作用前先校验参数、worktree 闸门、提示而不代劳)在这边逐条翻过来了。

## 硬约束

### 文件本身

- **每一份 `.ps1` 必须带 UTF-8 BOM。** PowerShell 5.1(Windows 10/11 自带的那个)读没有 BOM
  的文件时按**系统 ANSI 代码页**解码,于是整份脚本里的中文提示全成乱码 —— 连
  `错误:找不到 uv` 这种话都读不出来,**而脚本照常执行**。谁用编辑器重存一次就可能把 BOM 丢
  掉,`git diff` 里也看不见。
- **反过来钉一边:`kb.cmd` 不许有 BOM、不许有非 ASCII。** cmd.exe 按活动代码页逐行解析它,
  BOM 的三个字节会被当成命令(`'' 不是内部或外部命令`),中文注释在非中文代码页上同样是乱码。
  那份壳的全部职责就是把参数转给 `kb.ps1`,说明写在本文件里。
- **双引号串(含 here-string)里不许出现 markdown 那种反引号。** `@"…"@` 会插值,反引号在
  里头是**转义符**:照习惯写 `` `accounts discover `` 的话,PowerShell 把 `` `a `` 读成 BEL
  —— 打出来是「ccounts discover」(**少一个字母**)外加一声响,**解析期一个警告都没有**。
  here-string 里一个转义都不需要(要换行直接按回车),所以那里只许 `` `$ ``(故意转义,让
  `$env:…` 原样印出来)和 `` `` ``;单行串里 `` `n `` / `` `t `` 这类正常写法放行,但
  `` `a `` / `` `b `` / `` `v `` / `` `f `` 一律算误生效。
- **700 行上限照旧**(全局约定)。`kb.ps1` 按这条拆出了四份 dot-source 的文件,理由和
  `kb.sh` 拆 `web_env.sh` / `stale.sh` 一字不差。**新拆一份要同时登记进
  `tests/test_win_entry.py` 的 `PS_FILES`** —— 漏登记的后果是它的 BOM 和转义没人看着。

### PowerShell 自己那几个坑

- **`Set-StrictMode -Version Latest` 和 `$ErrorActionPreference = 'Stop'` 两条都要,缺一不可。**
  PowerShell 默认 `Continue`:cmdlet 报了错它打一行红、**然后接着往下走** —— 于是一个读不出来
  的状态被当成「确定没有」,正是元规则里「判不准一律抛,不许返回确定没有」禁的那件事。
  预期之内的失败就地显式放行(`-ErrorAction SilentlyContinue` 或 try/catch)。
  **原生命令的非零退出不触发 `$ErrorActionPreference`**,所以那些 `$LASTEXITCODE` 的判断
  照旧有效、也照旧必须写。
- **函数返回空数组时要 `@()` 包一层再取 `.Count`。** 空数组从函数返回时 PowerShell 把它拆成
  `$null`,StrictMode 下对 `$null` 取 `.Count` 当场报错。
- **JSON 的字段一律走 `Get-Prop`,不许直接 `$d.stages` / `$x.detail`。** 字段缺席在 StrictMode
  下抛 `PropertyNotFoundStrict`,而字段缺席是正常的(`kb.sh` 那边写的就是
  `s.get("detail", "")`)。配上 `$ErrorActionPreference = 'Stop'` 之后,抛出去会把整条 `status`
  带掉,而它只是想打一行进度。
- **`-Command <脚本> <参数...>` 不传参数,它把后面全部 token 用空格拼成一条命令再求值。**
  要给脚本传参就落成临时 `.ps1` 用 `-File` 跑。实测:扫描脚本和六个文件路径被拼成一条命令,
  第一个路径当成命令**真的执行了一次 `kb.ps1`** —— 那条命令要是恰好是 `stop`,它就真把服务停了。
- **不许给原生程序传空串参数。** `kb.sh` 那边探测环境写的是 `python -c ""`,而 PowerShell 调
  原生程序时会把 `''` **整个丢掉**,python 于是收到一个光秃秃的 `-c`,报
  `Argument expected for the -c option` 并以非 0 退出。**后果不是报错了事:那一步永远失败**,
  于是每次起服务都当成「环境要补一下」去联网装一次(没网就直接 Die)—— 而那正是
  `web_prepare_env` 这一整段要避免的事。写 `-c pass`,同样是空操作。
- **原生程序的 stdout 不许直接接来解析:PowerShell 按 `[Console]::OutputEncoding` 解码它,
  默认是系统 OEM 代码页。** 接口返回 UTF-8,于是中文的多字节序列被按 GBK 拆错,**连带把 JSON
  的引号吃掉**,`ConvertFrom-Json` 当场抛 `ArgumentException`。要解析的东西一律让 curl 落到
  文件(`-o`)、再 `[IO.File]::ReadAllText($p, UTF8)` 读回来;要发出去的 JSON 同理走
  `--data-binary @file`(传给原生程序的**参数**用的是 ANSI 代码页,`--targets 某个中文号`
  直接写在命令行上会在半路被改写)。


### 跟 `kb.sh` 不一样、但必须一样的那几处

- **`curl` 一律带 `--noproxy *`。** 这是 Windows 这边多出来的一条,不能省:这儿的人多半挂着
  全局代理,而 curl 在 Windows 上会读 `*_proxy`。本机回环被代理吃掉之后
  `Get-PipelineState` 永远落在「问不出来」那一档 —— stop 不敢停、sync 发不出去,而浏览器里
  打开面板一切正常。
- **「干净环境」是一份白名单,不是 `env -i`。** Windows 上照着 `env -i` 扔会让子进程压根起不
  来(CreateProcess/CRT 要 `SystemRoot` 才找得到系统 DLL,python 要 `TEMP`,uv 的缓存在
  `LOCALAPPDATA`)。所以 `scripts/win_env.ps1` 的 `CleanEnvKeep` 正着列出必须有的那些、其余
  一概不继承 —— 外层那些登录态变量(`ANTHROPIC_*`、`CLAUDE_*`)同样进不来,那正是 `env -i`
  要防的东西。**不要往这个名单里加 `KB_*`**(出口那一族由 `EgressVars` 单独透传)。
- **起服务用 `CREATE_NEW_CONSOLE` + 隐藏窗口,不用 `DETACHED_PROCESS`。** 后者没有控制台,
  于是**没有任何办法给它发控制台事件** —— 停服务只剩 TerminateProcess 一条路,会把管道里
  正在写 kb.db 的那一步砍在半路。这正是 `kb.sh` 不许用 `nohup ... &` 的同一条理由
  (那边的形态是继承来的 `SIGINT=SIG_IGN` 把 stop 降级成 SIGTERM)。
- **优雅停止只有两档,不是三档。** `kb.sh` 是 SIGINT → SIGTERM → SIGKILL;Windows 上只有
  CTRL_BREAK → `Stop-Process -Force`(= TerminateProcess,不可捕获),**中间那档没有对等物**。
  这是这次移植唯一真的少掉的东西,已经写在 `kb.ps1` 的文件头上。
- **`GenerateConsoleCtrlEvent` 要先共享对方的控制台**,所以 `win_daemon.py ctrlbreak` 得
  `FreeConsole()` → `AttachConsole(pid)` → `SetConsoleCtrlHandler(NULL, TRUE)`,而且**必须
  在一个用完就扔的子进程里做** —— 它会把自己的控制台换掉。
- **目录链接用 junction,不用符号链接。** `New-Item -ItemType SymbolicLink` 要管理员权限或
  「开发人员模式」,而报出来的是一句「客户端没有所需的特权」,跟采集器没有任何关系。
  junction 对目录不要任何特权,python 的 `open`、sqlite 都照常。
- **找解释器必须真跑一次 `--version` 并认退出码。** Windows 10/11 预装的 `python` /
  `python3` 是 Microsoft Store 的占位程序:它在 PATH 上、`Get-Command` 找得到,跑起来退 49
  并弹应用商店。按「PATH 上有没有」判会选中它。**实测这台机器上 PATH 上那个 `python3` 正是
  它**(`…\WindowsApps\python3`)。
- **找 uv 要带上 `%APPDATA%\Python\Python3XX\Scripts`**(`pip install --user uv` 落在那儿,
  版本号在目录名里所以只能 glob)。实测这台机器上 uv 就装在那里、且不在 PATH 上 ——
  少了这一条的后果是「uv 明明刚装好,入口却说找不到」,而人只会认为脚本坏了。
  **别拿 `Get-BootPython` 去推解释器的 Scripts 目录**:它返回的是 `@{Exe;Pre}` 不是路径,
  而且找不到 python 时自己就 `Die` —— 一次「找 uv」会打出一句叫人去装 python 的话。

### 编码:这一块最贵的一条

**同一个病根在两侧各咬了一次,两次都是静默的。** 判据一句话:
**「不写 encoding 就跟着系统代码页走」,而这个项目的事实来源全是 UTF-8。**

- **Python 侧:`PYTHONUTF8=1` 必须强制打开**(`win_env.ps1` 的 `ForcedEnv`;
  `Set-ForcedEnv` 管当前进程及其子进程、`Build-WebEnv` 管服务进程,
  **两个消费方从同一份名单取**)。不开的话 `open()` 在中文机器上用 cp936 去读写 UTF-8 的
  markdown / `defaults.json` / 提取结果:读进来是乱码、**写回去把原文替换成乱码**,
  一路零报错 —— 元规则里「静默丢数据、而且不可恢复」那一类。
  **覆盖而不是「没设才设」**:这不是用户配置,是平台校正,不该由环境投票。
  实测:同一批 179 条回归,不开 **171 条错**;开了 **179 条全绿**。
- **PowerShell 侧:要解析的原生程序输出一律落文件、按 UTF-8 读回来**(见上面
  `Invoke-Api` 那条)。PowerShell 按 `[Console]::OutputEncoding`(默认 OEM 代码页)解码
  原生 stdout,UTF-8 的中文被按 GBK 拆错、**连 JSON 的引号一起吃掉**。
- **残留(本轮没动,属别的块):`kb/` 下还有 16 处 `open()`/`read_text()`/`write_text()`
  不带 `encoding=`。** 其中锁文件那几处(`maintenance.py`、`pipeline_failures.py`、
  `fetch_rounds.py`、`llm.py`)只用来加锁、从不当文本读写,无所谓;要紧的是
  `config.py`、`daily_carry.py`、`feeds/registry.py`、`feeds/cdp_attach.py`、
  `feeds/x_login_import.py` 那几处。**`PYTHONUTF8=1` 把它们盖住了**,所以走这个入口是
  安全的;但**绕开入口直接跑**(`uv run kb index` 之类)还会踩。治本是给那些调用加
  `encoding='utf-8'`,那是各自那一块的事,不在本块。

### Python 侧(Windows 上 `kb-web` 能不能 import 的前提)

- **锁只有一个入口 `kb/filelock.py`,谁都不许再直接 `import fcntl`。** `fcntl` 是 POSIX-only
  的标准库模块;`kb.server` 的 import 链上原先有 6 个模块在**顶层**写着它,于是整个面板在
  Windows 上连 import 都过不去,报一句
  `ModuleNotFoundError: No module named 'fcntl'` —— **指不到任何跟锁、跟并发、跟业务有关的
  东西**。谁都不会从这句话想到「有一处为了互斥写了 flock」。而开发机是 macOS,所以写下这一句
  的那次改动**广域回归全绿**。
- **`filelock` 的语义对等到哪为止,要记住**:真 `LOCK_SH`/`LOCK_EX`、`LOCK_NB` 竞争时抛
  `BlockingIOError(errno.EAGAIN)`（仍是 `OSError` 子类）、句柄关闭与进程退出自动释放,这几样都对;**同一个句柄上的锁转换没有对等物**,
  所以重入必须留在上层(`maintenance._held`、`llm._lock_depth`),别挪进 filelock。
  Windows 的错误 33 只在非阻塞取锁时由共享层翻译；权限、解锁等失败仍报原始 `OSError`。
  接入状态仅将锁竞争视为正在运行，不得把目录保护失败或其他 I/O 故障报成忙碌。
  Windows 的 byte-range 锁是**强制锁**(mandatory),这里安全只因为全系统的锁都加在专门的
  lockfile 上、没人读写那个文件的内容 —— **不许把锁加到 kb.db 或任何真数据文件上**。
- **唯一的例外是 `kb/feeds/x_cdp.py`**:那条通道整条只能在 POSIX 上跑(它要把 `os.pipe` 搬到
  Chrome 写死的 fd 3/4,还要 `select` 那几个管道)。它的 `import fcntl` 包在 try 里,而
  `Chrome.__enter__` 第一句就显式报「这条通道 Windows 上没有,请用 RSS / 导入登录态」。
  **包 try 和显式报错两半缺一不可**:只包 try 的话它会带着 `fcntl = None` 一路跑到某个
  `None.flock`,报 AttributeError。
- **`tzdata` 那条依赖带着 `sys_platform == 'win32'` 标记,不许去掉标记、也不许删。**
  `ZoneInfo('Asia/Shanghai')` 在二十来个模块的**顶层**求值,而 Windows 没有系统 tz 数据库 ——
  少了它就是 `ZoneInfoNotFoundError`,同样是 import 期炸。标记要留着,因为 macOS/Linux 上
  读系统那份才是对的。
- **「不许直接 `import fcntl`」这条管到 `tests/`,而且连探针串一起管。** 两处漏洞各有自己的
  坏法,所以扫描也是两条:
  - **测试文件顶层那一句,代价是整份文件在 Windows 上一条用例都收不上来。** 实测
    `tests/test_pipeline_failure_drop.py` 就是这个形态:unittest 把它记成一条
    `_FailedTest`,于是「删一条明细要先记账本」那一族的不变量**一条都没验**,而全量回归
    的结尾只多出一行 ImportError,混在几百条平台差异里看不见(改完同一份文件收上来 23 条)。
  - **探针串里那一句,AST 的 import 扫描看不见** —— `tests/test_index_lock.py` 和
    `tests/test_feed_trim.py` 把子进程源码写成 `textwrap.dedent('''...''')`,那句
    `import fcntl` 对 AST 只是个 str 常量。而这两段**正是用来验「维护锁真的跨进程互斥」
    的**,在 Windows 上双双 `ModuleNotFoundError`,报出来指向 `<string>`,看不出跟维护锁
    有关。实测改之前 `test_feed_trim` 那条的红是 `'busy' != ''` —— 它说的是「子进程压根
    没跑起来」,而字面上像是「锁没拦住」。
  - **探针必须和被测那一侧用同一份后端。** 各用一套的话,两套后端对不上时这几条会**绿着
    骗人** —— 那比红更贵。
- **测试里攥着锁句柄要用 `try/finally` 关,不能靠 `addCleanup`。** `addCleanup` 排在
  `tearDown` **之后**,而夹具的 `tearDown` 要删临时目录 —— Windows 上删不掉还开着的文件
  (WinError 32)。实测形态:断言全过了,红出来的是一句临时目录清理失败的 PermissionError;
  **真断言哪天不过也会被这句盖掉**。

## 事故形态与实测

- **BEL 那一条是真进去过的**:`.\kb.ps1`(打用法)最后一段里写了 `` `accounts discover` ``,
  实测输出是「ccounts discover」。少的那个字母正是命令名的第一个字母,而人会照着抄。
- **StrictMode + `Continue` 那一条也是真进去过的**:`Test-PortBusy` 里一处
  `(Get-PortOwners $P).Count` 在空数组上报 `PropertyNotFoundStrict`,`status` 打了一行红,
  **紧接着照常输出「kb-web 未运行」** —— 而真相是「没问出来」。这正是「漏报是静默丢数据」
  在这一块的形态:据此去 `start`,会在端口已经有人听的情况下再起一份。
- **`-Command` 拼参数那一条是在写回归时撞上的**:扫描脚本和六个文件路径被拼成一条命令,
  第一个路径当成命令**真的执行了一次 `kb.ps1`**(报的是 kb.ps1 自己那句
  「不认识的命令 …\win_py.ps1」)。要是那条命令恰好是 `stop`,它就真把服务停了。
- **空串参数那一条是第一次真起服务时撞上的**:`uv.exe : Argument expected for the -c option`。
  那一步是「环境够不够、要不要联网补」的判据,**永远失败**意味着每次 `start` 都去联网装一次。
- **编码那一条是同一次起服务之后撞上的,而且最贵**:服务明明起着、浏览器里一切正常,
  `status` 的「同步进度」却打 `(读不到)`。病根是 PowerShell 按 cp936 解码了 curl 的 UTF-8
  输出,`"message":"采集器还没装好"` 变成 `"message":"采集器还没装�?,`,整份 JSON 报废。
  **真正危险的不是少打一行**:`Get-PipelineState` 读同一份 JSON,解析不了就落在「问不出来」
  那一档,于是 `stop` / `restart` **一律拒绝停**。
- **`LOCK_UN` 没人覆盖**,是反向验证当场发现的:`maintenance_lock` 靠 `fh.close()` 释放,
  压根不调 `LOCK_UN`,所以把 `LOCK_UN` 整个改坏、维护锁那几条用例照样全绿。显式用它的是
  `kb.fetch_rounds`(水位文件),坏了的后果是一个进程攥着不放、后面每一轮抓取都在等锁。

## 实测:整条起停链路

环境:`pip install --user uv`(uv 0.13.5)→ `uv sync` 建出 `.venv`(Python 3.11.17,
`tzdata` 按平台标记装上了)。

- `import kb.server` 真跑通(不打桩):18.7 秒,100 个 `kb.*` 模块,`FastAPI` 实例建出来,
  `filelock backend = LockFileEx`。
- `.\kb.cmd start` → 面板起来,`GET /` 200、`/api/pipeline/status` 返回合法 JSON。
  **pidfile 记的是 19052,而端口 8787 的属主是 17672** —— 正是「`uv run kb-web` 起的子进程
  才是真服务」那个形态,`Get-ServicePids` 以端口为准、pidfile 兜底,两个都认得。
- `.\kb.cmd status` → 「运行中」+「空闲」;最后那次请求带着 `?source=false`,
  正是三态闸门在停之前问的那句便宜话。
- `.\kb.cmd stop` → 8.5 秒,端口释放、pidfile 删掉。**日志证明是优雅停的**:
  `Shutting down` → `Waiting for application shutdown` → `Application shutdown complete`
  → `Finished server process [17672]`。被 TerminateProcess 砍掉的话这四行一个都不会有,
  所以 CTRL_BREAK 那条链(`FreeConsole` → `AttachConsole` → `GenerateConsoleCtrlEvent`)
  在真 uvicorn 上是通的。
- `code_stale` 在真实情形下自己响了一次(起服务之后又改了 kb.ps1),提示而不代劳、也没拦。

## 仍然没验到 / 做不到的

- **公众号采集**(要读 Mac 绿泡泡客户端的库)和 **X 浏览器通道**(要把管道搬到 Chrome 写死的
  fd 3/4)在 Windows 上做不到。两条都是**显式报错,不是静默降级**。
- **`sync` 整轮没跑过**(要配 API Key 和模型),所以管道各阶段在 Windows 上的行为没验;
  验到的只是「管道能被问状态、能被要求停」。
- **`graph` 没在主检出跑过**(这边是 worktree,闸门按设计把它拦下了)。
- **全量回归在 Windows 上还有一批红,病根都在测试自己的平台假设上,不是产品代码。**
  `PYTHONUTF8=1` 之后错误从 1495 条掉到 228 条(3712 条里 324 失败 / 228 错误 / 148 跳过)。
  剩下的按病根分:`signal.SIGKILL`、`os.geteuid`、`os.killpg`、`resource` 模块这几样
  Windows 压根没有;`WinError 32`(删不掉还开着的文件);路径分隔符写死成 `/`
  (`test_two_cleanups_in_the_same_second_get_their_own_backup` 比的是
  `source\rss\….md` 和 `source/rss/….md`);还有一批写 `#!/bin/sh` 桩、把 `PATH` 设成
  `/usr/bin:/bin` 的。**本轮只修了锁这一族**(理由:那正是本轮替换掉的那一层,留着等于
  自己改的东西在这个平台上没有回归);其余按「不扩大边界」留着。
- **node/CDP 那套无头探针没有port过。** `tests/headless.py` 靠 `os.killpg` 整组杀
  (Windows 上要换成 job object 或 `taskkill /T`),而这台机器也没装
  chrome-headless-shell,所以本轮连验的条件都没有。它自己有一份 `probe-shell.md`,
  是独立的一块。

## 回归

- `tests/test_filelock.py` —— 结构:`kb/`、`scripts/`、`collector/`、**`tests/`** 四棵树下没有
  顶层 `import fcntl`,**而且探针串里也没有** `fcntl.flock(` / `fcntl.LOCK_`(例外名单连它的
  两半守卫一起钉;谈论 fcntl 本身的那两份文件在 `FCNTL_IS_THE_SUBJECT` 里豁免)。
  判据用的是**调用写法**而不是「出现过 fcntl 这几个字」—— 后者会把所有解释这件事的注释连
  本文件自己一起判红。**反过来钉一边**:拿一份真的长成 `textwrap.dedent` 那样的样料喂给
  扫描,确认它看得见串里那一句、而 import 扫描看不见(不然串扫描退化成「只看 docstring」
  时上面那条会永远绿)。
  行为那一半:后端身份;维护锁跨进程互斥**以及释放**;同线程重入;`LOCK_UN` 显式解锁、
  关句柄自动释放、共享锁的两边。
- 解析源码只有 `_parse()` 一处(两条扫描共用),**转义序列那族警告要压两个类别**:
  3.12 起报 `SyntaxWarning`,3.11(本项目 venv)报 `DeprecationWarning`。只写一个的话在
  另一半解释器上这段抑制等于没有 —— 实测只写 `SyntaxWarning` 时 3.11 上照旧刷一屏跟 fcntl
  毫无关系的警告,看起来像是用例自己有毛病。
- `tests/test_win_entry.py` —— BOM(以及 `kb.cmd` 反过来的那一条)、700 行、dot-source 的文件
  都在且都登记了、两边命令面一一对应、双引号串里没有误生效的转义;以及 `Build-WebEnv` 拼出来
  的那份环境(出口这一族进得去、`KB_DATA_DIR` 和登录态变量进不去、空值不透传、白名单没收得
  太紧 —— 口径对着 `tests/test_egress_env.py` 给 bash 那边钉的那几条)。
  **前几条是纯字节/纯文本检查,在 macOS 上也跑**(日常门禁在那儿过);要真跑 PowerShell 的
  那几条(AST 扫转义、`Build-WebEnv`)挂在共享闸门 `_NeedsPowerShell` 上,没有 powershell 时
  显式 skip 并写明出路。
- `tests/test_win_status_encoding.py` —— 命令级:起一个只回「带中文的 UTF-8 JSON」的假
  kb-web(`KB_WEB_PORT` 指到空闲端口),跑**真** `kb.ps1 status`,判据落在它打出来的那行字上
  (「空闲」才算读懂了,「(读不到)」就是编码那个 bug 回来了)。
  **两种「读不到」要分开**:夹具的命中计数为 0 说明请求压根没到(`--noproxy *` 被删、全局
  代理把回环吃了,或者机器太忙超了 `--max-time 5`),那是另一条病根,报的话要指对地方。
  夹具自己先用 urllib 自检一次 —— 夹具坏掉要在 setUp 就红,不许伪装成那条断言
  (实测:第一版响应体多了一个 `}`,就是这道自检当场抓出来的)。

## 反向验证(定点注入,全红)

| 注入 | 红在 |
|---|---|
| 抽掉 `win_env.ps1` 的 BOM | `EncodingTest` |
| here-string 里塞回 `` `accounts discover` `` | `EscapeTest` |
| `kb.ps1` 的 `'held'` 分支改名 | `CommandParityTest` |
| 给 `kb/pipeline_results.py` 加顶层 `import fcntl` | `NoDirectFcntlTest` |
| 给 `tests/test_source_aliases.py` 加顶层 `import fcntl` | `test_no_top_level_fcntl` |
| 探针串里把 `filelock.flock` 换回 `fcntl.flock` | `test_no_fcntl_inside_probe_sources` |
| `_string_literals` 退化成「只看模块 docstring」 | `test_the_probe_scan_can_actually_see_a_probe` |
| 取锁失败被忽略(两个分支各注一次) | `CrossProcessTest` + `UnlockTest` |
| `LOCK_UN` 不生效 | `UnlockTest` |
| `LOCK_SH` 当独占实现 | `UnlockTest` |
| `EgressVars` 里去掉 `KB_FEED_DOH` / 去掉 `HTTPS_PROXY` | `WebEnvTest`(两条) |
| `CleanEnvKeep` 里加上 `KB_DATA_DIR` | `WebEnvTest` |
| 空值也透传 | `WebEnvTest` |
| 白名单改成「全都继承」 | `WebEnvTest` |
| 白名单里去掉 `SystemRoot` | `WebEnvTest` |
| `Invoke-Api` 退回「接 curl 的 stdout」 | `StatusEncodingTest` |

注入前后的基线都全绿,**而且每一条都确认过红在它该红的那条用例上**(只数「红了几条」会
把「锚点打偏、红在别的用例上」当成验过了 —— 本轮就撞上过一次:改坏 `LOCK_UN` 的那版
第一次注到了 POSIX 分支,广域回归照旧全绿)。
**不许整份换回旧版做反向验证** —— 那样满屏 ImportError,证明不了行为。
注入脚本要注意 **kb.ps1 是 CRLF**:锚点在归一化之后的文本上匹配,写回去换回 CRLF,
最后一律用原始字节还原(顺手确认 BOM 还在)。
