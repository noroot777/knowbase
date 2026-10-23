# 浏览器探针起的无头壳(tests/probe_shell.mjs + headless.py 的 run_probe)

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是完整的事故形态、
> 实测证据、回归清单和反向验证红数。CLAUDE.md 只留跨块的元规则和一张索引。
> 硬约束和下面的展开**说的是同一条口径,要改规则两边一起改**。
>
> 这一份管**探针起的 chrome**;夹具那个 Python 服务进程自己什么时候退在 `fixture-life.md`,
> 两份的凭据和判据**都不一样**(探针读 `KB_PROBE_PARENT` 这个 pid、没拿到就现读 ppid;夹具读
> `KB_FIXTURE_OWNER` 那根继承来的管道、没拿到就不装这道防线),动其中一边先把另一边读了。

## 硬约束(原 CLAUDE.md「浏览器探针起的无头壳(tests/probe_shell.mjs + headless.py 的 run_probe)」)

- **探针起的 chrome-headless-shell,在任何一条退出路径上都必须被收掉** —— 收尾那句 `proc.kill()` 写在正常路径末尾时,中间任何一步
  抛异常都让 node 以 unhandled rejection 退出,那一句**永远到不了**,而 node 死掉不会带走 spawn 出来的子进程。
- **三条泄漏路径、三层防线,谁也兜不住另外两档**:①**探针自己抛异常** → `probe_shell.mjs` 的 `process.on('exit')` 里**同步**
  SIGKILL;②**Python 侧 timeout** → `run_probe()` 按**进程组**收(`subprocess.run(timeout=)` 调的是 SIGKILL,node 连 'exit' 都来不
  及触发);③**调用方被取消** → 由**子进程自己**盯 `process.ppid`(node 不在调用方进程组里了,而 Python 对没有 handler 的 SIGTERM
  直接结束,`finally` 一行不跑)。
- **③的判据是 `process.ppid`,而且必须待在子进程里** —— 它是实时 getter,调用方一死内核立刻把我们过继给 init,是**唯一不依赖调用
  方跑任何代码**的信号。轮询间隔 400ms,**那个 interval 必须 `unref()`**。
- **「原调用方是谁」只能由调用方自己说(`KB_PROBE_PARENT`),不能让探针现读 `process.ppid`** —— 探针在起浏览器**之前**本来就有一
  段异步,调用方死在那一段里时读到的 ppid 已经是 1,而「1」和「我本来就是从终端直接起的」长得**一模一样**。这个 env 只有一个写入方
  (`run_probe` 无条件覆盖,`probe_env()` 把继承来的旧值摘掉)。
- **口径两半:检查在模块一加载就装上,外加起任何长寿东西之前同步再问一次**(`bailIfOrphan()`)—— 装在 spawn 之后的话那段异步整个在
  射程之外;而**node 是单线程的**,探针在起浏览器之前做一件**同步**的事时那个 400ms 轮询一次都跑不起来。**两道里只有顶层安装有独占
  射程**,那道闸实测**红 0 条** —— 留它是构造上的理由,如实记着。
- **调用方一死,探针往 stdout/stderr 写一个字节就会被 EPIPE 掐死 —— 这是一条巧合的自愈路径,不可依赖**,而且它会遮掉这几条用例的射
  程:放行屏障之后**不能立刻**去查「起过浏览器没有」,要先 `_wait_gone(node)`。
- **信号要单独挂一遍**(node 对没有 handler 的 SIGINT/SIGTERM 直接终止、**不触发 'exit'**)。**杀用 SIGKILL,理由是构造上的,不是
  「SIGTERM 不够」** —— 这是最后一道防线,而 SIGTERM 要对端配合,'exit' 回调只跑得了同步代码、**没法等**。
- **验证资源的所有权在"起它的那一侧",不在被信号收掉的那一侧**(第 5 轮实测的残留 + 同类的一处推断):
  - **沙箱服务那份临时库由 `tests.headless.fixture_server` 建和删**(它还顺手挑端口,免得两个测试同跑撞端口)。服务进程是被 SIGTERM
    收掉的,而 Python 对没有 handler 的 SIGTERM 是直接结束 —— 它那边 `with tempfile.TemporaryDirectory()` 的清理一行都不跑,一份完整
    的沙箱库留在盘上(定向回归之后残留一个、全量回归两个)。**调用点不要再各自 `Popen` + `terminate`。**
  - **临时 profile 目录交给 `launchShell(args, {profile: '前缀-'})`** —— 它建、它在同一个 `process.on('exit')` 钩子里删(注册在回收
    之后 = 先 SIGKILL 浏览器再删目录,`force` 必须给:被 SIGKILL 的 chrome 会留下半写的文件和锁)。探针自己 `mkdtempSync` + `finally`
    里 `rmSync` 的写法漏一条路:**建目录、起浏览器、连 CDP 都在 try 之前**,那一段抛异常时 finally 一行都不跑。
  - **而那个钩子挡不住 SIGKILL,所以 profile 的根归调用方**(`run_probe` 用 `KB_PROBE_PROFILE_ROOT` 把一个自己拥有的根传下去,在自己的
    `finally` 里 `rmtree`;探针那边"给了根就建在里面、没给才落到系统临时目录",跟 `tests/fixture_root.py` 同一条口径)。这一层的 timeout
    发的正是 SIGKILL —— 第 6 轮审查实测:真跑一次共享 `launchShell` + `run_probe(timeout=6)`,**壳被组杀收掉了、profile 留在盘上**。
    根**不要用 `TemporaryDirectory`**:它的 GC finalizer 会把这份目录也兜掉,于是"这一层到底有没有收"验不出来(注入去掉收尾那一句
    **红 0 条**,实测);`mkdtemp` + 自己 `rmtree` 之后两侧各有自己的红。
  - **类级 fixture 建完就登记 `addClassCleanup`,不许只写 `tearDownClass`** —— `setUpClass` 抛异常之后 unittest **不调**
    `tearDownClass`(只跑 `doClassCleanups`),而建完资源之后往往还有一长段会抛的活:摆盘、起服务、等端口、跑探针、**解析回包**。
    第 6 轮按代码指出的那一支是"回包非空但不是合法 JSON":`json.loads` 落在清理域外面,服务和整份临时库都留着。七个类级 fixture
    (`test_reading_literal`、`test_chat_height`、`test_fulltext_browser`、`test_catgraph_browser`、`test_kb_graph_guard`、
    `test_x_article_dom`、`test_x_article_fidelity`)一并改过来,收子进程的口径收成 `tests/headless.py` 的 `reap(proc)`。
  - **子进程那一侧的口径只有一份**:`tests/fixture_root.py` 的 `fixture_root(stack, '前缀-')` —— 有 `KB_FIXTURE_ROOT` 就用父进程那份
    **而且不许删它**(这边一个 SIGTERM 就没了,删不动也不该删),没给才自己建一个挂在 `stack` 上(手动跑那个脚本时靠 Ctrl-C 的
    `KeyboardInterrupt` 收尾,`with` 跑得到)。**空白串算"没给"**(`Path('')` 是当前目录)。四个沙箱服务(`serve_sources`、
    `serve_reading`、`serve_reader_membership`、`serve_openai_protocol`)全部过它;压根不建根的 `serve_workspace` 不在射程内,所以
    `test_chat_height.py` 直接 `Popen` 它是合法的。
  - 回归分两份:**行为**在 `tests/test_probe_leak.py`(判据是 pid 还在不在),**结构**在 `tests/test_probe_contract.py`(9 条,都不挂
    `@requires_headless`)。漏掉的那几条路都在"建目录 → 起进程 → 连上去"这一段,而这一段恰恰在 `try` 之前、正常跑一次都走不到,
    行为用例全绿也证明不了。
  - **新增两条结构守卫**(`tests/test_probe_contract.py`,11 条):① `setUpClass` 里建了资源的类必须在里面登记
    `addClassCleanup`,而且 `tearDownClass` 里不许再出现回收动作(走 AST:`TemporaryDirectory`/`mkdtemp`/`Popen`/`fixture_server`/
    `ExitStack` 对 `cleanup`/`rmtree`/`terminate`/`kill`/`close`/`__exit__`/`reap`);② profile 那个根的交接两侧认同一个变量名。
    注入各红 1 条(把 `reading_literal`、`chat_height` 的登记挪回 `tearDownClass`;两侧各改一次变量名)。②的第一版按"文件里出现过
    这个词"找,**红 0 条** —— 注释和 docstring 里到处都写着它;判据改成落在真正读写它的那一句上(`process.env.<名字>`、
    `PROFILE_ROOT_ENV = "..."`、`child_env[PROFILE_ROOT_ENV] = profiles`)之后两侧各红 1 条。
  - **结构守卫的判据是"代码里提到那个服务名",不是"这一行上有 `Popen`"**。按行往后开窗口的第一版**漏得一声不响**:把
    `SERVER = ... / "serve_reading.py"` 写在 `Popen` 前两行,注入之后 9 条全绿(实测)。现在走 AST:docstring 里本来就会写"数据由
    `tests/serve_reading.py` 摆出来"(逐行扫会把它当违例,第一版正是这么误报的),注释压根不进 AST,放行的只有
    `fixture_server(script=...)` 那个实参所在的调用范围。
- **测「挂住」的用例必须拿一个 ref 的 handle 挂住,`await new Promise(() => {})` 挂不住**(它不是 event loop handle,循环一空 node
  就以 exit 13 正常退出,于是**本该验第二层的用例测的是第一层**)。
- **`launchShell` 等端口那个 `setTimeout` 非清不可**(它是 ref 的 handle,否则探针跑完 node 也不退出)。**拿到端口后要 `unref()`**
  子进程 handle 和它的 stdio 管道。
- **`run_probe` 要 `start_new_session=True`**(让 node 成为新会话首进程,chrome 自动在同一个组里,`killpg` 一起带走)。收尾在
  `finally` 里**无条件**做一次。
- **把探针改到共用这个模块之后,必须真跑那几组页面回归 —— `node --check` 只过语法。**
- **起探针的测试类必须挂 `@requires_headless`,一个都不许漏。** CI 刻意不装无头壳,而 `probe_env()` 一进去就断言失败 —— 同一份
  discovery 里别的浏览器类都如实 skip 并说清怎么补,漏掉那一个报的是**红**,而红的原因和被测的那件事毫无关系(实测:新加一个浏览器
  类忘了挂,没有无头壳的机器上 `FAILED (failures=1, skipped=1)`)。**这一条只能靠结构守卫**:有壳的机器上漏没漏一个字都看不出来,所以
  它和下面那几条一样待在 `tests/test_probe_contract.py` 里(走 AST,判据是「这份文件从 `tests.headless` 导进来的名字 + 以裸名字调用」,
  两者各挡一种同名形态,射程如实记在那两个辅助函数的 docstring 里)。**不许用「给 CI 装个浏览器」或者「把 `probe_env()` 的断言改成静默
  放行」代替** —— 前者是把能力门换成成本,后者会让没壳的机器悄悄跑出一堆假绿。
- **五条契约不挂 `@requires_headless`**(没装无头壳的机器上靠它们守结构,在 `tests/test_probe_contract.py`):探针不许自己 `spawn`
  浏览器、不许自己管 profile 目录;测试不许裸 `subprocess.run` 跑探针、不许自己 `Popen` 沙箱服务;沙箱服务不许自己拥有临时根。
  **`test_probe_leak.py` 自己是"裸跑探针"那条唯一的例外,而且必须是**(走 `run_probe` 的话它那道 killpg 会把结论盖掉)。
- **`setUpClass` 里起沙箱要把"起完到摆完"整段包进清理**(setUpClass 抛异常之后 unittest **不调** `tearDownClass`):`ExitStack` 存到
  类上、`fixture_server` 和 `run_probe` 放在同一个 `try` 里,任何一条异常路径上 `stack.close()`。`tests/test_reading_literal.py` 是
  这个形状。
- **探针里「等页面就绪」要等那个元素自己,不是等定义它的 JS 全局**(`typeof subsCatalog !== 'undefined'` 回答的根本不是「来源目录取
  回来了吗」)。
- **「有没有漏」按工作目录归属数,不按进程名**(`lsof -p <pid> -a -d cwd` 落在本 worktree 下且 `ppid=1`)—— 本机常常同时开着好几条
  worktree 在跑各自的探针。别去动不归自己的那些。
- 详细见 `docs/decisions/probe-shell.md`。

- **「这一轮不跑浏览器回归」只有一个开关,而且所有起浏览器的组都得过同一道闸**
  (`KB_SKIP_BROWSER_TESTS=1`,判据在 `tests/headless.py` 的 `browser_tests_off`,
  闸在 `browser_gate`(同时给类打分层统计用的 `kb_browser` 标记),两个现成入口
  `requires_headless`(要 node + 壳)和 `requires_shell`(只要壳);`./kb.sh test`
  的 fast 层就是靠在进程上设这个开关把浏览器回归整层拦下的,见
  docs/decisions/test-tiers.md)。整轮全量约 13 分钟,这一组占掉大半 —— 日常提交走
  `./kb.sh test`(fast 层)就等于替你关了它;只有手动跑 README 那条 discover 想省
  时间才需要自己设。**带浏览器的那一遍跑不掉**:验收时 ash 闸门跑
  `./kb.sh test full`,而 probes/full 入口会把继承的开关清掉(层表态显式,见
  test-tiers.md),人关不了那一遍。
  - **不给 `HEADLESS_SHELL` 并不等于不跑**:上面那条口径会自己去 playwright 缓存里
    找一份(本机实测找到了),所以「我没给路径」关不掉它。这就是要有个显式开关的理由。
  - **跳过的理由必须说清是人关的**:一句 `OK (skipped=30)` 在「人关了」和「这台机器
    缺无头壳」两种情形下长得一模一样,而含义差很远 —— 后者是环境缺件,前者是**这一轮
    没有验过界面**。静默跳过正是本文件开头那段要避的坑。
  - **闸只能有这两个**。原先有六组自己写闸 —— 五组写 `skipUnless(SHELL is not None,
    ...)`(`test_cdp_attach`、`test_x_article_dom`、`test_x_article_fidelity`、
    `test_x_post_dom`、`test_x_response_dom`),它们起的也是真浏览器,而开关只认得过
    那两个装饰器的组,**关不住它们**。后两组是写完结构红之后它自己报出来的,不是人
    找出来的 —— 那条红的判据是「从 `tests.headless` 导入了 `run_probe` /
    `fixture_server` / `SHELL` 的 test 文件,必须同时导入其中一道闸」(走 AST,
    按导入名判,注释和 docstring 里照常讨论得动)。
  - **第六组是隔着别的测试模块伸手拿的,上面那条红看不见它**:`test_x_access_gate`
    自己不导入无头壳,写的是 `@unittest.skipUnless(dom.SHELL is not None,
    dom.INSTALL_HINT)` —— 从 `test_x_post_dom` 的命名空间里借。把那边收进统一闸、
    它不再导入 `INSTALL_HINT` 之后,这一行在**导入期**就 AttributeError:不是跳过、
    也不是某条用例失败,而是**整个模块 load 不起来**,连带着
    `from tests.test_x_access_gate import gate` 的 `test_x_provenance_merge_contract`
    一起塌(实测两条 `unittest.loader._FailedTest` ERROR、`Ran 3454` 比上一轮少 16 条,
    而收尾只写一句 `FAILED (errors=2)`)。**改一个模块的导入会从远处把第三个模块炸掉,
    这是「搬代码要逐符号对过原文」的实例**。所以结构红另加一条、按**属性取用**判
    (`<别的模块>.SHELL` / `.NODE` / `.INSTALL_HINT`,自己人 `headless.SHELL` 放行):
    和按导入判的那条**判的不是同一件事**,少了它这一路在那条眼里是干净的。
    定点注入(把那一行退回 `dom.SHELL` 那个写法)实测红,并点名到 `:119:dom.SHELL`。
    收进统一闸之后这一组也跟着受开关管(实测 `OK (skipped=4)`)。
  - 回归 `tests/test_browser_switch.py`:开关认哪些写法、**反过来**没写或写了别的
    一律照旧跑(误判成关掉 = 一轮界面都没验而收尾写着 OK)、两道闸各自跳过且理由
    点得出那个变量名、开关没开时两道闸都不许自己加 skip、外加上面那条结构红。
    **两道闸各用一个新类**:`unittest.skip` 是就地往类上写标记的,共用一个类时
    第一道留下的痕迹会被第二道认成自己的(定点注入实测:只给其中一道挂开关,
    共用类那一版照旧全绿)。
  - **开关打开之后,剩下的那些必须照旧全绿** —— 「省时间」不等于「少几条红」。
    `browser_gate` 把「人显式关了」排在缺件之前,于是**断言缺件提示**的那一组
    (`tests/test_headless_shell.py`,它一个浏览器都不起)在开关落进环境时拿到的是
    开关那句话:实测 `KB_SKIP_BROWSER_TESTS=1` 跑全量 3471 条,红
    `test_skip_message_says_how_to_install` 和 `test_missing_node_is_named_too`
    两条 —— 人按说明关掉浏览器回归,反而拿不到通过结果,还会以为是自己机器的问题。
    出路是**把开关从那一组的「干净环境」里清掉**(它问的是「找得到吗、找不到时
    怎么说」,开关是不相干的输入),**不是把这两条也跳过去**。回归两条:一条按
    **后果**判 —— 子进程里把那一组按开/关两种模式各跑一次,判据是退出码(定点注入
    把 `pop` 改成 `get` 实测只红 `switch=True` 那一半);一条是**类守卫** ——
    凡是真去读跳过理由(属性 `INSTALL_HINT`、或 `getattr` 那个
    `__unittest_skip_why__`)的 test 文件都得显式处理这个开关,**走 AST 不按文本**
    (按文本那一版当场咬住只在注释里讨论旧写法的 `test_x_access_gate`;定点注入
    新写一个读理由却不管开关的模块,实测红并点名到它)。
  - **`skipUnless(shutil.which('node'), ...)` 那几处不在这个开关的射程里**
    (`test_catgraph_window`、`test_daily_refs`、`test_fav_snippet_anchor`、
    `test_favorites`、`test_wechat_setup`):它们用 node 跑页面脚本、一个浏览器都不起,
    也不是全量的时间大头。这个开关管的是**起真浏览器**那一类。

## 事故形态与实测证据

- **探针起的 chrome-headless-shell,在任何一条退出路径上都必须被收掉。** 原来 12 个
  探针各写一份 `spawn` + 等端口,而收尾那句 `proc.kill()` 一律写在**正常路径的末尾**
  —— 探针整个是 ESM 顶层 await,中间任何一步抛异常(页面里 eval 报错、等不到元素、
  后端沙箱没起来、摆局中断)都让 node 以 unhandled rejection 退出,那一句**永远到
  不了**。而 node 死掉不会带走 spawn 出来的子进程,于是 chrome 被 init 收养
  (ppid=1)、永久活着。
  **2026-10-09 在本机实测到 16 个这样的孤儿**:存活 6 小时到 1 天 11 小时,连同渲染
  子进程共 66 个进程、3.8 GB,全部 ppid=1、0 条活跃 CDP 连接,而当时一个测试都没在跑
  (用户是在端口监控面板上看到十几个名叫 knowbase 的条目才发现的 —— 那个名字是**工作
  目录归属**,不是进程名,所以从面板上完全看不出它们是浏览器)。
- **三条泄漏路径、三层防线,谁也兜不住另外两档,所以各有自己的用例**:
  ①**探针自己抛异常** → `probe_shell.mjs` 的 `process.on('exit')` 里**同步** SIGKILL。
    'exit' 覆盖三条路:正常退出、抛异常退出、`process.exit()`。
  ②**Python 侧 timeout** → `headless.py` 的 `run_probe()` 按**进程组**收。
    `subprocess.run(timeout=)` 超时调的是 `kill()`,也就是 **SIGKILL** —— node 连
    'exit' 事件都来不及触发,①那一层在这一档上完全是瞎的。
  ③**调用方被取消** → `probe_shell.mjs` 的 `watchParent()` 由**子进程自己**盯
    `process.ppid`。这一档是 ② **自己开出来的**:见下一条。
- **②的独立会话反过来开出第三条泄漏路,而且它是这次用户看到的那一批的另一半来源。**
  node 不在调用方的进程组里了,于是取消调用方(CI 取消作业、`kill` 那个 pytest、
  `kill -- -<pgid>`)再也波及不到它 —— 而 Python 对**没有 handler** 的 SIGTERM 是
  直接结束,`run_probe` 的 `finally` 一行都不跑。①②在这一档上**都是瞎的**:node 既
  没抛异常也没收到任何信号,而那个 300 秒的超时保护随调用方一起没了。实测(改之前):
  调用方以 -15 退出,node 被过继给 init 之后照旧活着、chrome 挂在它下面、DevTools
  端口继续监听 —— **换成旧的裸 `subprocess.run()` 反而全都收掉了**(那时 node 就在
  调用方的组里),所以这是加②换来的回归,不是既有问题。
- **③的判据是 `process.ppid`,而且必须待在子进程里。** `process.ppid` 是实时 getter
  (每次都 `uv_os_getppid`),调用方一死内核立刻把我们过继给 init,这个值就变了 ——
  它是**唯一不依赖调用方跑任何代码**的信号,因此连调用方被 SIGKILL 那一档也盖得住。
  **没有改成在 `run_probe` 里装 SIGTERM handler**:那要在一个库函数里换掉跑测试那个
  进程自己的信号行为(pytest/unittest 各有一套),而且照样盖不住调用方被 SIGKILL ——
  两个方向上它都更差。轮询间隔 `PARENT_POLL_MS`(400ms)就是「调用方死了到浏览器被
  收掉」的上界。
- **「原调用方是谁」只能由调用方自己说(`KB_PROBE_PARENT`),不能让探针现读
  `process.ppid`。** 探针在起浏览器**之前**本来就有一段异步 —— 等沙箱后端监听、摆局、
  读 fixture(X 通道那一组先等本地后端、十行之后才 `launchShell`,如今在
  `x_channel_kit.mjs` 里)。调用方
  死在那一段里时,探针读到的 ppid 已经是 1,而「1」和「我本来就是从终端直接起的、
  没有调用方要盯」长得**一模一样** —— 于是判成后者、检查**永久不装**,随后照旧起一个
  浏览器留在后台。实测(第 2 轮审查独立复现 + 我自己复现各一次):取消之后 node 以
  ppid=1 继续创建 chrome、DevTools 端口持续监听,**3 秒后仍都在**(远超 400ms 的轮询
  间隔)—— 原因不是检测慢,是检查压根没装上。pid 在 `run_probe` 那边是现成的
  (`os.getpid()`),传过去探针就能回答「我还是不是那个人的子进程」。
  **这个 env 只有一个写入方**:`run_probe` 无条件覆盖,`probe_env()` 顺手把继承来的
  旧值摘掉 —— 留着的话那个 pid 说的是别人的调用方,探针一启动就判成「我已经没有调用
  方了」当场退出。
- **口径两半:检查在**模块一加载**就装上,外加**起任何长寿东西之前**同步再问一次。**
  装在 `reapOnExit`(也就是 spawn **之后**)里的话,上面那段异步整个在射程之外 ——
  那正是第 2 轮拦验收的形状。而光把安装提前还不够:**node 是单线程的**,探针在起
  浏览器之前做一件**同步**的事(`execSync` 起个辅助进程、读一份大 fixture)时,那个
  400ms 的轮询**一次都跑不起来**,同步段一结束就直奔 `spawn`,中间没有任何让定时器
  插进来的机会。于是 `launchShell` 里 `spawn` 之前那句 `bailIfOrphan()` 回答的是
  「同步段结束的那一刻,调用方还在吗」—— 它**不依赖**事件循环哪一刻恰好会转起来。
  **两道里只有顶层安装有独占射程**(「起之前取消、永不放行」那条只有它救得了);
  那道闸实测**红 0 条** —— 顶层安装那个轮询在 `spawn` 之后的第一个 `await` 上就跑
  起来了,于是去掉它只是「多起一个随后被①收掉的瞬死 chrome」。**如实记着**,留它
  的理由是构造上的(下一段和反向验证那一段各写了一遍),今天 12 个探针起浏览器之前
  那段都是异步的,所以同步那条用例钉的是**合约**,不是某个探针的复现。
- **调用方一死,探针往 stdout/stderr 写一个字节就会被 EPIPE 掐死 —— 这是一条巧合的
  自愈路径,不可依赖,而且它会把这几条用例的射程遮掉。** `run_probe` 把探针的两个
  输出收进管道,调用方死掉之后读端就关了;探针(或 `launchShell` 里转发 chrome 那行
  `DevTools listening on` 的那一下)下一次写就抛 `EPIPE`,没人接就是 unhandled →
  node 崩 → ①的 'exit' 钩子顺手把浏览器收掉。实测:给探针装一个吞掉 EPIPE 的
  `uncaughtException` handler,同一个坏实现下 node 和 chrome 就都活着、profile 目录
  也建出来了(`UNCAUGHT EPIPE` 两次、`还活着的 chrome=['84799']`)。**不能靠它**:
  探针在等沙箱后端、跑一长段 eval 的那几十秒里一个字节都不写,而审查实测到的泄漏正是
  那一档。用例这边的后果是:放行屏障之后**不能立刻**去查「起过浏览器没有」——
  要先 `_wait_gone(node)` 等它收场,不然查得比坏实现做得快,**任何**实现都答
  「还没起」(自查时同步那条就是这么写的,注入回去红 0 条)。
- **③那个 interval 必须 `unref()`。** 它只是「探针还活着的时候顺便盯一眼」,ref 的话
  每个探针跑完都要挂到它被清掉为止(永远不会)—— 同下面 `setTimeout` 那条事故。unref
  的 timer 照旧会在 event loop 因为别的事活着时触发,所以"探针正忙"那一段盯得到;
  探针闲下来那一刻 node 本来就退出了,①接着收尾。
  **顺带的后果,知情**:把探针脱离终端跑(`node tests/x_probe.mjs &` 之后关掉那个
  shell)会让它自己结束。那正是这条防线要的行为 —— 没有调用方在等结果的探针,留着的
  只有浏览器。
- **信号要单独挂一遍。** node 对**没有 handler** 的 SIGINT/SIGTERM 是直接终止、
  **不触发 'exit' 事件**;挂了 handler 就得自己 `process.exit()`。
- **杀用 SIGKILL,理由是构造上的,不是「SIGTERM 不够」** —— 后者**实测是够的**,
  如实记着:信号投递是内核的事,发送方随后死掉不影响已经投递的那一下,chrome 自己
  响应 SIGTERM 就退了(2026-10-09 直接实验:SIGTERM 收掉了那个 chrome;注入
  「'exit' 里改发 SIGTERM」跑回归**红 0 条**)。选 SIGKILL 是因为**这是最后一道
  防线**,而 SIGTERM 要对端配合:chrome 卡住(正在处理崩溃、被 SIGSTOP 停住、信号被
  屏蔽)时它不退出,而 'exit' 回调只跑得了同步代码、**没法等**,node 一返回就没有
  第二次机会了。SIGKILL 由内核执行,不需要对端配合。代价是 chrome 自建的临时 profile
  目录留在 /var/folders 下,那是几十 KB 的空目录、系统会清,比留一个 80 MB 的进程
  轻得多。
- **测「挂住」的用例必须拿一个 ref 的 handle 挂住,`await new Promise(() => {})` 挂不住**
  (`tests/test_probe_leak.py` 的 `HANG` 常量)。它不是 event loop handle,循环一空
  node 就以 **exit 13** 正常退出(`unsettled top-level await`)—— 而那条路上第一层的
  'exit' 钩子把浏览器收得好好的,于是**本该验第二层的用例测的是第一层**:去掉
  `run_probe` 里那两处 `killpg` 照样全绿。这是「红 0 条先怀疑用例」又一次应验,而且
  连带查出了下面那个 timer 缺陷。
- **`launchShell` 等端口那个 `setTimeout` 非清不可。** 它是一个 **ref 的** handle,
  拿到端口之后还挂在 event loop 里,于是**探针主体跑完 node 也不退出、要等它到期**
  —— 实测每个探针白挂满 20 秒(回归全套从 40.6 秒降到 20.9 秒就是清掉它换来的)。
  上面那几个 `unref()` 管不着它,那是子进程和管道的事。
- **`run_probe` 要 `start_new_session=True`。** 它让 node 成为新会话的首进程
  (pgid = node 的 pid),chrome 是它的后代、自动在同一个组里,`killpg` 一起带走;
  新开会话同时保证这个组里**只有** node 和它的后代 —— 不然那一下 killpg 会把跑测试
  的进程自己带走。收尾在 `finally` 里**无条件**做一次:正常跑完的那一轮探针已经自己
  kill 过了,对已退出的组成员再发一次信号是空操作。代价就是上面③那条,由子进程自己补。
- **`launchShell` 拿到端口后要 `unref()`。** 子进程 handle 和它的 stdio 管道都是
  ref 的,不摘的话**探针跑完 node 也不退出**。原来那 12 个探针看不出这件事,是因为
  末尾那句 `proc.kill()` 顺手把这几个 handle 释放掉了(**不是**靠 `process.exit()`
  强行收场:12 个里真的在末尾调它的只有 `daily_probe.mjs` 和 `redesign_probe.mjs`,
  另外 8 个写的是 `process.exitCode = …` —— 只记退出码、照样要等 handle 清空,所以
  数 `process.exit` 这个子串会连 `process.exitCode` 一起数进来);而一个忘了收尾的新
  探针会直接挂死
  (回归里 `fine.mjs` 当场挂满 90 秒超时,就是这么暴露的)。unref 只改「要不要为它等
  下去」,不影响 'exit' 钩子(那跟 handle 引用无关),所以回收照旧。
- **把 12 个探针改到共用这个模块之后,必须真跑那几组页面回归 —— `node --check` 只过
  语法。** 迁移时顺手删掉了各探针顶上那句 `const BIN = process.env.HEADLESS_SHELL`,
  有四个文件把紧跟着的 `if (!BIN) {...}` 留在了原处:`node --check`、`git diff --check`
  和逐文件编译全都通过,而真跑起来是顶层 `ReferenceError: BIN is not defined` ——
  四组页面检查(待提取数量 / 旧通道兼容 / 观点卡片 / X 通道)**一条都起不来**,跟
  有没有装无头壳毫无关系(2026-10-09 第 1 轮审查发现,它拿 HEAD 的旧探针跑了对照组,
  四个全过,确认是本轮引入的)。新增的那几条生命周期回归全绿也发现不了 —— 它们用
  的是自己摆的最小探针,碰不到那 12 个文件。
- **行为回归在 `tests/test_probe_leak.py`(13 条),判据一律是「那个 pid 还在不在」「起过
  浏览器没有」这类后果。** 只验「注册了钩子没有」的话,一个注册完却杀不掉它的实现
  (比如 'exit' 里发 SIGTERM)照样全绿。四条退出路径各一条(抛异常 / `process.exit` /
  正常跑完 / SIGTERM),超时那一档单独一条,**调用方被取消那一档按时机分四条**,外加
  `run_probe` 的形状与独立会话各一条。
  **调用方那一条要起一个真的第二个 Python 进程、`start_new_session` 之后取消它的
  整个进程组**:只对 node 发信号走的是①那条路(它的 handler 接得住),验不出这一档;
  而调用方要是跑测试的自己,那一下 killpg 会把测试进程一起带走。它另外断言调用方的
  `returncode` 真的是 `-SIGTERM` —— 不然「调用方自己跑完了」和「被取消了」长得一样,
  后面两条断言就不作数了。pid 走**盘上一份 pid 文件**(`PROBE_PID_FILE`),不走
  stdout:探针的 stdout 被调用方的 `run_probe` 收进管道,调用方一死就读不到了。
  **取消那四条按时机分,各有射程**:①浏览器**已经起来**之后取消(第 1 轮那条);
  ②起**之前**取消、随后放行那段异步 —— 坏实现在放行那一刻才真起一个浏览器,所以
  判据是 `--user-data-dir` 那个目录在不在(chrome 一启动就建),确定性的;
  ③起之前取消、**永不放行** —— 探针得自己结束,这一条只有「模块一加载就装上」救得了;
  ④同一个局但那段等待是**同步**的 —— 轮询跑不起来,只有「起之前先问一次」救得了。
  ⑤外加一条**合约**:`KB_PROBE_PARENT` 指向一个已经退出的 pid 时 `launchShell` 当场
  拒绝(走裸 `subprocess.run`,因为 `run_probe` 会覆盖这个 env)。
  **放行屏障之后不许立刻去查那个 profile 目录**:坏实现也要先走完手里那段工作、
  再 `spawn`、chrome 再建目录 —— 查得比它做得快,于是**任何**实现都答「还没起」。
  自查实测:起之前那两条第一版就是这么写的,注入去掉那道闸**红 0 条**,而那一版真的
  起了一个浏览器。顺序收成一句:**先 `_wait_gone(node_pid)` 等 node 收场,再问
  `profile.exists()`** —— node 死了就再没有谁会去 spawn,那一刻的答案是终态,不赌
  固定时长。
  **结构契约搬到 `tests/test_probe_contract.py`(9 条,都不挂 `@requires_headless`)**
  —— 没装无头壳的机器上靠它们守住结构:探针不许自己 `spawn` 浏览器、不许自己管
  profile 目录;测试不许裸 `subprocess.run` 跑探针、不许自己 `Popen` 自带沙箱根的服务;
  沙箱服务不许自己拥有临时根;外加 `fixture_root` 那三条(给了就用且不许删它 / 没给才
  自己建而且归自己删 / 空白串算没给)。拆出去的直接理由是行数(`test_probe_leak.py`
  本来 635 行,再加就破 700),顺带把两类判据分开:那边问「pid 还在不在」,这边问
  「源码里还写不写得出那个形状」。
  **`test_probe_leak.py` 自己是「裸跑探针」那条唯一的例外,而且必须是**:它那几条验的
  正是探针自己退出时收不收得干净,走 `run_probe` 的话它那道 killpg 会把结论盖掉 ——
  被测的实现坏掉了也照样全绿。
  **「提到名字」比「这一行上有 Popen」敏感**:第一版按行往后开 4 行窗口找
  `Popen`/`subprocess.run`,注入「`SERVER = ... / "serve_reading.py"` 写在 Popen 前两行」
  **红 0 条**;改成走 AST 收字符串常量(docstring 和注释都摘掉,只放行
  `fixture_server(script=...)` 那个调用范围)之后,两种形态(常量隔两行、就地写在
  `Popen` 那一行)各红 1 条。`fixture_root` 那三条的注入:无视 `KB_FIXTURE_ROOT` 红 2 条、
  把父进程那份根也挂到自己 stack 上(退出时删了别人的目录)红 1 条、不 `strip()` 把空白
  串当成「给了目录」红 1 条。
  **反向验证定点注入六版,各红 1~4 条**:③那一层整个不装(就是第 1 轮被审的那一版)
  红 1 条 —— 红的正是审查那份证据(调用方的进程组被取消之后,node 和 chrome 都还
  活着、端口还在监听);③那个 interval 不 `unref()` 红 1 条,而且**红的是「正常跑完」
  那一条** —— 也就是上面那个「探针跑完 node 也不退出」的后果,所以这条用例对它有射程;
  **把悬空的 `if (!BIN)` 塞回一个探针**红 1 条(那组页面回归压根起不来,正是第 2 条
  拦验收的形状);①的 `'exit'` 钩子去掉红 4 条 —— 抛异常 / `process.exit` / 正常跑完 /
  调用方被取消四条全红,**而 SIGTERM 那一条不红**:它走的是信号 handler 里那句直接
  `reap()`,跟 'exit' 无关。这也说明③只负责把 node 推向退出,真正收浏览器的仍然是①,
  两层不是一件事的两种写法;②的两处 `killpg` 去掉红 1 条(超时那一条);
  `run_probe` 不开新会话红 2 条(独立进程组那条契约 + 超时那一条的后果)。
  **第 2 轮审查那一条修完之后另注入四版**,各红 0~3 条:**整条退回被审那一版**
  (现读 ppid + `born <= 1` 直接返回 + 没有顶层安装 + 没有起之前那道闸)红 3 条 ——
  其中「起之前取消、随后放行」那条红的正是审查那份证据(`--user-data-dir` 那个目录
  建出来了,也就是端口面板上多出来的那一条);**只去掉顶层安装**红 1 条(「永不放行」
  那条,它的独占射程 —— 那道闸救不了它,因为压根走不到 `launchShell`);
  **只去掉环境变量这一支**红 1 条(合约那条;**真实取消那条路照旧绿** —— 模块加载
  那一刻调用方还活着,现读 ppid 答得对,所以这个 env 的理由是**构造上的**:
  「spawn 到模块求值」那个窗口里没有别的东西回答得了「我的调用方是谁」);
  **只去掉起之前那道闸**红 **0** 条,**如实记着**:顶层安装那个轮询在 `spawn` 之后
  的第一个 `await` 上就跑起来了,于是那一版只是「多起一个随后被①收掉的瞬死 chrome」,
  没有可稳定观测的泄漏后果(profile 目录在 chrome 被秒杀之前建不出来)。留着那道闸
  的理由因此也是构造上的:`launchShell` 今天 spawn 之后立刻 `await`,哪天它在 spawn
  之后多做一段同步工作,这道闸就是唯一的拦阻 —— 而同步段里那个轮询一次都跑不起来。
  **同步那条用例对单层注入都不红**(四版全绿),一样如实记着:①的 'exit' 钩子把这条
  路兜住了(连带 EPIPE 那条自愈路径),它钉的是「同步启动段 + 调用方取消」这个形状
  的末态,不是某一层的独占射程。
- **沙箱服务那一半:临时目录这一档收完了,进程那一档记在这儿备查。**
  - **目录**(第 5 轮 N2 的射程):四个自带沙箱根的服务全部走 `fixture_root`,根由
    `fixture_server` 建和删;`test_reading_literal` 改成 `ExitStack` + `fixture_server`
    (起服务和 `run_probe` 在同一个 `try` 里,任何异常路径上 `stack.close()`);
    `test_fulltext_browser` 的临时目录本来就在**父进程**手上(`cls.tmp`),方向是对的;
    `serve_workspace` 压根不建根,所以 `test_chat_height` 直接 `Popen` 它合法。
    Python 这边还有一处同形状的窗口顺手收了:`test_cdp_attach._live_browser` 的
    `mkdtemp` + `Popen` 原来都在 `try` 之前,`Popen` 自己抛出来那一下目录就留下了
    (**按代码推断,没实测到** —— `@_NEEDS_SHELL` 已经保证了那个二进制在)。
    **这一档没有结构守卫,如实记着**:`setUp` 里 `mkdtemp` + `tearDown`/`addCleanup`
    删是正确写法,源码结构上跟"建完到进 try 之间还夹着会抛的东西"分不开,机械判据
    会把十来处正确的一起判红。
    注入实测:把 `serve_reading` 退回就地 `with tempfile.TemporaryDirectory()`,结构
    守卫红 1 条,**而且真跑一遍阅读页回归之后盘上留下 `kb-reading-qa-*`、测试照旧报
    OK** —— 正是报告里那个形状。
  - **进程那一档(第 6 轮补上)**:`test_chat_height` / `test_fulltext_browser` 的 `setUpClass` 自己 `Popen` 预览服务,原来每条
    **显式**的失败路径上各写一句 `kill()`,而 `run_probe` 本身抛出来那一档(超时、回包不是 JSON)没人管(第 1 轮注入
    `TimeoutExpired` 实测:服务还活着、端口还在监听)。现在起完**立刻** `cls.addClassCleanup(reap, proc)`,各失败路径上那几句手写
    `kill()` 一并收成这一处 —— 一个资源只有一个回收点。

- **探针里「等页面就绪」要等**那个元素自己**,不是等定义它的 JS 全局。**
  `reading_probe.mjs` 原来等的是 `typeof subsCatalog !== 'undefined'` 再去点
  `#subs-switch-rss` —— 而 `subsCatalog` 是个模块级变量(初值空数组),脚本一加载
  `typeof` 就不再是 'undefined',它回答的根本不是「来源目录取回来了吗」;那颗按钮要等
  `openSubsPanel` 拿到目录之后才渲染(`sources.js` 的 `renderSubsSwitch`)。于是机器
  一忙就等成 `getElementById(...)` 为 null,`setUpClass` 整组当场红,报错是一句
  `Uncaught :: switchTab('subs'); …click()`,**和页面本身毫不相干**。
  **它是本轮全量跑里唯一那条红,而且是既有的**:4 路并发各 24 轮实测 —— `ed63603`
  原版 3/24、本轮迁移过启动路径的版本 7/24(n=24 下这两个数分不出差别,而出错的那
  一行两版逐字相同、页面渲染时机跟浏览器怎么起的没有关系)、改成等按钮之后 0/24。
  同小节另外三个探针点同一颗按钮都在「订阅面板已经渲染过」之后,所以只有这一处。

- **「有没有漏」按**工作目录归属**数,不按进程名。** 本机常常同时开着好几条 worktree
  在跑各自的探针,`ps | grep chrome-headless-shell` 数出来的那些多半是别人的 ——
  审查那一轮就有两条别的 worktree 正在跑 `x_channel_probe.mjs`。判据是
  `lsof -p <pid> -a -d cwd` 落在本 worktree 下、且 `ppid=1`;前后各取一次差集,
  本轮十模块 122 条跑完**差集为空**。别去动不归自己的那些。
