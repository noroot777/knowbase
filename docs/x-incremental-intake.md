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

- 新增四张表：来源覆盖/尝试账、按 status ID 共享的候选表、订阅修订绑定的归属/outbox、最近 30 次阶段统计。
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
- 发现未读完、历史缺口、正文待补、未轮到、本机错误都不能结清旧失败或旧成功账。
  可成功入库的成品仍正常入库；纯说明和真正的未完成分开显示。

## 预算与限流

默认整轮 480 秒；发现/历史/正文分别最多 240/120/120 秒，不是再加启动、冷却、保存和关闭时间。
关闭预留 5 秒，所有阶段取全局剩余预算；投递时也检查同一个绝对截止时刻，来不及的成品留在 outbox。
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

## 本轮执行证据（2026-10-31）

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
