# knowbase 项目约定

> 本文件只留**硬约束** —— 别人会真的违反、而违反之后有不可恢复后果的那些,只写「不许怎样」。
> 「为什么」、事故形态、实测证据、回归清单、反向验证红数全在 `docs/decisions/` 下,每节末尾有
> 指针:**改某一块之前先读它对应的那份**,比在本文件里猜意图快。
> 本文件每次会话都整份进上下文,所以**往里加东西之前先看最后一节**。

## 代码文件行数上限(硬性)

- **大模型生成的代码文件,单文件不得超过 700 行。** 预计超过就先按职责拆分(CSS/JS 按面板拆、
  Python 按模块拆)再生成。读大文件同理:超过 ~700 行分块读(offset/limit)。
- 这条管的是**代码文件**。本文件是文档,不在射程内,但同样越短越好。

## 元规则(硬性)

下面几条原先在十几个节里各自论证过一遍。现在只写一次,各节只说它那一处的特殊之处。

### 并发与数据安全

- **判据待在写语句自己的 WHERE 里,不许在 Python 那边先 SELECT 一次**(legacy `isolation_level`
  下 SELECT 不开事务、不持锁)。`cursor.rowcount == 0` 就是拒绝信号。要跨多条语句就**先取维护
  锁、再 `BEGIN IMMEDIATE`,锁序不许反**(反过来报的 `SQLITE_BUSY_SNAPSHOT` 不受 `busy_timeout`
  管)。问一句「现在忙不忙」只是**早一道**,不是互斥。
- **0 行不是失败,多半正是想要的末态。** 统计按**实际写入结果**报、跳过的如实列出,不按计划长度报。
- **拿不准就别固化终态**(`put_items` 的 `DO NOTHING`、`extracted_at` 非空、水位推进这几处写错都
  **不可恢复**;「没读完就留在原处」的代价只是按 URL 去重地重抓一次)。
- **漏报是静默丢数据,误报顶多重抓一次。** 判据宁可宽、宁可多拒;判不准一律抛或扣留,不许返回
  「确定没有」。**一条都没解析出来一律抛,不返回空列表**(静默空集 = 这条订阅从此永远「今天没有
  新内容」,而面板上一切正常)。
- **口径只有一份。** 同一个问题别在两处各判一遍 —— 抽成函数,所有消费方都过它。
- **主干的迁移只增不减**(表和列一视同仁)。判据是「另一条分支**不重启**会不会当场坏」,不是「它
  重启之后能不能自愈」。要停用一列就停止读写它,别 DROP。
- **时间一律按项目时区 Asia/Shanghai,不按宿主机**,不带时区的写法按它补齐。

### 测试方法论

- **先断言后果,别只验过程。** 判据要落在「下一轮还补不补得回来」「库里那条是什么」「页面上那行
  字」,不是「标志打上没有」「锁取了没有」。
- **反过来钉一边。** 验了「该拦的拦住了」就同时钉住「不该拦的照旧放行」。
- **红 0 条先怀疑用例**,想清楚是真冗余还是用例不敏感,再把结论如实写下来。
- **两份实现各有自己的回归**(主路和后路各注入一次都要有红)。
- **搬代码要逐符号对过原文**(广域回归可能全绿,因为新函数压根没进过执行路径)。
- **反向验证要定点注入,不能整份换回旧版**(接口变了满屏 ImportError,证明不了行为)。

## 用词(硬性)

- **面向人读的中文文案一律写「绿泡泡」,不写「微信」。** 方向由仓库主人拍板,不要改回去,也不要在
  提交信息或报告里重新讨论这个选择。
- **三类例外,只有这三类:**①代码标识符和技术字符串(`wechat_*`、`mp.weixin.qq.com`、
  `com.tencent.xinWeChat`、`xwechat_files`、表名列名、JSON 键、环境变量、HTTP 头);②上游项目名
  (`ZejunCao/WeChatOA_Aggregation`)与分支名 `wechat-oa`;③**法律声明** —— 根 README 的「免责与使
  用边界」整段、NOTICE 里涉及法律主体与服务条款的句子,必须指名「微信」。
- **`kb/config.py` 的 `SOURCE_ALIASES` 不许动,一个键都不许删** —— 那几个键是**查询别名**不是文案,
  `expand_sources()` 认不出的名字会当成来源名传进 SQL、**静默得到空集**。回归
  `tests/test_source_aliases.py`。

## 仓库结构

- `kb/` 知识库本体,`collector/` 来源采集器(公众号),同一个 git 仓库(为了改采集器时也能用 worktree
  隔离)。
- 知识库主干不认识任何具体来源,只认 `kb/collectors/` 的采集器契约;绿泡泡特有的东西全在
  `kb/collectors/wechat_local.py` 与 `collector/` 里。新增来源写个实现即可。

## 操作入口

- **`./kb.sh` 是唯一的操作入口**(不带参数打印全部命令)。日常两条:`./kb.sh sync`、`./kb.sh status`。
  **没有「同步前的准备」这一步了**(`prep` 连同整条凭证通道搬去了 `wechat-oa`)。
- **`restart` 只重启进程,不重建任何产物,尤其不重建三张图谱页**(改了 `catgraph.css/js` 重启看不出
  变化)。重建走 `./kb.sh graph`(`--no-llm`);同步时 wiki 阶段本来就带。`web_start` 之后比一次
  mtime,旧了打一行 `graph_stale` —— **提示而不代劳**。
- **`./kb.sh graph` 用仓库内的 `data/` 时只许在主检出跑**(`cmd_graph` 第一件事就用
  `git rev-parse --git-common-dir` 挡 worktree)—— 从 worktree 跑会拿测试库建出 0 节点页面、原子替换
  掉 Vault 里那张好的。
- **前置检查按 `KB_DATA_DIR` 解析数据目录**(没设才回退仓库 `data/`),跟 `kb/config.py` 与
  `scripts/wiki/taxonomy.py` 一个口径。
- **「是不是仓库内的 `data/`」要 `realpath` 之后再比,不能比原始字符串**(`pathlib.Path` 去不掉 `..`、
  也不消解符号链接)。
- **服务一律用 `daemonize()` 起,不用 `nohup ... &`** —— 后者继承来的 SIGINT=SIG_IGN 会让 stop 降级成
  SIGTERM,把管道里正在写 kb.db 的那一步砍在半路。
- **起服务那条命令一律 `uv run --offline`,「要不要联网装东西」挪到前台问**(`web_prepare_env`,那一步
  **用得上当前 shell 的代理设置**,`env -i` 把 `*_proxy` 清掉了)。**不许改用 `uv sync`** —— 它默认
  exact,会卸掉 venv 里旧项目名那份 editable 残留,而两份 dist-info 的 RECORD 记着**同一组** `bin/kb`、
  `bin/kb-web`、`bin/kb-mcp`。
- **凭证通道整条都不在主干上了**(全在 `wechat-oa`,**两个分支始终并行、不打算合并**)。主干采集只有
  一条路:读本机绿泡泡数据库。碰到提凭证的历史说法,那说的是另一条分支。
- **两个分支共用同一份 `collector/data`**,所以主干不许在那个库上做破坏性 schema 变更(见元规则「只增
  不减」)。**反过来也不要去「隔离数据」** —— 主干那 8850 篇文章就在这个库里。
- **`setup.sh` 要继续认旧变量名 `GZH_DATA_HOME` / `GZH_VENV_HOME`**(只读新名字会把显式隔离过数据目录
  的人静默换到别处,而脚本照旧返回 0)。口径在 `pick_path`:新名字优先、两个都设了打一行说明、只设旧
  的就沿用并提一句;**旧变量算显式选择**,不该被存量兜底顶掉。
- 详细见 `docs/decisions/ops-entry.md`。

## collector 采集器

- **公众号只有一条通道 `wechat_local`(读本机绿泡泡数据库)。** `wechat_oa` 在另一分支、主干注册表里压
  根没有它,所以「两条通道互斥、选哪条是个设置」那一整套全删了。手动请求、定时配置和失败重试照旧显式
  携带 `channel`,不修改进程全局环境。
- **退役的通道名要留成别名,不能当「不认识的来源」丢掉**(`_RETIRED_CHANNELS`
  = `{"wechat_oa": "wechat_local"}`,`resolve_channel` / `known_channels` 是唯一口径)—— 存量
  `schedule.json` / `daily.json` 里写的正是旧名字,丢掉的话**公众号从此不再定时同步、不再进日报,而面
  板上完全看不出少了什么**。配套:①退役名也要能翻成人话(`channel_labels()`,API 的 `labels`);②别名
  表要发到浏览器(`/api/pipeline/channels` 的 `aliases`),前端不许写死 `wechat_oa`。
- **「我的订阅」的公众号视图不信 `kb-sync-channel` 那份全局选择**(`sources.js` 的
  `wechatSubsChannel()` 按来源目录声明的通道过别名表解析)。**检查/重试入口同理**自己按目录现解析
  (`refreshPipeSource()`,「检查连接」重跑整个 `openWechatSubs`)。
- **逐号「就绪」那套在主干上永久是空的**(一律 `ready=None` / `state="按本机推送采集"`)。
  `Target.ready/tone/state` 和前端那条 `ready === null` 分支留着 —— 契约是通用的。别再把「刷新凭证」
  「去 Mac 绿泡泡里各点开一篇文章」那类文案加回来。
- **「翻过、确实没有图文」和「这一次没翻」不是一件事**(`local_accounts.discover` 给每行一个 `probe`:
  `index`/`live`/`none`/`unchecked`/`error`;文案是 `cli._NO_BIZ_STATE`(哪一档)+ `_NO_BIZ_FIX`(下一
  步)两半)。`discover` 裸列时一个会话都不翻,给了查询词也只翻前 `RESOLVE_LIMIT`(12)个。**「加号失
  败」这条提示也归这份口径管**(`_accounts_add` 的 `_worst_probe`):只有 `none` 那档该去同步一篇,另两档给「缩小关键词,或用 `--fakeid` 直接指定」。
- **同名会话里只要有一条没解析出归属,按名字添加就是歧义,一律拒掉**(`_accounts_add` 的 `blind`)。
  **归属只认 `__biz`,同名不是线索**,无 biz 的同名会话只生成「归属待核实」诊断、不能成为身份映射。配套
  两条在 `targets.js` 的 `renderTgtCands`:①归属是空的候选不许点添加(按钮禁用 + 写清下一步);②**「已
  订阅」只按身份(`byId`)认,名字一律不作数**。
- **一个档案名不许改挂到另一个 `__biz` 上**(`enable_crawl_account` 抛 `AccountNameTaken`)—— `name` 是
  主键、`articles.account_name` 是外键、vault 目录也按它归档,碰上同名就是把第一个号的**全部历史文章连
  同导出目录**接到第二个号身上。判据按元规则待在那条 UPSERT 自己的 `DO UPDATE … WHERE` 里。**四句豁免
  缺一不可**:①`excluded.fakeid = ''`;②`accounts.fakeid = ''`;③`accounts.fakeid = excluded.fakeid`;
  ④`import:` 开头的占位键。
- **本地库只负责文章发现,正文继续按链接抓取。** 本地读取只能通过已配置的 `rion-wechat-cli`,须确认
  `live_database_read_ok`,不允许通知预览替代、自动获取密钥或操作绿泡泡。归属最终按推送 URL 的 `__biz`
  核实,不能以同名自动匹配。
- **文章身份是 `article_identity` 的 `__biz + mid + idx`,保留已有文章 ID 和归属**(存量是凭证通道采
  的、ID 另一套规则,本地库再发现同一篇时必须认出来、不能新开一行);用户删除时同时记进链接身份黑名单。
  不要把推送接收时间或带跟踪参数的完整 URL 当成唯一键。
- **本机推送链接须保留正文校验参数 `chksm`,仅剔除跟踪参数。** 发现阶段用分片/表级 `local_id` 游标 ——
  不要用 timeline 的 after-local-id 做增量分页(读取器是在 LIMIT 后过滤 ID),也不能拿发布时间当消息水
  位;**分页/读取失败不推进对应游标**。未选公众号只进本地发现索引,不下载正文、不写共享 articles、不导
  出。正文失败存 `article_fetch_state` 按退避重试,**不能用发布日期判断删除**;本地正文暂不可读走
  `body_failures` 明细,不进 `errors`、不使抓取阶段判红。`is_wx_deleted` 的历史自动退休记录允许重新入
  库,用户删除及身份黑名单仍拦截。其余(源指纹重扫、异常表、XML 内存解码、`no_body` 明细协议)见决策
  文档。
- **`collector/data` 和 `collector/.venv` 丢了就跑 `./collector/setup.sh`**(两者都是指向仓库外的软链
  接,新开的 worktree 只带源码;脚本幂等)。缺链时面板顶栏报「采集器还没装好」,抓取/导出落 skipped 而
  不是 error。**别再让它退回 `sys.executable`**(判定在 `wechat_local._backend_problem()`)。
- 采集器是**一次性 CLI,不是常驻服务**:`cd collector && ./.venv/bin/python -m cli <命令>`(常用 `status` / `collect` / `export` /
  `accounts list|add|remove` / `login`)。stdout 只出
  NDJSON 给程序读,人看的提示走 stderr。
- **公众号列表**存在 `collector/data/wechatoa.db` 的 `accounts` 表,日常维护走 kb-web「我的订阅」页,
  **不要直接改库**;终端等价物是 `cli accounts list|discover|add|remove|enable|disable`。**添加候选来自
  本机绿泡泡的会话列表**(`cli accounts discover`):不登录公众平台、不走网络。
- **公众号改名只更新 `accounts.display_name`,永远不动 `name`**(主键 + 外键 + vault 目录都按它)。**主
  路是本机会话列表**(`local_accounts.refresh_display_names`,每次同步在发现阶段之后跑一次)。抓正文时
  读到的 `var nickname` 照旧写回;别用页里裸的 `nickname:`(那可能是推荐位里别人家的号)。**按 biz 一一
  对应,不许「只有一个就配上」**,否则两个号的名字会互换。`GZH_DISPLAY_RECHECK_DAYS` 和
  `display_checked_at` 都没了,别再加回来。
- **`enable_crawl_account` 先按 fakeid 认号,认出来只更新、不插新行**(否则同一个号两套主键、vault 目录
  裂开,而外键全合法)。它返回**实际入库的档案键**,调用方据此区分「新添加」和「这就是那个改了名的号」。
- 别把 `collector/data` 或 `collector/.venv` 加进版本库 —— 它们是符号链接,`data/` 那条带斜杠的规则挡不
  住,.gitignore 里另有不带斜杠的显式两行,别删。
- 详细见 `docs/decisions/collector.md`。

## X 的采集通道(kb/feeds/x_channels.py + x_browser / x_feedgrab / x_cdp / cdp_attach)

- **同一批大V账号,四条互斥的取法,选哪条是一个设置**(`social` 的 `x_channel`,默认 `bridge` —— 存量库里
  没这个键,读出来必须和升级前逐字同义):`bridge` 第三方 RSS 桥接、`feedgrab` 一份 X cookie 调 X 自己的接
  口(子进程调用用户装好的 CLI)、`browser` 复用**独立 profile** 里的登录态(我们起的无头 Chrome,CDP over
  管道)、`attach` 直连用户**已经开着**的日常浏览器(CDP over WebSocket)。**口径只有 `x_channels` 一份。**
- **通道只管「走桥接的那些 X 账号」**(判据 `registry.uses_bridge`):填了 `feed_url` 的和非 X 的 social 订
  阅照旧走 HTTP。
- **后两条不是 HTTP feed,不走 adapter 的 `endpoints()/parse()`**,而是在 `sync.collect` 开头**整批预取**一
  次(`x_channels.prefetch`),由 `fetch_source` 顶上那道分派按订阅各取自己那一份(`take`)——**同一个浏览器
  profile 同时只能被一个实例打开**。
- **HTTP 那一侧的采集预算要在预取之后才开始算。** 登录态通道另算一份串行预算(`X_LOCAL_SECONDS`,1200 秒)。
- **「停止」要在起浏览器之前就认**(两条 harvest 开头各一道,每个账号之间再问一次)。
- **水位按订阅存在 `subscriptions.config` 里,`attach` 和 `browser` 共用 `x_watermark_browser`,feedgrab 必
  须另记 `x_watermark_feedgrab`**(它的下界只到天)。判据是「精度和语义一不一样」,不是「是不是浏览器」。
- **没读到上次的位置,水位一步都不许动**(抓到的照旧入库)—— 推到「这一轮见过的最新」或「退到最老那条」都
  会让那一段从此任何一轮增量都选不到。**首轮存回这一轮真正用过的那个下界**(`x_browser.started`、
  `x_feedgrab.since`),**交空值回去等于什么都没存**。**读完的那一轮照旧 `_newest(...)`,不许一律存下界。**
- **「滚不出新东西」要连着 `_BOTTOM_TRIES`(3)次、而且每次都停在页面底部,才算真到底了。**
- **「翻到底」按页面出现顺序数,连续 `_OLD_STREAK`(3)条自己发的推都老于下界才算**(`_old_enough`):
  ①**不能按时间排序数**(转推显示的是原推时间);②**只有原创推的时间能当证据**(`_own_tweet`)。
  **任何一条新于下界的条目都把计数重置。**
- **「算不算这条时间线的内容」(入库)和「它的时间算不算证据」(翻到底)是两个判据**(`_in_timeline` /
  `_own_tweet`),别合成一个。入库排除推广位、以及作者不是这个号又没有转推那行 socialContext 的(推荐位);
  **转推一定带着那行上下文,所以不会被丢掉**,正文里保留它。
- **置顶要入库,只是不参与「翻到底」的证明**(旧置顶被 `put_items` 去重,而刚发就被钉上去的推文丢掉永久补
  不回来)。
- **「有推文卡片却一条都解析不出来」是 X 改版,不是「这个号今天没发推」**(`_harvest_one` 里
  `if seen_articles and not parsed: raise`)。登录失效 / 账号打不开 / 限流同理各一句话,不许折叠。
- **feedgrab 是子进程调用,环境变量白名单 + 显式清场,`OBSIDIAN_VAULT` 必须清掉** —— 它的输出目录**优先**认
  这个变量,而这台机器上它指的正是知识库那个 vault。每个账号一个干净的输出目录。
- **它的产物路径不许写死,只能整棵扫**(`_read_output` 走 `rglob('*.md')`;路径是「平台目录 + category」拼
  的,**猜不出来**)。递归不放松归属:判据是 `_entry` 按推文 URL 里的 handle 过滤。
- **它的退出码 0 不代表这一批抓全了,所以没抓全就不许推进水位**(`_incomplete` / `_batch_failures`)。两档各有独立
  证据源,谁命中都算漏、同时命中就两句话都说,只有 `fetched`/`skipped` 算没问题:①**翻页半途中断**只有一行
  `logger.error`(`_PAGE_ABORT`),判据要连「第 N 页」「M 个账号」一起认、**逐行看而不在整份输出上 `search`**,还要和
  **回显了用户内容的日志**分开(三道各有射程);②**逐条失败**两道判据:批量记录**认形状不认路径**、收尾摘要里的
  「失败: N」**必须带上「成功: N」那半截上下文**。放宽任何一道的后果见决策文档。
- **不要把 feedgrab 抄进 kb**(它要 X 内部 GraphQL + 登录态 manifest 里的签名算法,为此背着 60 多个 fetcher
  和 playwright)。模式同 `collector/`:kb 只知道一条命令行,升级是 `git pull` 的事。
- **登录提示里的可执行文件必须就是采集时会调的那一个**(`x_feedgrab.login_command` 过一遍 `find_binary`;
  `x_channels.login_command` 因此收**整份 settings**)。两头都 `shlex.quote`;配了路径但当下不可执行时照它
  **原样显示**。
- **拿到一个浏览器有两条路,各自是一条通道,不是一个实现细节**(`x_cdp.Chrome` 管道 / `cdp_attach.Attached`
  WebSocket)。**不引入 playwright。**
- **`attach` 那条的三条硬口径**:①**那个开关只能由用户自己去开**(`chrome://inspect` 勾一次),我们不代劳也
  不绕开,「没就绪」那档给的下一步就是这句话(`ENABLE_HINT`);②**端口是读出来的,不是我们指定的** —— 读
  user-data-dir 下的 `DevToolsActivePort` + TCP 探活,固定敲 9222 只作回落;出路是 `KB_CDP_ENDPOINT`,**显式
  指定了就不再去猜别的**(写错了也不回落,而且要说出是哪儿错了),`parse_endpoint` **一个异常都不许抛出来**
  (它坐在面板每次刷新那条路上;范围另用 `_ok_port` 卡住,`urlsplit` 本身也在 try 里);③**探活必须 TCP
  connect,不能拿 WebSocket 去试**(首次 WebSocket 连接会弹授权窗)。
- **独立 profile 那条的收尾一定做完:关浏览器怎么失败都不许把后面那几步跳过**(`__exit__` 只剩
  `try: self._shutdown()` + `finally:` 关两个 fd、状态复位;先好好说一次再 kill 的两步都在 `_shutdown` 里,
  **都有界、都不往外抛**)—— 异常冒出去留下的是一个还活着的无头浏览器。**反过来钉一边:好好退出的那一次不
  许被 kill**(`Browser.close` 存在的理由就是让它把 profile 存盘)。
- **「这个 profile 被占着吗」只认「`SingletonLock` 指着一个活着的 pid」**(`profile_busy`)—— 只看 `lexists`
  会把「检查连接」和「打开登录窗口」这两个用户唯一能自救的入口一起堵死。不设「看不懂就当占着」的特例,也不
  去替 Chrome 删那把锁。
- **在别人的浏览器里干活的卫生规矩**:标签页一律 `Target.createTarget {background: true}` 开、退出时**只关自
  己开的那几个**、**永远不发 `Browser.close`**、顺带路径上不列用户的标签页。**「这张标签页是我开的」要在
  `createTarget` 刚回来那一刻就登记**(`Session._adopt`),不等后面的 attach/enable 成功。
- **不抄 web-access 那道「拦截页面对调试端口的探测」**(要在协议层订阅事件,而 `Session.call` 把事件全丢掉;
  不拦的后果是「和用户自己浏览时一样」)。
- **这条通道不在面板上登录**,拒绝文案**不许建议他换档**。
- **面板上那句静态说明不许写死通道有几条** —— 跟着通道清单变的东西一律让后端那份清单自己说。
- **Chrome 把那两个 fd 的编号写死成「读 3 / 写 4」,而 `preexec_fn` 里 dup2 到 3 实测不成立。** 口径是:父进程先把两
  端复制到一个空闲高位 fd、`pass_fds` 带过去,再由 shell 的 `exec 3<&N 4>&M` 落到位。**高位编号必须 `fcntl.F_DUPFD`
  现取、不许写死常量**(dup2 会**静默关掉**目标 fd 原来那个对象)。
- **读写都永远带 deadline,deadline 要在命令发出去之前算好**(`Session.call` 里 `deadline = ...` 排在 `self._send(...)`
  **之前**,顺序不许反)。**socket 和管道一律设成非阻塞,超时由 `select` 按 deadline 管**:写是「select 等可写 →
  `send` 一段 → 接着等」,`BlockingIOError` 当成「再等一轮」(光「select 等可写 + send」挡不住一条阻塞的连接)。
  **写超时之后这条连接就废了**(`ws_client._broken` —— 半条帧已经进了对端缓冲,接着发就是帧错位,而错位之后的读是攥着
  锁阻塞的那一种);**上限两头取小**(`min(这条命令的 deadline, now + WRITE_TIMEOUT)`),收尾另有更紧的
  `CLOSE_TIMEOUT`。
- **桥接熔断只对桥接通道成立**(`sync.collect` 的 `shared_upstream`)—— 登录态通道的内容已经在预取里抓完了。
- **「这条订阅能不能开工」要问当前通道**(`registry.configuration_error` 按通道分支,`x_channels.readiness`
  给 (就绪, 说明, 下一步)),收尾那行阶段文案和面板横幅都读它。
- **`readiness` 只做便宜的判断,真连一次是 `check()`**,两个都要有(**cookie 过期在文件上看不出来**)。桥接通
  道没有「连一次」可言,那一档如实说清楚,不许谎称已就绪。
- **退役通道名的别名表只在服务端**(`_RETIRED` + `resolve()`)—— 浏览器手里永远拿不到一个退役名,跟公众号那
  边不一样。
- **同步间隔是一天一次**(1440)。**改 `interval_minutes` 只影响没有设置行的新库,存量要迁**(`_SETTINGS_REV`
  + `_SETTINGS_FIXUPS`,在 `get_settings` 里归一):①只迁仍然等于旧默认值的那些;②**读的时候归一、写的时候
  盖章**(反过来写会留一个永不迁移的窗口,而那正是升级的常态)。
- **水位和条目必须在同一个事务里提交**(`watermark_patch` 交回 config 补丁,`sync._save_result` 在一个事务里跑
  `put_items` + `merge_config` + 成功时间戳)。
- **存盘之前在那个写事务里认一次「这条订阅还是同一个号吗」**(`sync._repointed`,成功和失败两条路各一次)——
  采集手里那份快照是发请求之前读的,最狠的是**旧账号的水位写进新账号那个键**。判据按元规则排在 `_claim_write`
  拿到写锁**之后**,判据是**身份**(`url`/`feed_url`)、不是「这一行被动过没有」。作废的那一轮**一个字都不
  写**,记进 `summary['repointed']`;换链接那次 `reset` 要把 `last_note` 一起清掉。
- **区间补抓要把它那段日期交给采集器当下界,而且不许动日常水位**(`prefetch(since=...)`)。**用户点的那个日期
  要在一处换算成带时区、且把起点算在内的下界**(`since_floor`,时区按元规则;第二道在 `x_browser._utc`)——
  下游那条选择规则是严格大于,所以给的是那一天零点前的最后一瞬。换算不许下移给下游:feedgrab 的
  `X_USER_TWEETS_SINCE` 只认日期而且按 UTC 的日算。
- **「这一轮没读完」要在面板上看得见,而且不是 `last_error`** —— 写进 `subscriptions.last_note`、显示成「部分
  同步」;抓到的照旧入库、不进退避重试,读完的那一轮重写成空串。
- 前端在 `kb/static/js/x_channel.js`。**哪条通道要填什么由后端给的 id 决定、前端不许写死一张表**;改了选择还没
  保存时状态行要写明「保存之后才会生效」并**藏掉「检查连接」**。**「打开登录窗口」也跟着「保存了没有」走,而
  且是禁用、不是藏掉**(「检查连接」产出的是**结论**,贴在新选择旁边就是谎话;这颗是一个**动作**,藏掉它上面
  那段说明就悬空了)。`openXLoginWindow` 开头另有一道 `xChannelSaved()` 兜底,`finally` 里恢复成「此刻该不该
  能点」。**那两颗按钮打的是动作端点,一律 `pipePost`** —— `pipeJson(path, {}, 120000)` 实际发的是 **GET**。
- **面板空态那句「要先配置什么」不许写死桥接那一档**(`sources.js` 的 `sourceConfigHint()` 读后端给的
  `configuration_error`)。
- 详细见 `docs/decisions/x-channels.md`。

## X 长文(Article)的正文补全(kb/feeds/x_article.py)

- **长文推文在时间线上一个字都没有**(`text` 就是一条指向 `x.com/i/article/<id>` 的短链),两条通道入库的都是
  空正文。**主路是 FxTwitter 的公开接口**(`tweet.article.content` 是 Draft.js 块);**后路**是在
  `browser`/`attach` 的登录态浏览器里按 DOM 读(`browser_fetch`,由 `x_browser.harvest` 把还活着的会话传下
  去),它不当主路的理由是只对两条通道成立 + X 改版就要跟一次。`bridge`/`feedgrab` 没有浏览器,照旧「补不到就
  不入库、不推进水位」。
- **文章页地址 = 推文地址把 `/status/` 换成 `/article/`,不需要 `article_id`**(后者只出现在接口回包里,而后路
  恰恰在接口不可用时才跑)。
- **「这条推文没有长文」的判据是 X 自己的路由行为**:纯图推文的 `/article/<id>` 会 302 回 `/status/<id>`。这一
  档返回 `None`(照旧入库、**不冻水位**)。**判不准的时候一律抛,不许返回 `None`**(登录墙、空壳、渲染超时、
  CDP 报错全部抛)。CDP 报错只抛 `ArticleUnavailable`,不冒到「浏览器会话中断」上去。
- **后路落地之后要核对「这一页到底是不是我们要的那一篇」,这一道承重**(`seen[1] != want` 一律抛)—— 导航**可能
  不生效而页面停在原处**。配套三条:`Session.goto` **返回** navigate 回包而不抛(时间线那两处各自按 `_STATE_JS`
  判);回包里的 `errorText` 是**早一道**、不能靠它;**`back` 那一档也要求身份相符**。
- **这一步要带整轮的时间预算和停止开关**(`fill(..., deadline=, should_stop=)`,**两条通道各挂一次** ——
  `x_browser.harvest` 和 `x_feedgrab.harvest` 的调用点都传):它跑在时间线读完**之后**,外面那两道闸都够不着
  它,而 `_GIVE_UP_AFTER` 只数**连续**失败。拦下的按扣留办。**接口切后路之前再问一次停止。**
- **块内按子节点顺序拼,`<img>` 分两种,判据是 alt 的内容**:至少含一个表情码点**且**整串只由表情相关码点组成
  的是行内表情(按 alt 还原),其余一律是真·配图(`![alt](src)`)。「有 img 就只要图片」丢整段文字,「非空 alt
  就是表情」丢图片地址,`innerText` 不含 alt。
- **预格式化块里的空白是内容,一个都不许折叠**(两道任一成立:计算样式的 `white-space` 以 `pre` 开头,或标签本
  身是 `pre`/`code`/`samp`/`kbd`;折叠挪到文本节点那一层)。**反过来钉一边**:只存在于 HTML 源码里的换行缩进照
  旧折叠。
- **零宽字符只清 ZWSP 和 BOM,`\u200c`/`\u200d` 一个都不许动**(组合表情的一部分)。
- **代码块里一个字节都不改**(零宽清理和 nbsp 归一只对普通段落做),两条路对同一篇文章必须给出同一份代码。
  **占位清理整个挪去组边界**(`codeBody` 的 `blankRow`)—— 在逐块那一支做的话,组**中间**那一行零宽字符也被当占
  位符清掉。
- **代码块要写成 markdown 代码围栏 —— 读对那串字符只是半截**(两条路各一处:主路认 Draft.js 的 `code-block`,后路认标
  签)。写 `<pre>` 没用(阅读页按契约把剩下的标签一律当字面文字),改阅读页 CSS 会动全站且 vault 里躺的仍是裸代码。配套
  四条:①**相邻的代码块合并成一个围栏**;②**围栏里每一行都原样,一个字节都不改**(行首缩进和**行尾空白**都是内容,
  **收尾不许用 `.strip()`/`.trim()`**;占位空块只在**整组边界**削**完全空**的行,纯空格的一个都不削,两侧各一份实现
  `_code_body` / `codeBody`、**各有自己的回归**);③**围栏长度要避开代码里已有的反引号串**(`max(3, 最长串+1)`);
  ④**后路那道判据只认标签,不许用计算样式**(Draft.js 默认给**每一个块**都加了 `white-space: pre-wrap`)。`preish`
  (保住空白)和 `codeish`(写围栏)是两道**不同**的判据。
- **补完正文不许把转推上下文丢掉** —— 入库正文是三段按原顺序拼:转推上下文(`> …`)→ 推文自己写的介绍 → 文章正
  文(`quote_lines` / `lead_text` 各摘各的)。
- **接口答「有标题、没正文」时一律当成没取到**(回包被截断那个形状原来返回成功,而空正文一旦落库就改不回来)。
- **第三方回包的结构异常一律留在接口边界内,两道各有射程。** ①**承重的是每一层明确判类型**(`_rows` + `_media_urls` /
  `_entity_image` / `_entities` / `render` 各一遍 `isinstance`),认得出的结构异常**就地降级**(那张图跳过、那一块跳过)。
  **不许拿 `or {}` / `or []` 当类型检查**(它只认假值,而撒谎的回包给的是**非空**的错类型);**判一层不够,得每一层都
  判**,没有「这一层不会出问题」这种例外。②**兜底是 `fetch` 把 `render` 的数据形状异常一起翻成 `ArticleUnavailable`**
  (`AttributeError`/`TypeError`/`KeyError`/`IndexError`/`ValueError`),只圈这几类,不写 `except Exception`。
  `json.loads` 那道 except 要带上 **`RecursionError`**(它是 `RuntimeError` 的子类;但**绝不能写成
  `except RuntimeError`**)。
- **必需的结构认不出来一律抛,`str()` 强转和 `continue` 都是在给错内容盖「成功」的章**(`_must_text` / `_must_block`)
  —— 强转和丢成员都得到**非空**结果,于是「正文为空就抛」和异常兜底**都没有机会介入**(残缺不是空)。必需 = 块的
  `text`/`type` 和块列表的**成员**;可选(图片地址、标题)认不出来跳过那一块,**但同样不许强转**。**空值跳过、非空错
  类型一律抛**:真的空块是 `{}`,走正常路径留一个空行;而**非空的错类型 `article` 不是「确定没有长文」**,缺失、
  `null`、空对象、空列表、空串才照旧返回 `None`。**后路那一侧是同一条缺陷的两个位置**,各有自己的回归。
- **`render` 只按块级转 markdown,块内 inline 样式一概不动**(`inlineStyleRanges` 的偏移是 **UTF-16 码元**)。另
  两处形状坑:`entityMap` 是**列表不是字典**(键一律按字符串比);图片要 `mediaItems[].mediaId` →
  `media_entities[].media_id` → `original_img_url` 三跳,**查不到就跳过、不许编一个**。**正文里不写标题**
  (`sync.export` 自己会写)。
- **`needs_article` 三条判据**:①正文里**明写着**一条文章地址(**两种路径形式都算**);②**时间线上看见了那张长文
  卡片**(`x_article.HINT`,从 DOM 里读,排掉引用推容器里的那张,**只补不清**);③推文自己一个字都没说。**不拿
  「正文里有 t.co 短链」当信号。** 那个信号键**不进 `feed_items`**(`_strip_hint`,`fill` 每个出口都过它)——
  `put_items` 把整份条目哈希成 `content_hash`。`links_only` 是桥接那一档。
- **①那条判据要认 X 的两种文章路径形式**(`/i/article/<article_id>` 是 t.co 展开出来的,
  `/<handle>/article/<tweet_id>` 是 X 自己 302 到的**规范形式**),**而且和 `lead_text` 共用同一条正则**
  (`_ARTICLE_LINK`)—— 少认一种 = 那条链接本身当成正文入库、永久固定。**两处都要跟着改**:判据① 决定「要不要去
  问」,`lead_text` 决定「问到之后正文开头留什么」。handle 的字符类不许放宽(`[A-Za-z0-9_]{1,20}`)。
- **地址识别那一族判据,动之前必须读 `docs/decisions/x-article.md`:每一条收窄都对应一类真实写法,合并或放宽任意一
  条都会静默丢数据。** 两个消费方方向正好相反 —— 判据① 是 `search`(漏认 = 空正文永久固定),`lead_text` 是 `sub`
  (多删 = 作者写的字永久消失),所以**地址主体认宽、尾巴和起点一律收窄**。
- 不许动的那几道:`_URL_TAIL`(**只摘地址本身**,紧挨的标点和文字一个都不许跟着走;**不许写 `\S` 或 `\w`** —— 前者
  含汉字,而 str 模式下 `\w` 也是 Unicode 的;结尾必须落在「不像句读」那一档);**起点按候选带不带 scheme 分两档**
  (合成一档必然漏认一头);`_nested` 三步、**两种嵌套证据各管一类入口缺一不可**;`_URL_RUN` 按 RFC 3986 取**全
  集**(含 gen-delims)、**不许和裸域名那档的禁止集共用一份**(少认一个字符,外层起点就追不回来);`](` 是**起手标
  记不是终止符**,`_text_closed` 连显示文字一起收、**扫描范围是本行**;`_inert` + `_code_spans` 按 CommonMark 判哪些
  方括号是字面(**找闭合反引号串按字面、不认反斜杠转义**,而跨度外那个转义分支照旧留着)。
- 已知两道缝,知情且接受:非 ASCII 的外层路径、跨软换行的显示文字。
- **`bridge` 这条通道也接上了,钩子是 `x_channels.fill_bridge`、不在 `fetching`。** **桥接这条路的扣留 = 本轮不入库
  + 这一轮的 `etag`/`modified` 一个都不存** —— 它没有水位可以留在原处,存了 validators 的话下一轮 304,被扣下的再没
  有第二次机会。`resolved_url` 照旧存着。
- **补不到的那几条本轮不入库,而且这一轮算「没读完」**(水位留在原处,面板显示「部分同步」)。
- **两个开关各管一条路**:`KB_X_ARTICLE_API=''` 关掉接口(这正是「不想把推文 URL 发给第三方」要的那一档);
  `KB_X_ARTICLE_BROWSER=0` 关掉后路。**后路成功要把 `_GIVE_UP_AFTER` 的计数清零。**
- **失败文案只取 `fetching` 那句话分号前的那一半**(后半截在长文语境里是胡话)。
- **存量回填是 `scripts/x_article_backfill.py`(scan / run --yes)** —— 采集那侧修好只对**新**长文生效。它只改
  feeds.db 的 `title`/`body`/`content_hash` 并清 `exported_hash`,不写 kb.db、不写 vault。**`lang` 要一并清成空串**
  (空正文那会儿按占位标题判成了中文,而 `lang='zh'` 永远进不了翻译队列);不想花翻译钱就把 `translate_tries` 设成
  `translate.MAX_TRIES`,**别改成 `lang='zh'`**(那是谎报语言)。写回按元规则带条件。
- 读文章页的 JS 在 `x_article_js.py`、回包结构判定和块级零件在 `x_article_shape.py`、地址识别在
  `x_article_links.py`(起点判据再往下拆进 `x_article_nest.py`),**只因为行数**。
  `API`/`PAGE_WAIT`/`BROWSER_FALLBACK`/`MAX_CHARS` 一个都没搬(它们被 `patch.object` 改过);搬过去的名字一律原样再
  导出。
- 详细见 `docs/decisions/x-article.md`。

## 聚合源的正文补抓(kb/feeds/fulltext.py + readable.py)

- **feed 只给一个跳转壳时(标题 + 域名 + 得分),顺着原文链接把正文抓回来** —— 按壳入库的话 vault 里那篇 md 没
  有内容:检索搜不到、打标判不出实体、日报里只剩一行标题。
- **开关是订阅级的、默认关**(`subscriptions.fulltext`):对本来就给全文的源再抓一次是纯粹的降级。
  `defaults.json` 里**只标了 HN Buzzing** 一条;那段只对新库跑,存量库要在面板上点一下。**给 defaults.json 加字
  段必须同时改那条 INSERT。**
- **独立一列,不放 `config`** —— 换链接时 `save_subscription` 的 `reset` 会把 config 整个清空。
- **这一步排在翻译之前。还排在补抓队列里的条目既不翻译也不导出**,两边各认一次本模块的 `queued_sql`。队列判据:
  属于某条开了补抓的订阅、`fulltext_at` 空、`fulltext_tries < MAX_TRIES`;**出口就是第三条**(试满仍抓不动的按
  feed 原本那份壳走完翻译和导出)。`sync._export` 里收窄 base 那一句**必须排在所有统计之后**。
- **「在队列里」和「没入库」是两件事,按 `exported_hash` 分两态**(4 `fetching` / 5 `refetching`)—— 打开这个开
  关最常见的时机正是「几百条壳已经在 vault 里了」。已导出那一档如实显示库里那份壳、照旧能展开,顶上那句计数也只
  数还没入库那一档。**待补抓的条目标题照显示**,换掉的只有正文那一段。
- **提取器自己写,不装 readability/trafilatura**(我们要的输出是**纯文本**,而块级边界已经由 `fetching.plain` 定
  死;而且 `pyproject.toml` 一动 `uv run` 就要重建 editable 包)。
- **`find_all` 是快照,`decompose()` 之前必须先问「这个节点还在吗」**(`_gone`;bs4 把被销毁节点的 `attrs` 置成
  None)。
- **`header`/`footer`/`aside` 要看位置,不能无条件删** —— 只有落在 `<article>`/`<main>` **外面**的才是站点部件
  (`_DROP_OUTSIDE`)。按 class 名剔噪声只对 `div`/`section` 做。
- **提取不出来不是故障,是「这一页我们读不懂」**:保留原本那份壳、条目照旧入库、`fulltext_tries` 计次(不计次的
  话这条永远排在队首)。宁可少补一篇,也不能把导航栏当正文译了写进知识库。
- **「抓到了一页东西」不等于「拿到了正文」:版权声明和作者简介块要当成没拿到**(`readable.boilerplate_reason`,
  两道:授权措辞句子占比 ≥ `MAX_LEGAL_SHARE`(0.5),或开头是作者简介区抬头而整块 ≤ `MAX_BYLINE_CHARS`)—— 这两
  种页面**结构上完全正常**,于是补抓报成功、离开重试队列、接着买一次翻译,**这一档不可恢复**。
- **署名不是作者简介,`_BYLINE_HEAD` 只认「这一块是作者简介区」的抬头**(`about the author(s)` / `author bios?`
  / `作者简介` / `关于作者`)。`written by` 一度在名单里,而它是正常文章的标准开头。**判据是措辞,不是段落数。**
- **原文链接后面不是网页时按抓取失败处理,绝不把文件语法当成正文**(`readable.html_problem`)。**三道,各管一类
  撒谎方式**:①**声明的类型**不在 `_TEXT_TYPES` 里;②**文件头**命中已知魔数(PDF 的首个压缩流常在几十 KB 之后,
  RTF/PostScript 整份都是 ASCII);③头部有 **NUL 字节**,**而且拿 `decode` 真解一次之后它还在**(UTF-16/32 的
  **正常文本**每隔一个字节就是 `\x00`)。**判据是「拿真正会用的那个解码器解一次」,不是另开一张编码白名单。** 配套
  `decode` 自己要认 BOM(`_BOM_CODECS` 排在 HTTP charset 和 meta 前面,**UTF-32 必须排在 UTF-16 前面**)。
  `text/plain` 留在放行名单里。
- **存量复查要连那份 body 一起换掉**(`scripts/feed_fulltext_recheck.py`)—— 光清三个标记的话,重抓三轮失败后它离
  开补抓队列,那段版权声明会被**重新翻译一遍、重新导出**。换完它就是一条「从没补抓过」的普通条目
  (`content_kind='summary'`、`content_hash` 重算),**是正确的降级**。壳从备份库恢复(`--backup`,显式给了就只认
  那一个)或按标题链接重建;恢复回来的要**再过一遍判据**。`content_hash` 的口径抽成 `fulltext.body_hash`。**射程
  是「属于开了补抓的订阅」,不是「报过补抓成功」**(`_suspect`;后者漏掉的恰恰是这个脚本自己制造的中间态)。写回
  按元规则带 `AND body=?`(比旧 body 本身,不比 `content_hash` —— 那一列有两个写入方)。
- **失败过的优先**(`ORDER BY fulltext_tries DESC, fetched_at DESC, key`)。
- **`_save` 要把旧译文一起清掉**(旧译文译的是那份壳),`translate_tries` 归零。**但不许清 `exported_hash`** —— 导出闸门是
  `exported_hash<>content_hash`,正文一换前者就自然成立;而它同时是「这一篇入没入过库」的事实来源,清掉会让一篇**已经能展开阅读**的旧条目标题连同入口一起消失。
- **`subscription_items` 要一条按 `item_key` 的索引**(主键是复合的,按 item_key 查用不上)。
- **正文从壳换成全文之后,按旧壳算出来的提取结果要作废**(`fulltext.retire_stale`,由 `sync._export` 在导出之后调
  一次)—— 不作废不是「晚一点更新」:`extracted_at` 非空意味着这一篇**永远**不进 `pending_docs`。四件事:①**时点
  是「新正文真的落了盘」**(`extract_stale=1 AND exported_hash=content_hash`),不是「网络抓到了」;②**做完才把标
  记放下**,顺序不能反;③**异常要接住**;④**摘哪些 tag 只有一份口径**(`STALE_TAG_PREFIXES` + `keep_tags`,只摘
  `topic/` 和 `entity/`,`flag/软广` 和人工 tag 留着)。不清 `promo_level`,也不动 `store.upsert_document` 的
  `extracted_at` 语义。
- **一次提取的结果要和它基于的那份正文绑在一起**(`extract.body_fingerprint` + `save_extraction(..., body_seen=)`)。
  **两道判据各管一种时序、缺一不可**:①**索引还没扫到当前这份 md**(`store.doc_needs_reindex`);②**索引里那份正
  文已经不是我读到的那一份**。判据待在 `save_extraction` 里,**读 chunks 正文 + 有模型往返的那三条路径**必须传指纹
  (`kb/cli.py`、`kb/pipeline_stages.py`、`scripts/extract_parallel.py`),不读正文的两条不传。**`StaleBody` 不是故
  障**,三条路径一律归到「跳过」。
- **校验本身给不了保证,它必须和写入在同一个协调区间里**(`extract.commit_extraction`:维护锁 → `BEGIN IMMEDIATE`
  → 锁内校验 → 落库 → 提交 → 回写),锁内还要先问一次「这一篇还在吗」。**粒度仍然是每篇各取一次锁、模型请求留在锁
  外。** 反过来,**md 压根读不出来(清理正把它删掉)不算「版本不对」,照旧放行。**
- **`documents.content_hash` 的口径抽成了 `scanner.file_hash`。手摆 `documents` 行的测试摆盘必须按它算
  `content_hash`**,否则摆出来的是一个生产里不存在的状态。`_extract_pass` 的 `stats` 一律用 `_new_extract_stats()`
  构造。
- 详细见 `docs/decisions/fulltext.md`。

## 正文纯文本化与阅读渲染

- **只有块级元素换行,行内元素一个分隔符都不插**(`fetching.plain`)。别退回 `soup.get_text('\n', strip=True)`:那
  个分隔符对**每一个文本节点**各插一次换行,而 `<a>`/`<em>`/`<code>` 正是把一句话切成多个文本节点的东西,**段落边界
  跟着一起塌掉**(后果三重、只有第一重看得见:面板一行行照显、导出的 md 整篇没有空行、翻译按 `'\n\n'` 切块于是整篇
  算一个段落从句子中间硬切)。
- **边界分三档,各插一个标记,相邻标记合并成最强的那一档**(`\x02` 块级 > `\x03` 换行 > 空格),前后各插一个而不是
  只插一个(不依赖兄弟节点顺序)。`<br>`/`<tr>` 只断行,`<td>`/`<th>` 用空格,`<li>` 归块级。
- **`<pre>` 先整块摘出去、最后再放回来**(NUL 占位符),放回必须排在行清理之后;入口先把 `\x00`/`\x02`/`\x03` 从
  html 里清掉。**摘的时候换掉的是内容,不是整个标签**(`tag.string = 占位符`)—— 整个 replace 之后 `find_all` 找不
  到这个标签,它自己那道块级边界跟着一起没。
- **空白折叠要赶在插标记之前做**;逐个文本节点替换时判 `type(x) is NavigableString` 而不是 `isinstance`
  (`Comment`/`CData`/`Doctype` 都是它的子类)。
- **先问正文是什么类型,再决定怎么解析**(`body_text(value, kind)` 是唯一入口)。**纯文本里的换行就是内容**,所以
  纯文本只做最轻的归一(`_plain_lines`)。类型要跟着值一起取(`content[i].type` / `summary_detail.type` 各有一份)。
- **分流只问类型,不看内容长什么样。** 别加「内容里有标签就按 HTML 处理」那种嗅探 ——「这段文字能被解析成标签」证
  明不了作者标错了类型,`List<T>`、尖括号写的链接、字面 HTML 示例全是这个形态。类型取不到就按 HTML 走,并容忍
  `None`。
- **标题和作者走 `oneline`**(plain 之后把换行压成空格),**也要带上类型**。
- **实现里不许有递归**(畸形 HTML 嵌套几千层只是慢,不会 `RecursionError` 带崩整个源)。
- **存量不会自动修复**(`put_items` 是 `DO NOTHING`),改这里只对新条目生效。
- **vault 里文章正文按契约已经是纯文本,所以阅读页(`/doc/`)正文里剩下的尖括号一律按字面文字显示**
  (`kb/web/pages.py` 的 `_DOC_LITERAL_JS`:`marked.use` 把 html token 转义掉)。**不是关掉净化**,DOMPurify 照旧过
  一遍。**不许改成「渲染前把整份 md 里的尖括号先转义掉」**(那一刀落在 markdown 解析之前,**代码围栏里的内容是字面
  量**),**也不许在导出那一侧转义**(md 是事实来源)。**只改这一页**,`core.js` 的 `mdHtml` 渲染的是模型写的
  markdown。`renderer.html` 的入参 marked v12 给字符串、v13 起给 token 对象,两种都接住。
- **正文是嵌进页面内联 `<script>` 的,所以那份 JSON 里的 `<` 一律编码成 `\u003c`**(`_script_json`),**不是只防
  `</script>` 那一种写法** —— 脚本数据状态里 `<!--` 之后再出现 `<script` 会把分词器推进「双重转义」,于是整段脚本一
  行都没执行、阅读页**只剩标题**。放进 markdown 代码围栏里不管用(故障发生在 markdown 解析**之前**)。
- **大V观点那一档不显示标题,别再加回来**(`sources.js` 的 `renderSourceItems`)—— 推文没有「标题」这回事:采集器把
  正文前 500 字符原样当 title,于是卡片上那行大标题和紧跟着的正文是同一句话读两遍。阅读弹窗同理不显示它。**判据是条
  目自己的 `content_kind === 'post'`**,不是「当前视图是不是 social」。去掉标题之后正文从 muted 摘要提成次要正文
  (`.source-excerpt.is-post`);**等翻译那一档照旧显示那行状态文案**。`.source-entry-title` 要
  `white-space: normal`(base.css 给所有 button 设了 nowrap)。
- 详细见 `docs/decisions/feed-plain.md`、`doc-reader.md`、`source-cards.md`。

## 订阅的历史深度(kb/feeds/history.py + registry.py + sync.py)

- **新订阅只收最近 `reg.HISTORY_DAYS`(5)天的内容**(`sync._within_history`)—— 一个刚添加的源常常把整个存档一次交
  出来,这些旧文随后逐批翻译、逐批重新导出,每重新导出一次 `indexed_at` 就刷新一次,于是**连着十几天以「补录」的形
  态占满当天日报**。
- **下界是订阅创建(或改链接重置)那一刻钉死的一个日期**(`subscriptions.history_floor`),**不是「距今 N 天」的滑动
  窗口**(滑动窗口会在源站停更几天后恢复时把那几天静默丢掉)。**也不能只拦「第一次抓取」**:第一次之后 etag 命中
  304,等源站下次更新时整份 feed 又回来了,那时闸门早撤了。
- **老库里的订阅补成空串 = 不设下界,行为逐字不变。**
- **没有发布时间的条目照收**(身上没有任何「很旧」的证据),日报那边按「补录」接住。
- **区间补抓(`scope.ranged`)不受这道闸限制** —— 那是唯一的逃生口。
- **`_within_history` 只管往后,存量要单独清**(`scripts/feed_trim_history.py`)。清理**同时会钉上 `history_floor`**。
  顺序:备份 → 钉下界 → 删 feeds.db 行 → 删 vault md → `remove_missing_documents`;**feeds.db 要先删**(它是「要不要
  重新导出」的事实来源)。**被别的订阅共享的条目只摘归属、不删行**,而且要**连文件和索引一起算**(`_live_paths`)。
  **收藏保护只按真要删的那些判。**
- **另一条筛选口径 `--pending-translation`** 直接取 `translate.queued_sql()`。**这一路不钉 `history_floor`**(筛的维
  度不是日期)。它和 `--floor` 不能一起用,**而这道互斥检查必须排在所有副作用之前,「续上没做完的那一份」和取锁都算
  副作用** —— 口径在 `cmd_run` 的第一句,**不在 `_run_locked` 里**。**有没做完的计划时,这一趟只续它、续完就
  `return 0`。**
- **「给了 `--floor` 没有」问的是 `args.floor is not None`,不许问真值**(`--floor ""` 是变量没赋值那种最常见的 shell
  事故,而这是不可逆删除)。
- **下界先校验再用**(`norm_floor`),非规范写法在备份和任何删除之前就拒掉:比较是**字符串**比较,`2026-10-1` 少写一
  个零就既大于当天那篇、又大于第二天那篇。
- **删除分三步、每一步做完记一笔**(`progress.json`),各步幂等,重跑先把没做完的续完。
- **清理和同步、索引之间有一把跨进程锁**(见维护锁那节),锁之外还要**「下界现读」**(`sync._stored_floor`)—— 两道各
  管一段,缺任何一道都会复发。
- **问不到管道状态一律拦下,只有连接被拒才放行**(连接被拒 = 服务没起 = 没有管道在跑,这是唯一能证明空闲的那种)。
- **`--source` 是纯数字时只按 id 查,不许退回名字**(`trim.find_source`)。**名字命中多条时拒绝执行、要求改用 id。**
- **备份目录要原子地占一个唯一名字**(`tempfile.mkdtemp`,前缀带订阅 id)。
- **续跑不是「接着执行上次批准的那件事」,收藏那道护栏要按此刻的事实再查一遍**,而且**必须和删除在同一个写事务里**
  (`_apply` 在 kb.db 上 `BEGIN IMMEDIATE`,最终复查、unlink、`remove_missing_documents` 全在这一个事务里)——
  **索引能从 vault 重建,收藏和备注不能**。配套:**删索引必须用持锁那条连接**;`--allow-favorites` 放行时把要毁掉的那
  几行另存 `favorites.json` 进备份。
- **没有旧文可清 ≠ 不用钉下界**;**没带 `--yes` 一个设置都不改**。
- **测试样本的发布日期必须跟着今天走**(`_FRESH`/`DAY`),写死日期过几天就被这道闸挡住。
- 详细见 `docs/decisions/history-depth.md`。

## 「重试失败项」的范围(kb/pipeline_retry.py + feeds/sync.py + pipeline_state.py)

- **这颗按钮的范围 = 继承上一轮的「来源」+ 各来源自己留下的失败名单**(`retry_crawl_scopes`)。继承的是「哪几个
  channel、上次点名了哪几个目标」,**不是「抓多少东西」** —— 后者按**来源逐个**收窄,外加一个 `retry` 字段说明这一项
  是怎么来的(`targets` / `all`)。
- **不收窄的两个缺口是反着的**:①继承来的 `accounts` 是空的,而空在 `sync.collect` 里正好是**全部**;
  ②`FetchScope.force` 这条路上压根没人传过,而失败恰恰会记一个 `retry_at`。
- **名单要在失败那一刻记下来,不能事后从别处反推**(`note_retry_targets` 写、`retry_notes` 读):按 `last_error` 筛会
  漏掉「已改正、还没验证过」的;从阶段状态那份 failures 反推拿到的 `title` 对公众号来源是文章标题;一个全局布尔会让一
  个来源的失败类型决定另一个来源抓多少。
- **必须按来源各记一份,因为「整批失败」和「只坏了一条」是两件相反的事** —— 两者在 `subscriptions` 那边长得一模一样,
  **分得开它们的只有「这个来源留下名单了吗」**。
- **「仍然需要重抓」的判据是「那一轮失败之后有没有成功同步过」**(`sync.still_failing`)。**名单的时刻要用采集器自己那
  套时间格式**(UTC ISO,不是本地空格格式)—— ISO 的 `T` 排在空格后面,整份名单会被摘空。`at` 因此是**必填**的。
- **一条都没失败也要记一份空名单**(那份空名单本身就是「这个来源扫完了」,而「压根没留下名单」另有含义)。名单**带着它
  自己那一轮的 run id**。
- **报不出名单的来源按原范围重跑**(`collectors.RetryNarrowing`)。**这不是缺陷,是另一种真实情况**(公众号的失败是文章
  级的,而它的 targets 是账号级的)。生成计划这一步**不许抛**。
- **「这个来源一个都不用抓」= 整个来源不进这一轮,不是给它一个空 `accounts`**:全空时走
  `retry_failed_plan(nothing_to_crawl=True)`,crawl 整个摘出计划。
- **export 走上一轮**原来**那几个来源,不跟着 crawl 收窄**,而**「原来那几个来源」必须自己落一份盘**(`pipeline_state`
  的 `export_filter`,三处写入口)。**常规同步那条路要把它重写成自己那一份**,不靠「没设过就回落」兜底。
- **造范围这一步的故障不许扩散到别的阶段上,三道各有射程**:①**先定计划,只有 `"crawl" in plan` 才去问名单**;②**认名
  字和问名单一起护在那道 `except` 里**;③**名单是空的就别开库**。
- **收窄要和 `targets` 取交集**,不是扫一遍库里所有失败的订阅。**「预算跳过」和「退避中」那两档不进名单**(压根没被尝
  试过,下一轮轮转本来就会补齐)。
- **没有要重抓的时要说清为什么、并指到正确入口**(`RETRY_NOTHING`,crawl 落 `skipped`)。
- **收窄过的那一轮要把范围说出来,而且按来源各说各的**(`pipeRunScopes` 透传 `retry`)。
- 详细见 `docs/decisions/retry-scope.md`。

## kb.db 的维护互斥锁(kb/maintenance.py)

- **写 kb.db + vault 的几条路共用一把跨进程独占锁**(`maintenance_lock()`,flock 在 `data/feeds-trim.lock` 上):存量清
  理脚本、订阅同步的存盘与导出、索引的两条入口(`pipeline_stages._run_index`、`cli.cmd_index`)、条目化的就地入库、提取
  的 tag 回写(**四个**调用方:管道 `_extract_pass`、`kb extract --writeback`、`scripts/extract_parallel.py`、软广补判经
  `sync_promo_tag` 进来的)、提取结果的交付(`commit_extraction`)。
- **口径是「凡是写 kb.db 或 vault 文件的入口」,不是「两条索引入口」** —— 条目化那条自称「只增不删」,可它末尾照样调
  `embed_pending_chunks`,而那个函数补的是**全库**待编码的 chunk。判据只能是**写哪张表** + **写不写 vault 里的文章文
  件**。
- **粒度是整趟,不是只圈写的那几句**(竞态的那一半在「读」),所以 scan → upsert → remove 一整段都在锁里。**向量编码也
  在锁里**:`chunk_vec` 是 sqlite_vec 虚表、**没有外键级联**,清理在中间删掉那些 chunk 的话后面几批会按不存在的 chunk
  id 插回向量,而这些孤儿行**照样占着语义检索的候选名额**。代价是一轮大编码会攥着锁几分钟,这是统一互斥的标价。
- **等待有上限,而且等的时候要出声**(`KB_MAINT_LOCK_WAIT`,默认 900 秒;`on_wait` 立刻一次、之后每 60 秒一次)。超时抛
  `LockBusy`,三处一律「什么都没动」收场。**无限等更坏**:管道是单例,一个卡住的持锁者会把面板同步、定时同步和日报一起
  冻死。
- **等锁期间还认「停止」,而且认它的回调跟出声那个分开**(`on_poll` 每 0.25 秒一次,抛 `_StopWhileWaiting` 让阶段落
  `stopped` 而不是红)。**`flock` 一成功就返回,所以拿到锁之后还要再认一次**,但**只在真等过锁时才补这一次**(无条件检
  查会把刚放进 index 的那一轮当场顶成 stopped)。
- **提取的 tag 回写也在锁里,而且锁内要先确认这一篇还在**(`_still_indexed`,不在就返回 False、**不抛异常**)——
  `writeback_tags` 是整份读改写,清理在中间把这一篇删干净的话那次写回**把 md 文件重新创建出来**,比 index 那条更糟(文件
  回来之后谁也收不掉)。**回写的粒度是「每篇各取一次」,不是整趟提取**,模型请求留在锁外。
- **取锁之前必须先把写事务落盘,而这件事由共用入口自己负责**(`if conn.in_transaction: conn.commit()`)—— **SQLite 的写
  锁不认这把维护锁**,攥着没提交的写事务去等锁,持锁的清理就卡在自己的 DELETE 上。判据是**两层**:共用入口自己 flush +
  调用点时序照旧写明白,另有一条回归把全仓调用点的名单钉住。
- **补判等不到锁时 `LockBusy` 照常抛出去、整轮中止**(`_flag_in_vault` 只接 `OSError`)。
- **同形状但没闭合的是 `wiki_daily.step_linkify`** —— 分界线:**管道自动跑的写 vault 入口都该取这把锁**,手工一次性运维
  脚本留在外面。
- **不会抱死**:这是唯一一把跨进程维护锁,没有任何路径持着它再去取别的锁;同线程重入直接放行(flock 认的是 open file
  description)。锁的是 `data/` 下**单独的 lockfile**,只创建不删除;**文件名沿用 `feeds-trim.lock`**,改名等于在升级窗
  口里让两个持锁者同时写。
- **`test_feed_translate.ColumnBackfillTests.test_two_first_accesses_both_succeed` 是一条已知的负载敏感 flaky,红了先别当
  回归。** 判据是它自己的报错内容:`duplicate column name` 才是它要抓的回归,`database is locked` 是 `reg.db()` 在
  `BEGIN IMMEDIATE` 之前那两句 DDL/PRAGMA 上的争锁窗口。
- **测试里必须把 `maintenance.DATA_DIR` 指到 tmp**,不指的话会去 flock **生产**那个 lockfile。
- 详细见 `docs/decisions/maintenance-lock.md`。

## 没有 feed 的站点(kb/feeds/page_list.py + source_types 的 `web`)

- **「网页专栏」是独立的一类来源,不是 rss 的一个开关** —— 判据是**失败形态不同**:读 feed 坏在对方服务,解析页面还
  会坏在**对方改版**,后者需要人去适配。混在 rss 里的话那条线的状态、定时和退避会被它带着抖。
- **加这一类来源只动三处**:`SOURCE_TYPES` 注册一项、`ADAPTERS` 注册一个 adapter、新写一个解析器。索引、来源组、中
  文别名、采集器注册表、`registry.KINDS`、面板的来源 tab 全部从 `feed_types()` 自动派生。
- **定位判据只有一条:带发布时间的那一组链接就是文章列表。** 不认 CSS 类名(Next.js 的类名带构建哈希)、不认 tag
  名、**也不认「哪一组链接最多」**(实测最大的链接组是 90 条页脚菜单,真正的新闻列表只有 10 条)。
- **分组按结构路径**(从根到 `<a>` 的 tag 序列),不按父节点。
- **分开之后要全都收下,不是只取胜出那一组**(只留带日期条目最多的那一组等于把「单独展示的头条」整组丢掉,而漏掉的
  恰恰是最新那几条)。判据拆两层:**整页至少有一组够 `MIN_DATED` 条带日期**(只问一次),然后**收下每一个「够
  `MIN_DATED` 条带日期、或者每条都带日期」的组** —— 后半句是给单条头条卡片留的门,逐组去问第一层就把它永久关在门外。
- **记录容器要在整页的尺度上算,不在组内算**(`_containers` 收全页的 anchors)—— 组内计数时,只有一条链接的那一组里
  每一个祖先都「只属于它」,容器一路长到根节点,于是把页面别处的日期和最长的那段文字当成自己的。
- **一组里至少 `MIN_DATED`(2)条带日期才算列表**,而**宁可报错也不要把导航当列表**(误报用户看得见,漏报是往知识库
  里灌垃圾)。
- **标题:`h1`-`h6` 优先 → 摘掉 `<time>` 那个 meta 容器 → 剩下的块取最长。** 中间那一步是**结构**判据,不是长度判
  据:**标题可能比分类还短**,那时「最长块」拿回来的是分类,整条订阅的标题全变成同一个词。摘完一块不剩时退回全部块。
- **文本块遍历用 `node.descendants` + `type(x) is NavigableString`**,不用 `stripped_strings`(后者交出来的是 `str`、
  没有 `.parent`,摘不掉 meta 容器)。
- **「这一页上一条都没解析出来」一律抛异常**(口径见元规则),三档各给自己那句指引(填成了文章页 / 是 feed / 页面声
  明了 feed)。
- **反过来,能解析出列表就解析,哪怕这个站其实有 RSS** —— 在采集时拦会让一条已经在工作的订阅因为「对方某天加了 feed
  声明」而突然停摆。
- **正文这一层不碰,靠订阅级的 `fulltext` 补抓**,所以 defaults 里那条 Anthropic **必须** `fulltext: true`(回归直接
  读那份清单钉住)。
- **壳正文有两条硬要求**:①**有字**(正文为空的条目会落进 `body_gap` 那个「需回填」的永动清单);②**不重复标题**(面
  板上标题就显示在正文正上方)。所以壳是 `分类 · 日期` + 链接,**也不写「正文待补抓」这类状态**(补抓失败时它会永久留
  在 vault 里)。
- **日期精度如实保留**:列表页给到日就只给到日,不补一个 00:00 假装知道几点;时区按元规则。非法日期不算日期。
- **记录容器按祖先计数算,不对每个祖先 `find_all` 一遍**(后者在扁平列表上是 O(n²),而采集预算只有 90 秒)。实现里没
  有递归。
- **写死来源清单的测试要改成跟着 `feed_types()` 走** —— 写死的话**加任何一个来源都会红,而红的地方跟改动毫无关系**。
- 详细见 `docs/decisions/page-list.md`。

## 邮件简报(kb/feeds/digest.py + source_types 的 `digest`)

- **TLDR AI 这类简报,一期一个汇总页,内容在页里并列排布。** 三条路实测都堵死了:官方 RSS **连 `description` 都没
  有**、没有「最新一期」的固定地址、**不能按日期猜地址**(未发布的日期返回 **200 + 营销页**)。于是「有哪些期」只能
  从 RSS 拿、「正文」只能从每期落地页拿:**两跳绕不过去**,所以它走预取、形状照抄 X 的登录态通道。预算另算一份
  (`DIGEST_SECONDS`,240 秒,**串行**)。
- **拆块的判据不复用 `page_list`**(那边按「链接的记录容器」算,而 TLDR 有带第二个链接的块,容器会停在 `<a>` 上、
  **正文整个被排除**)。这里要的是**一页上并列的、结构路径逐字相同的重复块**,每块 = 一个外链 + 一段够长的文字
  (`MIN_ITEMS`=3 / `MIN_BODY`=80)。
- **不要求每一块都合格,够 `MIN_SHARE`(2/3)就算简讯区 —— 合格的收下、不合格的跳过。** 真网络实测:某一期夹着一条
  TLDR 自家的广告(链接指向**站内子域**),按「每块都要合格」判,**整整一期 18 条一条都拆不出来**。
- **广告位认站点自己的声明,不认域名**(`_AD` 认一小组通行措辞,**半角全角括号都认**)—— 维护一张域名名单会漂。
  **招聘位不特判**,这是有意的。
- **水位记的是「收到哪一期了」,一期是原子的**(`digest_issue` 存在 `subscriptions.config`,经 `config_patch` 和条目在
  **同一个事务**里提交)。**从旧到新抓,某一期失败就停在那儿、后面几期即使拿得到也不推水位。**
- **一轮的额度 `MAX_ISSUES`(6)只许截掉新的那一头,所以截断必须排在排序之后** —— 水位是一个标量、只能**连续**推进,
  而截断压根不算失败。先截再排的实测后果:停机攒下九期时第一轮收最新六期、水位推到最新,中间九条简讯**永久缺失**,两
  轮都 `errors=[]`。代价是积压时先补旧的那几期。
- **历史下界也在选期这一步筛一道**(`_harvest_one` 日常那一支按 `history_floor` 筛)—— 下界之前的期号无论如何都入不了
  库,而**预取是唯一知道「要不要发这个 HTTP」的地方**。**区间补抓不筛。**
- **日期认不出来的那一期不许被静默丢掉,而且两个日期条件必须一起判**(`_wanted`)—— 日期在这条通道上是承重的(选期、
  下界、水位全靠它),原来空日期被下界和水位**各剔一次**、水位又照常推进,**那一期简报静默消失**。**只修一边不够**:
  新订阅带着非空下界、老订阅带着非空水位,各自都能单独把它剔掉。三档:
  ①**feed 给了 `pubDate` 就用它**;
  ②**没给就从期号地址里认一个**(`_url_day`)。**只认 path 的最后一段** —— 认出来的日期是**会把这一期排除掉**的,认错
    一个就是静默丢一期(写成「扫整条 path 取最右边」的话 `/archive/2026-10-09/latest` 会把归档目录名当成期号日期)。只
    认真日历日;一段里有两个日期时取最右边。
  ③**两头都认不出来的照收**(口径同 `sync._within_history`)。**收下之后按地址记一笔**(`digest_undated`,和水位走同一
    份 config 补丁、同一个事务):它不受水位管、排序时又恒排在最前面,**一旦这种期号多过一轮额度,排在额度之外的那些就
    永远读不到**。**读失败的不记**(记了它就再也不会被重试)。
- **那份名单的上限是「当前那份清单」自己,不是一个常数**(`taken_patch` 收这一轮列出来的期号,存回去的永远是它的子集)
  —— 按容量淘汰会把真正未读的那几期**饿死**。**把那个常数改大治不了**:判据得和「已接受的输入」对齐,而那就是清单自己;
  按清单裁剪之后未读集合**单调缩小**。**清单这一轮没拿到(空的)时一条都不裁。**
- **③ 那一档的条目带着空 `published`**,日报那边按「补录」接住;**区间补抓那一支照旧只认有日期的期号**。
- **剩了几期没读必须说出来**(`_unread_note` → `note` → `summary['partial']` → 阶段文案)。**区间那一侧更要说** —— 它不
  推水位,下一轮不会自动接上,所以提示里给的是可操作的下一步(「把起始日期改到 X 再补一次」)。读完的那一轮不留提示。
- **区间补抓要把那段日期交给预取,而且不许动日常水位**(`prefetch(since=, until=)`)。**这条通道是预取的,所以
  `unconditional` 到不了它**:`fetch_source` 只是 `take` 走已经抓好的那一份,而 `_save_result` 的日期筛选排在拿到结果
  **之后** —— 预取时按日常水位筛掉的期号,后面没有任何一步救得回来。筛选是**闭区间、按日期前缀比**;水位在 `prefetch`
  里就摘掉。
- **点名的那段日期一期都没选中时,要说出为什么**(期号清单只能从 RSS 拿,它只留最近几十期;不说的话和「筛选写错了把它
  排空了」长得一模一样)。
- **一条都拆不出来一律抛异常**(口径见元规则)。这里有个**特有的**形态:那一期还没发时落地页回落成营销页(HTTP 200),
  和站点改版长得一模一样,所以文案把两种一起说。
- **这条通道不开 `fulltext`,`defaults.json` 里那条明确不标**(正文已经在落地页上了,而那个外链指向的是**别人家的原
  文**)。
- **身份用去掉跟踪参数之后的站外原文地址**(`canonical_url`)。
- **多期样本必须给各自不同的链接** —— `put_items` 按 `sha256(kind:url)` 去重,拿同一批链接当多期样本的话「第二期一条都
  没新增」会被误读成去重正常,正好把漏期这件事盖掉。
- 详细见 `docs/decisions/digest.md`。

## 默认订阅的补种(kb/feeds/registry.py 的 `DEFAULTS_REV`)

- **往 `defaults.json` 里加来源,存量库也要拿到** —— 原来只有 `defaults-v1` 一个标记,于是新增默认订阅对**所有存量库永
  久不生效**,而新装的机器上它好好的。
- **口径是修订号,不是「这条订阅还在不在」**:`defaults.json` 的条目标 `since`(不标的算 1),库里记 `defaults-rev`。
  **按名字查一遍补的话,用户删掉的默认订阅每次重启都会回来。新增默认订阅时记得 +1 并给那一条标 `since`。**
- 补种的那几条**对这个库也是新的**:照样 `history_floor()` 只收最近几天。`INSERT OR IGNORE` 是给「用户自己手工加过同一
  个地址」兜底的。
- **断言「旧库现在是空的」必须用裸 sqlite3 连接**(`reg.db()` 一打开就补种)。
- 详细见 `docs/decisions/defaults-backfill.md`。

## 英文内容翻译(kb/feeds/translate.py)

- **翻译在导出之前,不在阅读时**(vault md 是事实来源,索引/提取/检索/日报读的都是它):`materialize()` 里先
  `translate_pending` 再 `export`。**「该显示哪一份」只有一份口径**(`translate.display(item)`),导出和来源面板共用。
- **语言判定是数字符,不问模型**(`registry.text_language`,CJK 占字母数 ≥12% 算中文)。**一个汉字都没有、又有字母,一律
  判外文,不设长度下限** ——「Hello world」被稳定判成中文、永远进不了翻译队列,而 social 的短动态整段都是这个形态。产品名
  保留原文靠的是 prompt 里那条**译文规则**,不是把英文冒充成中文。真正判不准的只有「有汉字但太短」那一档
  (`_SHORT_MIXED`)。**判定规则收紧要连存量一起复判**(`_label_unknown`)。
- **改判会清 `exported_hash`**(不然内容变了却和 `content_hash` 相等,下一轮导出直接跳过)。
- **重导出要把别人写进那篇 md 的 frontmatter 字段留下来**(`sync._OWNED_META` / `_kept_meta`)—— `export` 是整份重写文件,
  抹掉提取回写的 `tags` 之后,索引按 frontmatter 重建 `doc_tags` 而 `extracted_at` 还在:那篇文章从此按原话题**再也**搜
  不到。保留字段拼在 meta **最后**(`writeback_tags` 是往末尾追加的)。
- **翻译失败必须合进导出阶段的最终状态**(`export(..., translated=summary)`)—— 导出是这条链上最后一个报阶段状态的人。
  **扣下的条目分两档**:`stuck`(译过、报了错、还扣着)判红,`held - stuck` 是正常积压。**试满 `MAX_TRIES` 按原文入库的不
  算失败**(那是设计好的出口)。
- **这几档要在整个翻译队列里数,不能只数「待导出」的那些**(按英文导出过、内容又没变的条目压根不在待导出集合里,可它照样
  排在队列里等补译)。四档分开说:`fresh` × `bad`;已经入库那档照样判红,只是文案写「库里仍是原文」。
- **导出循环里那个「这一篇是不是译文」的标志不许叫 `translated`**(现在是 `is_zh`)—— 那是 `export` 的形参名,同名的话收尾
  那句 `.get('errors')` 当场 `AttributeError`,而那时成功的文章已经落盘,异常却把 `materialize` 连同后面的 index/extract 一
  起带崩。
- **译文那几列是 ALTER 补上去的,补列和它前面那次列集合读取要在同一把写锁里。**
- **取数是「失败过的优先」**(`ORDER BY translate_tries DESC, fetched_at DESC, key`)—— 升序的话译不动的永远排在「从没试
  过」后面,按一轮 40 条算要三周才轮得到一次重试。
- **导出的失败文案和判红只认「本轮」,此前失败单独一档、不判红**(`sync._new_failures`)。分档口径是「这一轮真失败的那几个
  key **回库里再查一次**」,不能直接用摘要里的 `failed` 计数。
- **写回译文之前要确认「这一条还是我发请求时那份标题和正文吗」**(`input_version` 是唯一口径,`_save`/`_fail` 各带着它走
  `WHERE key=? AND title=? AND body=?`)—— 旧回包按 `key` 盖回去之后 `translated_at` 重新设上,这一条**再也不进翻译队
  列**:正文是新的、中文却是旧内容的译文。**失败那一侧同样要比**。`summary['stale']` 既不算 done 也不算 failed。**「比标
  题」那一半今天是冗余带**,如实记着 —— 留着是因为半成品那条路的请求输入**只有**标题。
- 成本闸门:一轮最多 40 条、每条正文 12000 字符、失败退避 3 次。**没配 key 是 `skipped`,不计入重试次数。**
- **还排在翻译队列里的条目这一轮不导出**(`queued_sql()` 是唯一口径)。队列两个出口缺一不可:试满 `MAX_TRIES` 按原文导出;
  **翻译整个被跳过时(没配 key)一律不拦**。
- **队列判据是 `translated_at` 空不空,不是「有没有 body_zh」**(只有一行标题的短动态译完之后 `body_zh` 仍是空串)。**改了
  判据就得把存量里那批老形态迁过来**(`PARTIAL_SQL` / `_requeue_partial`,每轮跑一次、幂等):旧判据和新判据**都选不到**它。
- **扣着不导出的条目,来源面板也得标出来**(`items()` 给每行一个 `translating`,口径还是 `queued_sql()`),**标题和正文位置
  都写中文状态文案**,原文入口留在右下角那个链接上。这个标记必须和导出闸门同真同假,所以还要问一次 `translate.available()`。
- **「在队列里」和「没入库」是两件事,按 `exported_hash` 分两态**(`translating` / `retranslating`),后者**如实按原文显示、
  可以展开**。**还有第三档 `partial`**(正文中文、标题还是原文),**排在两档队列状态之前**。**「已译为中文」的判据前后端必
  须同一个**:正文有译文、而且标题(只要原标题不空)也有译文。
- 正文按段落边界切 3000 字符一块,**标题跟第一块一起译**;回包按 `###TITLE###` / `###BODY###` 解析。**标题和正文都得有译文
  才算译完** —— 模型漏掉分隔标记时补问一次 `_TITLE_ONLY`,补不到就抛错重试。正文为空的条目直接走「只译标题」。
- 详细见 `docs/decisions/translate.md`。

## 每日日报(kb/daily.py + kb/daily_run.py)

- **窗口是「上一份日报的截止时刻 → 这一次的生成时刻」,不是自然日**(`daily.next_window`)。上一份的截止时刻记在**它自
  己的 frontmatter**(`window_until`)里,链条自描述。**算起点时跳过「今天那一份」**,所以一天里再生成一次就是**重算同
  一份**。**没有「最长回溯几天」这种上限** —— 上限会把起点往后挪,被挪过去那几天再也进不了任何一份日报;素材太多是**限
  量**问题,窗口断链不能补。**截止时刻在同步跑完之后才定下来。**
- **「这一条上报过没有」按账本判(`daily_carried`),不靠时间窗推断** —— 没带走的留在队列里,下一份接着带;「时间窗 + 取
  最新 N 条」会让落选条目在下一个窗口里两个桶都选不到,静默永久丢失。三条不许动:
  ①生成一份是**放回 → 落盘 → 盖章**三步:**先把这一天上一版的章放回**(否则被挤出清单的旧素材既不在文件里、又记着已上
    报),盖章再排在落盘**之后**(章盖不上只是下一份多报一遍,看得见);
  ②「落盘+记账」与「放回+删文件」整体互斥(`daily_carry.commit_lock`),启用时的一次性盖章也在这把锁里,**删一份日报要
    先盖章再放回**(它带走过什么只写在那个文件里);
  ③盖章两维判:**到达分界线**(`indexed_at` 晚于它的不盖;**分界线一旦钉下就不许再往前挪**)+ **那一份自己的素材清单**
    (`daily._carried_keys`;按窗口推断会把补录记成存量、把当时装不下的记成报过了,两处都不可恢复)。
  **改了盖章算法要把 `SEED_REV` 加一**(否则已经盖错过的库永远走快路径),**而那一趟只重算归属**;**清单解析也算盖章算
  法**,改了它同样要加一。**清单上露过面的内容一律不许判成「链条之前的历史存量」**(那一道兜底把代价从「永久写死」降到
  「多报一遍」);它只用来**否决**终态,不拿来盖归属,**而且那份原文要独立于切分**(`PastReport.text` 是整份正文)。
  **清单起点按抬头自己声明的条数挑**(`## 素材清单(N 条)`,切出来得恰好是 N 条)—— 这几个字在文章标题和推文里都出现
  过,按位置找必然在某个方向上错,而错一次是**一份日报里好几条一起**写死。
- **筛的是 `published`(内容的生产时间),不是 `indexed_at`。** **时间字符串必须解析成时间再比**(`daily.parse_ts`,库里
  实测四种写法)。SQL 只按日期前缀粗筛(两头各放一天),精确判断在 Python 里做。
- **发布在窗口之前、这一轮才入库的内容进「补录」**(`Material.late`),单列一节、单独计数、prompt 里明确要求不混进要点。
  没有发布时间的同样走这条路。
- **日报写进 vault(`<vault>/daily/YYYY-MM-DD.md`),不写 kb.db。** `daily/` 不在 `SOURCES` 里,否则日报会变成明天日报的
  素材。
- **「同步所有内容」= 依次借用 `kb.pipeline` 跑四个阶段,一个通道一趟。不能并发**(管道是单例)。条目化/wiki 不跑。
  **超预算(默认 3 小时)就请求停下、直接生成。**
- **用户按的「停止」和超预算是两件事,别共用一个标记**(`stop_requested` / `over_budget`)—— 超预算的本意就是「拿现有内容
  出报」,而用户停止之后还接着生成等于把半成品当成功交出去。
- **进了生成阶段就停不了了,这一档要如实拒掉**(`request_stop()` 返回 `(受理了没有, 说明)`)。**这个判断和「进入生成阶
  段」那一步共用 `_lock`**,所以两者严格二选一。
- **只停自己起的那一轮管道,而且认的是 run id、不是布尔**(`_state['pipeline_run']`;布尔答不了「现在跑的还是不是我那一
  轮」)。`start_pipeline()` 返回 run id,`request_stop(run_id)` 比一次 id;`_sync_channel` 的轮询同样**只认 run id 相同的
  快照**。
- **「拿到结果了」= 亲眼看到自己那一轮 `running` 放下的那份快照,或者按 id 查到它的收尾记录;两样都没有就如实记
  `unknown`,绝不按成功记账。** 中途快照不算终态。`pipeline_state` 按 run id 留最近 8 轮收尾快照。
- **勾了「先同步」却一个来源都没选,直接拒掉**(API 400)—— 放过去的话「同步全部并生成」被悄悄降级成「直接生成」。
- **有通道没同步成功时,日报照出,但「同步了」这件事不许糊过去**(`_sync_issues()` 给名单,三处都读它)。**必须写进文件本
  身**(`sync_failed` frontmatter + 正文引用块)—— 日报是拿走就读的产物。toast 换成 info,绿勾等于把降级说成完整成功。
- **没素材时抛 `LookupError`,绝不写一份空日报**(空日报会把这一天「用掉」)。
- **定时挂在 `pipeline._scheduler_tick` 那一跳上**,不另起线程;日报在跑时 `tick` 直接返回。
- **「已触发」和「已出报」是两个字段,别拿起跑那一刻去记 `last_fired`。** `tick` 只 `_mark_attempt`,收尾时由
  `_finish_schedule` 决定:临时故障同日补 `MAX_RETRIES` 次,成功/没素材/用户停止一律「今天到此为止」。**记的是起跑那一
  天**(23:50 起的那一轮 00:50 才收尾)。`_mark_attempt` 要在 `start()` **之前**调。
- **`_raw_settings()` 不碰采集器注册表**(定时线程每 30 秒读一次)。存下来的通道一个都不认识了时回落到默认范围,不是留一份
  空列表。
- **阶段落 `skipped`/`stopped` 的通道不算同步成功**,**只看这一轮请求了的那几个阶段**(`SYNC_STAGES`)。
- **正文里的出处编号(`[18][23]`)在落盘时就写成链接**(`daily._link_refs`),不留到阅读时。链接文字保留方括号并转义
  (Obsidian 会把裸 `[[18]]` 当 wikilink),**目标一律套尖括号**(`_md_dest`,库里的 url 实测有带圆括号的)。
- **每一处出处编号换不换,由渲染器裁决(`kb/daily_refs.py`),这里不写「哪些片段不许动」的规则** —— 手写规则的路走过三
  轮,每一轮都被真实渲染器找出新的漏网写法,**这份清单不会收敛**。判据换成:把某一处换成链接,**渲染一遍看结构变没变**,
  接受的条件是「这一处之外一个字都没动」。**比对要连 `<code>` 的内部结构一起比**,并**单数一道链接嵌套**。**判据是整份渲
  染结果逐字比,不是挑几类元素比** —— 要换的地方先换成**哨兵链接**,渲染后把哨兵**整组**还原,要求和原文的渲染结果**逐字
  相同**。**一组里没有素材的那几个编号也要打哨兵。两边都要过一次解析器再比**(`_norm`)。**哨兵链接不许落在
  `<a>`/`<code>`/`<pre>` 里面。**
- **裁决器的方言必须和页面那个渲染器一模一样**(`markdown-it-py` 的 `gfm-like`,`html=True`)。**但「换个预设名」不等于对
  齐**:linkify-it 只认一份有限的 TLD 名单而 marked 对 TLD 不做限制。所以①把 TLD 放宽成任意;②真正兜底的是 **`_autolinked`**
  —— marked 的 autolink 起手式只有 `www.` 和 `scheme://`、尾巴是 `[^\s<]*`,于是「这个编号落在某条裸网址里」**必然**满足
  「往左数到最近的空白或 `<` 为止出现过 `www.` 或 `://`」。**那条起手式正则必须大小写不敏感**(`re.I`),而且**① 会顺手遮
  住 ② 的缺口**,所以回归里另有一条直测 `_autolinked`。**代价是过度保护,已知且接受。**
- **先整篇一起试一次**,过不去才逐个各试各的。依赖是 `markdown-it-py[linkify]`(那个 extra 不能省)+ bs4,**得是显式依赖**。
- **加链接之前生成的那些日报由面板兜底**(`daily.js` 的 `enhanceDailyBody`),**读的是渲染之后那个 `<ol>`,不是 markdown 原
  文**(`dailyRefMap`)。**认的编号写法要和落盘那条路一致。** 粗筛文本节点别用带 `g` 的正则。
- **前端那条横幅有两个来源,清空时必须认一下是谁写的**(`dailyErrorKind`)。不在日报页时也慢速轮询状态(60/120 秒)。
- 详细见 `docs/decisions/daily.md`。

## 大模型阶段配置(kb/llm_stages.py)

- **每一处会调大模型的活都是一个「阶段」**,注册表在 `STAGES`,配置落 `data/secrets.json` 的 `stages` 段。**别再在各文件里
  写死模型字面量**,调用方一律 `stage_model("<key>")` 取;没配的阶段按 `Stage.default` 走。
- **provider 是显式存下来的,不是从模型名猜的**(前缀规则只是兜底)—— 同一个 id 在两家中转站都可能存在,猜错就是拿一家的
  key 去打另一家的站。**模型标识一出 Python 就得带家**(`llm.qualify`),界面上显示时过 `modelLabel()` 剥掉前缀。
- **下拉的候选 = 勾选的模型 + 各阶段当前指向的模型**(`llm.model_pool()`)—— 只要没有一个 option 匹配当前默认,浏览器就自动
  选中第一个候选,而 chat.js 每次发送都带上下拉的值。
- **逐条试候选只能用 `llm.parse_model`,不能用 `llm.resolve`** —— 后者认不出家时会去问默认模型,而默认模型自己也在走这段解
  析,旧配置里留一个谁也不认识的 id 就是无限递归。
- **阶段配置读时不缓存**(面板改完不用重启);批量脚本例外(整批开始时定一次)。
- **改 `secrets.json` 一律走 `llm.update_settings(mutate)`,别在外面 read → 改 → write。** **临界区里是两把锁**:进程内
  RLock 挡同一个 kb-web 里的并发,`secrets.json.lock` 上的 flock 挡跨进程的。flock 锁的是**独立的 lockfile,不是
  `secrets.json` 自己**(写盘是 rename 换文件,锁着的 inode 会被换走),lockfile 只创建不删除。写盘是「唯一名字的临时文件 +
  rename」—— 截断重写会让并发读者读到半截 JSON,而坏 JSON 被吞成 `{}`,接着一存盘就把整份 key 抹了。
- **面板上每一步都写着「推荐哪个模型」,但注册表里存的是档位不是 id**(`Stage.rec` = `cheap`/`main`,`recommended()` 按关键
  词在**当前实际可选的模型**里挑;写死 id 十有八九不在候选里)。挑的时候**已配 key 的那几家优先**,一个都不沾时返回空串。
  口径:量大的解析活推便宜档,写给人读或决定产物质量的那几步推主力档。
- 收藏页那个模型下拉和面板里的「收藏 · 分诊与行动卡」**是同一处配置**(`llm.set_fav_model` 直接写 stages)。
- 详细见 `docs/decisions/llm-stages.md`。

## 提取阶段的两道判定(kb/extract.py 的 promo 与 body_gap)

- **软广判定搭在提取阶段,不单开一个 LLM 阶段**(正文已经在 prompt 里了)。喂进去的是**头 4800 + 尾 1200**(转化钩子基本都
  在文末)。
- **三档 none/soft/hard,只有 hard 被当成广告处理。** 判定认不出时一律落 none —— 错杀会让一篇正常文章连同它的实体从条目化和
  检索里一起消失。**hard 的两条边界是拿真实样本调出来的,别删**:①只看正文主体,不看文末的固定推广位;②**推广对象是开源项
  目时一律不判 hard**,最多 soft。**口径只有一份**(`_PROMO_RULES`),完整 prompt 和存量补判的轻量 prompt 共用。
- **消费方一律读 `documents.promo_level`,不要各自再判一遍**:条目化 gate 读到 hard 直接否掉,检索和枚举默认挡掉 hard。
  **软广子句不进 `_filter_sql`**(后者的返回值还被 `_vec_leg` 用来判断「召回有没有被缩到小子集」)。transcribe 那侧**按当前
  判定再拦一道**。
- **hard 原文的衍生条目按 `url` 一起挡掉**,口径只有 `store.promo_exclude_sql()` 一份。里面那句 `pm.url <> ''` 不能省(库里
  几十篇没有 url 的文档会互相匹配上、集体消失)。
- **`flag/软广` 是双写的:DB 的 `doc_tags` + md frontmatter,两边都得跟着判定走。** `doc_tags` 每次 upsert 都按 frontmatter
  重建,只写库的标记下次重索引就没了;反过来 hard 改判成 none/soft 时 md 里那行也必须摘掉(`remove_writeback_tag`)。**三条
  提取路径的 tag 回写只能有一份实现**(`extract.writeback_extraction`)。`save_promo` 返回「上次是 hard、这次不是」的信号,提
  取流水线和 backfill 都据此调 `sync_promo_tag`;回写改了文件就同步 `refresh_content_hash`。`PROMO_TAG` 在 slug **之后**追加。
- 存量补判走 `scripts/promo_backfill.py`,按「标题 + 正文长度桶」**同稿归组**(8857 篇里 2357 篇是同一份稿子的副本),每组只判
  一篇、结论广播给同组。**归组键带长度桶**是因为光看标题会把撞名的不同文章判成同一篇。
- **soft 是「留着 + 降权 + 打标」,不是第二个 hard。** 检索里乘 `search.SOFT_PROMO_WEIGHT`(0.85);要调松紧改这一个数,别动
  `RRF_K`。**降权必须在截断 top_k 之前做**(排完再降只是在这 k 篇里换位置,被压下去的仍占着名额)。`Hit.score` 报降权后的分。
- **「有效档位」只有一份口径**(`store.promo_level_sql` / `effective_promo_levels`):自己判过用自己的,没判过(条目化衍生条目
  永远是空的)按 `url` 从原文继承,同 url 取最重的一档。
- **打标要覆盖四个展示面**(搜索结果、问答引用、图鉴卡片与时间线),统一走 `core.js` 的 `promoBadge()`。**图鉴是唯一不走
  search/aggregate 的面**(按 vault 文件聚合),所以 `projects._apply_promo` 补这一道,**过滤放在分组之前**;查库失败就降级成
  「全部保留、不打标」。
- **图鉴的缓存签名必须带上软广状态**(`projects._promo_sig`)—— 补判改的是**原文**那一行,衍生条目文件一个字节没动。签名取的是
  **逐篇 (path, promo_level, url) 的摘要,不能是聚合量**(分档计数 + MAX(promo_at) 在「两篇同一秒对调」时逐字相同)。取签名和
  重建共用一条连接。
- **`store.doc_body()` 返回空串有两种原因,下一步完全相反**(`body_gap`):`empty` = md 正文自己就是空的,重抓有机会补上,进
  「需回填」清单;`no_text` = 正文**有内容,只是一个字都没有**(整篇一张长海报图、纯表格),**回填救不了它**。不分档的话它会被
  当成「正文为空,需回填」**永动地**每轮重报、把 extract 标红,而「重试失败项」每次都去重抓那张图。
- **判据是「md 正文照 chunker 自己那套规则剥完还剩不剩字」**(走 `parse_frontmatter` + `normalize_body` + chunker 的
  `strip_markup`),不是「md 正文是不是空的」,更不是「有没有图片」。**「空没空」要在剥掉阅读装饰之后问**
  (`wikilib.linkify_doc` 对空壳照样追加图谱尾区)。**第三种形态:剥完还剩字、却一个 chunk 都没有 —— 这一档不许落终态**,一律
  按最保守的 `empty` 办。**读不到文件**同理落 `empty`。
- **终态存的是当时那份 `content_hash`,不是一个布尔**(`documents.no_body_hash`)—— 布尔是永久封死,按 hash 走则是 index 一写
  新 hash 就自动回队列。`no_body_hash <> ''` 那半不能省。写回按元规则带条件,**正文和它的版本号要在同一个快照里读出来**
  (`store.doc_body_version`,一条语句)。
- **`pending_docs` 是唯一口径**,所以一处改动同时治提取队列和面板那个「当前待处理 N 项」。**新那档的文案里不许出现「正文空」
  三个字** —— `retry_failed_plan` 就是按这三个字决定要不要跑回填脚本的。**它不判红、不进 failures 明细**,落终态时要把这一篇的
  旧失败计数一起清掉。
- **新增那一列在老库上是 ALTER 补的,所以「读列集合」和 ALTER 要在同一把写锁里**(`_add_columns`:缺列才 `BEGIN IMMEDIATE`,
  **并在锁里重新读一次**列集合)。**常态不取锁。**
- **面板上的「当前待处理 N 项」不能只在 running→idle 那个跳变上刷新**(`pipe.js` 另按「出现了一轮新的已结束运行」判)—— 这一
  档一次模型都不调、整轮几百毫秒,页面**一次都没见过 `running=true`**。这条路**不弹「已结束」那句 toast**。**而那次刷新不许被
  在途的积压查询吞掉**(`pipePendingWanted`:在途时记一笔欠账,等它收尾了补发,**真的应用上去才算刷新完**)。**回包「是哪一轮
  的」认 `run_id`,不拿 `started_at` 当身份。**
- **三条提取路径共用同一份判定。** `extract_parallel.py` 的预筛循环按「试过几次」提交、不按「写进去几条」(条件 UPDATE 返回 0
  行时也开着事务,而它后面的回写要先取维护锁)。
- **想真正读到这类文章的内容只能 OCR / 多模态读图**,那是按篇计费的另一笔开销,没做。
- 详细见 `docs/decisions/promo.md`、`body-gap.md`。

## wiki 层与分类图谱(scripts/wiki/)

- **主题页文件名必须过 `sanitize_filename`,双链用同一个名字**(命名统一在 `Universe.topic_file`,取链接走 `topic_link()`)
  —— 带斜杠的主题名拼成路径之后 `FileNotFoundError` 把整个 wiki 阶段带崩;只清理文件名那一侧的话,页面建出来了、链接却指向不
  存在的页。重名决胜按主题名排序,**不能按篇数**(篇数每轮都在变,一变就换文件名、留下孤儿页)。
- **入库侧收紧只针对主题(`extract._topic_slug`),实体侧不能跟着改**(`shadcn/ui` 这类仓库名里的斜杠是正当的)。
- **一共三张图谱,回答的不是同一个问题。** `graph.html` 是「最近两周公众号在聊什么」(实体共现、滚动 14 天);
  `graph-projects.html` / `graph-skills.html` 是「我长期盯的这两类东西现在都有哪些、各归在什么用途下」(全量、固定分类表分区、
  不设时间窗)。`/graph/{name}` 的文件名走**白名单**。
- **分组口径直接复用 `kb.projects`(图鉴那一套),不自己再扫一遍条目树。**
- **类目是写死的固定表(`taxo_tables.py`),模型只负责选 code** —— 让模型自由起标签的话,同一个项目今天叫「命令行工具」明天叫
  「CLI」。**判定结果按 norm 长期缓存**在 `data/taxonomy_{key}.json`,带 `Taxonomy.sig()`:**改分类表就自动全量重判**,而**指纹
  必须覆盖所有会进 prompt 的字段**(`hint`、`extra_rules`、`fallback`、`item_zh`),不只是 code。
- **「只看有仓库」按解析出的主机名判,不能退化成「有任意 http 网址」** —— 口径是 `kb.repo_url.repo_host()` 的白名单,精确比对或
  后缀匹配,**不用字符串包含**。图鉴聚合 `project_url` 时挑哪一条也得用它(`projects._pick_url`)。
- **判不出来的一律落 fallback(「其他 · 待归类」),不许瞎猜一个类目。**
- **归类整体失败时既不写缓存、也不覆盖旧图**:`classify` 不落盘(写一份空 items 上去等于把几千条结论抹掉);`build_one` 看到
  「有条目但一个都没归类、而且旧图已存在」直接 SystemExit。
- **「指纹变了的全量重判」和「同指纹下的增量新判」是两条路,别混成一条**(混着走时 `--max-new` 会把完整旧判定**就地盖掉**)。重
  判那一趟①**不受 `--max-new` 限制**;②有批次失败就保住旧缓存、把进度留在暂存里,`Verdicts(complete=False)`。判出来 0 条同样按
  未完成。
- **「判完了」= 每一条都拿到了有效 code,不是「没有批次抛异常」**(模型返回语法合法的 JSON 却只含一截时不抛异常)。promote 的判
  据是 `left == 0`;漏项会**只拿缺的那些再问几轮**(`_RESIGN_RETRIES`),那**不是放宽判据**。
- **判定一律先落暂存 `data/taxonomy_{key}.staging.json`,不直接写正式缓存**(增量那侧同样如此)。**正式缓存在「新图验收通过 + 页
  面落盘」之后才提交**(`Verdicts.commit()`)—— 两者不在一个事务里的话,上面那些拦截只保住页面、保不住缓存(fallback 本身是表内
  合法 code)。顺序也不能反。
- **重判结果「等于没判」按不作数处理**,连暂存一起扔掉。判据是「落在 `void_codes | {fallback}` 里的占到 `VOID_SHARE`(90%)」,
  **不是只有 100% 才算坏**;少于 `VOID_MIN_N`(20)条时只认 100%。**这条判据的分母是「这一趟新拿到的判定」,不是整份缓存**
  (`fresh = codes - base`)。故意收紧规则时用 `--allow-shrink`。
- **`res.complete=False` 无条件拦死,跟有没有旧页面无关。** **页面落盘走 `_atomic_write`**(同目录唯一临时文件 + rename)。
- **「验收基线 → 落页面 → 提交缓存」整段要拿跨进程锁圈起来**(`taxonomy.publish_lock`)—— 页面和缓存是两次独立 rename,各自原子
  管不住两个 writer 交错。**验收读旧图基线必须在锁内**;`_io_lock` 因此得是 `RLock`。**缓存提交失败要把页面回滚回去**
  (`_publish`;发布锁只挡交错,给不了跨文件的失败原子性)。回滚本身再失败时打一行 stderr 说清「页面是新的、缓存是旧的」。
- **暂存里的东西都是「没验过的半成品」,续用前要按原批次各验各的**(`load_runs`,暂存里的 `runs` 记着哪几条是同一趟判出来的)——
  质量判定跑在 `finish()` 里,而判定结果在那之前就落盘了。`finish()` 的分母因此是 `codes - base - resumed`。老格式暂存整份当一批
  验。
- **暂存里凡是正式缓存已经有的 key,一律以正式缓存为准**(`clash`),「有没有新东西可提交」改成比内容(`codes == official`)。
- **`save_cache` 成功就等于缓存提交完成,后面清暂存失败只是收尾告警**(`_promote`)—— 共用一个异常边界的话 `_publish` 会把它理解
  成「缓存没提交」、把页面回滚成旧的。**清暂存的每一处都得是这个边界**(统一走 `_cleanup` / `_rewrite_stage`,不许裸调 `unlink`)。
- **发布前要比一次「基线有没有被别人换过」**(`publish_sig`,读候选之前记一份,进锁后再取一次比)—— 读基线 → 判定 → 建图那一大段
  跑在**锁外**。不一致就**这一轮不发**(打一行说明、退出码 0),判出来的东西并回暂存。**基线是发布序号 + 两个文件的内容(正式缓存
  + 正式页面字节)**:**只比内容会被 ABA 骗过去**,只比序号又挡不住绕过发布流程的改动;页面那半按**字节**算。**要在
  `load_groups()` 之前取。**
- **发布序号在动任何正式产物之前推进,推进不了就整轮放弃** —— 三次独立 rename 凑不成事务,而**后推进**会留下「产物已经换了、序号没
  动」的窗口。危险的假阴性换安全的假阳性。**序号写盘要 fsync(文件 + 父目录)**;值 = 自增计数 + 随机后缀。
- **基线「读不出来」一律 fail-closed,不许退化成「还没有」**(`read_gen` 只把 `FileNotFoundError` 当空串,别的 `OSError` 和坏内容
  一律抛 `GenUnreadable`)。
- **共享暂存只有一个写入口 `_edit_stage`:读-改-写,整段在 `stage_lock` 里,而且各人只动自己那一批**(`runs` 是
  `{批次id: [norm]}`)。「提交完清暂存」= 把**自己刚提交的那些 key** 摘掉,**不是 unlink**;摘完真的一条不剩了才删文件。**取锁顺
  序永远是 publish → stage。**
- **CAS 拒绝发布时,要在锁里把自己判的并回暂存**(`_restash` / `Verdicts.keep()`)。
- **提示词/调模型/解析回包在 `taxo_llm.py`**、归类结果对象在 `taxo_verdicts.py`(都因为 700 行上限拆的),`taxonomy` 把名字原样绑回
  自己的模块全局,所以 `T._llm = fake` 这类打桩照旧有效。**`CACHE_DIR` 那种被测试改写的模块变量不能这么搬。**
- **页面验收看的是「这张图实际长什么样」,不是某个 code 的名字**(`quality_problem`)。有旧图在时三种情况一律拒绝写页面并
  `res.discard()`:0 节点、一个都没归类、**新图不到旧图的 `MIN_KEEP`(50%)**,而且**节点数和已归类数两个维度都要比**(fallback 节
  点不会被 drop 掉)。
- **「这一轮一个候选都没扫到」是故障信号,不是「没什么可验收的」**(原子写只保证不出半截文件,挡不住这种「完整但错」的覆盖)。
- **skill 图谱的成员资格也由归类这一步决定**(分类表里有个 `X` 档收「本体是软件、只是带 skill 能力」的,判成 X 的不进图谱)。
- **共现边 = 同一篇原文里被一起拆出来的两个条目**,权重是一起出现过几篇;一篇拆出 40 个以上条目的月度盘点跳过;每个节点最多留 10 条
  最强的边。**文章不内嵌进节点,只存文章表下标。**
- **点哪一份副本,就以哪一份为准**(`select(i, from)` 把来源元素记进 `anchor`)。**滚动还得等面板挤压布局落定再做**(延后 360ms)。
- **筛选改变可见集时,选中态得跟着走**(被筛掉了要整个 `reset()`);切排序后 `resort()` 末尾必须重画连线。**反过来,选中一个被筛掉
  的节点之前要先把挡它的筛选放开**(`relax()`,收在 `select()` 里)—— 面板那排「常一起出现」跟筛选没有半点关系。**只放开挡路的那几
  项,而且控件跟着回位**;**「挡没挡住」要逐项各判各的**(搜索那一项尤其容易写成「`F.q` 非空就清掉」,而组合筛选下目标常常仍然命中
  搜索词)。命中判定 `hitsQ` 只有一份。
- **搜索框回车要先把这次输入同步落定再找**(`F.q` 是防抖 120ms 之后才更新的),还要**排掉输入法组合态**
  (`e.isComposing || e.keyCode === 229`)。
- **「近 30/90 天」的截止日全程按 UTC 算**(`cutoff`)—— `meta.built` 是后端 `date.today()` 落下来的裸日历日,前端按本地时区解析再
  `toISOString()` 会让窗口凭空多含一天。
- 页面样式和脚本放 `scripts/wiki/assets/catgraph.{css,js}` 的真文件里,构建时读进来内联。
- **三张图谱的子进程挂了必须让 `wiki_daily.py` 非零退出**(`run_step`)—— 光打印一行 FAIL 的话,上面那一整套拦截**每一次生效都显示
  成 wiki: ok**。一张挂了另一张照样生成。**标红那条详情里,标签、根因、完整 stderr 三样都得留住**(`_fail_wiki`):标签单独拼在前
  面、**只截错误正文**;摘要取 **stderr 里最后一条真正的错误行**;完整 stderr 写进管道日志、也塞进 `_stage(..., failures=[...])`。
- 详细见 `docs/decisions/catgraph.md`。

## 服务与运行时

- **管道跑在 kb-web 进程内**,杀 8787 前先 `curl -s http://127.0.0.1:8787/api/pipeline/status` 确认 `"running": false`。server.py /
  projects.py 改动要重启才生效;`kb/static/` 下静态文件每次请求从磁盘读,免重启。重启用 `env -i` 干净环境(继承嵌套 claude 的环境
  变量会导致 Not logged in)。**不要用 preview_start 托管 kb-web**(会接管生命周期,杀掉管道)。
- **「改了不生效」只是这件事良性的那一半:坏的那一半是同一个进程里新旧两个版本的模块混跑。** kb 有一批模块是**延迟 import** 的
  (`kb.feeds.sync` 要等管道真跑一轮 crawl 才进来),而 Python 不热加载 —— 服务跑着的时候一次 `git checkout` 就够。**报出来的话指
  不到任何跟业务有关的东西**(实测报 `x_channels has no attribute 'fill_bridge'`、面板显示「连接失败」),**铁证在 `__pycache__`
  各文件的时间戳上**。**出路只有重启**;**重启之后退避还挂着**,得手动点一次同步(或 `POST /api/intake/{kind}/sync` 带
  `source_ids`,它自带 `force`)。
- **所以服务已经起着时 `./kb.sh` 会提示一句「代码比跑着的进程新」**(`code_stale`;判据是 pidfile 比 `$ROOT/kb` 下任何一个 `.py`
  旧)。**挂在两处**:`web_start` 的「已在」分支和 `web_status`。**只看 `.py`**(`kb/static/` 每次请求从磁盘读,`scripts/` 和
  `collector/` 每次都是新起的子进程)。口径同 `graph_stale`:**提示而不代劳,也不拦**。**「没有 pidfile 就安静」那道守卫是冗余带,
  别删** —— 「服务没跑」该由我们自己回答,不该押在某个 find 实现对缺失参照物的处理上。
- **地址栏的 `#tab` 被读走之前,谁都不许写它**(`workspace.js` 的 `workspaceLocated`)—— `restoreWorkspaceLocation()` 在
  `initWorkspace` 收尾时才读 hash,而 `loadGallery()` 是和它并排起跑的,图鉴接口先回来时 `applyGalleryFilter()` 会把 `#daily` 覆盖成 `#gallery`。
- **向量模型跑在子进程里,不在 kb-web 进程里**(`embed_worker.py` 是本体,`embed_pool.py` 管起停,`embedder.py` 是门面)。理由只有
  一个:内存 —— bge-m3 一碰 MPS 就占 ~3.7 GB,每出现一种新的 batch 形状显存池再涨一截、**永不回落**,而这笔内存**在进程内根本收不
  回**(`empty_cache()`、`del model` + gc、fp16 全试过),**进程边界是唯一可靠的回收手段**。懒起、空闲
  `KB_EMBED_IDLE_SECONDS`(600)秒自杀、批量任务结束时显式收掉;子进程 stdin 读到 EOF 就退出,所以父进程怎么死都不会留孤儿。
- **批量写向量一律走 `embedder.embed_pending_chunks(conn, progress)`**,别再抄那个循环。`KB_EMBED_BATCH_BUDGET` 决定**峰值**内存,
  取 8×768² 是拐点;**改这个值不需要 reembed**。
- 协议是「4 字节小端长度 + 载荷」的帧。**向量帧的长度前缀必须被吃掉** —— 按 `n*dim*4` 裸读会让整批向量平移 4 字节(余弦相似度掉到
  0),剩的 4 字节还会把下一个请求的帧同步带歪,而错位之后的 read 会**永久阻塞**、且是攥着锁阻塞的。所以父进程侧的读带帧长上限和超
  时,把「挂住」变成「报错重试」。
- 详细见 `docs/decisions/embedding.md`、`code-stale.md`。

## 浏览器探针起的无头壳(tests/probe_shell.mjs + headless.py 的 run_probe)

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
- **测「挂住」的用例必须拿一个 ref 的 handle 挂住,`await new Promise(() => {})` 挂不住**(它不是 event loop handle,循环一空 node
  就以 exit 13 正常退出,于是**本该验第二层的用例测的是第一层**)。
- **`launchShell` 等端口那个 `setTimeout` 非清不可**(它是 ref 的 handle,否则探针跑完 node 也不退出)。**拿到端口后要 `unref()`**
  子进程 handle 和它的 stdio 管道。
- **`run_probe` 要 `start_new_session=True`**(让 node 成为新会话首进程,chrome 自动在同一个组里,`killpg` 一起带走)。收尾在
  `finally` 里**无条件**做一次。
- **把探针改到共用这个模块之后,必须真跑那几组页面回归 —— `node --check` 只过语法。**
- **两条契约不挂 `@requires_headless`**(没装无头壳的机器上靠它们守结构):探针不许再自己 `spawn` 浏览器、测试不许再裸
  `subprocess.run` 跑探针。**`test_probe_leak.py` 自己是后一条唯一的例外,而且必须是**(走 `run_probe` 的话它那道 killpg 会把结论盖
  掉)。
- **同形状但没动的是沙箱服务那一半**:几个 `setUpClass` 自己 `Popen` 一个预览服务,而 `run_probe` 本身抛出来那一档没人管(setUpClass
  抛异常之后 unittest **不调** `tearDownClass`)。要修是起完服务**立刻** `cls.addClassCleanup(...)`。
- **探针里「等页面就绪」要等那个元素自己,不是等定义它的 JS 全局**(`typeof subsCatalog !== 'undefined'` 回答的根本不是「来源目录取
  回来了吗」)。
- **「有没有漏」按工作目录归属数,不按进程名**(`lsof -p <pid> -a -d cwd` 落在本 worktree 下且 `ppid=1`)—— 本机常常同时开着好几条
  worktree 在跑各自的探针。别去动不归自己的那些。
- 详细见 `docs/decisions/probe-shell.md`。

## 视觉语言(硬性)

- **一套色板:瓷白 `#F3F5F4` × 墨蓝黑 `#14202B` × 深松绿 `#17604F`**,唯一源头是 `kb/static/css/base.css` 的 `:root`。**别再换重点
  色** —— 靛蓝(`#3F43B9` 一带)是 AI 产品的默认色,而「开源项目」占了图鉴 5530 条里的 2976 条,类型色一旦跟重点色撞上,卡片上那枚
  类型标记在绝大多数卡片上就什么也没区分。
- **色值一律走 token,不许就地写十六进制。** 灰阶只有三档(`--ink` / `--ink-2` / `--muted`)、线两档(`--line` / `--line-soft`)、红
  绿褐各一个(`--danger` / `--ok` / `--warn`)。
- **有三处独立产物各抄一份色值和箭头几何**(阅读页 `kb/web/pages.py`、分类图谱 `scripts/wiki/assets/catgraph.{css,js}`、话题图谱
  `scripts/wiki/build_graph_html.py` —— 都要能脱离 kb-web 直接用浏览器打开,所以 CSS 和图标是内联的),**改 `:root` 或
  `ARROW_PATHS` 必须同步改它们**,三处都写了互指注释。不同步的后果是用户点一条搜索结果去读原文就换一套配色。
- **字体三件套各归其位**:宋体 900(`--font-display`)只给页面大标题、详情抽屉工具名、图谱与阅读页标题 —— 这是整套界面**唯一的字形
  签名**;等宽体给工具名/日期/计数/norm;黑体给正文。换 `--font-display` 时注意各处的 `font-weight: 900`(一度换成了没有 900 字重的
  字体,那些声明当场变成死代码)。
- **这是自己每天开的工具,不是落地页。** 不要给面板加英文 eyebrow、副标题、居中大图标 + 标语那类空态。
  **页头就一行:标题 + 同一行上的事实数字。**
- **改下拉框样式只许动 `background-color`** —— `background` 简写会连 base.css 里那个自绘箭头(`background-image`)一起清掉,下拉框退
  化成一段看不出可点的纯文字。
- **箭头一律走 `icons.js` 的 `arrow(kind)`,页面里不许再出现 ↗ ↑ ↓ → ▾ ▴ 字符**(这些字在 PingFang SC 里本身就细长歪斜,缺字还会回
  退到系统 emoji 字体)。几何只有一份(`ARROW_PATHS`,viewBox 12、stroke 1.4、尺寸 1em)。静态标记写 `<i data-arrow="ext">`
  (`renderIcons()` 会替换),JS 里拼 `arrow('ext')`;宿主元素得是 `innerHTML` 不能是 `textContent`。那三处产物各有一份副本
  (`pages.py._EXT_ARROW`、`catgraph.js` 的 `IA`、`build_graph_html.py` 的 `openwiki`)。
- **`icons.js` 的齿轮路径是按极坐标算出来的(8 齿等分,齿顶 R=9.3 / 齿根 r=7),别手改数字**(齿轮是旋转对称形状,少一齿或齿距不匀一
  眼就看得出来)。
- **图谱页是把 CSS 内联进单文件 HTML 的产物**:改 `catgraph.css` 只影响之后构建的页面,Vault 里现有的要重新生成才会变样(重建见
  「操作入口」那节)。
- 回归 `tests/redesign_probe.mjs`、截图脚本 `tests/shots.mjs`,都是 CDP 直连 chrome-headless-shell,不装 playwright、不连网。

## 前端反馈(硬性)

- **禁止用原生 `alert` / `confirm` / `prompt`**(系统弹窗跟本项目的调子完全不搭,还阻塞主线程)。统一走 `kb/static/js/ui.js` 三个出
  口:`toast(text, {type, detail})`、`confirmBox({...})`(返回 `Promise<boolean>`,Esc / 点遮罩 = 取消)、`apiFail(r, fallback)` /
  `toastIfFail(r, text)`。**别再写 `(await r.json()).detail || '失败'`** —— 未捕获异常时 Starlette 返回的是纯文本 500,`r.json()`
  会炸,原因就被吞成一句无信息的「失败」。**表单字段级校验不用 toast,用 `.field-err` 就地红字。**
- **问答的模型下拉收在输入框里,不许再单起一行。** `.chat-inputbar` 整体就是那个框(边和圆角画在它身上),下拉贴框内左下角、发送在右
  下角。焦点同理只画一条(`.chat-inputbar:has(#chat-input:focus-visible)`),`#chat-input` 得显式 `box-shadow: none` 压掉 base.css
  那条。
- **问答面板的高度按可用空间算,不许再写 `calc(100dvh - N)`**(那个 N 是照「页头上面还有多少东西」量出来的,页头一变就错)。现在是
  `body.chat-mode #main-content` 锁在 `calc(100dvh - var(--topbar-h))` 并改成 flex 列,`#askpanel` 与 `.chat-layout` 各用 `flex: 1`
  逐层吃掉剩余。`--topbar-h` 在 navigation.css 里定义,别再各写一个 56。**`#askpanel` 上那句 `min-height: 0` 不能省**(flex 项的自动
  最小尺寸按内容算,一段长对话会把面板顶到几千像素高),`width: 100%` 同理。
- **聊天区里一条固定高度地板都不许有,`.chat-main` 也不例外** —— **地板造不出不存在的空间**:手机横屏带首次配置横幅时
  `.chat-layout` 只有 115.8px,200px 的卡片直接把整块输入区顶到视口外。正解是把「输入区最小需要多少」压到任何支持的视口都放得下。
  **`.chat-layout` 的 `min-height: 0` 要显式写**(省掉等于 `auto`)。
- **留白收不收由 JS 按实际余量定,不许再用高度阈值**(`chat.js` 的 `fitChatTight` 加 `body.chat-tight` / `-2` … `-5`)——「放不放得
  下」从来不是一个高度数字能回答的,**换一个更小的阈值只是把这条缝挪个地方**。每次重算时先把 class 摘掉、把文本域压到一行下限,量一
  次还进不进得去卡片,进不去才加回来;**判定永远在常规留白下做**,所以不会来回抖。五级逐级试:①卡片**里面**那点空隙;②页头留白退
  让;③横幅按回一行;④横幅只留标题和两个按钮(收的是解释,不是功能);⑤模型下拉和发送按钮回到同一行(`flex: 1 1 0`,
  **`flex-basis: 0` 是关键** —— 折行按 flex 基准尺寸决定,只给 `min-width: 0` 不管用)。
- **「面板没显示」和「可见但被挤到只剩边框」是两回事,别拿 `clientHeight` 当隐藏判据**(零高度的可见卡片恰恰是最需要收缩的形态)。用
  `card.getClientRects().length`。**摘 class 要在这个判据之前**(提前 return 而不摘的话,紧凑样式会跟着走到别的页面上去)。
- **「这一级有没有用」要比缺口,不能只比卡片高度**(`gap = chatBarNeed(bar) - card.clientHeight`)—— 第一级收的全是卡片**里面**的空
  隙,它按定义不会让卡片外高变化。**已知下界**:812×200 带横幅确实放不下,如实标出来。
- **竖排断点下历史栏按比例限高**(`.chat-side { max-height: min(185px, 26%) }`)。**别指望 `flex-shrink` 顶事** —— `.chat-main` 的
  `flex-basis` 是 `0%`,这条 flex 线走的是 **grow** 分支。
- **文本域的增长上限得认卡片里的实际余量,这件事只能在 JS 里做**(`chatInputRoom`)—— `autoGrowChat` 写的是**行内 height**,而行内高
  度会成为 flex 的「specified size suggestion」,CSS 侧没有出路。**别反过来去调 `.chat-main` 的地板。**
- **重算的触发是 `ResizeObserver`,而且 `.chat-main` 和 `.chat-inputbar` 两个都要观察** —— 逼窄文本域的有两类:卡片自己变矮(展开历史
  栏、关横幅**压根不触发 window resize**),以及**卡片一动不动、输入框自己变高**(模型列表是 fetch 回来的,长模型名把它撑开)。所以观
  察那一行本身,不是去 `loadModels()` 尾巴上挂钩子。
- **验收不能只看「元素 bottom < innerHeight」**(被 `overflow: hidden` 的祖先裁掉时元素照样在首屏坐标里)。所以回归同时量卡片的裁剪边
  界,并用 `elementFromPoint` 做真实命中测试,**探中心点 + 贴下边沿两个点**。撑高必须走真实输入事件(CDP `Input.insertText`),异步模
  型那一格走真的 `loadModels()`、只打桩那一条 fetch。
- **探针收尾一律 `writeSync`,不许 `process.exit`** —— node 对管道上的 stdout/stderr 是异步写的,`exit()` 把没刷出去的直接丢掉(越过
  64 KB 管道缓冲时 JSON 断在半个中文字符中间)。**而 `writeSync` 自己要顶住短写和 `EAGAIN`** —— node 把这两个 fd 设成**非阻塞**,缓
  冲满时它不是等而是**抛**,于是单跑这一个文件是绿的、全量跑才崩。短写接着写剩下那段,`EAGAIN` 用 `Atomics.wait` 同步等 2 毫秒再试。
- **探针里等运行状态必须自己驱动 `refreshDailyStatus()`**(无头浏览器把后台页面的定时器节流到分钟级)。同理 `runDaily` 不能直接 await
  (它自己会停在确认框上等点击)。
- 详细见 `docs/decisions/frontend-feedback.md`、`subs-layout.md`。

## 往本文件添加内容(硬性)

- **除非经过同意,不要向 CLAUDE.md 中添加任何信息。** 这个文件每次会话都整份进上下文,而它曾经长到 4948 行 / 24 万字符 —— 来源不是谁
  一次性塞进去的,是「每轮审查修红之后追加一段完整留痕」这个只写不收的习惯累积出来的。
- 要记的东西按形态分三处:**硬约束**(别人会真的违反、违反了有不可恢复后果的)写进本文件,**先问**;**事故形态、实测证据、回归清单、
  反向验证红数**写 `docs/decisions/<节名>.md`;**怎么写那条测试**写进测试文件自己的 docstring。
- 本文件里不要再出现:某次审查是第几轮、注入了几版各红几条、回归文件里有几条用例分别叫什么、某个决定是哪一天做的(除非那个日期本身有
  时序含义)、测试文件为什么拆成两个。
- **改节标题要跟着改代码里的引用** —— 若干处注释和 docstring 按节名指回这里(`grep -rn 'CLAUDE\.md' kb/ tests/ scripts/ collector/`)。
