# 夹具服务自己什么时候退(tests/fixture_life.py + tests/serve_*.py)

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是完整的事故形态、
> 实测证据、回归清单和反向验证。CLAUDE.md 只留跨块的元规则和一张索引。
> 硬约束和下面的展开**说的是同一条口径,要改规则两边一起改**。
>
> 跟 `probe-shell.md` 是同一个所有权问题的另一半:那份管**探针起的 chrome**,这份管
> **夹具这个 Python 服务进程自己**。两份的判据**故意不一样**,理由见「为什么不能照搬探针那边」。

## 硬约束

- **判据是「没人用了」,不是「父进程没了」。** `tests/serve_*.py` 手起时的**合法**用法就是跨多个
  shell 共用一个:起服务那个 shell 当场就退,随后另一个 shell 才跑探针。所以这边**不许**照搬
  `probe_shell.mjs` 的「没拿到 `KB_PROBE_PARENT` 就现读 ppid」—— 那样夹具一起跑就自杀,这条合法的
  路直接断掉。两边读同一个环境变量、读法相反,`tests/headless.py` 的 `PARENT_ENV` 注释写了互指。
- **主防线是空闲超时**(`KB_FIXTURE_IDLE` 秒,默认 15 分钟,`0` 关掉给手动调试页面的人)。每个请求
  续一次命。它不问谁是父进程,**因此手起这条路也盖得住** —— 那正是实测泄漏的全部来源。
- **副防线是原调用方看门狗,只在调用方显式告知时启用**(`fixture_server()` 写自己的 pid)。它盖的是
  「跑测试那个进程被 SIGKILL、`finally` 里的 `reap()` 一行不跑」那一档。
- **`KB_PROBE_PARENT` 里那个 pid,启动这一刻必须正是我们的父进程,否则不认。** 继承来的陈旧值说的
  是**别人的**调用方,认了会让夹具凭一个无关进程的死活自杀。
- **收场发的是 SIGINT,不是 SIGTERM。** Python 对 SIGINT 的默认行为是抛 `KeyboardInterrupt`,
  `main()` 里那个 `with contextlib.ExitStack()` 退得了栈;SIGTERM 没有 handler 时是**直接结束**,
  清理一行都不跑 —— 手动跑那一档的临时根归夹具自己删(见 `fixture_root.md` 那条口径所在的
  `tests/fixture_root.py`),发错信号就是把那个事故形态又犯一遍。后面还有 SIGTERM 和硬退两道兜底:
  **一道自己卡住的防线比没有更糟**。
- **口径只有一份**:该不该退全在 `should_exit()`,续命全经过 `touch()`。两条适配路只负责把请求接上
  去 —— uvicorn 那 7 个走 `guard(app)`,`serve_workspace` 那个裸 `ThreadingHTTPServer` 走
  `TouchingHandlerMixin`。**两条路各有自己的红**,一条的回归盖不住另一条。
- **`install()` 要装在摆盘之后**(就在 `uvicorn.run` / `serve_forever` 那一下)。装早了的话建库、导出、
  建索引那几十秒本身就是「没有请求」,夹具会在起身途中被自己的空闲超时收掉。
- **`tests/test_fixture_life.py` 是 `test_no_test_launches_a_sandbox_server_itself` 唯一的例外,而且
  必须是**:它那条验的正是**夹具自己**退出时删不删得掉那份根,所以不能给 `KB_FIXTURE_ROOT` —— 走
  `fixture_server` 的话根归父进程,它的 `finally` 会把结论盖掉,收场信号改错了也照样全绿。

## 事故形态与实测证据

**2026-10-21 在本机实测到 9 个孤儿夹具**:存活 20 小时到 10 天 22 小时,全部 `ppid=1`,占着 9 个高位
端口(50997、51005、51901、52312、53579、53948、58817、59358、64257),进程名在端口面板上显示为
`knowbase`。**8 个里 7 个所属的 worktree 早就删了**,进程还钉着那份已删目录里的 `.venv`,磁盘也跟着
不释放。它们是 `serve_workspace.py` / `serve_sources.py` / `serve_snippets.py` 三种。

**漏在哪一条路上**:`tests/headless.py` 的 `fixture_server()` 起的夹具是有主的 —— 它挑端口、建临时
根、在 `finally` 里 `reap()`。漏的是另一条:会话为了跑一轮界面探针,在 Bash 里直接
`python tests/serve_workspace.py --port 51005 &`。那个 shell 一退,夹具被过继给 init,而这些脚本本身
是裸的 `serve_forever()` / `uvicorn.run()`,**没有任何一方负责收它**。这不是三层防线漏了一层,是这条
路压根在 `run_probe` 的射程之外。

**顺带的存量**:同一天 TMPDIR 里有 300 多份 `kb-source-qa-*`,那是另一个口径
(`tests/fixture_root.py`)在它生效之前攒下的,跟这一块无关。

## 为什么不能照搬探针那边

`probe_shell.mjs` 盯 `process.ppid`,而且「没拿到环境变量就现读 ppid」—— 那边成立是因为探针由
`run_probe()` 起、一轮之内跑完,调用方全程活着,ppid 变了就**一定**意味着没人在等结果了。

夹具不是。它的生命周期**本来就**跨会话里的多个 Bash 调用,起它的 shell 当场就退是正常的。把
「ppid 变了」当判据的话,夹具一启动就判自己是孤儿、当场退出。这条在回归里是**钉死**的:把
`caller()` 改成带 ppid 回退,那条用例就红。

所以两边读同一个 `KB_PROBE_PARENT`、**读法相反**:探针没拿到就现读,夹具没拿到就不盯。

## 回归与反向验证

- 回归 `tests/test_fixture_life.py`。分四组:判据(`should_exit` 的边界)、原调用方的认定、空闲上限的
  读法、**后果**(真起一个夹具,断言的是「那个进程还在不在」「那份根还在不在」)。
- **每一条该退的都配一条不该退的**:空闲到点要退 / 一直有请求不许退;原调用方没了要退 / 没有原调用方
  (手起、跨 shell 共用)不许退;空闲上限设 `0` 之后放着一天也不许退。后面那半边是这套设计的核心 ——
  它一旦失守,会话就没法再手起夹具跑探针了。
- 结构守卫在 `tests/test_probe_contract.py`(沙箱根的所有权、谁可以自己起沙箱服务)。
- **反向验证按定点注入做**,每处只改那一句:`caller()` 加 ppid 回退 / 去掉陈旧值防护、两条适配路各自
  去掉 `touch()`、`serve_workspace` 去掉 `install()`、`should_exit` 去掉两个守卫、收场信号改回
  SIGTERM —— 每一处都有对应的红,没有「绿 0 条」。
