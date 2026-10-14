# kb-web 的代码新旧混跑(code_stale)

> **这一份整份都是这一块的记录:硬约束 + 事故形态、实测证据、回归清单和反向验证红数** ——
> 动这块代码之前先读它。CLAUDE.md 只留跨块的元规则和一张索引。

- **「改了不生效」只是这件事良性的那一半:坏的那一半是同一个进程里新旧两个版本的
  模块混跑。** kb 里有一批模块是**延迟 import** 的(`kb.feeds.sync` 要等管道真跑一轮
  crawl 才进来),而 Python 不热加载 —— 于是服务运行期间一次 `git checkout` 就够:
  2026-10-10 实测,kb-web **10-09 21:23** 起(那一刻 main 还是 `e16c6fa`,
  `x_channels` 里**没有** `fill_bridge`),**07:08** 换成 `1d2adef`,**08:03** 第一次
  跑同步时 `kb.feeds.sync` 才被 import、从磁盘读到**新版**并调用
  `x_channels.fill_bridge` —— 而 `x_channels` 早在启动后不久就被
  `registry.get_settings` 拉进 `sys.modules`,是**旧版**。七条 RSS 订阅于是一起报
  `module 'kb.feeds.x_channels' has no attribute 'fill_bridge'`,各自被
  `sync._save_failure` 推进最长 24 小时的退避,而面板上显示的是「连接失败」。
  **那句话指不到任何跟订阅、跟网络有关的东西**,人只会去查源站。
  **定位的铁证是 `__pycache__` 的时间戳**:`sync.cpython-311.pyc` 是 10-10 08:03:51
  新编译的(和 `x_article*.pyc` 同一秒),而 `x_channels.cpython-311.pyc` 停在
  **10-09 14:48** —— 那次 import 链因为它已在 `sys.modules` 里而直接跳过了它。
  **出路只有重启**(进程边界,同 bge-m3 那条);重启之后退避还挂着,得手动点一次
  同步(或 `POST /api/intake/{kind}/sync` 带 `source_ids`,它自带 `force`)。
- **所以 `./kb.sh` 在服务已经起着时会提示一句「代码比跑着的进程新」**
  (`code_stale`,挂在 `web_start` 的「已在」分支和 `web_status` 两处 —— 前者覆盖
  `sync`/`collect`/`export`/`index`/`extract` 全部命令,也就是这个 bug 真正发作的
  那条路;后者让 `./kb.sh status` 那一屏看得见)。判据是 **pidfile 比 `$ROOT/kb` 下
  任何一个 `.py` 旧**:`daemonize` 写 pidfile 的那一刻就是进程启动那一刻,而别处只
  删它、不写它。**只看 `.py`** —— `kb/static/` 下的 js/css 每次请求从磁盘读,算进来
  就是每改一行样式催一次重启,那种提示两天就没人看了;`scripts/` 和 `collector/`
  同理不算,它们每次都是新起的子进程。口径同 `graph_stale`:**提示而不代劳,也不拦**
  (重启会把正在跑的同步断在半路,什么时候重启由人定;而且新旧混跑未必每次都撞上)。
  回归在 `tests/test_kb_code_stale.py`(9 条,**判据一律是后果** —— 那句提示出没出现、
  里面有没有可执行的下一步,而不是"函数定义在不在":定义对了却没人调的实现,只验
  定义的话照样全绿)。**挂载点那两条真跑 `web_start` / `web_status` 的代码**,只把
  `service_pids`(进程在不在跑)这个外部事实替掉。反向验证定点注入六版,各红 1~2 条:
  两处调用都去掉(= 加它之前的状态)红 2 条、只去掉任一处各红 1 条(**两组不重叠,所以
  这套用例分得出是哪一处漏了**)、判据放宽成所有文件红 2 条、提示里不写下一步红 1 条、
  判据写反红 2 条。**「没有 pidfile 就安静」那道守卫今天是冗余带,如实记着**:
  `find -newer <不存在的文件>` 在系统 find 和 bfs 上都是报错 + 输出空,后面那句判断
  自己也会安静 —— 留它的理由是构造上的,"服务没跑"该由我们自己回答,不该押在某个
  find 实现对缺失参照物的处理上。

- **同一类事故的另一种形态:从 worktree 跑测试,加载的可能是主检出的代码。** venv 在
  主检出,里面有**两份** editable `.pth`(旧项目名 `knowbase` 和新的 `knowledge_base`)
  都指向 `/Users/fjh/code/knowledge-base`,所以解释器一启动,主检出就已经在 `sys.path`
  里了。worktree 里的那份只靠各个测试文件自己开头那句
  `sys.path.insert(0, 父目录的父目录)` 顶上去 —— **谁先 import `kb` 就按谁的 path 定
  版本**,而那取决于模块名的字母序和各文件里 import 的先后。实测:`tests/test_writeback_lock.py`
  第 26 行 `from kb import cli, …` 排在它第 30 行引 `feed_trim_fixture`(插 path 的那
  一步)**之前**,所以单独指名跑 `test_writeback_lock test_promo` 时,`kb.extract` 来自
  **主检出**,新加的关键字参数当场报 `unexpected keyword argument`;而 `discover` 按字
  母序先 import `test_promo`(它开头就插了 path),同一批测试就全绿。
  **坏的那一半照旧是静默的**:worktree 里改完、测试全绿,跑的却是主检出那份代码 ——
  改动一行都没被执行过,而报告上写着「回归通过」。
  **出路是跑测试时显式带上 `PYTHONPATH=<worktree 根>`**(PYTHONPATH 的条目排在
  site-packages 之前,`.pth` 再怎么追加也盖不住它),不要依赖字母序。**别去删那两份
  `.pth` 里的任何一个** —— 两份 dist-info 的 RECORD 记着同一组 `bin/kb`、`bin/kb-web`、
  `bin/kb-mcp`(见 CLAUDE.md「操作入口」那条「不许改用 `uv sync`」)。

## 当次事故的处置记录

> 以下来自引入这段约定的那次提交的说明。

> 用户面板上七条 RSS 订阅同时显示「连接失败」,原因写着
> `module 'kb.feeds.x_channels' has no attribute 'fill_bridge'` —— 那句话指不到
> 任何跟订阅、跟网络有关的东西。
>
> **根因是同一个进程里跑着新旧两个版本的模块。** kb 里有一批模块是延迟 import
> 的(`kb.feeds.sync` 要等管道真跑一轮 crawl 才进来),而 Python 不热加载:
> kb-web 10-09 21:23 起,那一刻 main 还是 `e16c6fa`、`x_channels` 里没有
> `fill_bridge`;07:08 换成 `1d2adef`;08:03 第一次跑同步时 `kb.feeds.sync` 才被
> import、从磁盘读到**新版**并调用 `x_channels.fill_bridge` —— 而 `x_channels`
> 早在启动后不久就被 `registry.get_settings` 拉进 sys.modules,是**旧版**。
> 七条订阅于是各自被推进最长 24 小时的退避。
>
> 铁证在 `__pycache__` 的时间戳上:`sync.cpython-311.pyc` 是 10-10 08:03:51 新
> 编译的(和 `x_article*.pyc` 同一秒),而 `x_channels.cpython-311.pyc` 停在
> 10-09 14:48 —— 那次 import 链因为它已在 sys.modules 里而直接跳过了它。
>
> **眼前这次已经重启修好并逐条验过**(七条 RSS 的 last_error 清零、退避归零、
> 各自拿到新的 last_success)。这次提交加的是防再犯的那一半:`code_stale` 在
> `web_start` 的「已在」分支和 `web_status` 两处各比一次,pidfile 比 `$ROOT/kb`
> 下任何一个 .py 旧就说一句。前者覆盖 sync/collect/export/index/extract 全部
> 命令,也就是这个 bug 真正发作的那条路;后者让 `./kb.sh status` 那一屏看得见。
>
> **只看 .py**:`kb/static/` 下的 js/css 每次请求从磁盘读,算进来就是每改一行
> 样式催一次重启;`scripts/` 和 `collector/` 同理不算,它们每次都是新起的子进程。
> 口径同 `graph_stale`:提示而不代劳,也不拦 —— 重启会把正在跑的同步断在半路。
>
> 回归 `tests/test_kb_code_stale.py`(9 条),判据一律是后果(那句提示出没出现、
> 里面有没有可执行的下一步),挂载点那两条真跑 `web_start` / `web_status` 的代码、
> 只把 `service_pids` 这个外部事实替掉 —— 定义对了却没人调的实现,只验定义的话
> 照样全绿。反向验证定点注入六版各红 1~2 条:两处调用都去掉红 2 条、只去掉任一
> 处各红 1 条(两组不重叠,分得出是哪一处漏了)、判据放宽成所有文件红 2 条、提示
> 里不写下一步红 1 条、判据写反红 2 条。
