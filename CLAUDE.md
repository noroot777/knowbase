# knowbase 项目约定

> 本文件只留**硬约束** —— 别人会真的违反、而违反之后有不可恢复后果的那些。事故形态、
> 实测证据、回归清单、反向验证红数全在 `docs/decisions/` 下,每节末尾有指针。
> 它每次会话都整份进上下文,所以**往里加东西之前先看最后一节**。

## 代码文件行数上限(硬性)

- **大模型生成的代码文件,单文件不得超过 700 行。** 预计超过就先按职责拆分(CSS/JS
  按面板拆、Python 按模块拆),再分别生成。
- 读取大文件同理:超过 ~700 行的文件分块读(offset/limit),不要整读。
- 这条管的是**代码文件**。本文件自己不在射程内(它是文档),但同样越短越好 —— 理由见最后一节。

## 元规则(硬性)

下面几条原先在十几个节里各自论证过一遍。现在只写一次,各节只说它那一处的特殊之处。

### 并发与数据安全

- **判据待在写语句自己的 WHERE 里,不许在 Python 那边先 SELECT 一次。** 先查后写在两个
  执行体之间压根不成立 —— python 的 `sqlite3` 在 legacy `isolation_level` 下 SELECT
  不开事务、不持任何锁,那次预检和随后的写入之间没有任何东西把它们绑在一起;而 SQLite
  把写入串行化,判据待在写语句的 WHERE 里才和那次写入同生共死。`cursor.rowcount == 0`
  就是拒绝信号。
  要跨多条语句就**先取维护锁、再 `BEGIN IMMEDIATE`,锁序不许反** —— 反过来写的话 WAL
  下拿不到快照报的是 `SQLITE_BUSY_SNAPSHOT`,`busy_timeout` 管不着它。
  问一句「现在忙不忙」只是**早一道**,不是互斥。
  落地处:collector 的 `enable_crawl_account`、`sync._claim_write`、
  `feed_fulltext_recheck._downgrade`、`store.mark_no_body`、`x_article_backfill.apply_one`、
  `translate._save`/`_fail`。
- **0 行不是失败,多半正是想要的末态。** 所以统计按**实际写入结果**报、跳过的如实列出,
  不按计划长度报 —— 否则一趟整批被跳过的命令会说「已处理 N 条」而库里一个字节没改。
- **拿不准就别固化终态。** `put_items` 是 `ON CONFLICT DO NOTHING`、`extracted_at` 非空
  意味着再不提取、水位推进意味着那一段再也选不到 —— 这几处写错都是**不可恢复**的,
  下一轮重试也救不回来。反过来「没读完就留在原处」的代价只是重叠重抓一次(按 URL 去重)。
- **漏报是静默丢数据,误报顶多重抓一次 —— 两个方向差着一个量级。** 所以判据宁可宽、
  宁可多拒;判不准一律抛或扣留,不许返回「确定没有」。
- **口径只有一份。** 同一个问题在两处各判一遍,两边必然各漂各的。抽成一个函数,所有
  消费方都过它。
- **主干的迁移只增不减**(表和列一视同仁)。判据是「另一条分支**不重启**会不会当场坏」,
  不是「它重启之后能不能自愈」—— `ADD COLUMN` 对在途连接是安全的,删东西不是。
  要停用一列就停止读写它,别 DROP。

### 测试方法论

- **先断言后果,别只验过程。** 「有没有打上标志」「锁取了没有」这类断言,一个什么都没
  防住的实现也能全绿。判据要落在「下一轮还补不补得回来」「库里那条是什么」「页面上
  那行字」。
- **反过来钉一边。** 只验「该拦的拦住了」会让一个一律拦死的实现全绿,所以同时钉住
  「不该拦的照旧放行」。
- **红 0 条先怀疑用例。** 定点注入报 0 红时,先想清楚是真冗余还是用例不敏感,再把结论
  如实写下来。
- **两份实现各有自己的回归。** 同一条判据在主路和后路各实现一次时,注入任一侧都要有
  用例红。
- **搬代码要逐符号对过原文。** 广域回归可能全绿,因为新函数压根没进过执行路径。
- **反向验证要定点注入,不能整份换回旧版** —— 接口变了会满屏 ImportError,证明不了
  回归抓的是行为。

## 用词(硬性)

- **面向人读的中文文案一律写「绿泡泡」,不写「微信」。** 方向由仓库主人拍板,不要改回去,
  也不要在提交信息或报告里重新讨论这个选择。
- **三类例外,只有这三类:**①代码标识符和技术字符串(`wechat_*`、`mp.weixin.qq.com`、
  `com.tencent.xinWeChat`、`xwechat_files`、表名列名、JSON 键、环境变量、HTTP 头);
  ②上游项目名(`ZejunCao/WeChatOA_Aggregation`)与分支名 `wechat-oa`;③**法律声明** ——
  根 README 的「免责与使用边界」整段、NOTICE 里涉及法律主体与服务条款的句子,那里必须
  指名「微信」这个真实主体。
- **`kb/config.py` 的 `SOURCE_ALIASES` 不许动,一个键都不许删。** 那里面的「绿泡泡」
  「微信」「公众号」「wechat」是**查询别名键**,不是文案:`expand_sources()` 对认不出的
  名字不报错,而是当成具体来源名原样传进 SQL,于是精确匹配**静默得到空集**。用户问答里
  这几种写法都可能给,全都留着。回归在 `tests/test_source_aliases.py`。

## 仓库结构

- `kb/` 知识库本体,`collector/` 来源采集器(公众号)。两者在同一个 git 仓库里,就是为了
  改采集器时也能用 worktree 隔离 —— 分成两个 git 的话,worktree 只会复制 kb 那一半。
- 知识库主干不认识任何具体来源,只认 `kb/collectors/` 的采集器契约;绿泡泡特有的东西全在
  `kb/collectors/wechat_local.py` 与 `collector/` 里。新增来源写个实现即可。
- `docs/decisions/` 是本文件外移出去的决策记录,一节一个文件,内容是完整的事故形态、实测证据、
  回归清单和反向验证红数。改某一块之前先读它对应的那份,比在本文件里猜意图快。

## 操作入口

- **`./kb.sh` 是唯一的操作入口**。不带参数打印全部命令。日常两条:`./kb.sh sync`
  (采集→导出→索引→打标,后台跑)、`./kb.sh status`。**没有「同步前的准备」这一步了** ——
  原来的 `prep` 连同整条凭证通道一起搬去了 `wechat-oa` 分支。
- **`restart` 只重启进程,不重建任何产物,尤其不重建三张图谱页。** 图谱是 Vault 里的
  单文件 HTML,改了 `catgraph.css/js` 重启看不出任何变化。重建走 `./kb.sh graph`
  (`--no-llm`,零模型调用,约 6 秒);跑同步时 wiki 阶段本来就会带上。没塞进 restart 是
  因为 restart 失败顶多服务没起来、磁盘一个字节不动,而重建每次都在原子替换三张正式
  页面。`web_start` 之后比一次 mtime,旧了就打一行 `graph_stale` —— **提示而不代劳**。
- **`./kb.sh graph` 用仓库内的 `data/` 时只许在主检出跑**,`cmd_graph` 第一件事就是用
  `git rev-parse --git-common-dir` 把 worktree 挡掉。`data/` 在 .gitignore 里、只有主
  检出有,而 **`build_graph_html.py` 自己不做质量验收** —— 从 worktree 跑会拿测试库建出
  0 节点页面,原子替换掉 Vault 里那张好的。
- **`cmd_graph` 的前置检查按 `KB_DATA_DIR` 解析数据目录**(没设才回退仓库 `data/`),跟
  `kb/config.py` 与 `scripts/wiki/taxonomy.py` 一个口径。数据目录指到仓库外时上面那道
  worktree 闸门不再适用,空目录由归类缓存那道检查挡住。
- **「是不是仓库内的 `data/`」要 `realpath` 之后再比,不能比原始字符串。**
  `pathlib.Path` 去不掉 `..`、也不消解符号链接,于是 `$ROOT/data/../data`、一条指回
  `data/` 的软链接都是同一个目录的另一种写法,闸门换个写法就绕过去了。回归在
  `tests/test_kb_graph_guard.py`。
- **服务一律用 `daemonize()` 起,不用 `nohup ... &`**。后者起出来的进程跟脚本同一个进程
  组,非交互 shell 会给它把 SIGINT 设成 SIG_IGN,而这个 SIG_IGN 被子进程继承 —— 于是
  stop 时的 `kill -INT` 也被忽略,只能降级 SIGTERM,而 kb-web 进程里跑着管道,SIGTERM
  会把正在写 kb.db 的那一步砍在半路。
- **起服务那条命令一律 `uv run --offline`,「要不要联网装东西」挪到前台问**
  (`web_prepare_env`)。**pyproject.toml 一动 uv 就要重建那个 editable 包**,不带
  `--offline` 时即使缓存里已经有也会去 pypi 问 hatchling 的最新版 —— 网络一抖启动就挂
  在后台进程里,日志尾巴是一段跟知识库毫无关系的 tls 报错。前台那一步先离线确认,不成
  才联网装一次(**用得上当前 shell 的代理设置**,`env -i` 把 `*_proxy` 清掉了),装完
  照旧离线起服务。
  **不许改用 `uv sync`**:它默认 exact,会卸掉 venv 里那份旧项目名的 editable 残留,而
  两份 dist-info 的 RECORD 记着**同一组** `bin/kb`、`bin/kb-web`、`bin/kb-mcp`。
  回归在 `tests/test_kb_web_start.py`。
- **凭证通道整条都不在主干上了。** 那一套(四态判定、新鲜度、`cli creds`、`cli login`
  扫码、Clash 路由切换)留在 `wechat-oa` 分支,**两个分支始终并行、不打算合并**。主干
  采集只有一条路:读本机绿泡泡数据库。碰到提凭证的历史说法,那说的是另一条分支。
- **两个分支共用同一份 `collector/data`**,所以主干不许在那个库上做破坏性 schema 变更
  —— 见元规则「只增不减」。**反过来也不要去「隔离数据」**:主干那 8850 篇文章就在这个
  库里,换个默认目录等于从一个空库开始。回归在
  `collector/tests/test_shared_db_compat.py`。
- **`setup.sh` 要继续认旧变量名 `GZH_DATA_HOME` / `GZH_VENV_HOME`。** 只读新名字的话,
  用旧名字**显式隔离过**数据目录的人升级后被静默换到别处,脚本照旧返回 0 报成功。口径在
  `pick_path`:新名字优先、两个都设了打一行说明、只设旧的就沿用并提一句新名字;
  **旧变量算显式选择**,不该被「原位置还有东西」那条存量兜底顶掉。
- 详细事故形态见 `docs/decisions/ops-entry.md`。

## collector 采集器

- **公众号只有一条通道 `wechat_local`(读本机绿泡泡数据库)。** 逐号文章凭证的 `wechat_oa`
  在 `wechat-oa` 分支,主干注册表里压根没有它,所以「两条通道互斥、选哪条是个设置」那
  一整套全部删掉了。手动请求、定时配置和失败重试照旧显式携带 `channel`,不修改进程
  全局环境。
- **退役的通道名要留成别名,不能当「不认识的来源」丢掉**(`_RETIRED_CHANNELS`
  = `{"wechat_oa": "wechat_local"}`,`resolve_channel` / `known_channels` 是唯一口径)。
  存量 `schedule.json` / `daily.json` 里写着的正是旧名字,丢掉的话**公众号从此不再定时
  同步、不再进日报,而面板上完全看不出少了什么**。两条配套的:①**退役名也要能翻成人话**
  (`channel_labels()`,API 的 `labels` 字段);②**别名表要发到浏览器**
  (`/api/pipeline/channels` 的 `aliases`)—— localStorage 在后端射程之外,而前端不该把
  `wechat_oa` 写死。
- **「我的订阅」的公众号视图不信 `kb-sync-channel` 里那份全局选择**(`sources.js` 的
  `wechatSubsChannel()`:按来源目录声明的通道过别名表解析)。那是旧版同步中心记「上次
  看哪个来源」用的,存量值可能是 rss/social。**检查/重试入口同理**不依赖「某次成功初始化
  改过全局选择」:`refreshPipeSource()` 自己按目录现解析,「检查连接」重跑整个
  `openWechatSubs`。
- **逐号「就绪」那套在主干上永久是空的。** 采集器一律返回 `ready=None` /
  `state="按本机推送采集"`。`Target.ready/tone/state` 三个字段和前端那条 `ready === null`
  的渲染分支留着 —— 契约是通用的,别的来源用得上。别再把「刷新凭证」「去 Mac 绿泡泡里
  各点开一篇文章」那类文案加回来。
- **「翻过、确实没有图文」和「这一次没翻」不是一件事**(`local_accounts.discover` 给每行
  一个 `probe`:`index`/`live`/`none`/`unchecked`/`error`,文案口径是 `cli._NO_BIZ_STATE`
  (哪一档)+ `_NO_BIZ_FIX`(下一步怎么办)两半)。`discover` 裸列时一个会话都不翻,给了
  查询词也只翻前 `RESOLVE_LIMIT`(12)个 —— 把所有空 fakeid 一律说成「本机暂无可解析的
  图文推送」是在报一个没做过的结论。**「加号失败」这条提示也归这份口径管**
  (`_accounts_add` 的 `_worst_probe`):只有 `none` 那档该去同步一篇,另两档给「缩小
  关键词再搜,或者用 `--fakeid` 直接指定」。
- **同名会话里只要有一条没解析出归属,按名字添加就是歧义,一律拒掉**(`_accounts_add`
  里那个 `blind`)。**归属只认 `__biz`,同名不是线索。** 配套两条在前端
  (`targets.js` 的 `renderTgtCands`):①**归属是空的候选不许点添加**(按钮禁用 + 写清
  下一步);②**「已订阅」只按身份(`byId`,公众号是 `__biz`)认,名字一律不作数** ——
  同名的两个会话是两个号。
- **一个档案名不许改挂到另一个 `__biz` 上**(`enable_crawl_account` 抛 `AccountNameTaken`)。
  `accounts.name` 是主键、`articles.account_name` 是指向它的外键、导出的 vault 目录也按
  它归档,所以 `ON CONFLICT(name) DO UPDATE SET fakeid=excluded.fakeid` 碰上「两个号同名」
  就是把第一个号的**全部历史文章连同导出目录**接到第二个号的身份上。**拒掉是唯一不丢
  东西的做法**;没有历史文章的号移除时整行真删,所以点错了自己有退路。
  判据按元规则待在那条 UPSERT 自己的 `DO UPDATE … WHERE` 里,`name_held_by` 降级成只给
  错误文案用的读取。**四句豁免缺一不可**:①`excluded.fakeid = ''`(空 fakeid 的调用不许
  把已有归属抹成空);②`accounts.fakeid = ''`(归属还没核实的名字不算「另一个号」);
  ③`accounts.fakeid = excluded.fakeid`(同一个号重新订阅只更新);④`import:` 开头的占位
  键不算「另一个号」。回归在 `collector/tests/test_account_subscription.py`。
- **本地库只负责文章发现,正文继续按链接抓取。** 本地读取只能通过已配置的
  `rion-wechat-cli`,须确认 `live_database_read_ok`,不允许通知预览替代、自动获取密钥或
  操作绿泡泡。归属最终按推送 URL 的 `__biz` 核实,不能以同名自动匹配。
- **文章身份是 `article_identity` 的 `__biz + mid + idx`,保留已有文章 ID 和归属。** 存量
  是凭证通道采的(ID 另一套规则),本地库再发现同一篇时必须认出来、不能新开一行;用户
  删除时同时记进链接身份黑名单。不要把推送接收时间或带跟踪参数的完整 URL 当成唯一键。
- **本机推送链接须保留正文校验参数 `chksm`,仅剔除跟踪参数。** 发现阶段经 Reader 只读 SQL
  批量读 Msg 表,使用分片/表级 `local_id` 游标 —— 不要用 timeline 的 after-local-id 做
  增量分页(读取器是在 LIMIT 后过滤 ID),也不能拿发布时间当消息水位。未选公众号只进本地
  发现索引,不下载正文、不写共享 articles、不导出。`local_wechat_sources` 保存数量及首尾
  指纹,检测源变化后重扫;分页/读取失败不推进对应游标。内容异常存
  `local_wechat_message_issues`,不阻塞游标。整条图文解析失败仍从完整文章 URL 提取唯一
  biz,另存 `local_wechat_issue_biz`。无 biz 的同名会话只生成「归属待核实」诊断,不能成为
  文章身份映射。同名冲突/无归属线索的异常只汇总一次。原始 XML 只在内存中解码;压缩消息
  用锁定依赖 zstandard。正文失败存 `article_fetch_state`,以退避控制重试,不能用发布日期
  判断删除。本地正文暂不可读走 `body_failures` 待处理明细,不进 `errors`、不使抓取阶段
  判红。导出保留 `无正文,跳过` 明细协议供 CLI 生成 `no_body`。`is_wx_deleted` 的历史自动
  退休记录允许重新入库,用户删除及身份黑名单仍拦截。
- **`collector/data` 和 `collector/.venv` 丢了就跑 `./collector/setup.sh`**。两者都在
  .gitignore 里、都是链到仓库外那份真实目录的软链接,所以新开的 worktree 只带源码。
  脚本幂等。缺链时面板顶栏报「采集器还没装好」,抓取/导出两阶段落 skipped 而不是 error。
  **别再让它退回 `sys.executable`**:那是知识库自己的解释器,拿它跑 `-m cli` 必死在
  `import requests`(判定在 `wechat_local._backend_problem()`)。
- 采集器是**一次性 CLI,不是常驻服务**:`cd collector && ./.venv/bin/python -m cli <命令>`。
  常用 `status` / `collect` / `export` / `accounts list|add|remove` / `login`。stdout 只出
  NDJSON 给程序读,人看的提示走 stderr。
- **`collector/api.py` 不再是 API**。那套 FastAPI 后端和 Vue 前端已经删了,现在只剩三个
  函数(数正文字数、把一篇文章写进库、抓正文)。文件名没改是因为 `cli.py` 和
  `src/collect/` 里的 `import api` 还指着它。要看旧服务翻 git 历史。
- **公众号列表**存在 `collector/data/wechatoa.db` 的 `accounts` 表,日常维护走 kb-web
  「我的订阅」页的公众号视图,不要直接改库。终端等价物是
  `cli accounts list|discover|add|remove|enable|disable`。**添加候选来自本机绿泡泡的会话
  列表**(`cli accounts discover`):不登录公众平台、不走网络。
- **公众号改名只更新 `accounts.display_name`,永远不动 `name`。** `name` 是主键、
  `articles.account_name` 是指向它的外键(没有 ON UPDATE CASCADE),已导出的 vault
  markdown 也按它归档。**主路是本机会话列表**(`local_accounts.refresh_display_names`,
  每次同步在发现阶段之后跑一次):公众号改名后绿泡泡的会话列表立刻显示新名字,零请求、
  不限于发了新文章的号。抓正文时读到的 `var nickname` 照旧写回;别用页里裸的 `nickname:`
  (那可能是推荐位里别人家的号)。**按 biz 一一对应,不许「只有一个就配上」** —— `__biz`
  对不上的会话一律跳过,否则两个号的名字会互换。`GZH_DISPLAY_RECHECK_DAYS` 那条定期重抓
  和 `display_checked_at` 列都没了,别再加回来。
- **`enable_crawl_account` 先按 fakeid 认号,认出来只更新、不插新行。** 改名后用户必然拿
  新名字来搜索添加,照新名字插一行就会有两条同 fakeid 的订阅:同一个号的新文章分别写进
  两套主键,vault 目录裂开,外键还全合法。它返回**实际入库的档案键**,调用方据此区分
  「新添加」和「这就是那个改了名的号」。前端候选的「已订阅」判定同理按身份认。
- 别把 `collector/data` 或 `collector/.venv` 加进版本库。它们是符号链接,`data/` 那条带
  斜杠的规则挡不住,所以 .gitignore 里另有不带斜杠的显式两行,别删。
- 详细事故形态见 `docs/decisions/collector.md`。

## X 的采集通道(kb/feeds/x_channels.py + x_browser / x_feedgrab / x_cdp / cdp_attach)

- **同一批大V账号,四条互斥的取法,选哪条是一个设置**(`social` 的 `x_channel`,默认
  `bridge` —— 存量库里没这个键,读出来必须和升级前逐字同义):`bridge` 第三方 RSS 桥接、
  `feedgrab` 一份 X cookie 调 X 自己的接口(子进程调用用户装好的 CLI)、`browser` 复用
  **独立 profile** 里的登录态(我们自己起的无头 Chrome,CDP over 管道)、`attach` 直连
  用户**已经开着**的日常浏览器(CDP over WebSocket)。**口径只有 `x_channels` 一份。**
- **通道只管「走桥接的那些 X 账号」。** 填了 `feed_url` 的订阅和非 X 的 social 订阅照旧
  走 HTTP。判据沿用 `registry.uses_bridge`。
- **后两条不是 HTTP feed,不走 adapter 的 `endpoints()/parse()`**,而是在 `sync.collect`
  开头**整批预取**一次(`x_channels.prefetch`),再由 `fetch_source` 顶上那道分派按订阅
  各取自己那一份(`take`)。起一个 Chrome 要两三秒,而**同一个浏览器 profile 同时只能被
  一个实例打开**;硬塞进 adapter 会把账号主页当 feed 地址返回,报出来的原因还是错的。
- **HTTP 那一侧的采集预算要在预取之后才开始算。** 算在前面的话预取一跑完每条订阅都已经
  超预算,整轮被记成「留待下次同步」,而东西已经抓在手里了。登录态通道另算一份预算
  (`X_LOCAL_SECONDS`,默认 1200 秒),它是串行的。
- **「停止」要在起浏览器之前就认**(两条 harvest 开头各一道,每个账号之间再问一次)。
- **水位按订阅存在 `subscriptions.config` 里,`attach` 和 `browser` 共用
  `x_watermark_browser`**(同一条时间线、精度都到分钟、语义逐字相同),**feedgrab 必须
  另记 `x_watermark_feedgrab`** —— 它的下界只到天,混用会拿带时分的值去填
  `X_USER_TWEETS_SINCE`。判据是「精度和语义一不一样」,不是「是不是浏览器」。
- **没读到上次的位置,水位一步都不许动。** 抓到的照旧入库,但水位留在原处 —— 推到
  「这一轮见过的最新」就等于宣布没读那一段已经收过,从此任何一轮增量都选不到;
  「退到最老那条」同样不行。**首轮没有旧水位可留,所以存回这一轮真正用过的那个下界**
  (`x_browser` 的 `started`、`x_feedgrab` 的 `since`)—— 交空值回去等于什么都没存,
  那段没读完的日期会随时间往前滑走。**读完的那一轮照旧 `_newest(...)`,不许一律存下界。**
- **「滚不出新东西」要连着 `_BOTTOM_TRIES`(3)次、而且每次都停在页面底部,才算真到底了。**
  一次懒加载空窗和「时间线到底了」在页面上完全同形。
- **「翻到底」按页面出现顺序数,连续 `_OLD_STREAK`(3)条自己发的推都老于下界才算**
  (`_old_enough`)。①**不能按时间排序数** —— 转推显示的是原推时间,排序会把三条互不
  相邻的旧推拼成一条「连续的老尾巴」;②**只有原创推的时间能当证据**(`_own_tweet`):
  置顶在最上面而时间可能几年前,转推的时间必然 ≤ 活动时间。反过来「它很新」证明得了,
  所以**任何一条新于下界的条目都把计数重置**。
- **「算不算这条时间线的内容」(入库)和「它的时间算不算证据」(翻到底)是两个判据**
  (`_in_timeline` / `_own_tweet`),别合成一个。入库排除两种:推广位、以及作者不是这个号
  又没有转推那行 socialContext 的(推荐位)。**转推一定带着一行上下文,所以不会把转推丢掉**
  —— 正文里保留那行上下文。
- **置顶要入库,只是不参与「翻到底」的证明。** 旧置顶重复出现被 `put_items` 去重,而刚发
  出来就被钉上去的推文(新版本、活动预告)丢掉就永久补不回来。
- **「有推文卡片却一条都解析不出来」是 X 改版,不是「这个号今天没发推」**(`_harvest_one`
  里 `if seen_articles and not parsed: raise`)。登录失效 / 账号打不开 / 限流同理各一句话,
  不许折叠成「时间线上没有推文」。
- **feedgrab 是子进程调用,环境变量白名单 + 显式清场,`OBSIDIAN_VAULT` 必须清掉** ——
  它的输出目录**优先**认这个变量,而这台机器上它指的正是知识库那个 vault,不清掉抓来的
  推文会绕过整条链直接写进 `vault/X/`。每个账号一个干净的输出目录。
- **它的产物路径不许写死,只能整棵扫**(`_read_output` 走 `rglob('*.md')`)。保存位置是
  「平台目录 + category」拼出来的,而账号推文的 category 是 `status_author/<显示名>`,
  显示名只过了一遍文件名清理 —— **猜不出来**。按顶层扫的话抓到的推文一条也读不到,而
  面板上是「已同步 · 0 条」。递归不放松归属:判据是 `_entry` 按推文 URL 里的 handle 过滤。
- **它的退出码 0 不代表这一批抓全了,所以没抓全就不许推进水位**(`_incomplete` /
  `_batch_failures`)。两档各有独立证据源:①**翻页半途中断**只有一行 `logger.error`
  (`_PAGE_ABORT`),判据要连「第 N 页」「M 个账号」一起认 —— 松成裸「均失败」会把
  「降级到 FxTwitter」算进来,而把「已到达日期过滤边界」当中断会让水位永不前进;
  这一行还要和**回显了用户内容的日志**分开,三道各有射程(`>>>`/`<<<` 两个标记、跳过
  回显了本轮产物目录的行、`>>>` 紧挨在 `[标签] ` 后面且整句话落在行尾),**逐行看而不在
  整份输出上 `search`**;②**逐条失败**两道判据(上游落的批量记录**认形状不认路径**、
  收尾摘要里的「失败: N」**必须带上「成功: N」那半截上下文**),谁命中都算漏。
  只有 `fetched`/`skipped` 算没问题。两档同时命中就两句话都说。
- **不要把 feedgrab 抄进 kb。** 它要 X 内部 GraphQL 接口 + `x-client-transaction-id` 签名,
  签名算法要从登录态页面下发的 manifest 里取,它自己为此背着 60 多个 fetcher 和 playwright。
  模式和 `collector/` 一样:kb 只知道一条命令行,升级是 `git pull` 的事。
- **登录提示里的可执行文件必须就是采集时会调的那一个**(`x_feedgrab.login_command` 过一遍
  `find_binary`;`x_channels.login_command` 因此收**整份 settings**)。写死裸 `feedgrab` 时,
  只在面板上填了绝对路径的人照页面抄下来那条命令是 `command not found`。两条配套:可执行
  文件和数据目录两头都 `shlex.quote`;配了路径但当下不可执行时照它**原样显示**。
- **拿到一个浏览器有两条路,各自是一条通道,不是一个实现细节**(`x_cdp.Chrome` 管道 /
  `cdp_attach.Attached` WebSocket)。**不引入 playwright**(`\0` 分隔的 JSON 帧一个 recv
  循环就够,而登录态在用户本机 Chrome 的 profile 里)。
- **`attach` 那条的三条硬口径**:①**那个开关只能由用户自己去开**(`chrome://inspect`
  勾一次),我们不代劳也不绕开,「没就绪」那档给的下一步就是这句话(`ENABLE_HINT`);
  ②**端口是读出来的,不是我们指定的** —— 读 user-data-dir 下的 `DevToolsActivePort` +
  TCP 探活,固定敲 9222 只作回落;出路是 `KB_CDP_ENDPOINT`,**显式指定了就不再去猜别的**
  (写错了也不回落,而且要说出是哪儿错了),`parse_endpoint` **一个异常都不许抛出来**
  (它坐在来源面板每次刷新那条路上,范围另用 `_ok_port` 卡住,`urlsplit` 本身也在 try 里);
  ③**探活必须 TCP connect,不能拿 WebSocket 去试** —— 首次 WebSocket 连接会弹授权窗,
  而这条判断每次刷新面板都做;端口文件可能是陈旧的,所以读到端口不等于连得上。
- **独立 profile 那条的收尾一定做完:关浏览器怎么失败都不许把后面那几步跳过**
  (`__exit__` 只剩 `try: self._shutdown()` + `finally:` 关两个 fd、状态复位;先好好说
  一次再 kill 的两步都在 `_shutdown` 里,**都有界、都不往外抛**)。异常冒出去的话留下的
  不是用户能关掉的窗口,是一个还活着的无头浏览器。**反过来钉一边:好好退出的那一次不许
  被 kill** —— `Browser.close` 存在的理由就是让它把 profile 存盘。
- **「这个 profile 被占着吗」只认「`SingletonLock` 指着一个活着的 pid」**(`profile_busy`)。
  只看 `lexists` 会在收尾没能优雅关闭时把「检查连接」和「打开登录窗口」一起堵死,而根本
  没有窗口可关 —— 那是用户唯一能自救的两个入口。不设「看不懂就当占着」的特例,也不去替
  Chrome 删那把锁。
- **在别人的浏览器里干活的卫生规矩**:标签页一律 `Target.createTarget {background: true}`
  开、退出时**只关自己开的那几个**、**永远不发 `Browser.close`**(那会关掉用户整个浏览器)、
  顺带路径上不列用户的标签页。**「这张标签页是我开的」要在 `createTarget` 刚回来那一刻就
  登记**(`Session._adopt`),不等后面的 attach/enable 都成功 —— 初始化失败时那张标签页
  已经开在用户的浏览器里了。
- **不抄 web-access 那道「拦截页面对调试端口的探测」** —— 那需要在协议层订阅事件,而
  `Session.call` 现在把事件全丢掉。不拦的后果是「和用户自己浏览时一样」。
- **这条通道不在面板上登录**,拒绝文案**不许建议他换档** —— 他要的登录态就在自己那个
  浏览器里。
- **面板上那句静态说明不许写死通道有几条** —— 条数、名字这类跟着通道清单变的东西,一律
  让后端给的那份清单自己说。
- **Chrome 把那两个 fd 的编号写死成「读 3 / 写 4」,而 `preexec_fn` 里 dup2 到 3 实测不
  成立。** 口径是:父进程先把两端复制到一个空闲高位 fd、`pass_fds` 带过去,再由 shell 的
  `exec 3<&N 4>&M` 落到位。**高位编号必须 `fcntl.F_DUPFD` 现取、不许写死常量** ——
  kb-web 进程里 sqlite、httpx、uvicorn 的 socket 都在占 fd,dup2 会**静默关掉**目标 fd
  原来那个对象。
- **读写都永远带 deadline,而且 deadline 要在命令发出去之前算好**(`Session.call` 里
  `deadline = ...` 排在 `self._send(...)` **之前**)。写那一半原来整个没有上限:对端
  (**用户自己那个浏览器**)不收数据时 `send` 在内核里等多久,这条命令就在采集线程上挂
  多久。顺序不许反。
- **socket 和管道一律设成非阻塞,超时由 `select` 按 deadline 管。** 光「select 等可写 +
  send」挡不住一条阻塞的连接 —— BSD/macOS 对大于可用空间的载荷会在内核里循环等到写完,
  select 说「可写」只是说「有一点空间」;管道同理。所以写是「select 等可写 → `send`
  一段 → 接着等」,`BlockingIOError` 当成「再等一轮」。
- **写超时之后这条连接就废了**(`ws_client` 的 `_broken`):半条帧已经进了对端缓冲,接着
  发下一条就是帧错位,而错位之后的读是**攥着锁阻塞**的那一种。**上限是两头取小**
  (`min(这条命令自己的 deadline, now + WRITE_TIMEOUT)`),收尾另有更紧的 `CLOSE_TIMEOUT`。
- **桥接熔断只对桥接通道成立**(`sync.collect` 的 `shared_upstream`)。登录态通道的内容
  已经在预取里抓完,断开只会白扔掉已经付过代价的结果。
- **「这条订阅能不能开工」要问当前通道**(`registry.configuration_error` 按通道分支,
  `x_channels.readiness` 给 (就绪, 说明, 下一步)),收尾那行阶段文案和面板横幅都读它。
- **`readiness` 只做便宜的判断,真连一次是 `check()`** —— 前者每次刷新面板都调,后者是
  用户点「检查连接」才走。**cookie 过期在文件上看不出来**,所以两个都要有;桥接通道没有
  「连一次」可言,那一档如实说清楚,不许谎称已就绪。
- **退役通道名的别名表只在服务端**(`_RETIRED` + `resolve()`)。X 的通道选择只存在
  `feed_settings` 里,浏览器手里永远拿不到一个退役名 —— 跟公众号那边不一样。
- **同步间隔是一天一次**(1440)。**改 `interval_minutes` 只影响没有设置行的新库,存量要
  迁**(`_SETTINGS_REV` + `_SETTINGS_FIXUPS`,在 `get_settings` 里归一):①**只迁仍然等于
  旧默认值的那些**;②**读的时候归一、写的时候盖章** —— 反过来写会在标记落下之后、设置行
  写进来之前留一个永不迁移的窗口,而那正是升级的常态。
- **水位和条目必须在同一个事务里提交**(`watermark_patch` 交回一份 config 补丁,
  `sync._save_result` 在一个事务里跑 `put_items` + `merge_config` + 成功时间戳)。
- **存盘之前在那个写事务里认一次「这条订阅还是同一个号吗」**(`sync._repointed`,成功和
  失败两条路各一次)。采集手里那份快照是发请求之前读的,这中间用户完全可能把它改指到另
  一个账号 —— 最狠的是**旧账号的水位写进新账号那个键**。判据按元规则排在
  `_claim_write` 拿到写锁**之后**;判据是**身份**(`url`/`feed_url`),不是「这一行被动过
  没有」。作废的那一轮**一个字都不写**,记进 `summary['repointed']`。换链接那次 `reset`
  要把 `last_note` 一起清掉。
- **区间补抓要把它那段日期交给采集器当下界,而且不许动日常水位**(`prefetch(since=...)`)。
  **用户点的那个日期要在一处换算成带时区、且把起点算在内的下界**(`since_floor`):
  ①时区按项目统一的 **Asia/Shanghai**,不按部署机器的(第二道在 `x_browser._utc`:不带
  时区的写法一律按项目时区补齐);②**起点算在区间里** —— 下游那条选择规则是严格大于,
  所以给的是那一天零点前的最后一瞬。换算在这里做而不是给下游加开关,因为 feedgrab 的
  `X_USER_TWEETS_SINCE` 只认日期而且按 UTC 的日算。
- **「这一轮没读完」要在面板上看得见,而且不是 `last_error`** —— 写进
  `subscriptions.last_note`,显示成「部分同步」。抓到的照旧入库、不进退避重试;读完的
  那一轮把它重写成空串。
- 前端那一块在 `kb/static/js/x_channel.js`(独立一个文件)。**哪条通道要填什么由后端给的
  id 决定、前端不许写死一张表**;改了选择还没保存时状态行要写明「保存之后才会生效」并且
  **藏掉「检查连接」**。**「打开登录窗口」也跟着「保存了没有」走,而且是禁用、不是藏掉**
  ——「检查连接」产出的是**结论**,贴在新选择旁边就是谎话;这颗是一个**动作**,藏掉它上面
  那段说明就悬空了。`openXLoginWindow` 开头另有一道 `xChannelSaved()` 兜底,`finally` 里
  恢复成「此刻该不该能点」。**那两颗按钮打的是动作端点,一律 `pipePost`** ——
  `pipeJson(path, {}, 120000)` 实际发的是 **GET**,而后端只收 POST。
- **面板空态那句「要先配置什么」不许写死桥接那一档**(`sources.js` 的 `sourceConfigHint()`
  读后端给的 `configuration_error`)。
- 详细事故形态、回归清单与反向验证见 `docs/decisions/x-channels.md`。

## X 长文(Article)的正文补全(kb/feeds/x_article.py)

- **长文推文在时间线上一个字都没有** —— 推文的 `text` 就是一条指向 `x.com/i/article/<id>`
  的短链,X 把它改渲染成一张卡片。两条通道入库的都是空正文。
- **主路是 FxTwitter 的公开接口**(`tweet.article.content` 是 Draft.js 块)。另外几条路各自
  不成立:`/i/article/<id>` 不登录打不开;X 的 syndication 接口只给标题和约 120 字预览;
  feedgrab 同样要登录态签名。**后路**是在 `browser`/`attach` 的登录态浏览器里按 DOM 读
  (`browser_fetch`,由 `x_browser.harvest` 把还活着的会话传下去)—— 它不当主路的理由不是
  读不到,是只对两条通道成立 + X 改版就要跟一次。`bridge`/`feedgrab` 没有浏览器,照旧
  「补不到就不入库、不推进水位」。
- **文章页地址 = 推文地址把 `/status/` 换成 `/article/`,不需要 `article_id`。** 这是后路
  成立的前提 —— `article_id` 只出现在接口回包里,而后路恰恰在接口不可用时才跑。
- **「这条推文没有长文」的判据是 X 自己的路由行为**:纯图推文的 `/article/<id>` 会 302 回
  `/status/<id>`。这一档返回 `None` —— 照旧入库、**不冻水位**,不分开的话一条纯图推文能把
  这个号的水位永久冻住。**判不准的时候一律抛,不许返回 `None`**(登录墙、空壳、渲染超时、
  CDP 报错全部抛出去)—— 见元规则「拿不准就别固化终态」。CDP 报错只抛
  `ArticleUnavailable`,不冒到「浏览器会话中断」上去。
- **后路落地之后要核对「这一页到底是不是我们要的那一篇」,这一道承重**(`seen[1] != want`
  一律抛)。导航**可能不生效而页面停在原处**(一篇 HTTP 204 就够)。配套三条:`Session.goto`
  改成**返回** navigate 回包而不抛(时间线那两处各自按 `_STATE_JS` 判);回包里的
  `errorText` 是**早一道**、不能靠它;**`back` 那一档也要求身份相符**。
- **这一步要带整轮的时间预算和停止开关**(`fill(..., deadline=, should_stop=)`,**两条通道各挂
  一次** —— `x_browser.harvest` 和 `x_feedgrab.harvest` 的调用点都传)。它跑在时间线读完**之后**,外面那两道闸都够不着它,而 `_GIVE_UP_AFTER` 只数
  **连续**失败,拦不下「大量慢但成功」。拦下的按扣留办。**接口切后路之前再问一次** ——
  开标签页等渲染是最贵的动作。
- **块内按子节点顺序拼,`<img>` 分两种,判据是 alt 的内容**:至少含一个表情码点**且**整串
  只由表情相关码点组成的是行内表情(按 alt 还原),其余一律是真·配图(`![alt](src)`)。
  「这一块里有 img 就只要图片」会把**整段文字丢掉**;「非空 alt 就是表情」会把**图片地址
  整个丢掉**(给配图配描述是无障碍常规做法);`innerText` 不包含 alt,表情会静默消失。
- **预格式化块里的空白是内容,一个都不许折叠。** 判据两道、任一成立即可:计算样式的
  `white-space` 以 `pre` 开头,或者标签本身是 `pre`/`code`/`samp`/`kbd`。折叠挪到文本节点
  那一层。**反过来钉一边**:只存在于 HTML 源码里的换行缩进照旧折叠。
- **零宽字符只清 ZWSP 和 BOM,`\u200c`/`\u200d` 一个都不许动**(它们是组合表情的一部分)。
- **代码块里一个字节都不改**(零宽清理和 nbsp 归一只对普通段落做)—— 两条路对同一篇文章
  必须给出同一份代码。**占位清理整个挪去组边界**(`codeBody` 的 `blankRow`):在逐块那一支
  做的话,组**中间**那一行零宽字符也被当占位符清掉,而它多半是三引号字符串里的数据。
- **代码块要写成 markdown 代码围栏 —— 读对那串字符只是半截。** 两条路各一处(主路认
  Draft.js 的 `code-block`,后路认标签)。裸代码在 markdown 段落规则下会把换行和连续空格
  一起折叠,阅读页渲染和**真实选区复制**出来都是一行。只能靠围栏:写 `<pre>` 没用(阅读页
  按契约把正文里剩下的标签一律当字面文字),改阅读页 CSS 会动全站且 vault 里躺的仍是裸代码。
  配套四条:①**相邻的代码块合并成一个围栏**(Draft.js 是一行一个块);②**围栏里每一行都
  原样,一个字节都不改** —— 行首缩进和**行尾空白**都是内容,所以**收尾不许用 `.strip()`/`.trim()`**
  (从一段代码中间截出来的片段首行就带缩进,而行尾空白在多行字符串、提示词模板、字符画里是数据)。
  占位空块只在**整组边界**削**完全空**的行,纯空格的一个都不削;两侧各一份实现
  (`_code_body` / `codeBody`)、**各有自己的回归**;③**围栏长度要避开代码里已有的反引号串**
  (`max(3, 最长串+1)`);④**后路那道判据只认标签,不许用计算样式** —— Draft.js 官方默认
  样式给**每一个块**都加了 `white-space: pre-wrap`。`preish`(保住空白)和 `codeish`
  (写围栏)是两道**不同**的判据。
- **补完正文不许把转推上下文丢掉。** 入库正文是三段按原顺序拼:转推上下文(`> …`)→ 推文
  自己写的介绍 → 文章正文(`quote_lines` / `lead_text` 各摘各的)。只用 `lead_text` 的话
  那行「某某转发了」会连 feed_items、vault md 和阅读页一起没了。
- **接口答「有标题、没正文」时一律当成没取到。** 回包被截断、`content.blocks` 缩成空的
  那个形状原来返回成功,而空正文一旦落库就改不回来。
- **第三方回包的结构异常一律留在接口边界内,两道各有射程。** ①**承重的是每一层明确判类型**
  (`_rows` + `_media_urls` / `_entity_image` / `_entities` / `render` 各一遍 `isinstance`)—— 认得出的结构异常**就地降级**(那张图跳过、那一块跳过),
  整篇正文照旧补得到。**不许拿 `or {}` / `or []` 当类型检查**(它只认假值,而撒谎的回包给的
  是**非空**的错类型);**判一层不够,得每一层都判**,这张名单上没有「这一层不会出问题」
  这种例外。②**兜底是 `fetch` 把 `render` 的数据形状异常一起翻成 `ArticleUnavailable`**
  (`AttributeError`/`TypeError`/`KeyError`/`IndexError`/`ValueError`),只圈这几类,不写
  `except Exception`。`json.loads` 那道 except 要带上 **`RecursionError`**(它是
  `RuntimeError` 的子类、不是 `ValueError`;但**绝不能写成 `except RuntimeError`**)。
- **必需的块结构只收字符串,`str()` 强转是在给错内容盖「成功」的章**(`_must_text`)。强转
  得到的是**非空**结果,于是「正文为空就抛」和异常兜底**都没有机会介入**。必需(块的
  `text`/`type`)认不出来一律抛;可选(图片地址、标题)认不出来跳过那一块,**但同样不许强转**。
- **正文块列表的*成员*也是必需结构,整个成员不合格时不许 `continue`**(`_must_block`)。
  丢掉一个字符串成员后剩下的块照样拼出**非空**正文 —— 这是「残缺」不是「空」,两道兜底都
  接不住。**空值跳过、非空错类型一律抛**(真的空块是 `{}`,走正常路径留一个空行)。
  **后路那一侧是同一条缺陷的两个位置**,各有自己的回归。
- **非空的错类型 `article` 不是「确定没有长文」,一律抛。** 反过来钉一边:缺失、`null`、
  空对象、空列表、空串照旧返回 `None`(纯图推文真实的形状,照旧入库、不冻水位)。
- **`render` 只按块级转 markdown,块内 inline 样式一概不动** —— `inlineStyleRanges` 的偏移
  是 **UTF-16 码元**。另两处形状坑:`entityMap` 是**列表不是字典**(键一律按字符串比);
  图片要 `mediaItems[].mediaId` → `media_entities[].media_id` → `original_img_url` 三跳,
  **查不到就跳过、不许编一个**。**正文里不写标题**(`sync.export` 自己会写)。
- **`needs_article` 三条判据**:①正文里**明写着** `x.com/i/article/<id>`;②**时间线上看见了
  那张长文卡片**(`x_article.HINT`,从 DOM 里读,排掉引用推容器里的那张,**只补不清**);
  ③推文自己一个字都没说。**不拿「正文里有 t.co 短链」当信号。** 那个信号键**不进
  `feed_items`**(`_strip_hint`,`fill` 每个出口都过它)—— `put_items` 把整份条目哈希成
  `content_hash`。`links_only` 是桥接那一档。
- **`bridge` 这条通道也接上了,钩子是 `x_channels.fill_bridge`、不在 `fetching`。** 它待在
  `x_channels` 有两个独立理由:职责(「哪些订阅不走 HTTP」的唯一口径)和行数。
  **桥接这条路的扣留 = 本轮不入库 + 这一轮的 `etag`/`modified` 一个都不存** —— 它没有水位
  可以留在原处,存了 validators 的话下一轮条件请求命中 304,被扣下的再也没有第二次机会。
  `resolved_url` 照旧存着。
- **补不到的那几条本轮不入库,而且这一轮算「没读完」** —— `put_items` 是 `DO NOTHING`,
  空正文落库就再也更新不了。水位留在原处,面板显示「部分同步」。
- **两个开关各管一条路**:`KB_X_ARTICLE_API=''` 关掉接口(手里有浏览器时就只走后路,这正是
  「不想把推文 URL 发给第三方」要的那一档);`KB_X_ARTICLE_BROWSER=0` 关掉后路。
  **后路成功要把 `_GIVE_UP_AFTER` 的计数清零。**
- **失败文案只取 `fetching` 那句话分号前的那一半** —— 后半截「请检查来源或更换订阅桥接」
  在长文语境里是胡话。
- **存量回填是 `scripts/x_article_backfill.py`(scan / run --yes)** —— 采集那侧修好只对**新**
  长文生效。它只改 feeds.db 的 `title`/`body`/`content_hash` 并清 `exported_hash`,不写
  kb.db、不写 vault。**`lang` 要一并清成空串**(空正文那会儿按占位标题判成了中文,而
  `lang='zh'` 永远进不了翻译队列);不想花翻译钱就把 `translate_tries` 设成
  `translate.MAX_TRIES`(队列自己的出口),**别改成 `lang='zh'`**(那是谎报语言)。
  写回按元规则带条件(`AND content_hash=? AND length(trim(body))=0`)。
- 那段读文章页的 JS 在 `kb/feeds/x_article_js.py`、回包结构判定和块级零件在
  `kb/feeds/x_article_shape.py`,**只因为行数**。`API`/`PAGE_WAIT`/`BROWSER_FALLBACK`/
  `MAX_CHARS` 一个都没搬 —— 它们被 `patch.object` 改过,搬走之后打桩只改到一份副本。
- 详细事故形态、回归清单与反向验证见 `docs/decisions/x-article.md`。

## 聚合源的正文补抓(kb/feeds/fulltext.py + readable.py)

- **feed 只给一个跳转壳时(标题 + 域名 + HN 得分),顺着原文链接把正文抓回来。** 按壳入库的话
  vault 里那篇 md 没有内容:检索搜不到、打标判不出实体、日报里只剩一行标题。
- **开关是订阅级的、默认关**(`subscriptions.fulltext`)。只有链接聚合源需要它,对本来就给
  全文的源再抓一次是纯粹的降级。`defaults.json` 里**只标了 HN Buzzing** 一条;那段只对新库
  跑,存量库要在面板上点一下。**给 defaults.json 加字段必须同时改那条 INSERT。**
- **独立一列,不放 `config`** —— 换链接时 `save_subscription` 的 `reset` 会把 config 整个清空,那是采集进度该有的
  下场,不是用户配置。
- **这一步排在翻译之前**(翻译是这条链上最贵的一步)。**还排在补抓队列里的条目既不翻译也不
  导出**,两边各认一次本模块的 `queued_sql`。队列判据:属于某条开了补抓的订阅、`fulltext_at`
  空、`fulltext_tries < MAX_TRIES`。**出口就是第三条** —— 试满仍抓不动的按 feed 原本那份壳
  走完翻译和导出。`sync._export` 里收窄 base 那一句**必须排在所有统计之后**(那几条统计语句
  各自只带一个占位符)。
- **「在队列里」和「没入库」是两件事,按 `exported_hash` 分两态**(4 `fetching` /
  5 `refetching`)。打开这个开关最常见的时机正是「几百条壳已经在 vault 里了」,对它们说
  「抓到之后才写进知识库」和事实相反。已导出那一档如实显示库里那份壳、照旧能展开,顶上那句
  计数也只数还没入库那一档。**待补抓的条目标题照显示**,换掉的只有正文那一段。
- **提取器自己写,不装 readability/trafilatura。** 我们要的输出是**纯文本**,而块级边界已经
  由 `fetching.plain` 定死;而且 `pyproject.toml` 一动 `uv run` 就要重建 editable 包。
- **`find_all` 是快照,`decompose()` 之前必须先问「这个节点还在吗」**(`_gone`)—— bs4 把被
  销毁节点的 `attrs` 置成 None,一个 `<div class="sidebar">` 里套着 `<div class="share">`
  就够当场抛。
- **`header`/`footer`/`aside` 要看位置,不能无条件删** —— `<article><header>` 里装的是文章
  自己的标题作者日期。只有落在 `<article>`/`<main>` **外面**的才是站点部件(`_DROP_OUTSIDE`)。
  按 class 名剔噪声只对 `div`/`section` 做。
- **提取不出来不是故障,是「这一页我们读不懂」**:保留原本那份壳、条目照旧入库、`fulltext_tries`
  计次(不计次的话这条永远排在队首)。宁可少补一篇,也不能把导航栏当正文译了写进知识库。
- **「抓到了一页东西」不等于「拿到了正文」:版权声明和作者简介块要当成没拿到**
  (`readable.boilerplate_reason`,两道:授权措辞句子占比 ≥ `MAX_LEGAL_SHARE`(0.5),或开头是
  作者简介区抬头而整块 ≤ `MAX_BYLINE_CHARS`)。这两种页面**结构上完全正常**,于是补抓报成功、
  条目离开重试队列、接着买一次翻译 —— **这一档不可恢复**。
- **署名不是作者简介,`_BYLINE_HEAD` 只认「这一块是作者简介区」的抬头**(`about the author(s)`
  / `author bios?` / `作者简介` / `关于作者`)。`written by` 一度在名单里,而它是正常文章的
  标准开头。**判据是措辞,不是段落数。**
- **原文链接后面不是网页时按抓取失败处理,绝不把文件语法当成正文**(`readable.html_problem`)。
  **三道,各管一类撒谎方式**:①**声明的类型**不在 `_TEXT_TYPES` 里;②**文件头**命中已知魔数
  (PDF 的首个压缩流常在几十 KB 之后,RTF/PostScript 整份都是 ASCII —— NUL 那道对它全是瞎的);
  ③头部有 **NUL 字节**,**而且拿 `decode` 真解一次之后它还在**。③不能裸着问 —— UTF-16/32 的
  **正常文本**每隔一个字节就是 `\x00`。**判据是「拿真正会用的那个解码器解一次」,不是另开一张
  编码白名单** —— 两张名单一漂,只有 BOM、不声明 charset 的页面就从安全失败变成错误覆盖。
  配套 `decode` 自己要认 BOM(`_BOM_CODECS` 排在 HTTP charset 和 meta 前面,**UTF-32 必须排在
  UTF-16 前面**)。`text/plain` 留在放行名单里。
- **存量复查要连那份 body 一起换掉**(`scripts/feed_fulltext_recheck.py`)—— 光清三个标记的话,
  重抓三轮失败后它离开补抓队列,那段版权声明会被**重新翻译一遍、重新导出**。换完它就是一条
  「从没补抓过」的普通条目(`content_kind='summary'`、`content_hash` 重算),**是正确的降级**。
  壳从备份库恢复(`--backup`,显式给了就只认那一个)或按标题链接重建;恢复回来的要**再过一遍
  判据**。`content_hash` 的口径抽成 `fulltext.body_hash`,脚本和 `_save` 共用。
  **射程是「属于开了补抓的订阅」,不是「报过补抓成功」**(`_suspect`)—— 后者漏掉的恰恰是这个
  脚本自己制造的中间态。写回按元规则带 `AND body=?`(比旧 body 本身,不比 `content_hash` ——
  那一列有两个写入方、两套口径)。
- **失败过的优先**(`ORDER BY fulltext_tries DESC, fetched_at DESC, key`);`key` 收尾是为了
  定序,`fetched_at` 只精确到秒。
- **`_save` 要把旧译文一起清掉**(旧译文译的是那份壳),`translate_tries` 归零。
  **但不许清 `exported_hash`** —— 导出闸门是 `exported_hash<>content_hash`,正文一换前者就
  自然成立;而它同时是「这一篇入没入过库」的事实来源,清掉会让一篇**已经能展开阅读**的旧条目
  标题连同入口一起消失。
- **`subscription_items` 要一条按 `item_key` 的索引**(主键是复合的,按 item_key 查用不上)。
- **正文从壳换成全文之后,按旧壳算出来的提取结果要作废**(`fulltext.retire_stale`,由
  `sync._export` 在导出之后调一次)。不作废不是「晚一点更新」:`extracted_at` 非空意味着这一篇
  **永远**不进 `pending_docs`,而 `_kept_meta` 又把旧 `tags` 原样写回。四件事:①**时点是「新
  正文真的落了盘」**(`extract_stale=1 AND exported_hash=content_hash`),不是「网络抓到了」;
  ②**做完才把标记放下**,顺序不能反,所以它查的不限于本轮刚落盘的那几篇;③**异常要接住**;
  ④**摘哪些 tag 只有一份口径**(`STALE_TAG_PREFIXES` + `keep_tags`,只摘 `topic/` 和
  `entity/`,`flag/软广` 和人工 tag 留着)。不清 `promo_level`,也不去动
  `store.upsert_document` 的 `extracted_at` 语义。
- **一次提取的结果要和它基于的那份正文绑在一起**(`extract.body_fingerprint` +
  `save_extraction(..., body_seen=)`)。**两道判据各管一种时序、缺一不可**:①**索引还没扫到
  当前这份 md**(`store.doc_needs_reindex`)—— 那一刻 chunks 压根没动过,②对它是瞎的;
  ②**索引里那份正文已经不是我读到的那一份** —— 中间那次 index 已经把 chunks 换了,①对它是
  瞎的。判据待在 `save_extraction` 里,**读 chunks 正文 + 有模型往返的那三条路径**必须传指纹
  (`kb/cli.py`、`kb/pipeline_stages.py`、`scripts/extract_parallel.py`),不读正文的两条不传
  (`extract` 自己改判软广、`scripts/pretagged_refresh.py` 走 `parse_pretagged`)。**`StaleBody` 不是故障**,三条路径一律归到「跳过」那一档。
- **校验本身给不了保证,它必须和写入在同一个协调区间里**(`extract.commit_extraction`:维护锁
  → `BEGIN IMMEDIATE` → 锁内校验 → 落库 → 提交 → 回写)。锁内还要先问一次「这一篇还在吗」。
  **粒度仍然是每篇各取一次锁、模型请求留在锁外。** 反过来,**md 压根读不出来(清理正把它删掉)
  不算「版本不对」,照旧放行** —— 那一档本来就有人管。
- **`documents.content_hash` 的口径抽成了 `scanner.file_hash`**(索引、`refresh_content_hash`
  原来各写一份)。**手摆 `documents` 行的测试摆盘必须按它算 `content_hash`**,否则摆出来的是
  一个生产里不存在的状态。`_extract_pass` 的 `stats` 一律用 `_new_extract_stats()` 构造。
- 详细事故形态、回归清单与反向验证见 `docs/decisions/fulltext.md`。

## 正文纯文本化与阅读渲染

- **只有块级元素换行,行内元素一个分隔符都不插**(`fetching.plain`)。别退回
  `soup.get_text('\n', strip=True)`:那个分隔符对**每一个文本节点**各插一次换行,而
  `<a>`/`<em>`/`<code>` 正是把一句话切成多个文本节点的东西,**段落边界跟着一起塌掉**。后果是
  三重的,只有第一重看得见:面板按 `pre-wrap` 一行行照显、导出的 md 整篇没有空行、翻译按
  `'\n\n'` 切块于是整篇算一个段落从句子中间硬切。
- **边界分三档,各插一个标记,相邻标记合并成最强的那一档**(`\x02` 块级 > `\x03` 换行 > 空格)。
  前后各插一个而不是只插一个,为了不依赖兄弟节点顺序。`<br>`/`<tr>` 只断行,`<td>`/`<th>` 用
  空格,`<li>` 归块级。
- **`<pre>` 先整块摘出去、最后再放回来**(NUL 占位符),放回必须排在行清理之后。入口先把
  `\x00`/`\x02`/`\x03` 从 html 里清掉。**摘的时候换掉的是内容,不是整个标签**
  (`tag.string = 占位符`)—— 整个 replace 之后 `find_all` 找不到这个标签了,它自己那道块级
  边界跟着一起没。
- **空白折叠要赶在插标记之前做**;逐个文本节点替换时判 `type(x) is NavigableString` 而不是
  `isinstance`(`Comment`/`CData`/`Doctype` 都是它的子类)。
- **先问正文是什么类型,再决定怎么解析**(`body_text(value, kind)` 是唯一入口)。**纯文本里的
  换行就是内容**,HTML 空白折叠会把它全抹平,所以纯文本只做最轻的归一(`_plain_lines`)。
  类型要跟着值一起取(`content[i].type` / `summary_detail.type` 各有一份)。
- **分流只问类型,不看内容长什么样。** 别加「内容里有标签就按 HTML 处理」那种嗅探 ——
  「这段文字能被解析成标签」证明不了作者标错了类型,`List<T>`、尖括号写的链接、字面 HTML
  示例全是这个形态,而它们正是作者声明 `type="text"` 要原样显示的东西。类型取不到就按 HTML 走,
  并且容忍 `None`。
- **标题和作者走 `oneline`**(plain 之后把换行压成空格),**也要带上类型**。
- **实现里不许有递归**(畸形 HTML 嵌套几千层只是慢,不会 `RecursionError` 带崩整个源)。
- **存量不会自动修复**(`put_items` 是 `DO NOTHING`),改这里只对新条目生效。
- **vault 里文章正文按契约已经是纯文本,所以阅读页(`/doc/`)正文里剩下的尖括号一律按字面文字
  显示**(`kb/web/pages.py` 的 `_DOC_LITERAL_JS`:`marked.use` 把 html token 转义掉)。**不是关掉净化**,
  DOMPurify 照旧过一遍 —— 改的只是「原始 HTML 怎么渲染」,比原来更严。**不许改成「渲染前把
  整份 md 里的尖括号先转义掉」**(那一刀落在 markdown 解析之前,**代码围栏里的内容是字面量**),
  **也不许在导出那一侧转义**(md 是事实来源,索引/FTS 读的就是它)。**只改这一页**,
  `core.js` 的 `mdHtml` 渲染的是模型写的 markdown。`renderer.html` 的入参 marked v12 给字符串、
  v13 起给 token 对象,两种都接住。
- **正文是嵌进页面内联 `<script>` 的,所以那份 JSON 里的 `<` 一律编码成 `\u003c`**
  (`_script_json`),**不是只防 `</script>` 那一种写法** —— 脚本数据状态里 `<!--` 之后再出现
  `<script` 会把分词器推进「双重转义」,于是整段脚本一行都没执行、阅读页**只剩标题**。放进
  markdown 代码围栏里不管用(故障发生在 markdown 解析**之前**)。同形状但没动的是两张图谱页。
- **大V观点那一档不显示标题,别再加回来**(`sources.js` 的 `renderSourceItems`)。推文没有
  「标题」这回事:采集器把正文前 500 字符原样当 title,于是卡片上那行大标题和紧跟着的正文是
  同一句话读两遍,而标题那个 `<button>` 继承了 `white-space: nowrap`,长推文直接撑出卡片。
  阅读弹窗同理不显示它。**判据是条目自己的 `content_kind === 'post'`**,不是「当前视图是不是
  social」—— 要回答的是「这一条内容有没有标题」。页脚那句「社交观点 / 订阅正文」问的才是来源。
  去掉标题之后正文从 muted 摘要提成次要正文(`.source-excerpt.is-post`);**等翻译那一档照旧
  显示那行状态文案**(它回答的是「为什么没有内容」)。`.source-entry-title` 要
  `white-space: normal`(base.css 给所有 button 设了 nowrap)。
- 详细事故形态见 `docs/decisions/feed-plain.md`、`docs/decisions/doc-reader.md`、
  `docs/decisions/source-cards.md`。

## 订阅的历史深度(kb/feeds/registry.py + sync.py)

- **新订阅只收最近 `reg.HISTORY_DAYS`(5)天的内容**(`sync._within_history`)。一个刚添加的源
  常常把整个存档一次交出来(实测 500 条、最早到两年前),这些旧文随后逐批翻译、逐批重新导出,
  每重新导出一次 `indexed_at` 就刷新一次,于是**连着十几天以「补录」的形态占满当天日报**。
- **下界是订阅创建(或改链接重置)那一刻钉死的一个日期**(`subscriptions.history_floor`),
  **不是「距今 N 天」的滑动窗口** —— 滑动窗口会在源站停更几天后恢复时把那几天静默丢掉,而那
  正是最该补回来的一批。**也不能只拦「第一次抓取」**:第一次之后 etag 命中 304,等源站下次
  更新时整份 feed 又回来了,那时闸门早撤了。
- **老库里的订阅补成空串 = 不设下界,行为逐字不变。** 它们的存档早就抓完了。
- **没有发布时间的条目照收**(身上没有任何「很旧」的证据),日报那边按「补录」接住。
- **区间补抓(`scope.ranged`)不受这道闸限制** —— 那是唯一的逃生口。下界按 **Asia/Shanghai**
  算,因为被比较的 `published` 就是按这个时区格式化出来的。
- **`_within_history` 只管往后,存量要单独清**(`scripts/feed_trim_history.py`)。清理**同时
  会钉上 `history_floor`**,不钉的话下一次同步按空下界把整个存档原样再拉一遍。顺序:备份 →
  钉下界 → 删 feeds.db 行 → 删 vault md → `remove_missing_documents`;**feeds.db 要先删**
  (它是「要不要重新导出」的事实来源)。**被别的订阅共享的条目只摘归属、不删行**,而且要
  **连文件和索引一起算**(`_live_paths`)—— 否则另一个订阅仍然列着这篇文章,点进去读不到、
  搜不到、再同步也不恢复。**收藏保护只按真要删的那些判。**
- **另一条筛选口径 `--pending-translation`**:清掉某个源还排在翻译队列里的条目,判据直接取
  `translate.queued_sql()`。**这一路不钉 `history_floor`**(筛的维度不是日期)。它和 `--floor`
  不能一起用,**而这道互斥检查必须排在所有副作用之前,「续上没做完的那一份」也算副作用** —— 口径在
  `cmd_run` 的第一句,**不在 `_run_locked` 里**;取锁同理算副作用。**有没做完的计划时,这一趟只续它、续完就 `return 0`**,不许接着按这次的
  口径再开一轮 —— `cmd_scan` 给出的恢复提示不带任何筛选开关。
- **「给了 `--floor` 没有」问的是 `args.floor is not None`,不许问真值** —— `--floor ""` 是
  `--floor "$CUTOFF"` 而变量没赋值这种最常见的 shell 事故,而这是不可逆删除。
- **下界先校验再用**(`norm_floor`),非规范写法在备份和任何删除之前就拒掉:比较是**字符串**
  比较,`2026-10-1` 少写一个零就既大于当天那篇、又大于第二天那篇。
- **删除分三步、每一步做完记一笔**(`progress.json`),各步幂等,重跑先把没做完的续完。
  feeds.db 是**先提交**的,所以后面抛异常时重跑会说「无事可做」而文件和索引都还在。
- **清理和同步、索引之间有一把跨进程锁**(见维护锁那节),锁之外还要**「下界现读」**
  (`sync._stored_floor`)—— 两道各管一段,缺任何一道都会复发。
- **问不到管道状态一律拦下,只有连接被拒才放行**(连接被拒 = 服务没起 = 没有管道在跑,这是
  唯一能证明空闲的那种)。
- **`--source` 是纯数字时只按 id 查,不许退回名字**(`trim.find_source`)—— 一个名字恰好叫
  「62」的订阅会抢在 id=62 前面。**名字命中多条时拒绝执行、要求改用 id。**
- **备份目录要原子地占一个唯一名字**(`tempfile.mkdtemp`,前缀带订阅 id)—— slug + 秒 +
  `exist_ok=True` 会让两个同名订阅在同一秒里复用同一个目录。
- **续跑不是「接着执行上次批准的那件事」,收藏那道护栏要按此刻的事实再查一遍**,而且
  **必须和删除在同一个写事务里**(`_apply` 在 kb.db 上 `BEGIN IMMEDIATE`,最终复查、unlink、
  `remove_missing_documents` 全在这一个事务里)—— **索引能从 vault 重建,收藏和备注不能**。
  配套:**删索引必须用持锁那条连接**;`--allow-favorites` 放行时把要毁掉的那几行另存
  `favorites.json` 进备份。
- **没有旧文可清 ≠ 不用钉下界**;**没带 `--yes` 一个设置都不改**。
- **测试样本的发布日期必须跟着今天走**(`_FRESH`/`DAY`),写死日期过几天就被这道闸挡住。
- 详细事故形态见 `docs/decisions/history-depth.md`。

## 「重试失败项」的范围(kb/pipeline_retry.py + feeds/sync.py + pipeline_state.py)

- **这颗按钮的范围 = 继承上一轮的「来源」+ 各来源自己留下的失败名单**(`retry_crawl_scopes`)。
  继承的是「哪几个 channel、上次点名了哪几个目标」,**不是「抓多少东西」** —— 后者按**来源
  逐个**收窄,外加一个 `retry` 字段说明这一项是怎么来的(`targets` / `all`)。
- **不收窄的两个缺口是反着的**:①继承来的 `accounts` 是空的,而空在 `sync.collect` 里正好
  是**全部** —— 一个账号填错地址换来整条通道十几分钟重读;②`FetchScope.force` 这条路上压根
  没人传过,而失败恰恰会记一个 `retry_at`,于是半小时内点这颗按钮,唯一该抓的那条被跳过。
- **名单要在失败那一刻记下来,不能事后从别处反推**(`note_retry_targets` 写、`retry_notes`
  读)。三条反推的路都不成立:按 `last_error` 筛会漏掉「已改正、还没验证过」的(改链接时
  `reset` 把它清空了);从阶段状态那份 failures 反推拿到的 `title` 对公众号来源是文章标题;
  一个全局布尔会让一个来源的失败类型决定另一个来源抓多少。
- **必须按来源各记一份,因为「整批失败」和「只坏了一条」是两件相反的事** —— 两者在
  `subscriptions` 那边长得一模一样,**分得开它们的只有「这个来源留下名单了吗」**。
- **「仍然需要重抓」的判据是「那一轮失败之后有没有成功同步过」**(`sync.still_failing`)。
  **名单的时刻要用采集器自己那套时间格式**(UTC ISO,不是本地空格格式)—— ISO 的 `T` 排在
  空格后面,同一天里恒大于,整份名单会被摘空。`at` 因此是**必填**的。
- **一条都没失败也要记一份空名单** —— 那份空名单本身就是「这个来源扫完了、没有要重抓的」,
  而「压根没留下名单」另有含义。名单**带着它自己那一轮的 run id**。
- **报不出名单的来源按原范围重跑**(`collectors.RetryNarrowing`)。**这不是缺陷,是另一种真实
  情况**(公众号的失败是文章级的,而它的 targets 是账号级的)。生成计划这一步**不许抛**。
- **「这个来源一个都不用抓」= 整个来源不进这一轮,不是给它一个空 `accounts`。** 全空时走
  `retry_failed_plan(nothing_to_crawl=True)`,crawl 整个摘出计划 —— 真让它跑,
  `crawl_channels` 会回落到默认采集器。
- **export 走上一轮**原来**那几个来源,不跟着 crawl 收窄**,而**「原来那几个来源」必须自己落
  一份盘**(`pipeline_state` 的 `export_filter`,三处写入口)—— 否则这条口径只在第一次重试上
  成立。**常规同步那条路要把它重写成自己那一份**,不靠「没设过就回落」兜底。
- **造范围这一步的故障不许扩散到别的阶段上,三道各有射程**:①**先定计划,只有 `"crawl" in
  plan` 才去问名单**(这一道和采集器无关);②**认名字和问名单一起护在那道 `except` 里**;
  ③**名单是空的就别开库**。
- **收窄要和 `targets` 取交集**,不是扫一遍库里所有失败的订阅。**「预算跳过」和「退避中」
  那两档不进名单**(压根没被尝试过,本来就会在下一轮轮转补齐)。
- **没有要重抓的时要说清为什么、并指到正确入口**(`RETRY_NOTHING`,crawl 落 `skipped`)。
- **收窄过的那一轮要把范围说出来,而且按来源各说各的**(`pipeRunScopes` 透传 `retry`)。
- 详细事故形态见 `docs/decisions/retry-scope.md`。

## kb.db 的维护互斥锁(kb/maintenance.py)

- **写 kb.db + vault 的几条路共用一把跨进程独占锁**(`maintenance_lock()`,flock 在
  `data/feeds-trim.lock` 上):存量清理脚本、订阅同步的存盘与导出、索引的两条入口
  (`pipeline_stages._run_index`、`cli.cmd_index`)、条目化的就地入库、提取的 tag 回写
  (**四个**调用方共用它这一份:管道 `_extract_pass`、`kb extract --writeback`、
  `scripts/extract_parallel.py`,外加软广补判经 `sync_promo_tag` 进来的)、提取结果的交付
  (`commit_extraction`)。每一条都是「先把一份快照读进内存 → 过一会儿再按它写盘」。
- **口径是「凡是写 kb.db 或 vault 文件的入口」,不是「两条索引入口」。** 条目化那条自称
  「只增不删」,可它末尾照样调 `embed_pending_chunks`,而那个函数补的是**全库**待编码的
  chunk。判据只能是**写哪张表** + **写不写 vault 里的文章文件**。
- **粒度是整趟,不是只圈写的那几句** —— 竞态的那一半在「读」。所以 scan → upsert → remove
  一整段都在锁里。**向量编码也在锁里**:`chunk_vec` 是 sqlite_vec 虚表、**没有外键级联**,
  清理在中间删掉那些 chunk 的话后面几批会按不存在的 chunk id 插回向量,而这些孤儿行**照样
  占着语义检索的候选名额**。代价是一轮大编码会攥着锁几分钟,这是统一互斥的标价。
- **等待有上限,而且等的时候要出声**(`KB_MAINT_LOCK_WAIT`,默认 900 秒;`on_wait` 立刻一次、
  之后每 60 秒一次)。超时抛 `LockBusy`,三处一律「什么都没动」收场。**无限等更坏**:管道是
  单例,一个卡住的持锁者会把面板同步、定时同步和日报一起冻死。
- **等锁期间还认「停止」,而且认它的回调跟出声那个分开**(`on_poll` 每 0.25 秒一次,抛
  `_StopWhileWaiting` 让阶段落 `stopped` 而不是红)。**`flock` 一成功就返回,所以拿到锁之后
  还要再认一次**,但**只在真等过锁时才补这一次** —— 无条件检查会把刚放进 index 的那一轮当场
  顶成 stopped。
- **提取的 tag 回写也在锁里,而且锁内要先确认这一篇还在**(`_still_indexed`,不在就返回
  False、**不抛异常**)。原来它被显式排除在外,依据是外键级联会拒掉 —— **那个依据只看了库、
  没看文件,是错的**:`writeback_tags` 是整份读改写,清理在中间把这一篇删干净的话那次写回
  **把 md 文件重新创建出来**,而且比 index 那条更糟(文件回来之后谁也收不掉)。
- **回写的粒度是「每篇各取一次」,不是整趟提取**,模型请求留在锁外。
- **取锁之前必须先把写事务落盘,而这件事由共用入口自己负责**(`if conn.in_transaction:
  conn.commit()`)。**SQLite 的写锁不认这把维护锁** —— 攥着没提交的写事务去等锁,持锁的清理
  就卡在自己的 DELETE 上,两边互等到 busy 超时把清理打断。**这条原来只写在调用方那一侧,
  于是漏掉了第四个调用方**(软广补判经 `sync_promo_tag`),而那一轮的回归**按名单枚举了三条
  路径**。所以判据是**两层**:共用入口自己 flush + 调用点时序照旧写明白,另有一条回归把
  全仓调用点的名单钉住。
- **补判等不到锁时 `LockBusy` 照常抛出去、整轮中止**(`_flag_in_vault` 只接 `OSError`)。
- **同形状但没闭合的是 `wiki_daily.step_linkify`** —— 分界线:**管道自动跑的写 vault 入口都
  该取这把锁**,手工一次性运维脚本留在外面。
- **不会抱死**:这是唯一一把跨进程维护锁,没有任何路径持着它再去取别的锁;同线程重入直接
  放行(flock 认的是 open file description)。锁的是 `data/` 下**单独的 lockfile**,只创建
  不删除;**文件名沿用 `feeds-trim.lock`**,改名等于在升级窗口里让两个持锁者同时写。
- **`test_feed_translate.ColumnBackfillTests.test_two_first_accesses_both_succeed` 是一条已知
  的负载敏感 flaky,红了先别当回归。** 判据是它自己的报错内容:`duplicate column name` 才是
  它要抓的回归,`database is locked` 是 `reg.db()` 在 `BEGIN IMMEDIATE` 之前那两句 DDL/PRAGMA
  上的争锁窗口(PRAGMA 不走 busy handler)。要修得动核心路径的锁序,单独立项。
- **测试里必须把 `maintenance.DATA_DIR` 指到 tmp**,不指的话会去 flock **生产**那个 lockfile。
- 详细事故形态见 `docs/decisions/maintenance-lock.md`。

## 英文内容翻译(kb/feeds/translate.py)

- **翻译在导出之前,不在阅读时。** vault md 是事实来源,索引/提取/检索/日报读的都是它;留到
  阅读时翻,库里躺的还是英文。所以 `materialize()` 里先 `translate_pending` 再 `export`。
- **「该显示哪一份」只有一份口径**(`translate.display(item)`),导出和来源面板共用。
- **语言判定是数字符,不问模型**(`registry.text_language`,CJK 占字母数 ≥12% 算中文)。
  **一个汉字都没有、又有字母,一律判外文,不设长度下限** —— 「Hello world」被稳定判成中文、
  永远进不了翻译队列,而 social 的短动态整段都是这个形态。产品名保留原文靠的是 prompt 里那条
  **译文规则**,不是把英文冒充成中文。真正判不准的只有「有汉字但太短」那一档(`_SHORT_MIXED`)。
  **判定规则收紧要连存量一起复判**(`_label_unknown` 顺带重判短条目)。
- **改判会清 `exported_hash`**,不然内容变了却和 `content_hash` 相等,下一轮导出直接跳过。
- **重导出要把别人写进那篇 md 的 frontmatter 字段留下来**(`sync._OWNED_META` / `_kept_meta`)
  —— `export` 是整份重写文件,抹掉提取回写的 `tags` 之后,索引按 frontmatter 重建 `doc_tags`
  而 `extracted_at` 还在:那篇文章从此按原话题**再也**搜不到。保留字段拼在 meta **最后**
  (`writeback_tags` 是往末尾追加的)。
- **翻译失败必须合进导出阶段的最终状态**(`export(..., translated=summary)`)—— 导出是这条链
  上最后一个报阶段状态的人。**扣下的条目分两档**:`stuck`(译过、报了错、还扣着)判红,
  `held - stuck` 是正常积压。**试满 `MAX_TRIES` 按原文入库的不算失败**(那是设计好的出口)。
- **这几档要在整个翻译队列里数,不能只数「待导出」的那些** —— 按英文导出过、内容又没变的条目
  压根不在待导出集合里,可它照样排在队列里等补译。四档分开说:`fresh` × `bad`。已经入库那档
  照样判红,只是文案写「库里仍是原文」。
- **导出循环里那个「这一篇是不是译文」的标志不许叫 `translated`**(现在是 `is_zh`)—— 那是
  `export` 的形参名,同名的话收尾那句 `.get('errors')` 当场 `AttributeError`,而那时成功的
  文章已经落盘,异常却把 `materialize` 连同后面的 index/extract 一起带崩。
- **译文那几列是 ALTER 补上去的,补列和它前面那次列集合读取要在同一把写锁里**。
- **取数是「失败过的优先」**(`ORDER BY translate_tries DESC, fetched_at DESC, key`)—— 升序的
  话译不动的永远排在「从没试过」后面,按一轮 40 条算要三周才轮得到一次重试。
- **导出的失败文案和判红只认「本轮」,此前失败单独一档、不判红**(`sync._new_failures`)。
  分档口径是「这一轮真失败的那几个 key **回库里再查一次**」,不能直接用摘要里的 `failed`
  计数(本轮失败之后试满出队的那些已经按原文入库了)。
- **写回译文之前要确认「这一条还是我发请求时那份标题和正文吗」**(`input_version` 是唯一口径,
  `_save`/`_fail` 各带着它走 `WHERE key=? AND title=? AND body=?`)。模型请求要几十秒到几分钟,
  而换正文的写入方有两个 —— 旧回包按 `key` 盖回去之后 `translated_at` 重新设上,这一条**再也
  不进翻译队列**:正文是新的、中文却是旧内容的译文。**失败那一侧同样要比**。统计里
  `summary['stale']` 既不算 done 也不算 failed。**「比标题」那一半今天是冗余带**,如实记着 ——
  留着是因为请求输入是(标题, 正文)两项,而半成品那条路的请求输入**只有**标题。
- 成本闸门:一轮最多 40 条、每条正文 12000 字符、失败退避 3 次。**没配 key 是 `skipped`,
  不计入重试次数。**
- **还排在翻译队列里的条目这一轮不导出**(`queued_sql()` 是唯一口径)。队列两个出口缺一不可:
  试满 `MAX_TRIES` 按原文导出;**翻译整个被跳过时(没配 key)一律不拦**。
- **队列判据是 `translated_at` 空不空,不是「有没有 body_zh」** —— 只有一行标题的短动态译完
  之后 `body_zh` 仍是空串。**改了判据就得把存量里那批老形态迁过来**(`PARTIAL_SQL` /
  `_requeue_partial`,每轮跑一次、幂等):旧判据和新判据**都选不到**它。
- **扣着不导出的条目,来源面板也得标出来**(`items()` 给每行一个 `translating`,口径还是
  `queued_sql()`),**标题和正文位置都写中文状态文案**,原文入口留在右下角那个链接上。这个
  标记必须和导出闸门同真同假,所以还要问一次 `translate.available()`。
- **「在队列里」和「没入库」是两件事,按 `exported_hash` 分两态**(`translating` /
  `retranslating`),后者**如实按原文显示、可以展开**。**还有第三档 `partial`**(正文中文、
  标题还是原文),**排在两档队列状态之前**。**「已译为中文」的判据前后端必须同一个**:正文有
  译文、而且标题(只要原标题不空)也有译文。
- 正文按段落边界切 3000 字符一块,**标题跟第一块一起译**;回包按 `###TITLE###` / `###BODY###`
  解析。**标题和正文都得有译文才算译完** —— 模型漏掉分隔标记时补问一次 `_TITLE_ONLY`,补不到
  就抛错重试。正文为空的条目直接走「只译标题」。
- 详细事故形态见 `docs/decisions/translate.md`。

## 每日日报(kb/daily.py + kb/daily_run.py)

- **窗口是「上一份日报的截止时刻 → 这一次的生成时刻」,不是自然日**(`daily.next_window`)。
  上一份的截止时刻记在**它自己的 frontmatter**(`window_until`)里,链条自描述。**算起点时
  跳过「今天那一份」**(重新生成今天的日报要的是完整一天),所以一天里再生成一次就是**重算
  同一份**。**没有「最长回溯几天」这种上限** —— 上限会把起点往后挪,被挪过去那几天再也进不了
  任何一份日报;素材太多是**限量**问题(条数上限只决定这一份带多少条,没带走的留在账本外
  面等下一份),窗口断链不能补。**截止时刻在同步跑完之后才定下来。**
- **「这一条上报过没有」按账本判(`daily_carried`),不靠时间窗推断。** 没带走的留在队列里,
  下一份接着带 —— 旧实现「时间窗 + 取最新 N 条」会让落选条目在下一个窗口里两个桶都选不到,
  静默永久丢失。三条不许动:盖章排在落盘**之后**;「落盘+记账」与「放回+删文件」整体互斥
  (`daily_carry.commit_lock`),启用时的一次性盖章也在这把锁里,**删一份日报要先盖章再放回**
  (它带走过什么只写在那个文件里,先删就永远读不回来了);盖章两维判 —— **到达分界线**
  (`indexed_at` 晚于它的不盖,否则刚补抓回来的内容被永久写死;**分界线一旦钉下就不许再往前
  挪**)+ **那一份自己的素材清单**(`daily._carried_keys`;按窗口推断会把补录记成存量、把
  当时装不下的记成报过了,两处都不可恢复)。**改了盖章算法要把 `SEED_REV` 加一**(否则已经
  盖错过的库永远走快路径、修不到它),**而那一趟只重算归属**;**清单解析也算盖章算法**,
  改了它同样要加一(回归 `test_changing_the_material_parser_requires_bumping_the_seed_revision`
  按名单钉着)。**清单上露过面的内容一律不许判成「链条之前的历史存量」** —— 解析漏一种
  写法连着四轮各出现一次,这份清单不会收敛,那一道兜底把它的代价从「永久写死」降到
  「多报一遍」;它只用来**否决**终态,不拿来盖归属(同稿副本会往不可恢复的方向错),
  **而且那份原文要独立于切分**(`PastReport.text` 是整份正文)—— 跟着切分走的话起点
  一偏,归属和兜底一起错。**清单起点按抬头自己声明的条数挑**(`## 素材清单(N 条)`,
  切出来得恰好是 N 条):这几个字在文章标题和推文贴的模板里都出现过,按位置找
  (取最前/取最后)必然在某个方向上错,而错一次是**一份日报里好几条一起**写死。
  详见 `docs/decisions/daily.md`。
- **筛的是 `published`(内容的生产时间),不是 `indexed_at`。** 按入库时间出日报读到的是「今天
  抓到了什么」,而人要的是「这段时间世界上发生了什么」。**时间字符串必须解析成时间再比**
  (`daily.parse_ts`)—— 库里实测四种写法,同一天里 `T` 排在空格后面。SQL 只按日期前缀粗筛
  (两头各放一天),精确判断在 Python 里做。
- **发布在窗口之前、这一轮才入库的内容进「补录」**(`Material.late`),单列一节、单独计数、
  prompt 里明确要求不混进要点。没有发布时间的同样走这条路。
- **日报写进 vault(`<vault>/daily/YYYY-MM-DD.md`),不写 kb.db。** `daily/` 不在 `SOURCES`
  里,否则日报会变成明天日报的素材。
- **「同步所有内容」= 依次借用 `kb.pipeline` 跑四个阶段,一个通道一趟。不能并发**(管道是
  单例)。条目化/wiki 不跑。**超预算(默认 3 小时)就请求停下、直接生成。**
- **用户按的「停止」和超预算是两件事,别共用一个标记**(`stop_requested` / `over_budget`)——
  超预算的本意就是「拿现有内容出报」,而用户停止之后还接着生成等于把半成品当成功交出去。
- **进了生成阶段就停不了了,这一档要如实拒掉**(`request_stop()` 返回 `(受理了没有, 说明)`)。
  **这个判断和「进入生成阶段」那一步共用 `_lock`**,所以两者严格二选一。
- **只停自己起的那一轮管道,而且认的是 run id、不是布尔**(`_state['pipeline_run']`)。布尔只
  回答「是不是我起的」,答不了「现在跑的还是不是我那一轮」。`start_pipeline()` 因此返回 run id
  (原子地拿到身份),`request_stop(run_id)` 比一次 id。`_sync_channel` 的轮询同样**只认 run id
  相同的快照**。
- **「拿到结果了」= 亲眼看到自己那一轮 `running` 放下的那份快照,或者按 id 查到它的收尾记录;
  两样都没有就如实记 `unknown`,绝不按成功记账。** 中途快照不算终态。`pipeline_state` 按 run id
  留最近 8 轮收尾快照。
- **勾了「先同步」却一个来源都没选,直接拒掉**(API 400)—— 放过去的话「同步全部并生成」被悄悄
  降级成「直接生成」。
- **有通道没同步成功时,日报照出,但「同步了」这件事不许糊过去**(`_sync_issues()` 给名单,
  三处都读它)。**必须写进文件本身**(`sync_failed` frontmatter + 正文引用块)—— 日报是拿走就
  读的产物。toast 换成 info,绿勾等于把降级说成完整成功。
- **没素材时抛 `LookupError`,绝不写一份空日报** —— 空日报会把这一天「用掉」。
- **定时挂在 `pipeline._scheduler_tick` 那一跳上**,不另起线程;日报在跑时 `tick` 直接返回。
- **「已触发」和「已出报」是两个字段,别拿起跑那一刻去记 `last_fired`。** `tick` 只
  `_mark_attempt`,收尾时由 `_finish_schedule` 决定:临时故障同日补 `MAX_RETRIES` 次,成功/
  没素材/用户停止一律「今天到此为止」。**记的是起跑那一天**(23:50 起的那一轮 00:50 才收尾)。
  `_mark_attempt` 要在 `start()` **之前**调。
- **`_raw_settings()` 不碰采集器注册表**(定时线程每 30 秒读一次)。存下来的通道一个都不认识了
  时回落到默认范围,不是留一份空列表。
- **阶段落 `skipped`/`stopped` 的通道不算同步成功**,**只看这一轮请求了的那几个阶段**
  (`SYNC_STAGES`)。
- **正文里的出处编号(`[18][23]`)在落盘时就写成链接**(`daily._link_refs`),不留到阅读时 ——
  日报是拿走就读的产物。链接文字保留方括号并转义(Obsidian 会把裸 `[[18]]` 当 wikilink),
  **目标一律套尖括号**(`_md_dest`,库里的 url 实测有带圆括号的)。
- **每一处出处编号换不换,由渲染器裁决(`kb/daily_refs.py`),这里不写「哪些片段不许动」的
  规则。** 手写规则的路走过三轮,每一轮都被真实渲染器找出新的漏网写法(行内代码、已有链接的
  文字、跨行代码跨度、引用式链接、多行定义、原始 HTML、缩进代码块、autolink 地址)——
  **这份清单不会收敛**。判据换成:把某一处换成链接,**渲染一遍看结构变没变**,接受的条件是
  「这一处之外一个字都没动」。**比对要连 `<code>` 的内部结构一起比**,并**单数一道链接嵌套**。
  **判据是整份渲染结果逐字比,不是挑几类元素比** —— 要换的地方先换成**哨兵链接**,渲染后把
  哨兵**整组**还原,要求和原文的渲染结果**逐字相同**。**一组里没有素材的那几个编号也要打哨兵。**
  **两边都要过一次解析器再比**(`_norm`)。**哨兵链接不许落在 `<a>`/`<code>`/`<pre>` 里面。**
- **裁决器的方言必须和页面那个渲染器一模一样**(`markdown-it-py` 的 `gfm-like`,`html=True`)。
  **但「换个预设名」不等于对齐**:linkify-it 只认一份有限的 TLD 名单而 marked 对 TLD 不做限制。
  所以①把 TLD 放宽成任意;②真正兜底的是 **`_autolinked`** —— marked 的 autolink 起手式只有
  `www.` 和 `scheme://`、尾巴是 `[^\s<]*`,于是「这个编号落在某条裸网址里」**必然**满足「往左
  数到最近的空白或 `<` 为止出现过 `www.` 或 `://`」。它是超集,多拒几处换「再不会有下一个漏网
  的后缀」。**那条起手式正则必须大小写不敏感**(`re.I`),而且**① 会顺手遮住 ② 的缺口**,所以
  回归里另有一条直测 `_autolinked`。**代价是过度保护,已知且接受。**
- **先整篇一起试一次**(多半没有一处落在代码或已有链接里),过不去才逐个各试各的。依赖是
  `markdown-it-py[linkify]`(那个 extra 不能省)+ bs4,**得是显式依赖**。
- **加链接之前生成的那些日报由面板兜底**(`daily.js` 的 `enhanceDailyBody`),**读的是渲染之后
  那个 `<ol>`,不是 markdown 原文**(`dailyRefMap`)—— 裸写的 url 自己可以带平衡圆括号。
  **认的编号写法要和落盘那条路一致。** 粗筛文本节点别用带 `g` 的正则。
- **前端那条横幅有两个来源,清空时必须认一下是谁写的**(`dailyErrorKind`)。不在日报页时也慢速
  轮询状态(60/120 秒)。
- 详细事故形态见 `docs/decisions/daily.md`。

## 大模型阶段配置(kb/llm_stages.py)

- **每一处会调大模型的活都是一个「阶段」**,注册表在 `STAGES`。面板在顶栏「模型」,配置落
  `data/secrets.json` 的 `stages` 段。**别再在各文件里写死模型字面量**(原来五处各写一份),
  调用方一律 `stage_model("<key>")` 取。
- **provider 是显式存下来的,不是从模型名猜的。** 前缀规则只是没有别的信息时的兜底:同一个 id
  在两家中转站都可能存在,猜错就是拿一家的 key 去打另一家的站。**模型标识一出 Python 就得带家**
  (`llm.qualify`),界面上要显示时过 `modelLabel()` 剥掉前缀。
- **下拉的候选 = 勾选的模型 + 各阶段当前指向的模型**(`llm.model_pool()`)—— 只要没有一个 option
  匹配当前默认,浏览器就自动选中第一个候选,而 chat.js 每次发送都带上下拉的值。
- **逐条试候选只能用 `llm.parse_model`,不能用 `llm.resolve`** —— 后者认不出家时会去问默认模型,
  而默认模型自己也在走这段解析,旧配置里留一个谁也不认识的 id 就是无限递归。
- 没配的阶段按 `Stage.default` 走,**那些 default 就是这套设置出现之前各文件里写死的那个 id**。
- **阶段配置读时不缓存**(面板改完不用重启);批量脚本例外(整批开始时定一次)。
- **改 `secrets.json` 一律走 `llm.update_settings(mutate)`,别在外面 read → 改 → write。**
  **临界区里是两把锁**:进程内 RLock 挡同一个 kb-web 里的并发,`secrets.json.lock` 上的 flock 挡
  跨进程的。flock 锁的是**独立的 lockfile,不是 `secrets.json` 自己**(写盘是 rename 换文件,
  锁着的 inode 会被换走),lockfile 只创建不删除。写盘是「唯一名字的临时文件 + rename」——
  截断重写会让并发读者读到半截 JSON,而坏 JSON 被吞成 `{}`,接着一存盘就把整份 key 抹了。
- **面板上每一步都写着「推荐哪个模型」,但注册表里存的是档位不是 id**(`Stage.rec` =
  `cheap`/`main`,`recommended()` 按关键词在**当前实际可选的模型**里挑)。写死 id 十有八九不在
  候选里。挑的时候**已配 key 的那几家优先**,一个都不沾时返回空串、退回档位说法。口径:量大的
  解析活推便宜档,写给人读或决定产物质量的那几步推主力档。
- 收藏页那个模型下拉和面板里的「收藏 · 分诊与行动卡」**是同一处配置**(`llm.set_fav_model` 直接
  写 stages)。旧的顶层键还读得到,一旦在面板里改过就会被清掉。
- 详细事故形态见 `docs/decisions/llm-stages.md`。

## 提取阶段的两道判定(kb/extract.py 的 promo 与 body_gap)

- **软广判定搭在提取阶段,不单开一个 LLM 阶段** —— 正文已经在 prompt 里了,多要一个字段只多几十
  个输出 token。喂进去的是**头 4800 + 尾 1200**(转化钩子基本都在文末)。
- **三档 none/soft/hard,只有 hard 被当成广告处理。** 判定认不出时一律落 none —— 错杀会让一篇
  正常文章连同它的实体从条目化和检索里一起消失。**hard 的两条边界是拿真实样本调出来的,别删**:
  ①只看正文主体,不看文末的固定推广位;②**推广对象是开源项目时一律不判 hard**,最多 soft。
  **口径只有一份**(`_PROMO_RULES`),完整 prompt 和存量补判的轻量 prompt 共用。
- **消费方一律读 `documents.promo_level`,不要各自再判一遍**:条目化 gate 读到 hard 直接否掉
  (省的是 gate + transcribe 两次调用),检索和枚举默认挡掉 hard。**软广子句不进 `_filter_sql`**
  —— 后者的返回值还被 `_vec_leg` 用来判断「召回有没有被缩到小子集」。transcribe 那侧**按当前判定
  再拦一道**(存量补判之前 gate 已经放行过一批)。
- **hard 原文的衍生条目按 `url` 一起挡掉**,口径只有 `store.promo_exclude_sql()` 一份。里面那句
  `pm.url <> ''` 不能省(库里几十篇没有 url 的文档会互相匹配上、集体消失)。
- **`flag/软广` 是双写的:DB 的 `doc_tags` + md frontmatter,两边都得跟着判定走。** `doc_tags`
  每次 upsert 都按 frontmatter 重建,只写库的标记下次重索引就没了;反过来 hard 改判成 none/soft
  时 md 里那行也必须摘掉(`remove_writeback_tag`)。**三条提取路径的 tag 回写只能有一份实现**
  (`extract.writeback_extraction`)。`save_promo` 返回「上次是 hard、这次不是」的信号,提取流水线
  和 backfill 都据此调 `sync_promo_tag`;回写改了文件就同步 `refresh_content_hash`。`PROMO_TAG` 在 slug **之后**追加(它已经是最终形态)。
- 存量补判走 `scripts/promo_backfill.py`,按「标题 + 正文长度桶」**同稿归组**(8857 篇里 2357 篇
  是同一份稿子的副本),每组只判一篇、结论广播给同组。归组键带长度桶是因为光看标题会把撞名的
  不同文章判成同一篇。
- **soft 是「留着 + 降权 + 打标」,不是第二个 hard。** 检索里乘 `search.SOFT_PROMO_WEIGHT`(0.85);
  RRF 分数全挤在窄带里,乘系数约等于**平移名次**(往后约 11 名)。要调松紧改这一个数,别动 `RRF_K`。
  **降权必须在截断 top_k 之前做** —— 排完再降只是在这 k 篇里换位置,被压下去的仍占着名额。
  `Hit.score` 报降权后的分。
- **「有效档位」只有一份口径**(`store.promo_level_sql` / `effective_promo_levels`):自己判过用
  自己的,没判过(条目化衍生条目永远是空的)按 `url` 从原文继承,同 url 取最重的一档。
- **打标要覆盖四个展示面**(搜索结果、问答引用、图鉴卡片与时间线),统一走 `core.js` 的
  `promoBadge()`。**图鉴是唯一不走 search/aggregate 的面**(按 vault 文件聚合),所以
  `projects._apply_promo` 补这一道,**过滤放在分组之前**;查库失败就降级成「全部保留、不打标」。
- **图鉴的缓存签名必须带上软广状态**(`projects._promo_sig`)—— 补判改的是**原文**那一行,衍生
  条目文件一个字节没动。签名取的是**逐篇 (path, promo_level, url) 的摘要,不能是聚合量**(分档
  计数 + MAX(promo_at) 在「两篇同一秒对调」时逐字相同)。取签名和重建共用一条连接。
- **`store.doc_body()` 返回空串有两种原因,下一步完全相反**(`body_gap`):`empty` = md 正文自己
  就是空的,重抓有机会补上,进「需回填」清单;`no_text` = 正文**有内容,只是一个字都没有**(整篇
  一张长海报图、纯表格),**回填救不了它**。不分档的话它会被当成「正文为空,需回填」**永动地**
  每轮重报、把 extract 标红、占着待处理计数,而「重试失败项」每次都去重抓那张图。
- **判据是「md 正文照 chunker 自己那套规则剥完还剩不剩字」**(走 `parse_frontmatter` +
  `normalize_body` + chunker 的 `strip_markup`),不是「md 正文是不是空的」,更不是「有没有图片」。
  **「空没空」要在剥掉阅读装饰之后问** —— `wikilib.linkify_doc` 对空壳照样追加图谱尾区,于是
  「什么都没有」被那一行顶成非空、落进终态。**第三种形态:剥完还剩字、却一个 chunk 都没有 ——
  这一档不许落终态**(小节标题那行字看得见,只是整节清理完是空的),一律按最保守的 `empty` 办。
  **读不到文件**同理落 `empty`。
- **终态存的是当时那份 `content_hash`,不是一个布尔**(`documents.no_body_hash`)—— 布尔是永久
  封死。按 hash 走则是 index 一写新 hash 就自动回队列。`no_body_hash <> ''` 那半不能省。
  写回按元规则带条件(`WHERE path=? AND content_hash=?`),**正文和它的版本号要在同一个快照里
  读出来**(`store.doc_body_version`,一条语句)。
- **`pending_docs` 是唯一口径**,所以一处改动同时治提取队列和面板那个「当前待处理 N 项」。
  **新那档的文案里不许出现「正文空」三个字** —— `retry_failed_plan` 就是按这三个字决定要不要跑
  回填脚本的。**它不判红、不进 failures 明细**,落终态时要把这一篇的旧失败计数一起清掉。
- **新增那一列在老库上是 ALTER 补的,所以「读列集合」和 ALTER 要在同一把写锁里**(`_add_columns`:
  缺列才 `BEGIN IMMEDIATE`,**并在锁里重新读一次**列集合)。**常态不取锁**(列都在时只花一句
  PRAGMA)。
- **面板上的「当前待处理 N 项」不能只在 running→idle 那个跳变上刷新**(`pipe.js` 另按「出现了一轮
  新的已结束运行」判)—— 这一档一次模型都不调、整轮几百毫秒,页面**一次都没见过 `running=true`**。
  这条路**不弹「已结束」那句 toast**。**而那次刷新不许被在途的积压查询吞掉**(`pipePendingWanted`:
  在途时记一笔欠账,等它收尾了补发,**真的应用上去才算刷新完**)。**回包「是哪一轮的」认 `run_id`,
  不拿 `started_at` 当身份**(它只精确到秒,而这一档治的正是几百毫秒就结束的运行)。
- **三条提取路径共用同一份判定。** `extract_parallel.py` 的预筛循环按「试过几次」提交、不按「写
  进去几条」—— 条件 UPDATE 返回 0 行时也开着事务,而它后面的回写要先取维护锁。
- **想真正读到这类文章的内容只能 OCR / 多模态读图**,那是按篇计费的另一笔开销,没做。
- 详细事故形态见 `docs/decisions/promo.md` 和 `docs/decisions/body-gap.md`。

## wiki 层与分类图谱(scripts/wiki/)

- **主题页文件名必须过 `sanitize_filename`,双链用同一个名字**(命名统一在 `Universe.topic_file`,
  取链接走 `topic_link()`)。模型偶尔给候选表外的词,带斜杠的那种拼成路径之后目录不存在 →
  `FileNotFoundError` 把整个 wiki 阶段带崩。只清理文件名那一侧的话,页面建出来了、链接却指向不
  存在的页。重名决胜按主题名排序,**不能按篇数**(篇数每轮都在变,一变就换文件名、留下孤儿页)。
- **入库侧收紧只针对主题(`extract._topic_slug`),实体侧不能跟着改** —— `shadcn/ui` 这类仓库名
  里的斜杠是正当的。
- **一共三张图谱,回答的不是同一个问题。** `graph.html` 是「最近两周公众号在聊什么」(实体共现、
  滚动 14 天);`graph-projects.html` / `graph-skills.html` 是「我长期盯的这两类东西现在都有哪些、
  各归在什么用途下」(全量、固定分类表分区、不设时间窗)。`/graph/{name}` 的文件名走**白名单**。
- **分组口径直接复用 `kb.projects`(图鉴那一套),不自己再扫一遍条目树。**
- **类目是写死的固定表(`taxo_tables.py`),模型只负责选 code** —— 让模型自由起标签的话,同一个
  项目今天叫「命令行工具」明天叫「CLI」,分区每轮换一副样子。**判定结果按 norm 长期缓存**在
  `data/taxonomy_{key}.json`,带 `Taxonomy.sig()`:**改分类表就自动全量重判**,而**指纹必须覆盖
  所有会进 prompt 的字段**(`hint`、`extra_rules`、`fallback`、`item_zh`),不只是 code。
- **「只看有仓库」按解析出的主机名判,不能退化成「有任意 http 网址」** —— 口径是
  `kb.repo_url.repo_host()` 的白名单,精确比对或后缀匹配,**不用字符串包含**。图鉴聚合
  `project_url` 时挑哪一条也得用它(`projects._pick_url`),所以这份口径单独放在 `kb/repo_url.py`。
- **判不出来的一律落 fallback(「其他 · 待归类」),不许瞎猜一个类目。**
- **归类整体失败时既不写缓存、也不覆盖旧图。** `classify` 不落盘(写一份空 items 上去等于把几千
  条结论抹掉);`build_one` 看到「有条目但一个都没归类、而且旧图已存在」直接 SystemExit。
- **「指纹变了的全量重判」和「同指纹下的增量新判」是两条路,别混成一条** —— 混着走时
  `--max-new` 会把 3183 条完整旧判定**就地盖掉**成 400 条。重判那一趟①**不受 `--max-new` 限制**;
  ②有批次失败就保住旧缓存、把进度留在暂存里,`Verdicts(complete=False)`。判出来 0 条同样按未完成。
- **「判完了」= 每一条都拿到了有效 code,不是「没有批次抛异常」** —— 模型返回语法合法的 JSON 却
  只含一截时不抛异常。所以 promote 的判据是 `left == 0`;漏项会**只拿缺的那些再问几轮**
  (`_RESIGN_RETRIES`),那是为了别因为偶尔漏一条就整轮白等,**不是放宽判据**。
- **判定一律先落暂存 `data/taxonomy_{key}.staging.json`,不直接写正式缓存**(增量那侧同样如此)。
  **正式缓存在「新图验收通过 + 页面落盘」之后才提交**(`Verdicts.commit()`)—— 两者不在一个事务
  里的话,上面那些拦截只保住页面、保不住缓存(fallback 本身是表内合法 code)。顺序也不能反。
- **重判结果「等于没判」按不作数处理**,连暂存一起扔掉。判据是「落在 `void_codes | {fallback}`
  里的占到 `VOID_SHARE`(90%)」,**不是只有 100% 才算坏**;少于 `VOID_MIN_N`(20)条时只认 100%。
  **这条判据的分母是「这一趟新拿到的判定」,不是整份缓存**(`fresh = codes - base`)—— 拿整份当
  分母的话缓存越大越掩盖问题。故意收紧规则时用 `--allow-shrink` 放行。
- **`res.complete=False` 无条件拦死,跟有没有旧页面无关。** **页面落盘走 `_atomic_write`**
  (同目录唯一临时文件 + rename)。
- **「验收基线 → 落页面 → 提交缓存」整段要拿跨进程锁圈起来**(`taxonomy.publish_lock`)—— 页面和
  缓存是两次独立 rename,各自原子管不住两个 writer 交错。**验收读旧图基线必须在锁内。**
  `_io_lock` 因此得是 `RLock`。**缓存提交失败要把页面回滚回去**(`_publish`):发布锁只挡交错,
  给不了跨文件的失败原子性。回滚本身再失败时打一行 stderr 说清「页面是新的、缓存是旧的」。
- **暂存里的东西都是「没验过的半成品」,续用前要按原批次各验各的**(`load_runs`,暂存里的 `runs`
  记着哪几条是同一趟判出来的)—— 质量判定跑在 `finish()` 里,而判定结果在那之前就落盘了。
  `finish()` 的分母因此是 `codes - base - resumed`。老格式暂存整份当一批验。
- **暂存里凡是正式缓存已经有的 key,一律以正式缓存为准**(`clash`),「有没有新东西可提交」改成
  比内容(`codes == official`)。
- **`save_cache` 成功就等于缓存提交完成,后面清暂存失败只是收尾告警**(`_promote`)—— 共用一个
  异常边界的话 `_publish` 会把它理解成「缓存没提交」、把页面回滚成旧的。**清暂存的每一处都得是
  这个边界**(统一走 `_cleanup` / `_rewrite_stage`,不许裸调 `unlink`)。
- **发布前要比一次「基线有没有被别人换过」**(`publish_sig`,读候选之前记一份,进锁后再取一次比)
  —— 读基线 → 判定 → 建图那一大段跑在**锁外**。不一致就**这一轮不发**(打一行说明、退出码 0),
  判出来的东西并回暂存。**基线是发布序号 + 两个文件的内容(正式缓存 + 正式页面字节)**:
  **只比内容会被 ABA 骗过去**,只比序号又挡不住绕过发布流程的改动;页面那半按**字节**算。
  **要在 `load_groups()` 之前取。**
- **发布序号在动任何正式产物之前推进,推进不了就整轮放弃** —— 三次独立 rename 凑不成事务,而
  **后推进**会留下「产物已经换了、序号没动」的窗口(进程死在页面换好之后就够)。危险的假阴性换
  安全的假阳性。**序号写盘要 fsync(文件 + 父目录)**;值 = 自增计数 + 随机后缀。
- **基线「读不出来」一律 fail-closed,不许退化成「还没有」**(`read_gen` 只把 `FileNotFoundError`
  当空串,别的 `OSError` 和坏内容一律抛 `GenUnreadable`)。
- **共享暂存只有一个写入口 `_edit_stage`:读-改-写,整段在 `stage_lock` 里,而且各人只动自己那一
  批**(`runs` 是 `{批次id: [norm]}`)。「提交完清暂存」= 把**自己刚提交的那些 key** 摘掉,
  **不是 unlink**;摘完真的一条不剩了才删文件。**取锁顺序永远是 publish → stage。**
- **CAS 拒绝发布时,要在锁里把自己判的并回暂存**(`_restash` / `Verdicts.keep()`)。
- **提示词/调模型/解析回包在 `taxo_llm.py`**、归类结果对象在 `taxo_verdicts.py`(都因为 700 行
  上限拆的),`taxonomy` 把名字原样绑回自己的模块全局,所以 `T._llm = fake` 这类打桩照旧有效。
  **`CACHE_DIR` 那种被测试改写的模块变量不能这么搬**(搬走之后只改到一份副本)。
- **页面验收看的是「这张图实际长什么样」,不是某个 code 的名字**(`quality_problem`)。有旧图在时
  三种情况一律拒绝写页面并 `res.discard()`:0 节点、一个都没归类、**新图不到旧图的 `MIN_KEEP`
  (50%)**,而且**节点数和已归类数两个维度都要比**(fallback 节点不会被 drop 掉)。
- **「这一轮一个候选都没扫到」是故障信号,不是「没什么可验收的」** —— 原子写只保证不出半截文件,
  挡不住这种「完整但错」的覆盖。
- **skill 图谱的成员资格也由归类这一步决定**(分类表里有个 `X` 档收「本体是软件、只是带 skill
  能力」的,判成 X 的不进图谱)。
- **共现边 = 同一篇原文里被一起拆出来的两个条目**,权重是一起出现过几篇;一篇拆出 40 个以上条目
  的月度盘点跳过;每个节点最多留 10 条最强的边。**文章不内嵌进节点,只存文章表下标。**
- **点哪一份副本,就以哪一份为准**(`select(i, from)` 把来源元素记进 `anchor`)。**滚动还得等面板
  挤压布局落定再做**(延后 360ms)—— 面板一开主栏变窄、pill 整体重排。
- **筛选改变可见集时,选中态得跟着走**(被筛掉了要整个 `reset()`);切排序后 `resort()` 末尾必须
  重画连线。**反过来,选中一个被筛掉的节点之前要先把挡它的筛选放开**(`relax()`,收在 `select()`
  里)—— 面板那排「常一起出现」跟筛选没有半点关系。**只放开挡路的那几项,而且控件跟着回位**;
  **「挡没挡住」要逐项各判各的**(搜索那一项尤其容易写成「`F.q` 非空就清掉」,而组合筛选下目标
  常常仍然命中搜索词)。命中判定 `hitsQ` 只有一份。
- **搜索框回车要先把这次输入同步落定再找**(`F.q` 是防抖 120ms 之后才更新的),还要**排掉输入法
  组合态**(`e.isComposing || e.keyCode === 229`)—— 中日韩输入法在组合阶段就用 Enter 确认候选。
- **「近 30/90 天」的截止日全程按 UTC 算**(`cutoff`)—— `meta.built` 是后端 `date.today()` 落下来
  的裸日历日,前端按本地时区解析再 `toISOString()` 会让窗口凭空多含一天。
- 页面样式和脚本放 `scripts/wiki/assets/catgraph.{css,js}` 的真文件里,构建时读进来内联。
- **三张图谱的子进程挂了必须让 `wiki_daily.py` 非零退出**(`run_step`)—— 光打印一行 FAIL 的话,
  上面那一整套拦截**每一次生效都显示成 wiki: ok**。一张挂了另一张照样生成。
  **标红那条详情里,标签、根因、完整 stderr 三样都得留住**(`_fail_wiki`):标签单独拼在前面、
  **只截错误正文**;摘要取 **stderr 里最后一条真正的错误行**(多行 traceback 的根因在那儿);
  完整 stderr 写进管道日志、也塞进 `_stage(..., failures=[...])`。
- 详细事故形态见 `docs/decisions/catgraph.md`。

## 服务与运行时

- **管道跑在 kb-web 进程内**,杀 8787 前先 `curl -s http://127.0.0.1:8787/api/pipeline/status`
  确认 `"running": false`。server.py / projects.py 改动要重启才生效;`kb/static/` 下静态文件每次
  请求从磁盘读,免重启。重启用 `env -i` 干净环境(继承嵌套 claude 的环境变量会导致 Not logged in)。
  **不要用 preview_start 托管 kb-web**(会接管生命周期,杀掉管道)。
- **「改了不生效」只是这件事良性的那一半:坏的那一半是同一个进程里新旧两个版本的模块混跑。**
  kb 有一批模块是**延迟 import** 的(`kb.feeds.sync` 要等管道真跑一轮 crawl 才进来),而 Python
  不热加载 —— 服务跑着的时候一次 `git checkout` 就够:先被拉进 `sys.modules` 的那个是旧版,后来才 import 的从磁盘读到新版,于是新版调不到
  旧版里还没有的函数。**报出来的话指不到任何跟业务有关的东西**(实测七条 RSS 一起报
  `x_channels has no attribute 'fill_bridge'`、面板显示「连接失败」,人只会去查源站),
  **定位的铁证在 `__pycache__` 各文件的时间戳上**。**出路只有重启**(进程边界,同 bge-m3 那条);
  **重启之后退避还挂着**,得手动点一次同步(或 `POST /api/intake/{kind}/sync` 带 `source_ids`,
  它自带 `force`)。
- **所以服务已经起着时 `./kb.sh` 会提示一句「代码比跑着的进程新」**(`code_stale`;判据是 pidfile
  比 `$ROOT/kb` 下任何一个 `.py` 旧 —— `daemonize` 写 pidfile 那一刻就是进程启动那一刻,别处只
  删它、不写它)。**挂在两处**:`web_start` 的「已在」分支(覆盖 `sync`/`collect`/`export`/
  `index`/`extract` 全部命令,也就是这个 bug 真正发作的那条路)和 `web_status`(让
  `./kb.sh status` 那一屏看得见)。**只看 `.py`** —— `kb/static/` 下的 js/css 每次请求从磁盘读,
  算进来就是每改一行样式催一次重启;`scripts/` 和 `collector/` 每次都是新起的子进程,同理不算。
  口径同 `graph_stale`:**提示而不代劳,也不拦**(重启会把正在跑的同步断在半路)。
  回归在 `tests/test_kb_code_stale.py`。**「没有 pidfile 就安静」那道守卫是冗余带,别删** ——
  `find -newer <不存在的文件>` 在系统 find 和 bfs 上都是报错 + 输出空,后面那句判断自己也会安静;
  留它的理由是构造上的:「服务没跑」该由我们自己回答,不该押在某个 find 实现对缺失参照物的处理上。
- **地址栏的 `#tab` 被读走之前,谁都不许写它**(`workspace.js` 的 `workspaceLocated`)——
  `restoreWorkspaceLocation()` 在 `initWorkspace` 收尾时才读 hash,而 `loadGallery()` 是和它并排
  起跑的,图鉴接口先回来时 `applyGalleryFilter()` 会把 `#daily` 覆盖成 `#gallery`。
- **向量模型跑在子进程里,不在 kb-web 进程里**(`embed_worker.py` 是本体,`embed_pool.py` 管起停,
  `embedder.py` 是门面)。理由只有一个:内存。bge-m3 一碰 MPS 就占 ~3.7 GB,而且批量编码时每出现
  一种新的 batch 形状,MPS 的显存池就再涨一截、**永不回落**。**这笔内存在进程内根本收不回**
  (`empty_cache()`、`del model` + gc、改 fp16 全试过),**进程边界是唯一可靠的回收手段**。
- 生命周期:懒起、空闲 `KB_EMBED_IDLE_SECONDS`(默认 600)秒自杀、批量任务结束时显式收掉。子进程
  stdin 读到 EOF 就退出,所以父进程怎么死都不会留孤儿;它自己 `start_new_session=True`。
- **批量写向量一律走 `embedder.embed_pending_chunks(conn, progress)`**,别再抄那个循环(原来三处
  各有一份)。`KB_EMBED_BATCH_BUDGET` 决定**峰值**内存,取 8×768² 是拐点;**改这个值不需要
  reembed**(新旧预算出来的向量余弦相似度 min 0.99999994)。
- 协议是「4 字节小端长度 + 载荷」的帧。**向量帧的长度前缀必须被吃掉** —— 按 `n*dim*4` 裸读会让
  整批向量平移 4 字节(余弦相似度掉到 0),剩的 4 字节还会把下一个请求的帧同步带歪,而错位之后的
  read 会**永久阻塞**、且是攥着锁阻塞的。所以父进程侧的读带帧长上限和超时,把「挂住」变成「报错
  重试」。
- 已知遗留:`kb-mcp` 有好几个进程,各自会起自己的 worker。空闲超时能兜住,但同时活跃时仍是几倍
  开销。真要治得让它们共用一个常驻 worker(要 socket + 守护进程),现在没做。
- 详细事故形态见 `docs/decisions/embedding.md`、`docs/decisions/code-stale.md`。

## 浏览器探针起的无头壳(tests/probe_shell.mjs + headless.py 的 run_probe)

- **探针起的 chrome-headless-shell,在任何一条退出路径上都必须被收掉。** 收尾那句 `proc.kill()`
  写在正常路径末尾时,中间任何一步抛异常都让 node 以 unhandled rejection 退出,那一句**永远到不
  了**,而 node 死掉不会带走 spawn 出来的子进程(实测攒出 16 个 ppid=1 的孤儿、66 个进程 3.8 GB)。
- **三条泄漏路径、三层防线,谁也兜不住另外两档**:①**探针自己抛异常** → `probe_shell.mjs` 的
  `process.on('exit')` 里**同步** SIGKILL(覆盖正常退出/抛异常/`process.exit()`);②**Python 侧
  timeout** → `run_probe()` 按**进程组**收(`subprocess.run(timeout=)` 调的是 SIGKILL,node 连
  'exit' 都来不及触发,①在这一档是瞎的);③**调用方被取消** → 由**子进程自己**盯 `process.ppid`
  (这一档是②的独立会话**自己开出来的** —— node 不在调用方进程组里了,而 Python 对没有 handler
  的 SIGTERM 直接结束,`finally` 一行不跑)。
- **③的判据是 `process.ppid`,而且必须待在子进程里** —— 它是实时 getter,调用方一死内核立刻把我们
  过继给 init,这个值就变了,是**唯一不依赖调用方跑任何代码**的信号。轮询间隔 400ms。
- **「原调用方是谁」只能由调用方自己说(`KB_PROBE_PARENT`),不能让探针现读 `process.ppid`** ——
  探针在起浏览器**之前**本来就有一段异步,调用方死在那一段里时读到的 ppid 已经是 1,而「1」和
  「我本来就是从终端直接起的」长得**一模一样**。这个 env 只有一个写入方(`run_probe` 无条件覆盖,
  `probe_env()` 把继承来的旧值摘掉)。
- **口径两半:检查在模块一加载就装上,外加起任何长寿东西之前同步再问一次**(`bailIfOrphan()`)。
  装在 spawn 之后的话那段异步整个在射程之外;而**node 是单线程的**,探针在起浏览器之前做一件
  **同步**的事时那个 400ms 轮询一次都跑不起来。**两道里只有顶层安装有独占射程**,那道闸实测
  **红 0 条** —— 留它是构造上的理由,如实记着。
- **调用方一死,探针往 stdout/stderr 写一个字节就会被 EPIPE 掐死 —— 这是一条巧合的自愈路径,
  不可依赖**(探针在等后端的几十秒里一个字节都不写),而且它会把这几条用例的射程遮掉:放行屏障
  之后**不能立刻**去查「起过浏览器没有」,要先 `_wait_gone(node)`。
- **③那个 interval 必须 `unref()`**(否则每个探针跑完都要挂到它被清掉为止)。顺带的后果,知情:
  把探针脱离终端跑会让它自己结束 —— 那正是这条防线要的行为。
- **信号要单独挂一遍**(node 对没有 handler 的 SIGINT/SIGTERM 直接终止、**不触发 'exit'**)。
  **杀用 SIGKILL,理由是构造上的,不是「SIGTERM 不够」**(后者实测是够的)—— 这是最后一道防线,
  而 SIGTERM 要对端配合,'exit' 回调只跑得了同步代码、**没法等**。
- **测「挂住」的用例必须拿一个 ref 的 handle 挂住,`await new Promise(() => {})` 挂不住** ——
  它不是 event loop handle,循环一空 node 就以 exit 13 正常退出,于是**本该验第二层的用例测的是
  第一层**。
- **`launchShell` 等端口那个 `setTimeout` 非清不可**(它是 ref 的 handle,拿到端口之后还挂着,
  于是探针跑完 node 也不退出、要等它到期,每个探针白挂满 20 秒)。**拿到端口后要 `unref()`**
  子进程 handle 和它的 stdio 管道。
- **`run_probe` 要 `start_new_session=True`**(让 node 成为新会话首进程,chrome 自动在同一个组里,
  `killpg` 一起带走;新开会话同时保证这个组里只有 node 和它的后代)。收尾在 `finally` 里**无条件**
  做一次。
- **把探针改到共用这个模块之后,必须真跑那几组页面回归 —— `node --check` 只过语法。** 迁移时删掉
  各探针顶上那句 `const BIN = ...` 而把紧跟着的 `if (!BIN)` 留在原处,逐文件编译全过,真跑起来是
  顶层 `ReferenceError`、四组页面检查一条都起不来。
- **两条契约不挂 `@requires_headless`**(没装无头壳的机器上靠它们守结构):探针不许再自己 `spawn`
  浏览器、测试不许再裸 `subprocess.run` 跑探针。**`test_probe_leak.py` 自己是后一条唯一的例外,
  而且必须是** —— 走 `run_probe` 的话它那道 killpg 会把结论盖掉。
- **同形状但没动的是沙箱服务那一半**:几个 `setUpClass` 自己 `Popen` 一个预览服务,而
  `run_probe` 本身抛出来那一档没人管(setUpClass 抛异常之后 unittest **不调** `tearDownClass`)。
  要修是起完服务**立刻** `cls.addClassCleanup(...)`。
- **探针里「等页面就绪」要等那个元素自己,不是等定义它的 JS 全局** —— `typeof subsCatalog !==
  'undefined'` 回答的根本不是「来源目录取回来了吗」(模块级变量初值就是空数组)。
- **「有没有漏」按工作目录归属数,不按进程名**(`lsof -p <pid> -a -d cwd` 落在本 worktree 下且
  `ppid=1`)—— 本机常常同时开着好几条 worktree 在跑各自的探针。别去动不归自己的那些。
- 详细事故形态见 `docs/decisions/probe-shell.md`。

## 视觉语言(硬性)

- **一套色板:瓷白 `#F3F5F4` × 墨蓝黑 `#14202B` × 深松绿 `#17604F`。** 唯一源头是
  `kb/static/css/base.css` 的 `:root`。**别再换重点色** —— 靛蓝(`#3F43B9` 一带)是 AI 产品的默认色,而「开源项目」
  占了图鉴 5530 条里的 2976 条,类型色一旦跟重点色撞上,卡片上那枚类型标记在绝大多数卡片上就什么
  也没区分。前因后果见 [全系统视觉统一](design/visual-system-unification.md)。
- **色值一律走 token,不许就地写十六进制。** 灰阶只有三档(`--ink` / `--ink-2` / `--muted`)、线
  两档(`--line` / `--line-soft`)、红绿褐各一个(`--danger` / `--ok` / `--warn`)。一度是 8 个
  CSS 文件里 61 个各写各的色值,整体发灰发浑就是这么来的。
- **有三处独立产物抄同一份值,改 `:root` 必须同步改它们**:阅读页 `kb/web/pages.py`、分类图谱
  `scripts/wiki/assets/catgraph.css`、话题图谱 `scripts/wiki/build_graph_html.py`(都要能脱离
  kb-web 直接用浏览器打开,所以 CSS 是内联的、拿不到 token)。三处都写了互指注释。不同步的后果是
  用户点一条搜索结果去读原文就换一套配色。
- **字体三件套各归其位**:宋体 900(`--font-display`)只给页面大标题、详情抽屉工具名、图谱与阅读
  页标题 —— 这是整套界面**唯一的字形签名**;等宽体给工具名/日期/计数/norm(标识符不是散文);
  黑体给正文。换 `--font-display` 时注意各处的 `font-weight: 900`(一度换成了没有 900 字重的
  字体,那些声明当场变成死代码)。
- **这是自己每天开的工具,不是落地页。** 不要给面板加英文 eyebrow、副标题、居中大图标 + 标语那类
  空态。页头就一行:标题 + 同一行上的事实数字。
- **改下拉框样式只许动 `background-color`** —— `background` 简写会连 base.css 里那个自绘箭头
  (`background-image`)一起清掉,下拉框退化成一段看不出可点的纯文字。`#ask-model`、
  `.gallery-view-controls select`、`.select-field select` 都踩过。
- **箭头一律走 `icons.js` 的 `arrow(kind)`,页面里不许再出现 ↗ ↑ ↓ → ▾ ▴ 字符。** 这些字在
  PingFang SC 里本身就细长歪斜,缺字还会回退到系统 emoji 字体。几何只有一份(`ARROW_PATHS`,
  viewBox 12、stroke 1.4、尺寸 1em)。下拉框那个自绘箭头用的也是里面的
  `caretDown`。静态标记写 `<i data-arrow="ext">`(`renderIcons()` 会替换),JS 里拼
  `arrow('ext')`;宿主元素得是 `innerHTML` 不能是 `textContent`。**三处独立产物各抄一份同样的
  几何**(阅读页 `pages.py._EXT_ARROW`、分类图谱 `catgraph.js` 的 `IA`、话题图谱
  `build_graph_html.py` 的 `openwiki`),理由同配色。
- **`icons.js` 的齿轮路径是按极坐标算出来的(8 齿等分,齿顶 R=9.3 / 齿根 r=7),别手改数字** ——
  齿轮是旋转对称形状,少一齿或齿距不匀一眼就看得出来。
- **图谱页是把 CSS 内联进单文件 HTML 的产物**:改 `catgraph.css` 只影响之后构建的页面,Vault 里
  现有的要重新生成才会变样。**`./kb.sh restart` 不管用**,重建跑 `./kb.sh graph`,而且**用仓库内
  `data/` 时只能在主检出跑**。
- 回归在 `tests/redesign_probe.mjs`(48 项),截图脚本 `tests/shots.mjs`,都是 CDP 直连
  chrome-headless-shell,不装 playwright、不连网。

## 前端反馈(硬性)

- **禁止用原生 `alert` / `confirm` / `prompt`** —— 系统弹窗跟本项目的调子完全不搭,还阻塞主线程。
  统一走 `kb/static/js/ui.js` 三个出口:`toast(text, {type, detail})` 一次性操作反馈、
  `confirmBox({...})` 破坏性动作确认(返回 `Promise<boolean>`,Esc / 点遮罩 = 取消)、
  `apiFail(r, fallback)` / `toastIfFail(r, text)` 从失败的 `Response` 榨错误文案。**别再写
  `(await r.json()).detail || '失败'`** —— 未捕获异常时 Starlette 返回的是纯文本 500,`r.json()`
  会炸,原因就被吞成一句无信息的「失败」。**表单字段级校验不用 toast,用 `.field-err` 就地红字。**
- **问答的模型下拉收在输入框里,不许再单起一行。** `.chat-inputbar` 整体就是那个框(边和圆角画在
  它身上),下拉贴框内左下角、发送在右下角。焦点同理只画一条
  (`.chat-inputbar:has(#chat-input:focus-visible)`),`#chat-input` 得显式 `box-shadow: none`
  压掉 base.css 那条。
- **问答面板的高度按可用空间算,不许再写 `calc(100dvh - N)`** —— 那个 N 是照「页头上面还有多少
  东西」量出来的,页头一变就错(`#wechat-welcome` 这条首次配置横幅就不在估算里)。现在是
  `body.chat-mode #main-content` 锁在 `calc(100dvh - var(--topbar-h))` 并改成 flex 列,`#askpanel`
  与 `.chat-layout` 各用 `flex: 1` 逐层吃掉剩余。`--topbar-h` 在 navigation.css 里定义,别再各写
  一个 56。**`#askpanel` 上那句 `min-height: 0` 不能省**(flex 项的自动最小尺寸按内容算,一段长
  对话会把面板顶到几千像素高),`width: 100%` 同理不能省。
- **聊天区里一条固定高度地板都不许有,`.chat-main` 也不例外** —— **地板造不出不存在的空间**:
  手机横屏带首次配置横幅时 `.chat-layout` 只有 115.8px,200px 的卡片直接把整块输入区顶到视口外。
  正解是把「输入区最小需要多少」压到任何支持的视口都放得下。**`.chat-layout` 的 `min-height: 0`
  要显式写**(省掉等于 `auto`)。
- **留白收不收由 JS 按实际余量定,不许再用高度阈值**(`chat.js` 的 `fitChatTight` 加
  `body.chat-tight` / `-2` … `-5`)—— 「放不放得下」从来不是一个高度数字能回答的,**换一个更小的
  阈值只是把这条缝挪个地方**。每次重算时先把 class 摘掉、把文本域压到一行下限,量一次还进不进得
  去卡片,进不去才加回来;**判定永远在常规留白下做**,所以不会来回抖。五级逐级试:①卡片**里面**
  那点空隙;②页头留白退让(收卡片里的空隙给不出卡片**本身**的高度);③横幅按回一行;④横幅只留
  标题和两个按钮(收的是解释,不是功能);⑤模型下拉和发送按钮回到同一行(`flex: 1 1 0`,
  **`flex-basis: 0` 是关键** —— 折行按 flex 基准尺寸决定,只给 `min-width: 0` 不管用)。
- **「面板没显示」和「可见但被挤到只剩边框」是两回事,别拿 `clientHeight` 当隐藏判据** ——
  零高度的可见卡片恰恰是最需要收缩的形态,越需要释放空间反而越不释放。用
  `card.getClientRects().length`。**摘 class 要在这个判据之前**(提前 return 而不摘的话,紧凑样式
  会跟着走到别的页面上去)。
- **「这一级有没有用」要比缺口,不能只比卡片高度**(`gap = chatBarNeed(bar) - card.clientHeight`)
  —— 第一级收的全是卡片**里面**的空隙,它按定义不会让卡片外高变化,只比卡片高度等于把它一律判成
  无效。缺口的两头哪边变好都算数。**已知下界**:812×200 带横幅确实放不下,如实标出来。
- **竖排断点下历史栏按比例限高**(`.chat-side { max-height: min(185px, 26%) }`)。**别指望
  `flex-shrink` 顶事** —— `.chat-main` 的 `flex-basis` 是 `0%`,这条 flex 线走的是 **grow** 分支。
- **文本域的增长上限得认卡片里的实际余量,这件事只能在 JS 里做**(`chatInputRoom`)——
  `autoGrowChat` 写的是**行内 height**,而行内高度会成为 flex 的「specified size suggestion」,
  CSS 侧没有出路。**别反过来去调 `.chat-main` 的地板。**
- **重算的触发是 `ResizeObserver`,而且 `.chat-main` 和 `.chat-inputbar` 两个都要观察** ——
  逼窄文本域的有两类:卡片自己变矮(展开历史栏、关横幅**压根不触发 window resize**),以及
  **卡片一动不动、输入框自己变高**(模型列表是 fetch 回来的,长模型名把它撑开、发送被挤到新行)。
  所以观察那一行本身,不是去 `loadModels()` 尾巴上挂钩子。它自己收敛。
- **验收不能只看「元素 bottom < innerHeight」** —— 被 `overflow: hidden` 的祖先裁掉时元素照样在
  首屏坐标里。所以回归同时量卡片的裁剪边界,并用 `elementFromPoint` 做真实命中测试,**探中心点 +
  贴下边沿两个点**。撑高必须走真实输入事件(CDP `Input.insertText`),异步模型那一格走真的
  `loadModels()`、只打桩那一条 fetch。
- **探针收尾一律 `writeSync`,不许 `process.exit`** —— node 对管道上的 stdout/stderr 是异步写的,
  `exit()` 把没刷出去的直接丢掉(项数涨到七百多条之后越过 64 KB 管道缓冲,JSON 断在半个中文字符
  中间)。**而 `writeSync` 自己要顶住短写和 `EAGAIN`** —— node 把这两个 fd 设成**非阻塞**,缓冲满
  时它不是等而是**抛**,于是单跑这一个文件是绿的、全量跑才崩。短写接着写剩下那段,`EAGAIN` 用
  `Atomics.wait` 同步等 2 毫秒再试。
- **探针里等运行状态必须自己驱动 `refreshDailyStatus()`** —— 无头浏览器把后台页面的定时器节流到
  分钟级。同理 `runDaily` 不能直接 await(它自己会停在确认框上等点击)。
- 详细事故形态见 `docs/decisions/frontend-feedback.md`。

## 往本文件添加内容(硬性)

- **除非经过同意,不要向 CLAUDE.md 中添加任何信息。** 这个文件每次会话都整份进上下文,而它曾经长
  到 4948 行 / 24 万字符 —— 来源不是谁一次性塞进去的,是「每轮审查修红之后追加一段完整留痕」这个
  只写不收的习惯累积出来的(有一天加了 1687 行)。
- 要记的东西按形态分三处:**硬约束**(别人会真的违反、违反了有不可恢复后果的)写进本文件,**先问**;
  **事故形态、实测证据、回归清单、反向验证红数**写 `docs/decisions/<节名>.md`;**怎么写那条测试**
  写进测试文件自己的 docstring。
- 本文件里不要再出现:某次审查是第几轮、注入了几版各红几条、回归文件里有几条用例分别叫什么、
  某个决定是哪一天做的(除非那个日期本身有时序含义)、测试文件为什么拆成两个。
- **改节标题要跟着改代码里的引用。** 若干处注释和 docstring 按节名指回这里
  (`grep -rn 'CLAUDE\.md' kb/ tests/ scripts/ collector/tests/`),改了名不跟着改就指不到了。
