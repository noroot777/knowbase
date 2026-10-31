# 大 V 观点：五天有界增量采集

实施日期：2026-10-31。任务 `J45OI3nIhEEf`，开工基线 `65ecd14`。
本轮只改变未配置专用 RSS 的 X 账号自动采集，沿用已有 DOM 解析、作者身份与详情完整性判据；
不重做 YouTube、公众号、通用 RSS 或存量正文修复策略。

## 五天不是每轮再往前五天

- 本轮启动时冻结 `now - 120 hours`，与订阅既有 `history_floor` 取较新值。无时区的项目时间按上海解释。
- 老水位不能把自动采集拖回几周前；下一轮只扫描它当时最近五天，不继续向历史深处滚动。
- 已发现但过期的正文待办变成 `expired`，历史窗口外的部分另留说明；两者都不算完成。
- 已有旧正文不删除。用户显式选择日期的区间采集保留原有独立语义，既不消费日常队列，也不改日常水位。

## 三阶段，共享会话

1. **时间线发现**：先轮询账号的近期段，通常从最近一天或更近的已确认水位开始。
   账号切片按当前剩余发现预算和未轮到数量缩小；先保存 ID、URL、预览及正文欠账，不当场串行补所有长推。
2. **历史追补**：只核对最近五天尚未确认的覆盖。复用旧“连续三条本人旧推”的边界证明，置顶/旧转推不能冒充到底。
   当前没有经实测可重放的原生 cursor，故降级为从顶部的有界 DOM 重扫，并保留历史缺口。
   **持久候选队列可恢复，不等于远端分页游标可恢复**；不承诺解决源站无法提供的历史，亦不改成未验证的日期搜索。
3. **正文补全**：按独立逐条队列补折叠推文与 Article；跨账号轮转，未尝试/最久未试优先。
   单条失败只更新该候选重试次数/到期时间，不重置账号发现，也不增加账号失败账。

`feedgrab` 没有可控的分页/详情拆分接口。它也服从五天过滤、全局预算、账号切片和持久 outbox；
其上游扫描（可能内部含详情）如实记为一个发现阶段，历史扫描共享该阶段，指标标注
`timeline_phases_separable=false`。由本项目执行的 Article 补全另外排队；不声称能控制 feedgrab 内部请求。

## 持久化与一致性

- 持久表包括来源覆盖/尝试账、按 status ID 共享的候选、订阅修订绑定的归属/outbox、最近 30 次阶段统计，
  扫描批次与批次成员两张表，以及按归属记录的过期事件表。
  仅 `CREATE TABLE/INDEX IF NOT EXISTS`，不删旧表、不改已有正文。
- 来源身份绑定 URL、专用订阅链接和单调修订号；改链接后旧任务不能写进新账号，A→B→A 也不复活旧结果。
- 候选与暂存进度同事务；时间线消费者保留到会话收尾，迟到核心错误阻止该批归属确认和覆盖推进。
  即使确认前进程退出，候选仍在，但不会把未确认成品送入正文库。
- 覆盖保存连续区间的两端：只有新扫描下界与旧覆盖末端相接才合并。停机数日后，旧“最早读到哪”
  不能遮住中间几天缺口；必须重新追补。跨轮过期的在途正文也不能借迟到完成重新变成有效待投递成品。
- 成品队列直到 `put_items`、水位补丁和 outbox 确认同事务提交才消费。崩溃/本机写失败后仍可再次投递。
- 只复用当前同版来源和正文完整证明：带展开/Article 欠账的预览不算证明；存量行需匹配当前完整读数或已完成候选回执。
  旧库不明来源/正文差异进入 `repair` 待核，不靠 `DO NOTHING` 假称已修好，也不绕过版本化 CAS 回填。
- 永久排除仍有效；远端明确不可用保留 URL/原因，不再每轮重新排该正文。
- 成功账只认可本轮确实返回、并完成会话收尾确认的时间线读数。上轮已经证明完整不能替本轮作证；
  上游 skipped、启动限流、切片超时和空的不完整读数不结清旧成功/失败账，旧 outbox 仍可单独投递。
- 本轮实际接收内容与正文欠账全部持久化、没有未确认候选或当前核心/本机故障时，更新成功并结清旧失败；
  五天历史缺口与安全跳过的说明不单独扣留成功，仍保留 `gap` 和提示继续追补。
- 历史覆盖必须显式满足 `coverage_complete=True` 且 `incomplete=''`；缺键、None、上游跳过都不是完整证明。
  真实过期待办才触发窗口外提示，下界只显示上海日期，不把每轮下界滑动冒充丢失内容。

## 预算与限流

默认整轮预算 480 秒，先保留 30 秒收尾（小预算取20%）；发现/历史/正文分别最多 240/120/120 秒，
共享提前后的联网截止，不是三个阶段都能用满。关闭另保留 5 秒，不挤占那份本地收尾窗口。
已确认持久成品的投递只有本地事务，不再被联网截止拒绝；即使关闭超时跨过总截止仍投递，超支如实记录。
维护锁和队列 SQLite 写锁不长期等：拿不到立即报本机待续，不把 15 分钟维护锁等待塞入预算。
CDP 命令受当前切片截止约束；关闭命令必须能释放自有资源。操作系统/关闭本身不可硬实时中止，
`overrun_seconds` 如实记录超时尾部，不把它伪装成零，不承诺 51 个账号都在八分钟完成。

环境变量（秒）：`KB_X_LOCAL_SECONDS`（480）、`KB_X_DISCOVERY_SECONDS`（240）、
`KB_X_HISTORY_SECONDS`（120）、`KB_X_BODY_SECONDS`（120）、`KB_X_ACCOUNT_SECONDS`（发现 8/历史 20）、
`KB_X_BODY_ITEM_SECONDS`（25）、`KB_X_FEEDGRAB_ACCOUNT_SECONDS`（25）。
这些是上限，不能借某一阶段调大越过全局截止。

沿用同会话 45→90→180 秒冷却：时间线、详情和 Article 浏览器后路共用闸门。
启动接口限流可排尾有限重试；剩余时间不够“等完 + 至少两秒有效工作”则直接留待下轮，不把尾部预算睡光。
内容源 429、安全关卡、登录失效、会话死亡仍停止相应余批；不把辅助请求错误升级为内容限流。
未轮到的账号不记连接失败。数据库忙/锁、磁盘满、只读等本机故障也不推账号的失败/退避。

## 统计与页面

- SQLite 持久保存准备、启动、发现、历史、正文、关闭、确认、投递的单调时钟墙上耗时及全局耗时。
- 冷却和候选/正文写回是对应阶段耗时的**子项**，不能再次相加。冷却统计实际等候时间，不是配置的 45/90/180 之和。
- `page_ready_seconds` 是导航到页面可读的等待，不冒充网络请求延迟。日志不存 cookie、请求头和完整响应负载。
- 记录候选读数、复用命中、正文尝试/完成、待补、过期、历史缺口、尝试/未轮到账号以及本机错误。
  复用和候选数是处理读数次数（同候选跨阶段可命中），不是去重后的独立推文数量。
- 来源 API 和订阅行显示“最新发现已完成/未读完/尚未轮到、历史缺口、正文待补、超出窗口”；
  最近运行展示阶段耗时。重启服务或清空内存预取缓存后仍可查看，不依赖短暂流水日志。
- 旧连接失败账保留时，本轮未完成/本机故障说明仍同时展示，不被旧错误遮住。
  统计绑定采集通道；切换通道不沿用另一通道的指标，显式日期采集也不把旧自动批次耗时冒充本轮耗时。

## 首版执行证据（2026-10-31，提交 1d49f24）

### 限定新增测试

- 本轮只登记并运行新增 `tests/test_x_intake.py`，没有运行既有测试文件或全仓回归。
- 首次登记 `_iUyz8s584WE`：31 项中 2 失败、7 错误，原因是新增夹具键使用大写账号，
  与生产已有的小写归一化不一致。只改夹具，不更改生产账号归一化。
- 随后登记 `nuo4vHKrJkua`：实际日志为 `Ran 33 tests in 0.695s / OK`、退出码 0；
  另补连续覆盖两端及过期在途任务的用例。日志：
  `/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/nuo4vHKrJkua.log`。
- 页面冒烟后另补旧错误/当前说明并存、通道指标隔离、显式日期不认领旧指标三项，
  最终登记 `F6zGWuqIrLvO`：实际日志为 `Ran 36 tests in 0.754s / OK`、退出码 0。
  已读取真实日志，未以先前 33 项通过代替最终结果。最终日志：
  `/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/F6zGWuqIrLvO.log`。

原始命令（最后一轮同此命令，不过滤输出）：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-intake-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-intake-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-intake-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake -v
```

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。
18 份实现/测试代码的逐文件 SHA-256 清单：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-implementation/tested-code.json`；
清单自身 SHA-256 为 `e997e81231f3ac5420a90156d325fffdb45228fabc9e2e104fe6856030e4af5d`。
代码摘要用于提交前后逐项核对；文档补充不改变登记代码摘要。

### 隔离 API、数据库与真实页面

证据根目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-implementation/`。

- `serve.py` 使用全新临时数据/笔记/视频目录，禁止对外 socket 连接，禁用调度器。
  只替代外部时间线、详情和登录就绪边界，调用真实 `sync.collect`、队列、成品事务与 Web API。
- 四个实际同步夹具产生 2 条成品，1 条正文待补、1 个历史缺口、1 个本机候选写入故障；
  只有健康来源记成功，`failed_ids=[]`。未完成来源原有成功时间、错误、失败次数、退避全部保留。
  另外通过真实队列入口准备“未轮到”和“超出五天”的两种状态，共 6 个可见来源。
- `/api/health`、`/api/intake/social`、`/api/intake/social/items?limit=100` 均实际 HTTP 200。
  清空预取内存缓存后，待补/过期/覆盖状态及阶段指标仍可读取；预览未混入 `feed_items`。
  证据：`http-evidence.json`、`api-summary.json`、`database-evidence.json`、
  `subscription-ledgers.json`、`sync-summary.json`。
- 切到 feedgrab 后 API 不返回旧 browser 指标，见 `api-channel-switch.json`。
  显式日期页面验证使用独立构造的区间完成记录，确认页面不拼接旧自动指标；
  它不是实际远端区间抓取，真实 `sync.collect` 区间路径另由限定测试覆盖。
  证据：`api-ranged-summary.json`、`page-evidence-ranged.json`。
- 无头浏览器实际加载本轮页面和后端，只点击“订阅管理”，不点同步、登录、保存或外链。
  6 种状态、旧错误与本轮本机说明、发现/历史/正文/投递耗时均可见；
  无 JS 异常、无横向溢出。检查截图：`sources.png`（自动批次）、`sources-ranged.png`（区间记录）。
  DOM/执行记录：`page-evidence.json`、`page-visible.txt`、`page-command.log`。

浏览器通道严格先尝试扩展：具名会话“🔎 大V五天采集验证”，要求 `visible:false` 新后台标签。
本轮真实返回 `Capability is not available: visibility`，选项在创建前被拒，未改成前台标签。
因此降级为独立临时 profile 的 `chrome-headless-shell`，只访问 localhost，
浏览器拦截外域和所有非 GET 请求；不连接日常 Chrome、不读其 cookie、不使用有头浏览器。
通道证据：`browser-channel.json`。浏览器进程/profile、服务进程及临时数据目录已清理，
见 `browser-cleanup.json`、`server-cleanup.json`。

### 静态检查与留白

Ruff（`uv tool run --offline ruff check`，仅本轮 Python 文件）、两份 JS 的 `node --check`、
Python AST、`git diff --check` 与 700 行限制均通过；最大文件 `sync.py` 697 行。
本轮没有访问真实 X、读取真实 cookie、执行生产同步或调用模型。
离线模拟的毫秒级耗时不是源站吞吐测量，不承诺真实 51 账号八分钟完成；
远端原生 cursor 捕获/重放、真实风控与长期运行效果未验证。全仓回归留给验收阶段。

## 第 1 轮审查修复记录（14SfRWnTYU8-，提交 ad337a9）

报告五条必修全部接受，范围限于本次 X 自动增量链路，未改 YouTube、通用 RSS 或显式区间采集判据。

1. `Intake` 独立记录本轮时间线返回与收尾确认，不再依赖上轮 `gap` 判成功；每轮重置发现状态，
   超时/限流/未轮到的当前原因保留。已持久化成品可以投递，但不据此冒称本轮访问过远端。
2. feedgrab 的 skipped 不进候选扫描确认，完整性缺键按未知处理；`store.confirm` 显式检查覆盖凭据，
   防止把跳过批次盖成最近五天全覆盖。
3. 将本轮接收并保存成功与五天覆盖拆开：历史缺口和安全跳过说明仍保留，但不阻止已接收完整内容的
   成功时间、订阅清账及 `succeeded_ids`。正文欠账、当前核心错误、本机故障、停止仍保留旧账。
4. 仅真实 `expired` 待办产生窗口外提示，并按 Asia/Shanghai 输出日期；旧的误报提示会被清除。
5. feedgrab 超时文案与真正传给子进程的时限一致，包括账号预算裁短后的时限和默认时限。

本轮修改 `tests/test_x_intake.py` 的显式覆盖夹具，并新增 `tests/test_x_intake_review.py`，
覆盖旧覆盖后的零读取/启动限流/browser 与 attach 超时、旧 outbox 投递不清账、三轮有历史缺口的真实入库、
引用跳过、正文欠账、当前核心错误、feedgrab 跳过/缺字段/恢复、覆盖证明缺省、实际过期/日期与真实超时文案。
审查列出的非阻断事项未在本轮扩展处理，全仓兼容回归仍留验收阶段。

### 本轮限定测试与代码摘要（2026-10-31）

- 仅登记并运行本轮修改的 `tests/test_x_intake.py`（36 项）与新增的
  `tests/test_x_intake_review.py`（22 项），没有运行其他既有测试文件或全仓回归。
- 首次登记 `xHBmFUO0_jqD`：`Ran 58 tests in 1.268s / FAILED (failures=2)`。
  两条失败均是新夹具口径错误：三轮历史缺口用例误用了已完整覆盖的初态，合法跳过了历史扫描；
  正文欠账用例把仅返回到期任务的队列查询误当成持久未完成数量。分别补真实缺口初态，
  并断言持久欠账为 1、当前到期队列为空；没有为此改动生产逻辑。
- 最终登记 `GeeCjfH1lG7g`：已读取真实日志，`Ran 58 tests in 1.240s / OK`，退出码 0。
  日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/GeeCjfH1lG7g.log`。
  首次失败日志保留于同目录 `xHBmFUO0_jqD.log`，没有用首版结果替代本轮结果。

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-review1-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-review1-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-review1-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake tests.test_x_intake_review -v
```

本轮 4 份实现、2 份测试的 SHA-256 清单：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-1/tested-code.json`，
清单自身 SHA-256：`4454d517c175ebcc3a0a0e9158d6b74d751cd5ffcfda2a6c0dc1a51e5b74266c`。
测试通过后实现和测试文件未再改动，收尾只补充文档；提交前后逐项核对这 6 份代码摘要。

### 本轮隔离 API 与页面点检

证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-1/`。

- `serve.py` 使用新临时数据/笔记/视频目录，阻断对外 socket、禁用调度器。
  只替代外部时间线、正文、登录就绪边界与冷却等待，实际调用 `sync.collect`、队列、保存事务与 API。
  冒烟脚本初次因证据辅助代码误写不存在的历史表而退出，已删除该辅助写入并从全新临时目录重跑；
  最终展示的运行记录及耗时均来自真实隔离采集，没有构造或覆盖运行记录。
- 先实际运行 feedgrab skipped 场景：没有覆盖证明、没有成功 ID，原成功/失败/次数/退避账全部保留，
  `covered=''`，发现状态不为 complete。证据 `feedgrab-skip.json`；随后恢复 browser 取法。
- 再实际同步 4 个 browser 来源，落库 3 条成品（含一条上轮 ready outbox），只有历史缺口来源和健康来源
  进入 `succeeded_ids`，`failed_ids=[]`。旧完整覆盖但本轮零读取的来源、正文仍待补的来源均保留旧账；
  历史仍有缺口但本轮完整内容已入库的来源更新成功时间、清错误/失败次数/退避，同时保留缺口提示。
  证据 `sync-summary.json`、`scan-evidence.json`、`subscription-ledgers.json`。
- 另外从真实队列入口准备 1 条已超出五天的待办与未轮到状态。真正 expired 才产生上海日期提示；
  历史缺口来源无 expired，不会产生窗口外误报。正文预览仍不进入 `feed_items`，冷却中仍显示待补 1 条。
  证据 `database-evidence.json`、`api-overview.json`、`api-items.json`。
- 清空预取内存缓存后，实际服务 `/api/health`、`/api/intake/social`、
  `/api/intake/social/items?limit=100` 均 HTTP 200，见 `http-evidence.json`、`api-summary.json`。
- 独立无头浏览器真实加载页面，仅点击订阅管理以及展开两张卡片的失败详情，没有点同步、登录、保存或外链。
  共 7 张卡片（6 个启用来源，另 1 个停用的 feedgrab 夹具）：零读取和正文欠账保留旧失败与旧成功时间；
  历史缺口来源显示本轮成功时间、无旧失败详情；真实过期仅显示日期，无 ISO 时间戳。
  阶段耗时可见，页面无横向溢出、无 JS 异常。截图 `sources.png` 已人工查看；
  DOM/执行证据 `page-evidence.json`、`page-visible.txt`、`page-command.log`。

本轮重新尝试扩展具名会话“🔎 大V审查修复验证”，`visible:false` 新建后台标签，真实返回
`Capability is not available: visibility`，在创建前被拒，未改用前台标签。
因此按通道顺序降到独立临时 profile 的 `chrome-headless-shell`，始终无头，浏览器只允许 localhost GET，
拦截外域及非 GET 请求。只连接自己启动的无头进程，不接管用户 Chrome、不读 cookie、不开有头窗口。
通道见 `browser-channel.json`；无头进程/profile、隔离服务/临时数据均已清理，
见 `browser-cleanup.json` 与 `server-cleanup.json`。

### 本轮静态与留白

本轮 6 份 Python 文件 Ruff、AST、700 行上限及 `git diff --check` 均通过。
未抓真实 X、未读取生产库或 cookie、未调用模型；隔离耗时不是实际源站吞吐数据。
真实账号风控、远端游标、51 账号八分钟吞吐及其他未修改文件的兼容回归均未验证，留给专门验证或验收。

## 第 2 轮审查修复（14SfRWnTYU8-）

三条必修均在本次 X 自动增量链路内，接受修复；第 1 轮的正文欠账拦清账判据由本节替代。
非阻断清单没有顺带扩展修复，未改变其他来源的采集/清账规则。

1. 候选与批次成员同事务写入新增的 `x_intake_batches` / `x_intake_batch_posts`，扫描回执按批次号确认，
   不依赖会话收尾后可能被清空的 entry 列表。收尾失败保持未确认，API/卡片显示“候选待确认 N 条”。
   下轮优先为已有未确认候选的来源重扫五天历史，不能被旧覆盖证明跳过；新的完整区间收尾证明
   才可恢复区间内先前已持久化但未重现的候选。部分区间不得借此放行；旧库无批次记录的孤儿同样可恢复。
   未确认 ready 也会在五天后过期；被别的订阅确认的共享成品不被此路径改成过期。
2. `XBudgetExceeded` 是明确预算耗尽；仅发送前预算截止、或已由预算缩短的命令收到结构化响应超时，
   才使用这个类型。DOM 改版、未识别账号页、协议错误及非预算命令超时仍为真实失败，不再按异常发生时
   还剩几秒去猜。两支都将原始诊断保存到来源状态；pipe/attach 使用同一结构化超时口径。
3. 本轮已读取、收尾确认、候选及欠账持久化，并实际完成投递事务后，即可更新发现成功和清旧连接失败。
   已确认的正文 pending/repair 只是独立待办，不再无限扣留 `last_success` / `succeeded_ids`；
   预览仍不进正文库，正文失败仍按候选冷却重试。零读取、未确认批次、当前真故障、本机故障或停止仍不清账。

本轮修改两份已有 X 测试以反映明确预算类型与正文欠账独立的新契约，新增 `tests/test_x_intake_receipts.py`，
覆盖批次成员原子性、迟到限流后跨轮恢复、旧库孤儿、恢复优先级、部分覆盖不放行、过期、真实故障原文，
以及 pipe/attach 超时类型和连续正文欠账的独立清账。

### 二轮限定测试与代码摘要（2026-10-31）

- 仅登记并运行本轮修改的 `tests/test_x_intake.py`（36 项）、`tests/test_x_intake_review.py`（22 项）
  与新增的 `tests/test_x_intake_receipts.py`（18 项），没有运行其他既有测试文件或全仓回归。
- 登记哨兵 `WEC-pGBZJfGE`，已读取真实日志：`Ran 76 tests in 2.077s / OK`，退出码 0。
  日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/WEC-pGBZJfGE.log`。
  这次结果对应二轮代码，不用首版或首轮结果代替。

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-review2-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-review2-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-review2-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake tests.test_x_intake_review tests.test_x_intake_receipts -v
```

本轮 7 份 Python 实现、3 份测试和 1 份 JS 的 SHA-256 清单：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-2/tested-code.json`，
清单自身 SHA-256：`09442c0a7253fda8b6fc48ef98ee4c0a10fa977996f1a95062f32ad96682b905`。
测试通过后这 11 份代码未再改动；收尾只补充文档和仓库外证据，提交前后逐项核对代码摘要。

### 二轮隔离 API 与页面点检

证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-2/`。

- `serve.py` 使用全新临时数据/笔记/视频目录，阻断对外 socket、禁用调度器。
  只替代外部 X 会话、时间线、正文、登录就绪和冷却等待；实际调用 `sync.collect`、队列、保存事务与 API。
- 两个来源先分别真实执行迟到收尾限流：候选已经 stage，会话收尾把结果字典清空后写入 429 错误，
  均保留旧成功/失败账，`unconfirmed=1`，不入正文库。清空预取缓存后再次采集，其中一个没有重现旧推文，
  仍优先执行五天历史扫描；完整区间收尾成功后，三天前的旧候选确认、投递并更新来源成功账。
  另一个来源本轮遇到明确预算耗尽，仍保留待确认候选和旧账，不能借别的来源的收尾放行。
  证据 `late-receipts.json`、`scan-evidence.json`、`database-evidence.json`。
- 再次真实采集 6 个来源，落库 2 条成品（三天前候选与健康来源），成功 ID 仅为恢复、正文待补、健康三个来源。
  正文待补来源的 `last_error` 清空、失败次数及退避归零、成功时间更新，持久正文欠账仍为 1；预览不进 `feed_items`。
  真实 DOM 故障在账号切片结束后抛出，仍保留“X 可能改版了”原文，失败次数从 2 增到 3，并进入未来退避；
  `failed_ids` 仅有该来源。明确预算异常保留原始切片诊断，原成功/失败/退避账不变。
  证据 `sync-summary.json`、`subscription-ledgers.json`、`api-summary.json`。
- 另一个来源的未确认完整候选已超过 120 小时，经过真实 `prepare` 后过期，不再计入待确认，
  卡片显示上海日期和“未计完成”；共享候选保护、旧库孤儿及部分扫描不放行另由本轮限定测试覆盖。
- 清空预取缓存后，实际服务 GET `/api/health`、`/api/intake/social`、
  `/api/intake/social/items?limit=100` 均 HTTP 200，见 `http-evidence.json`。
- 真实页面共 7 张卡片：待确认 1 条可见，三天前候选显示本轮成功；真实 DOM 原因和重试时间可见，
  预算来源保留原始说明与旧账；正文待补 1 条与本轮成功同时可见，不再残留旧连接失败详情。
  分阶段耗时可见，无横向溢出、无 JS 异常。截图 `sources.png` 已人工查看，
  DOM/执行证据见 `page-evidence.json`、`page-visible.txt`、`page-command.log`。
  页面冒烟最初把既有“1 条”文案误写为“已入库 1”，并假设页面显示失败次数；
  按实际 DOM 修正仓库外检查，次数仍由真实 API/账本核对，没有为匹配冒烟改动产品代码。

本轮重新尝试扩展具名会话“🔎 大V二轮修复验证”，以 `visible:false` 新建后台标签，真实返回
`Capability is not available: visibility`，在创建前被拒，未省略后台限制重试。
因此按通道顺序降到独立临时 profile 的 `chrome-headless-shell`，始终无头，只允许 localhost GET，
拦截外域及非 GET 请求。只点击订阅管理和三张卡片的只读失败详情，不点击同步、登录、保存或外链。
只连接自己启动的无头进程，没有接管用户 Chrome、读取 cookie 或打开有头窗口。
通道记录 `browser-channel.json`；无头进程/profile、隔离服务/临时数据均已清理，
见 `browser-cleanup.json`、`server-cleanup.json`。

### 二轮静态与留白

本轮 10 份 Python 文件 Ruff、AST、700 行限制、JS 语法及 `git diff --check` 均通过。
未抓真实 X、未读取生产库或 cookie、未调用模型；本地替身耗时不证明真实源站吞吐。
真实账号风控、原生 cursor、51 账号八分钟吞吐与未修改文件的兼容回归未验证，仍留专门验证或验收。
二轮报告的非阻断事项未顺带修改，没有擅自追加审查轮数或执行验收。

## 新逻辑审查六项修复（qFDXLbqYsLQ4，第 1 轮）

修复基线 `47d91d5`，日期 2026-10-31。六项必修均接受，只处理本任务 X 自动采集链路：

1. **账号结果隔离**：整轮限流仍立即停止后续联网，原因保留在本轮统计和汇总；不再复制进其他账号的
   `pending` 或 `last_note`。已真实读到且完成确认、持久化与投递的健康账号照常记成功、清旧失败账。
   当前来源自己的核心错误仍阻止它取得成功，未轮到的来源不借别人的读数清账。
2. **超窗提示有期限，终态回执有清理**：按来源身份/推文记录过期事件，只显示最近24小时事件数；
   API 即使没有新同步也会重新计算，使提示自然归零。来源卡片只显示一份超窗说明。
   候选/归属/批次成员在五天资格范围外再保留 `pipeline_retention.history_keep_days()` 天后清理，
   删除订阅的关联账也清理；已确认但未投递的完整 outbox 与正式 `feed_items` 不被删掉。
3. **读库不抢写锁，本机故障按账号隔离**：初始化后的读取使用只读 SQLite URI 与 `query_only`，
   不执行迁移或 `BEGIN IMMEDIATE`；面板读取等待30秒，队列读取即时失败后记该账号本机说明。
   prepare、排序用进度、扫描重读、尝试登记和正文队列读取都在逐账号异常边界内，
   browser 与 feedgrab 复用同一份安全排序和轮转队列，不再由一个账号的库忙终止整轮。
4. **联网截止与本地投递分离**：480秒预算默认预留30秒收尾，小预算预留20%；三阶段服从更早的
   联网截止。已经确认、已经持久化的成品即使遇到关闭变慢、跨过总截止，也继续完成纯本地提交。
   因此480秒是调度预算而非不可突破的硬实时上限，实际超支仍记录，不能再把本地成品推迟一天。
5. **问题分类由事实生产者给出**：自动 X 输出历史缺口、正文待补、候选待确认、最近超窗四类
   结构化问题；不按任意 `last_note` 猜成“覆盖未完成”。历史尝试凭据也支持只有历史阶段的读取。
   正文提示明确独立冷却，强制同步不绕过单条正文冷却；超窗禁用普通重试并建议显式历史区间。
   原有执行端点、身份票、无 extract 阶段边界保留，其他来源分类不变。
6. **有效限制如实显示**：browser 的超时秒数取本次实际截止，滚动次数取本次传入上限，
   不再总写模块默认50秒/40次。与此前已修 feedgrab 超时口径一致。

### 本轮限定测试与静态证据

只运行本轮修改的 `tests/test_x_intake.py` 和新增的 `tests/test_x_intake_isolation.py`，
未运行其他文件或全仓回归。通过 ash 回归哨兵登记的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-qFDX-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-qFDX-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-qFDX-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake tests.test_x_intake_isolation -v
```

- 实际 cwd：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。
- 最终哨兵 `FuISngIgk2tJ`，实际日志 `Ran 58 tests in 1.383s / OK`，退出码0；
  日志 `/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/FuISngIgk2tJ.log` 已读取。
- 首次哨兵 `lakjCVcZqc4C` 有2处断言失败：旧限流断言要求污染另一个未访问账号，已按新契约反向约束；
  新历史分类夹具缺少真实 `attempt` 凭据，补齐并让结构化状态带上 `history_at`。
- 15份实现/测试/JS 的测试前 SHA-256 清单：证据目录 `tested-code.json`，清单自身 SHA-256
  `c93895ee7283e610c4759e5217ce1d43adeda0c926683ef3edebc063f825567b`。
  后续 API/UI 冒烟没有修改这15份代码，提交时逐项核对。
- 14份变更 Python 的离线 Ruff、AST/内存编译、JS `node --check`、700行约束及
  `git diff --check` 均通过。命令、退出码及行数见证据目录 `final-static.json`。

### 本轮真实保存、API 与页面

本轮证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-qFDX/`。

- 仓库外 `serve.py` 使用全新临时 KB 数据/笔记/视频目录，禁止对外 socket 并关闭调度器；
  只替代外部 X 会话、时间线、正文、登录就绪和冷却等待。实际调用 `sync.collect`、队列、事务与 API。
- 4个来源实际同步：前3个真实读取后，第4个抛出会话限流。2条成品正常进入 `feed_items`，
  健康、正文欠账、历史缺口三个来源均记本轮成功，旧错误/失败次数/退避清空，均不带另号429；
  正文预览保留在独立待办、不进入正式内容库；受限来源保留自己的旧成功账。汇总仍显示限流原因。
  见 `sync-summary.json`、`subscription-ledgers.json`、`database-evidence.json`。
- 另用真实 stage/prepare 产生一个未确认候选、一个近期过期事件、一个25小时前过期事件。
  清空预取内存缓存后 API 仍显示待确认1条、近期超窗1条、旧事件超窗0条。
  `GET /api/health`、`/api/intake/social`、`/api/intake/social/items?limit=100`、
  `/api/intake-care?kind=social` 均200；24路真实 HTTP 并发 overview 也全部200。
  见 `api-summary.json`、`http-evidence.json`，限定测试另核对持写锁时只读不抢锁。
- 实际页面7张订阅卡片：前三个来源有本轮最近成功、无旧连接错误和另号429；
  正文待补与成功并存，待确认数量可见，近期超窗只显示一次，旧超窗不再显示；分阶段耗时仍可见。
  “问题与待办”四档分别显示独立正文冷却、五天有界重扫、完整收尾确认、显式历史补抓建议；
  超窗的普通重试按钮禁用。无 JS 异常、无横向溢出、无外域或非 GET 请求。
  `sources.png`、`care-body.png`、`care-history.png`、`care-unconfirmed.png`、`care-expired.png`
  均已查看；DOM/网络见 `sources-evidence.json`、`care-*.json`、`page-evidence.json`。
- 仓库外冒烟初次把“旧超窗归零”误写成“该来源没有任何问题”；实际上该夹具还保留已尝试的真实历史缺口，
  只应断言没有超窗问题、没有从普通说明猜出的通用 partial，已修正检查，未改产品代码。
  页面点检首次已全部通过，但脚本等待一个被 `unref` 的浏览器退出事件导致 Node 返回13；
  修正仓库外退出等待后再次点检，返回0，清理记录确认无进程/profile残留。

### 浏览器通道、清理与留白

本审查轮重新尝试扩展具名会话“🔎 大V逻辑修复验证”，以 `visible:false` 新建后台标签，实际返回
`Capability is not available: visibility`，创建前被拒。没有省略后台约束重试，
随后才降到独立临时 profile 的 `chrome-headless-shell`，始终无头、仅允许本机服务 GET。
只点击订阅管理、问题与待办及四个只读问题详情；没有点击同步、登录、保存、重试或外链。
只连接自己启动的无头进程，未接管用户 Chrome/普通标签、未激活窗口、未读取 cookie。
见 `browser-channel.json`、`page-final-network.json`。自建无头进程/profile与隔离服务/临时数据均已清理，
见 `browser-cleanup.json`、`server-cleanup.json`。

真实 X 登录态、远端风控、原生 cursor、51账号八分钟吞吐均未验证；没有读取生产库或调用模型。
本地替身用时不证明源站性能；未修改测试的兼容回归与全仓回归仍由验收前置命令覆盖，
本轮没有补跑，也未扩大到报告非阻断事项或擅自增加审查轮数。
