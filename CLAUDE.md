# knowbase 项目约定

> **这个文件只留跨块通用的东西:元规则、用词、操作入口、界面底线,外加最后那张索引。**
> 每一块功能自己的硬约束搬去了 `docs/decisions/<那一块>.md` 的**第一节**(「硬约束」),同一份文件
> 里紧跟着就是它的事故形态、实测证据、回归清单和反向验证红数。
> **要动某一块代码,先按索引读它那一份** —— 那些约束违反之后多半是**静默丢数据、而且不可恢复**,
> 光看代码看不出来。本文件每次会话整份进上下文,所以**往里加东西之前先看最后一节**。

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
  「确定没有」。**「拿到东西了、却一条都没解析出来」是故障,一律抛,不许安静返回空集**
  (`fetching.parse_feed` 的 `parsed.entries and not entries`、`x_browser` 的
  `seen_articles and not parsed`)—— 安静返回空会让这条订阅从某天起永远「今天没有新内容」,而面板上
  一切正常。**反过来钉一边:合法的空源照旧返回空集**(空 RSS 就是 `[]`,`tests/test_feed_sources.py`
  钉着)。「这一类来源零条就算故障」是**某些来源自己**的更严口径(网页专栏、邮件简报),写在它们各自
  那份决策文档里,不是通用规则。
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
- 向量子进程、帧协议见 `docs/decisions/embedding.md`;模块混跑那条的完整形态见 `code-stale.md`。

## 视觉语言(硬性)

- **一套色板:瓷白 `#F3F5F4` × 墨蓝黑 `#14202B` × 深松绿 `#17604F`**,唯一源头是 `kb/static/css/base.css` 的 `:root`。**别再换重点
  色** —— 靛蓝(`#3F43B9` 一带)是 AI 产品的默认色,而「开源项目」占了图鉴 5530 条里的 2976 条,类型色一旦跟重点色撞上,卡片上那枚
  类型标记在绝大多数卡片上就什么也没区分。前因后果见 [全系统视觉统一](design/visual-system-unification.md)。
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
- 问答面板的高度/留白/文本域增长、以及探针那几条,全部在 `docs/decisions/frontend-feedback.md`
  的硬约束一节(**动 `chat.js`/`chat.css` 或页面探针之前先读**);来源页布局在 `subs-layout.md`。

## 硬约束索引(按你要动的东西查)

**本轮搬过去的那些,第一节就是「硬约束」;余下几份(`body-gap`、`doc-reader`、`source-cards`、
`extract-optin`、`subs-layout`)整份都在讲它那一块,没单拆首节。** 动对应的代码之前先读那一份 —— 下面这些块的约束,违反之后的
典型后果是水位跳过一段、空正文永久落库、素材被永久写死、整批静默不同步,**重试救不回来**。

| 你要动的东西 | 先读 `docs/decisions/` 下的 |
|---|---|
| 公众号采集、订阅名单、本机绿泡泡库(`collector/`、`kb/collectors/wechat_local.py`) | `collector.md` |
| X 大V的四条通道、水位、CDP(`kb/feeds/x_channels.py`、`x_browser`/`x_feedgrab`/`x_cdp`/`cdp_attach`) | `x-channels.md` |
| X 长文正文补全、地址识别那一族正则(`kb/feeds/x_article*.py`) | `x-article.md` |
| 聚合源正文补抓、正文提取器、提取结果与正文版本绑定(`kb/feeds/fulltext.py`、`readable.py`) | `fulltext.md` |
| 订阅正文转纯文本、阅读页渲染、来源卡片(`kb/feeds/fetching.py` 的 `plain`、`kb/web/pages.py`) | `feed-plain.md`、`doc-reader.md`、`source-cards.md` |
| 新订阅只收几天、存量清理(`kb/feeds/history.py`、`scripts/feed_trim_history.py`) | `history-depth.md` |
| 「重试失败项」那颗按钮的范围(`kb/pipeline_retry.py`、`pipeline_state.py`) | `retry-scope.md` |
| 同步的历史运行、一轮的红怎么留档、恢复时挑哪一轮(`kb/pipeline_state.py`、`pipeline_persist.py`、`pipeline_open.py`、`pipe_history.js`) | `pipeline-history.md` |
| **任何写 kb.db 或 vault 文件的入口**(索引、导出、提取回写、条目化、清理脚本) | `maintenance-lock.md` |
| 没有 feed 的站点、网页专栏解析(`kb/feeds/page_list.py`) | `page-list.md` |
| 邮件简报 / TLDR(`kb/feeds/digest.py`) | `digest.md` |
| 往 `defaults.json` 加来源、存量库补种(`kb/feeds/registry.py` 的 `DEFAULTS_REV`) | `defaults-backfill.md` |
| 英文内容翻译、译文显示口径(`kb/feeds/translate.py`) | `translate.md` |
| 每日日报、素材账本、出处编号(`kb/daily*.py`、`kb/daily_refs.py`) | `daily.md` |
| 同步的定时、日报那一轮的两段式编排(`kb/daily_run.py`、`kb/pipeline._scheduler_tick`) | `sync-schedule.md` |
| 大模型阶段配置、`secrets.json`(`kb/llm_stages.py`、`kb/llm.py`) | `llm-stages.md` |
| 软广判定与「正文空」判定(`kb/extract.py`、`kb/extract_promo.py`、`kb/extract_writeback.py`、`scripts/promo_backfill.py`) | `promo.md`、`body-gap.md` |
| 条目化提取的开关(`kb/extract_optin.py`) | `extract-optin.md` |
| wiki 层、三张图谱、归类缓存与发布(`scripts/wiki/`) | `catgraph.md` |
| 测试探针起无头浏览器(`tests/probe_shell.mjs`、`tests/headless.py`) | `probe-shell.md` |
| 向量子进程与帧协议(`kb/embed_worker.py`、`embed_pool.py`、`embedder.py`) | `embedding.md` |
| 问答面板布局、聊天区留白、页面探针收尾(`kb/static/js/chat.js`) | `frontend-feedback.md` |
| 「我的订阅」页布局(`kb/static/js/sources.js`、`targets.js`) | `subs-layout.md` |

## 往本文件添加内容(硬性)

- **除非经过同意,不要向 CLAUDE.md 中添加任何信息。** 这个文件每次会话都整份进上下文,而它曾经长到
  4948 行 / 24 万字符 —— 来源不是谁一次性塞进去的,是「每轮审查修红之后追加一段完整留痕」这个只写不
  收的习惯累积出来的。
- 要记的东西按形态分三处:**跨块通用的元规则 / 用词 / 操作入口 / 界面底线**写进本文件,**先问**;
  **某一块自己的硬约束,连同它的事故形态、实测证据、回归清单**写
  `docs/decisions/<那一块>.md`(硬约束放第一节,**新开一块要顺手在上面那张索引里加一行**);
  **怎么写那条测试**写进测试文件自己的 docstring。
- 本文件和决策文档里都不要再出现:某次审查是第几轮、注入了几版各红几条、回归文件里有几条用例分别
  叫什么、某个决定是哪一天做的(除非那个日期本身有时序含义)、测试文件为什么拆成两个。
- **改节标题或搬动约束,要跟着改代码里的引用** —— 若干处注释和 docstring 按名字指回这里或指回某一
  份决策文档(`grep -rn 'CLAUDE\.md\|docs/decisions' kb/ tests/ scripts/ collector/`)。
