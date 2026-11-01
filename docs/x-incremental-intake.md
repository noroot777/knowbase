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
其上游扫描（可能内部含详情）以 `combined` 明确拥有发现和历史两阶段状态；耗时仍记在发现阶段，历史共享，指标标注
`timeline_phases_separable=false`。由本项目执行的 Article 补全另外排队；不声称能控制 feedgrab 内部请求。

## 持久化与一致性

- 持久表包括来源覆盖/尝试账、按 status ID 共享的候选、订阅修订绑定的归属/outbox、最近 30 次阶段统计，
  扫描批次与批次成员两张表，以及按归属记录的过期事件表。
  新表/索引用 `IF NOT EXISTS`；旧来源表以加列迁移补充 `window_end`、分阶段阻断与 `screen_waits`，批次补充阻断令牌，
  不删旧表、不改已有正文或回执。
- 来源身份绑定 URL、专用订阅链接和单调修订号；改链接后旧任务不能写进新账号，A→B→A 也不复活旧结果。
- 候选、批次成员与尝试元数据同事务；暂存不发布已确认的最新读数、覆盖、历史缺口或发现完成。
  时间线消费者保留到会话收尾，迟到核心错误阻止该批归属确认和覆盖推进。
  即使确认前进程退出，候选仍在，但不会把未确认成品送入正文库。
- 覆盖保存连续区间的两端：只有新旧区间相接或重叠才合并，缺口须同时对照本轮冻结的 `floor` 与 `window_end`。
  discovery 只更新发现状态；history/recovery 不得冒充发现完成，发现提示不得改写历史缺口。停机数日后，旧“最早读到哪”
  不能遮住中间几天缺口；必须重新追补。跨轮过期的在途正文也不能借迟到完成重新变成有效待投递成品。
- 成品队列直到 `put_items`、水位补丁和 outbox 确认同事务提交才消费。崩溃/本机写失败后仍可再次投递。
- 投递与来源结论独立：自动路径已有已确认成品且发现/combined 真失败时，`take` 返回条目及 `meta.x_failure`，
  入库、outbox 确认、失败记账在同一事务完成。失败仍累计退避，保留旧成功时间及水位，不伪造成功清账；
  新增与失败可以并存。空成品自动失败和显式区间/旧路径仍抛原异常，未确认条目仍不可投递。
- 只复用当前同版来源和正文完整证明：带展开/Article 欠账的预览不算证明；存量行需匹配当前完整读数或已完成候选回执。
  旧库不明来源/正文差异进入 `repair` 待核，不靠 `DO NOTHING` 假称已修好，也不绕过版本化 CAS 回填。
- 永久排除仍有效；远端明确不可用保留 URL/原因，不再每轮重新排该正文。
- 成功账只认可本轮确实返回、并完成会话收尾确认的时间线读数。上轮已经证明完整不能替本轮作证；
  上游 skipped、启动限流、切片超时和空的不完整读数不结清旧成功/失败账，旧 outbox 仍可单独投递。
- 本轮实际接收内容与正文欠账全部持久化、没有未确认候选或发现阶段核心/本机故障时，更新成功并结清旧失败；
  五天历史缺口与安全跳过的说明不单独扣留成功，仍保留 `gap` 和提示继续追补。
- 访问阻断归发现/历史/正文阶段；成功与水位只看发现自己的阻断。正文原话附正文待补问题，
  历史与正文阻断不抹掉独立确认的发现成功。旧单份记录保守归发现；按阶段令牌确认，清除保留时间回执。
  专用状态写失败时观测先随结果进入运行摘要，面板按来源身份及通道回放；不能因零等待维护锁丢掉源站事实。
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
首屏预算按本阶段配置的账号时限判别，不与原生50秒比较：完整发现8秒/历史20秒仍不可读须报真实错误，
只有公平分配或全局截止缩短才是预算用尽。连续三轮首屏前用尽缩短预算仍无确认，升级来源失败与退避；
`screen_waits` 按阶段/运行身份去重计数，确认读取只重置自己的阶段，首屏已读到后的预算耗尽不计入该计数。

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

## 状态生产层根因修复（2026-10-31）

本轮响应“找根因，不要打补丁”，开工基线 `4c8bdba`。不是另调限流参数、修改页面隐藏规则，
也没有给同步成功判据再加例外；改动落在产生持久事实的状态转换层。

### 为什么不同症状会反复出现

修改前的全新临时库探针实际观察到三种矛盾：未确认的 `stage` 已推进 `latest` 并把 `gap` 写成
“等待会话收尾”；只有 history 确认却把 discovery 从 unattempted 改为 complete；已有完整五天覆盖后，
一次部分 discovery 又把历史缺口改成它自己的滚动上限说明。该修改前探针只留在当时会话输出，
没有另存可重放的旧代码运行工件，不把下面的修复后证据冒充修改前日志。

共同根因是同一来源投影同时接收**暂存回执与已确认事实**、**发现与历史两个独立阶段**的无归属写入。
因此加某条错误文案判断、在页面上屏蔽某一种缺口，都无法消除上游自相矛盾的数据。

现在由 `x_intake_store` 统一维护下列不变量，所有生产调用方走同一转换：

- 阶段所有权集中在 `_PHASE_FIELDS`：discovery 拥有发现尝试与完成；history/recovery 只拥有历史尝试；
  combined 明确拥有两者。未知阶段在候选、批次或状态写入前拒绝。
- stage 只保存候选、批次归属和尝试元数据；不改已确认的 latest、covered、coverage_end、gap，
  不宣布发现完成。confirm 才原子确认归属并发布投影；无效区间证明连候选确认一起回滚。
- 历史缺口由连续覆盖两端相对本轮冻结 `[floor, window_end]` 计算。相接区间才合并，
  不跨中间空洞；更旧的孤立证明不会毁掉较新覆盖。发现切片说明不能改写历史事实，
  历史完整也不能宣布发现完成；历史未完整的具体原因只在确有缺口时补充。
- 新增 window_end 的加列迁移保留旧回执；prepare 冻结当轮上界并从覆盖事实重算缺口，
  窗口前移后重新暴露真实缺口，不继续相信上一轮遗留提示。
- feedgrab 改为 combined 的尝试、暂存和确认，不再通过“发现 stage / 历史 confirm”借道。
  上游无法拆分的耗时仍如实共享，不因此声称原生游标可续抓。

### 本轮限定测试与代码身份

只新增并登记 `tests/test_x_intake_progress.py`，没有运行未修改的旧 X 测试或全仓回归。
20项涵盖暂存/确认隔离、阶段双向隔离与确认顺序、combined、连续覆盖两端和断档、严格证明、
事务回滚、旧库加列、真实 collect 历史恢复和 feedgrab 路由。

原始登记命令（不滤输出）：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-root-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-root-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-root-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake_progress -v
```

- 实际 cwd：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。
- 哨兵 `jeKWL23uV5bo`，已读取真实日志 `Ran 20 tests in 0.495s / OK`，退出码0。
  日志 `/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/jeKWL23uV5bo.log`。
- 本轮证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-root-cause/`。
  5份代码/测试的测试前 SHA-256 清单 `tested-code.json`，清单自身摘要
  `954dc0484b2463506b9915ecbdd50cd7c11b66b7c6e33e7659504d26fc61e427`。
  通过测试后代码未再修改；静态检查和提交前后逐项核对均以此清单为准。
- 离线 Ruff、AST/内存编译、700行约束及 `git diff --check` 结果见 `final-static.json`；
  没有用编译或手工冒烟代替已登记的20项测试。

### 隔离保存、API 和真实页面证据

仓库外 `serve.py` 使用全新临时 KB 数据/笔记/视频目录，禁止外部 socket、禁调度器；
仅外部 X 会话/时间线/正文/登录就绪/冷却使用替身，实际执行 sync.collect、队列、事务和 API。
采集时间取当时真实时间并在当轮冻结；直接覆盖证明和 collect 共享同一截止，不用虚构未来证明掩盖缺口。

- 四来源覆盖：历史完整恢复但发现受限；同一冻结窗口历史完整后再部分发现；
  新候选只暂存不推进已确认读数；发现完整但历史确实未到底。
- 三来源实际 collect 成功，清旧连接失败/退避，4条成品入 feed_items（含此前已确认的 outbox）；
  另一个来源的较新未确认候选不入库、latest 不变、待确认计数1。
  前两来源 discovery 都为 partial 但 gap 为空；真实缺口来源 discovery 为 complete，gap 保留
  “五天历史实际未到底”。见 `progress-proof.json`、`database-evidence.json`、`subscription-ledgers.json`。
- 清空预取缓存后，health、social overview、items、intake-care 四个真实 API 都200；
  另有24路真实 HTTP 并发 overview 全部200。历史问题只归属真实缺口来源，待确认问题只归属暂存来源，
  见 `api-summary.json`、`api-diagnostics.json`、`http-evidence.json`。
- 页面4张卡片和两个只读问题详情符合上述独立事实；最近成功与“最新发现未读完”可以诚实并存，
  没有把发现受限误报历史缺口。阶段耗时与非原生游标说明仍可见；无 JS 异常、横向溢出、外域或非 GET 请求。
  已查看 `sources.png`、`care-history.png`、`care-unconfirmed.png`；DOM/网络见 `page-evidence.json` 等。
- 首次页面脚本错误地查找通用历史提示，而实际行显示来源给出的具体原因；保留
  `page-command-first.log`，按实际已观察文本修正仓库外断言后全程通过，未修改产品代码迎合冒烟。

### 浏览器通道、清理与覆盖留白

本轮重新尝试具名扩展会话“🔎 大V根因修复验证”，`visible:false` 新建后台标签实际返回
`Capability is not available: visibility`，创建前拒绝。没有省略后台条件再试，才降为独立临时 profile
的 chrome-headless-shell；始终无头，仅允许本机 GET。只读导航订阅管理、问题与待办及详情，
没有点击同步/登录/保存/重试或外链；未接管用户 Chrome、普通标签或 cookie，也未激活窗口。
见 `browser-channel.json`、`page-final-network.json`。

两次自建无头进程/profile、隔离服务和临时数据均已清理，见 `browser-cleanup-first.json`、
`browser-cleanup.json`、`server-cleanup.json`；没有留下监听服务。
未访问真实 X、生产库、登录态或模型；原生 cursor、远端风控、51账号八分钟吞吐仍未验证。
未修改测试的兼容回归与全仓回归留验收，不扩大到其他模块，也未擅自添加复审轮次。

## gNHc 首轮审查修复（2026-10-31，开工基线 665e4fb）

本轮完整读取437行报告，接受预算归因、阻断归属和遥测污染三项已证实问题；未扩展非阻断清单。
默认入口切换的覆盖要求通过新增默认路径契约落实，既有 X 用例族仍交验收前置命令执行，
不违反本轮“只运行自己新增/修改测试”的限制，也不把留白宣称已验证。

### 修复层级与不变量

1. 首屏轮询显式接收 `budget_limited`，自己的切片走空抛 `XBudgetExceeded`；确认过的真实源站错误
   不被时钟推断覆盖。发现切片不再随账号数量压到低于默认8秒，历史队尾不足同样不起跑，
   单条正文预算仍独立。详情与 Article 的异常包装保留预算身份，预算中止不消耗候选失败次数。
2. 采集器把实际观察到的访问阻断持久化为种类、原话、令牌和时间；结构化诊断分别解释启动限流、
   内容限流、安全验证、账号不可用、明确登录失效、会话不可用。错误不再抑制历史问题，
   但也不向未访问来源摊派阻断。stage 为批次保存当时阻断令牌，确认可用读数只清同一令牌，
   防止先前成功回执抹掉后来出现的验证墙。正文中被消费的启动限流通过显式回调交付，不从散文猜。
3. 耗时/统计是观测数据，不是业务凭据；统计保存和序列化异常只写日志告警，不能回写任何来源的
   pending、skipped、incomplete、成功、水位或失败账。内容投递和旧失败清除不再被遥测故障否决。
4. 新增测试从真实默认 `sync.collect` 入口钉当前扫描签名、水位、折叠正文、引用作者、来源改指、
   attach、迟到限流与排尾恢复；显式日期入口单独钉住，不拿旧整批函数的桩冒充默认实现。

### 本轮验证登记

只登记新增 `tests/test_x_intake_review_runtime.py` 和 `tests/test_x_intake_default_path.py`；
包括52账号切片、六类持久阻断、迟到阻断令牌、真实竞争 SQLite 写锁以及默认入口行为。
未修改的 `test_x_channels`、水位、详情、页面政策、timeline 等旧族均未在本轮运行。
证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-gNHc/`（下述短文件名均在此目录）。
基线 `665e4fb`，代码/测试/JS 共15份 SHA-256 存于 `tested-code.json`，清单自身 SHA-256：
`3e189ddcae70f6c739014e543da00a3638e2a5e492a12b340cedf92b9c193e70`。
每份均通过 AST/内存编译（Python）、Ruff（14份 Python）、JS 语法、700行限制与差异空白检查。

首次登记 `6_lsADJMsOBQ` 的真实日志为30项、1.078秒、两失败；均为新增夹具调用错误：
Article 后路收推文 URL 再转文章页，不能直接传 `/i/article`；更新订阅须传第三个 `source_id` 参数，
body 中的 `id` 不会更新旧订阅。修正参数并保留预算异常透传、改指后旧内容不得入库的断言，
随后登记 `qrPfJxd897DF`，真实日志30项、1.069秒、全部通过（此时尚未补下面的 JS 反例）。
日志分别在任务 `regression/6_lsADJMsOBQ.log`、`regression/qrPfJxd897DF.log`。

真实截图又发现一个本轮消费者错误：Python 的空阻断对象 `{}` 在 JS 为真，健康来源也显示
“访问受阻”。修正的是阻断记录的类型判别（`block.kind`），不是隐藏错误文本；新增 Node 执行的
渲染契约覆盖字段缺失、null、空对象与六类真实阻断。最终登记 `9EEbitXvMECE` 的真实日志
31项、1.090秒、全部通过，无跳过；日志：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/9EEbitXvMECE.log`。
本轮两模块的未过滤原始命令，实际 cwd：
`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-gNHc-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-gNHc-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-gNHc-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake_review_runtime tests.test_x_intake_default_path -v
```

### 本轮真实 collect / API / 页面证据

- 全新临时 KB_DATA_DIR、vault、videos，socket 禁止外域连接，调度器禁用；仅 X 首屏/详情、会话、
  配置就绪和冷却边界替身，实际运行 `sync.collect`、候选队列、批次回执、入库事务、FastAPI 与静态页面。
  首次仓库外服务脚本漏清新库自带的默认订阅，数量断言失败；清默认订阅后只种9个合成来源，
  未改产品来迎合冒烟。见 `serve.py`、`smoke-environment.json`、`validation-history.json`。
- 同轮健康账号先取得内容，另一账号随后内容限流；健康账号仍成功并清旧失败/退避，阻断只属于
  实际看到429的账号。另分别实际 collect 产生启动限流、安全验证、账号冻结、明确登录失效和会话故障；
  正文阶段走真实 `x_post.complete/fill`，详情边界返回启动429，候选仍欠账且阻断可持久读取。
- 遥测阶段持有另一条真实 SQLite `BEGIN IMMEDIATE` 写连接，再调用原 `save_metrics`，实际得到
  `database is locked`；该来源仍新增1条、进入 succeeded_ids、清旧连接失败与退避。
  总计2条成品入库，正文预览未混入成品；见 `sync-summary.json`、`subscription-ledgers.json`、
  `database-evidence.json`、`progress-evidence.json`。
- 清预取缓存后 health、social overview、items、intake-care 均200；24路真实 HTTP 并发 overview 全200。
  七个实际阻断来源分别有对应结构化问题和原话（含正文启动限流）；生成的重试描述仍只点名当前来源，
  保留 rev/scope_sig，不含 extract 阶段，未执行任何重试。见 `api-summary.json`、`api-diagnostics.json`、
  `http-evidence.json`。健康来源可以仍欠历史覆盖，但不得继承别人的访问阻断。
- 真实页面9张卡片显示六类短阻断状态、独立正文欠账与最近成功；健康及统计锁来源无虚构“访问受阻”。
  只读打开六类问题详情，原话、含义与各自建议均匹配；无 JS 异常、横向溢出、外域或非 GET 请求。
  已查看 `sources.png`、六份 `care-*.png` 对应详情（含裁剪 `detail-*.png`），保留 DOM/网络证据
  `sources-evidence.json`、`page-evidence.json`、`page-final-network.json`。
  首轮截图暴露的空对象误报及当时过宽的页面断言保留为 `first-sources.png`、`first-page-*` 等；
  加强健康来源反断言后重新运行页面冒烟通过，不以首轮脚本的绿色输出替代目视检查。

### 浏览器通道、清理与留白

本审查轮重新尝试具名扩展“🔎 大V审查阻断验证”，`visible:false` 新建后台标签实际返回
`Capability is not available: visibility`，创建前拒绝。没有省略后台条件重试，才降独立临时 profile
的 chrome-headless-shell，始终 headless，仅本机 GET；未连接用户 Chrome/CDP、普通标签或 cookie，
未激活窗口。页面仅导航订阅管理、问题与待办及详情，未点同步、登录、保存、重试或外链。
通道与原始原因见 `browser-channel.json`、`first-browser-channel.json`。

两次独立无头进程及 profile 均已销毁，服务正常 SIGTERM 退出，临时 KB 根已删除、48999端口无监听；
见 `browser-cleanup.json`、`first-browser-cleanup.json`、`server-cleanup.json`。
本轮未访问真实 X、生产数据、用户登录态或模型；源站实时风控、原生 cursor、51账号八分钟吞吐未验证。
默认入口新增契约不替代既有 X 族兼容回归：未修改的旧测试及全仓回归仍留验收，不能宣称它们已通过。

## gNHc 第2轮：阻断事件的阶段所有权与可靠交付

本轮基线 `8d03424`，对应报告 `free-review/gNHcRvki9H1R/round-2/report.md`。
F1—F3 在原自动 X 链路内修根因；非阻断清单不扩展。F4 的既有回归留白属实，但执行阶段必须服从
“仅本轮新写/修改的测试可执行并登记”的明确协议，不借改动旧测试来解锁整族运行，提交后正式请求裁定。

### 根因与不变量

1. **独立阶段共用一份全来源 block**。旧 `package()` 将正文/历史阻断作为发现成功与水位的否决票。
   修复在状态生产/确认层而非隐藏 UI：结构化事件按 discovery/history/body 保存；批次冻结自己阶段的令牌，
   时间线回执只清自己，正文实际完成才清正文，combined 只拥有发现与历史。迟到事件的不同令牌仍受保护。
   清除保存时间化回执，旧摘要不得复活它；旧单事件兼容为发现，保留原话，不伪造历史阶段。
   诊断正文原因附正文欠账，发现/历史同类问题的 id 与步骤按阶段区分；卡片直接显示阶段。
2. **唯一事实依赖一次零等待写盘**。`mark_block` 现在先保留原观测，再尝试专用状态写；本机维护锁争用
   只警告，不能把远端事实丢掉或转成采集本机故障。所有结果（含 error/skipped）均可带身份/通道绑定的
   observation；遥测指标之外独立交付到既有 `feed_runs.summary.x_observations`。读状态回放事件，
   不限于最近30轮指标，不会因31轮无关同步抹掉阻断；实际暂存时合并入来源投影。恢复的清除回执参与回放，
   改指、订阅修订或切通道不能继承旧观测。
3. **把默认阶段时限当成外部预算缩短**。原8/20秒恒小于原生50秒，未知账号的错误出口不可达。
   `_harvest_one` 接收本阶段配置时限及外部缩短身份；公平分配的有效时限与配置时限保持分离。
   完整切片不可读仍走账号错误；真正缩短才是 `XBudgetExceeded`。另以来源/阶段/UUID运行身份记录首屏等待，
   连续三轮未确认升级所属阶段真实故障，防止无限占预算；只有发现故障升级来源失败与退避，
   历史故障保留阶段提示但不否决发现投递。确认只重置自己的阶段，首屏后的正常预算耗尽不罚。

### 本轮测试登记

只运行有实质修改的 `tests.test_x_intake_review_runtime`（21项）和新增
`tests.test_x_intake_phase_delivery`（21项）；未运行未改的 `test_x_intake_default_path` 或其他旧套件。
哨兵 `xauYwXqAP0qP` 真实日志：**41项、1.539秒、全部通过，无跳过**，
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/xauYwXqAP0qP.log`。
最终逐类核对补齐历史阶段 `XAccountUnavailable` 的同根错误出口：只属于历史的账号/登录异常仍保留
阶段阻断，不能通过通用 `result.error` 再次否决已确认发现。新增六类历史阻断逐一真实 collect 的反例后，
登记 `D2NgbEH5Lvl5`，真实日志 **42项、1.702秒、全部通过，无跳过**：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/D2NgbEH5Lvl5.log`。
范围涵盖阶段相互隔离、同阶段迟到令牌、实际正文完成、combined、旧记录兼容、真实维护锁争用、
error结果交付、清缓存回放、31轮无关运行与恢复清除、改指/通道隔离、完整8/20秒与真正缩短、
三轮升级、同轮去重/阶段重置、老库加列、JS 阶段渲染；52账号夹具使用独立 status ID 和6秒真实等待判据。

实际 cwd：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。未过滤原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-gNHc2-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-gNHc2-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-gNHc2-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake_review_runtime tests.test_x_intake_phase_delivery -v
```

本轮证据根：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-gNHc-2/`。
测试前12份代码/测试/JS的 SHA-256 在 `tested-code.json`；清单自身 SHA-256：
`924188dbbd8d7751178581f6796943f4cc8535f2bc760e9ad988983f554af9ca`。
41项时的代码清单另存 `tested-code-41.json`，最终代码再次做了下述全新隔离 API/页面点检。
Ruff、JS语法、AST/内存编译、单代码文件700行上限、diff空白检查均通过。不是全族回归通过声明。

### 真实 collect / API / 页面点检

- 仓库外 `serve.py` 起全新临时 KB 数据/vault/videos，禁调度器并阻断外域 socket；仅远端X/会话/详情、
  就绪与冷却边界替身，真实执行 `sync.collect`、候选/批次/入库事务、FastAPI 和静态页面。
  首次脚本漏导入用于核对动作描述的 `actions`，退出且清理临时数据；仅补脚本导入后重跑，产品不改、断言不减。
  见 `validation-history.json`、`smoke-environment.json`。
- 五个来源：健康、正文启动429、仅历史访问429、维护锁期间安全验证、完整首屏未知。前3个均成功清旧错误/
  失败/退避并推进发现水位；正文候选仍待补且原话归正文，历史429仍保留历史问题，不扣留发现成功。
  实际完成2条入库，正文预览不入库。见 `subscription-ledgers.json`、`sync-summary.json`、`database-evidence.json`。
- 用真实独立文件描述符持有 `fcntl.flock`，原 `set_block` 确实得到 LockBusy；专用 block 原值为空，
  运行摘要仍含安全验证原话，清预取缓存后 API 和诊断仍正确显示人工处理，未误报本机采集失败。
  对未知页面走真实 `_harvest_one`/首屏判因、仅虚拟时间与DOM边界；完整默认切片后 failed_ids/失败1次/未来退避均存在，
  不再无限预算待续。见 `api-summary.json`、`progress-evidence.json`、`scan-evidence.json`。
- health/social overview/items/intake-care 均200；24路真实 HTTP 并发 overview 均200。
  四类详情原话、建议、单来源 rev/scope_sig 与无 extract 阶段已核对，未执行重试。
  五张订阅卡显示阶段阻断及独立最近成功，四份问题详情分别为正文、历史、写锁安全验证、未知首屏失败；
  `sources.png`、`care-body.png`、`care-history.png`、`care-lock.png`、`care-unknown.png` 已逐一查看。
  JS异常、横向溢出、外域/非GET请求均为0，见 `http-evidence.json`、`page-evidence.json`、`page-final-network.json`。

### 浏览器通道、清理与留白

本轮两次新尝试扩展具名会话“🔎 大V阶段根因验证”（含最终代码复核），保持 `visible:false`，均在实际创建前返回
`Capability is not available: visibility`。未去掉后台要求重试，才降独立临时 profile 的
chrome-headless-shell；始终无头、仅 localhost GET，不接管用户 Chrome/CDP、普通标签、cookie 或激活窗口。
只导航订阅管理、问题与待办及详情，未点同步、登录、保存、重试或外链。见 `browser-channel.json`。
两次自建浏览器进程/profile、服务与临时 KB 根均已清理，48999无监听；见 `browser-cleanup.json`、`server-cleanup.json`
及保留首次点检的 `first-*` 证据。

真实 X 风控、用户登录态、生产数据、模型、原生cursor和51账号八分钟吞吐未验证。既有 X/历史套件及页面回归
未补跑，不能用本轮冒烟或42项定点结果冒充旧回归全绿；这些仍是验收前置回归留白。F4请求由用户裁定，
不会擅自增加审查轮数或执行验收，报告其他非阻断项也未扩展。

## P0g 首轮审查：阶段结论在出库边界隔离（2026-10-31）

本轮完整读取 `free-review/P0gZuUe0mZf5/round-1/report.md`（268行），接受两条阻断，
只修自动 X 的阶段结论/投递出口及诊断元数据消费；不扩大报告列出的非阻断项。

### 根因与不可回退的不变量

1. **阶段异常与来源结果共用一个字典**。上一轮仅在部分异常分支判断 phase，通用页面/CDP异常及
   首屏三轮升级仍写来源 error，package 再因 error 跳过持久投递，令健康发现被历史故障永久扣留。
   现在异常生产者统一写 `outcomes[source_id][phase]`，浏览器与 feedgrab 共用同一个入口；
   `results` 只由 package 生成。来源 error 仅从拥有 discovery 的阶段错误升级，combined 明确拥有发现。
   package 无条件读取持久 delivery，即使真实发现失败也组装 entries，且不提前消费出库资格。
   历史阶段 error 不升级来源失败，原话以 `phase_errors` 和带阶段名的 note 保留，进入持久 last_note；
   已确认发现的成品、成功清账及水位照常提交。真实发现错误、三轮发现首屏升级仍进入失败与退避。
2. **把展示字典当成完整枚举**。采集器交付未知 kind 时，诊断直接索引 `PROBLEMS[name]` 导致整页500。
   消费边界现在用 get 和通用模板降级：标题为原始 kind，建议为空，原始证据、阶段及来源范围不变。
   六类已知阻断继续使用 owner 的同一份元数据，未新增分类副本、路由或执行射程。

### 本轮定点测试及静态校验

仅运行本轮实质修改的 `tests.test_x_intake_phase_delivery`（21项）和新增
`tests.test_x_intake_outcomes`（11项）。登记哨兵 `yCW3Ea-erVb9` 的真实日志为
**32项、1.025秒、全部通过，无跳过**：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/yCW3Ea-erVb9.log`。
其中包含连续四轮真实 collect：发现逐轮独立新推文，历史走真实 `_harvest_one`/首屏轮询、虚拟时间和
8.5秒外部切片；第三、四轮诚实报历史故障，但每轮新增1条、清旧失败/退避、推进水位，最终4条入库且无出库积压。
另含历史页面/CDP故障与发现反例、出库始终读取、combined失败、阶段 skipped 不覆盖发现 error、
未知新/旧格式阻断的真实 API 与单来源动作描述、六类已知元数据、未知不可重试条目的空建议。

首次登记 `ymJ6_NPyrB3A`：32项、1.194秒，3个断言失败和3个子例错误；原因是新测试把原始观测水位
与本地化发布时间比较，以及误传 `scope_of(problem_id=...)`。分别改为 `x_observed_at` 与实际 `problem` 参数，
不改业务实现、不删减业务断言。首轮日志及 `validation-history.json` 保留原结果。

实际 cwd：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。原始登记命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-P0g-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-P0g-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-P0g-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake_phase_delivery tests.test_x_intake_outcomes -v
```

本轮证据根：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-P0g/`。
5份代码/测试 SHA-256 位于 `tested-code.json`；清单自身 SHA-256：
`2907afc5adabc3e5b08af6386eb901f3f366fff2c7f1eed54e781801aa9934d0`。
Ruff、AST/内存编译、700行上限、diff空白检查通过；本轮未改 JS。
最终提交与代码哈希、最终 HEAD 的真实测试登记单独存本证据根，不以旧树结果冒充新树结果。

### 隔离 collect、API 和真实页面

- `serve.py` 使用全新临时数据/vault/videos，禁调度器、外域 socket；远端 X/会话、就绪和冷却边界替身，
  但运行真实 collect、候选/批次确认、入库事务、API 和项目静态页面。不访问生产数据或真实登录态。
- 六来源包括健康、历史页面/CDP故障、四轮历史首屏未确认、未知阶段阻断、未知旧版阻断、已知安全验证。
  前三者共7条成品入库，错误/失败次数/退避清零，发现水位推进，已确认出库无滞留；历史原话留在 last_note。
  对未知种类持久写入新/旧格式，清空预取缓存后诊断仍200；未知标题为原 kind、建议为空，已知安全验证建议保留。
  三份动作描述仍点名单来源 rev/sig，不带 extract，未执行动作。见 `sync-summary.json`、
  `subscription-ledgers.json`、`database-evidence.json`、`progress-evidence.json`、`api-summary.json`。
- health/social overview/items/intake-care 四接口均200；24路真实并发 HTTP GET 均200。
  六张订阅卡显示7条已收录、独立最近成功、历史错误原话；未知新/旧种类与已知安全验证三份详情可读。
  `sources.png`、`care-future-phase.png`、`care-future-legacy.png`、`care-known.png` 已逐一实际查看。
  JS异常、横向溢出、外域和非GET请求均0；见 `http-evidence.json`、`page-evidence.json`、`page-final-network.json`。

### 浏览器通道、清理及留白

本审查轮重新尝试扩展具名后台会话“🔎 大V出库根因验证”，`visible:false` 在实际建标签前被拒：
`Capability is not available: visibility`。未去掉后台要求或接管普通标签，才降独立临时 profile 的
chrome-headless-shell；始终无头，仅本机 GET。未连接用户 Chrome/CDP、cookie，未激活窗口或切验证标签。
页面只导航订阅管理、问题与待办和详情，未点同步、登录、保存、重试或源站链接；见 `browser-channel.json`。
自建无头进程55018及临时 profile 已清理，服务51413正常停止且临时 KB 根已删除，48999无监听；
见 `browser-cleanup.json`、`server-cleanup.json`。

既有未修改 X/历史套件和全量回归未运行，统一留验收前置执行；不把32项定点测试或本轮冒烟称为全族通过。
真实 X 风控、用户登录态、模型、生产数据、原生游标及51账号八分钟吞吐未验证。
报告其余非阻断项不改，不自行追加审查轮次或执行验收。

## P0g 第二轮审查修复：投递与来源失败独立提交

本轮完整读取 `free-review/P0gZuUe0mZf5/round-2/report.md`（269行），接受唯一阻断类问题。
基线 `18223fdb742be03d7f8d3a0bf3c6da93c381c6af`，只修改自动 X 成品出口、同事务失败记账及对应测试；
不把来源故障改判成成功，不增加 ready 诊断计数，也不扩报告的非阻断项。

### 根因与边界

上一轮只让 package 从持久队列组装 entries，`take` 仍见 error 就抛异常；组装结果没有走到真正入库。
现在仅对自动 `x_intake` 且 entries 非空的结果放行条目，以原 `XCollectionError` 格式的 `meta.x_failure`
单独传失败；空成品或旧显式区间仍照原样抛错。发现页面/CDP故障、第三轮起的发现首屏升级以及 feedgrab
combined 上游故障共用这个出口，不在各异常分支分别补丁。

存盘在同一事务完成条目、真实 outbox 确认及原失败策略写账。失败存在时忽略水位补丁、保留 last_success，
只增加真实失败/退避；阶段 note 与跳过提示照常保存。事务提交后才向 summary 记失败，不重复报 notice，
不把已入库但失败的来源记成 succeeded 或 deferred。后置失败写账出错会连条目与确认一起回滚，下轮仍可投递。
原失败 SQL/1200字符上限/退避及摘要提取到 `source_failure.py` 共用，非 X 的失败入口签名与身份检查不变；
这是避免 sync 超过700行所需的窄拆分，不改其它采集器行为。

### 定点测试与代码身份

仅本轮实质修改的 `tests.test_x_intake_outcomes`（11项）与新增
`tests.test_x_intake_delivery_verdict`（12项）执行并登记。哨兵 `_wsgpUvCJhsU` 的真实结果为
**23项、0.725秒、全部通过，无跳过**，日志：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/_wsgpUvCJhsU.log`。
原先停在 package 的断言已改成实际 take/save 后 feed_items 增加、outbox 清空及真实失败仍在。
新用例覆盖三条触发入口的真实 collect、多轮去重、原始失败/notice各一次、API后果、保存失败与后置失败写账
整体回滚、改指隔离、水位/成功账保护，以及空结果/旧路径/原失败上限与退避兼容。

实际 cwd：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`；登记的未过滤原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-x-P0g2-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-P0g2-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-x-P0g2-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_x_intake_outcomes tests.test_x_intake_delivery_verdict -v
```

证据根目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/x-intake-review-P0g-2/`。
`tested-code.json` 记录本轮6份代码/测试的逐文件SHA，清单自身SHA256为
`8169c0a28e3414436340c0b8465369c8b0a32c809aa6fe0ef4c88c91f9b57e73`：

| 文件 | SHA256 |
| --- | --- |
| `kb/feeds/source_failure.py` | `c29e1a53e3b46e75d674a0617329551ca158c80129cc3607d394c66df2315de4` |
| `kb/feeds/sync.py` | `f3136a07bd980072aa09ff37e79ecd8038438fb1c400f212d0b05850187e1956` |
| `kb/feeds/x_channels.py` | `59744a1434b494653285556c43b227c85d467c21be8f5b0237c2d28adc40ec60` |
| `kb/feeds/x_intake_commit.py` | `c169d58ce8c2c3b47ec9695619d0782f9cc5584663c9f0332169ece72dd8f8ed` |
| `tests/test_x_intake_outcomes.py` | `7047c7d128895aed9f759559c1c511967a8baa66b43b25cb69584269b55f516b` |
| `tests/test_x_intake_delivery_verdict.py` | `87b71a5144cc7dae8fb975ad73b5aa51e6f93d026a3fe840547235d3cace04fb` |

六文件 Ruff、AST/内存编译、700行检查与 diff 空白检查通过，sync 为691行。
最终提交的逐文件对应与最终 HEAD 定点登记以同目录 `commit-evidence.json` 记录，不复用旧树测试作最终证据。

### 隔离真实 collect、接口与页面

本轮新建 `serve.py`、`page.mjs`、`browser-smoke.py`，使用全新临时 KB data/vault/videos，
禁 scheduler、Python 外网 socket 及页面非本机/非GET请求；仅远端 X/session、配置就绪、冷却边界替身，
collect、持久队列、批次确认、事务及HTTP接口均为真实实现。首屏升级走真实 `_harvest_one`/首屏轮询，
用虚拟时间/DOM固定输入复现20秒账号配置被8.5秒外限缩短，不访问真实 X 或睡满窗口。

- 四来源共9条成品真实入库：健康1条清旧账，发现页面/CDP/响应故障三轮各1条，发现首屏四轮各1条，
  feedgrab 上游失败投递旧确认成品1条。所有 outbox 均清空，API items 包含全部9个URL。
  三类故障最终失败次数分别3/2/1，退避均保留，失败轮不推进旧成功时间或水位，原始错误照存。
  首屏前两轮尚未升级，其成功账与失败第三/四轮分开断言，不误把所有预算读数当真故障。
- 清空预取缓存后 health/social overview/items/intake-care 四接口均200，24路并发实际HTTP GET均200。
  单来源动作描述仍绑定 rev/sig、无 extract，未执行重试或其它动作。数据库/摘要/API/账本证据见
  `database-evidence.json`、`sync-summary.json`、`api-summary.json`、`http-evidence.json`、`subscription-ledgers.json`。
- 真实页面内容列表显示9条，四张订阅卡显示1/3/4/1条；后三张保留红色失败与原话，健康对照正常。
  内容列表、订阅管理和三份故障详情截图 `feed.png`、`sources.png`、`care-{browser,budget,feedgrab}.png`
  均已逐一实际查看；JS异常、横向溢出、外域及非GET请求均0，见 `page-evidence.json`、`page-final-network.json`。

首次外部冒烟夹具误把第三轮失败应保留的成功时间设为初始化值，忽略了前两轮未升级时已有正常投递成功。
仅修外部断言为前轮账本，并给各轮独立时间戳防止错误更新被同秒掩盖；未改产品或已通过的23项测试。
首个失败日志和临时根清理证据保留于 `server-first.log`、`server-first-cleanup.json`、`validation-history.json`。

### 浏览器与清理、留白

本审查轮实际重新尝试具名扩展后台会话“🔎 大V投递与失败验证”，`visible:false` 创建前即被拒：
`Capability is not available: visibility`。未去掉后台限制，才降独立临时 profile 的 chrome-headless-shell，
始终无头、仅本机GET；未连接用户Chrome/CDP/普通标签/cookie，未激活窗口或切换验证标签。
只查看列表、展开失败说明、导航待办及详情；未点同步、重试、保存、登录或外部链接。
自建无头进程92956及profile已清，服务84789正常停止且临时KB根已删除，48999无监听；
见 `browser-channel.json`、`browser-cleanup.json`、`server-cleanup.json`。

未运行本轮未修改的 phase_delivery/runtime/default_path/其它X或历史套件及全量回归，统一留验收前置；
不将23项或本轮冒烟称为全族通过。真实X风控、用户登录态、模型、生产数据及51账号吞吐未验证。
原通用连接详情仍假定“一条内容都没取到”，在混合状态下文案过强，本轮只核对原话/退避/条目实际后果，
未扩到该既有展示模板；报告所列非阻断问题同样保留。未自行追加审查轮次或执行验收。
