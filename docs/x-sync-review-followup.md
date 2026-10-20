# X 审计与同步契约承接修复

日期：2026-10-20。任务：`AACErxUciYs8`，开工版本：`39882d0`。

## 范围

已完整读取 `7DUhYZrZy1lb/round-1/report.md`，只承接用户指定的四条意见。
没有重新打开原任务，也未处理报告中的统计、退出码、报告原子性等其它事项。
账号生命周期分类、合法空正文、PostGone 计数留痕、probe/登录导入适配与完整 Article 资格均未改动。

## 四条修复

1. **审计欠账收口**：局部修复后仍先排队复查，不假称整篇修复成功。
   同版得到与欠账匹配的安全扣留结论后，把 `remaining` 转为 `retained`，默认不再反复开页。
   同版累计两次安全保留结论（含 `identical`）也收口，不要求相同或相邻，避免另一种永久欠账。
   失败、暂缓和首次相同读数仍保留重试能力；正文换版、`--recheck`、`--url` 可重新核对并补齐。
   正文比较策略版本不变，证据检查、CAS、译文作废规则均不放宽。
2. **HTTP 与空态冲突**：共享 `problem` / `unavailable` 判据保留原始 DOM 证据，
   但当前内容接口的 429/4xx/5xx 不允许被空态文案解释成“不存在”。
   详情不产生错误的 PostGone，时间线不宣告账号不存在，回填失败收据仍可在下一轮恢复。
   429 与安全关卡仍保持批次停止；辅助接口错误仍只作证据，不否决正文。
   历史 `unavailable` 回执没有新判据标记，保守重查一次；新判据确认不可用后正常收口。
   仅重开这一类可能受旧冲突策略影响的回执，不批量重开其它结论，也不改已有正文。
3. **提示与未完成分离**：采集结果的 `incomplete` 继续进入元数据 `note`、
   `subscriptions.last_note`、`summary.partial`；纯提示独立进入 `notice`、`last_notice`、`summary.notices`。
   前端同时展示两类说明，但只有 `last_note` 触发“部分同步”和“下次接着读”。
   已验证新列的旧库迁移、实际入库、水位、API 返回与运行汇总；成功重写两字段、改链接清空、区间补抓保留。
   不按中文文案猜测历史字段语义；旧库中曾混存的提示在下一次成功同步时由结构化结果重写。
4. **时间线部分接收与停止**：普通核心请求失败连续确认两次后立即结束等待。
   有卡片时只采集一次，将解析合格且通过既有正文补全门槛的条目入库，标未完成并冻结原下界，
   不再滚动或把整个账号预算耗完；没有卡片则报账号请求失败，后续账号仍能运行。
   完整目标详情仍须身份匹配、未折叠及正文/证据两次稳定；不把时间线缓存卡片等同于完整详情。

## 初次实现验证（5177e72）

- 最终定向测试：`HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest
  tests.test_x_post_audit tests.test_x_page_policy tests.test_x_review_contracts
  tests.test_x_page_state tests.test_x_post_repair_contract -q` → **61 tests, OK**。
- 最终全仓测试：`HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest discover -s tests -t . -q`
  → **2724 tests, OK (skipped=110)**，116.498 秒，退出码 0。
  这是 2614 项执行通过、110 项跳过，不把 2724 项全部称为执行通过。
- `uvx --offline ruff check` 覆盖全部变更 Python 文件与新增测试 → **All checks passed**；
  `node --check kb/static/js/sources.js` 与 `git diff --check` 均通过。
- 已完成四种内存反向验证：分别恢复基线的审计 `record`、共享 `problem`、通道 `take`、
  时间线 `_wait_state`，对应回归分别红 2、3、1、2 个断言；源文件没有为反向验证而改写。
- 所有数据验证均使用临时 SQLite 库和内存 CDP 事件，不读取或写入生产库、真实 vault、cookie 或 X 页面。
- 所有修改的代码文件均不超过 700 行。

## 初次实现浏览器通道与证据

使用第 1 档 Chrome 扩展，先命名 `🔎 X 同步四项修复验证`，再创建本任务的后台验证标签。
没有接管用户现有标签、主动激活窗口或切换验证标签，没有连接用户 Chrome 的调试端口。
创建时显式 `visible:false` 被扩展以 `Capability is not available: visibility` 拒绝，
随后使用该扩展正常支持的具名分组新标签 API；没有因此降级或调用激活接口。

页面来自本任务隔离服务 `127.0.0.1:58821`，使用实际 API、数据库与前端脚本。
浏览器只导航到“大V观点 / 订阅管理”，没有点击同步、保存、登录或其它数据写入动作。
三行页面断言均通过：纯提示为“已同步”且没有“接着读”；未完成加提示为“部分同步”且两句都保留；
普通完成为“已同步”。截图：`screenshots/x-sync-notices.jpg`（1452×806，JPEG）。

扩展可用，因此没有降级到独立无头/有头浏览器。为了不绕过通道选择规则，终端测试以
`HEADLESS_SHELL=/nonexistent` 禁用要求独立无头壳的既有浏览器探针；它们的跳过数如实列入完成记录，
不把跳过当作通过。本次界面改动另由上述第 1 档真实浏览器验证。

验证标签已关闭，隔离服务已停止；临时库和伪 cookie 随隔离目录清理。
没有触碰别的任务的工作树、服务或浏览器会话。

## 第 1 轮审查修复（2026-10-20）

本轮只处理审查报告的两条必修及“改链接”测试盲点；历史混存提示的过渡策略不变。

- 审计收据按 `(item_key, version)` 持久化 `safe_rechecks`。安全保留结果累计两次即收口，
  不要求结果相同或相邻；`failed`、`stale` 不清零，`deferred` 不覆盖原回执。
  匹配原欠账的安全扣留仍可立即收口，换版从零开始，旧回执的最后一次安全结果计一次。
  保留失败重试、显式重查和后续补齐机会，不改正文证据门、比较策略版本或 CAS。
- 真实 DOM 回归区分核心 `UserTweets` 与辅助 `UserByScreenName`：核心 404/503 为失败，
  429 为限流，不能被空态变成不存在；辅助错误不否决已确认的空态。
  增加真实 `TweetDetail` HTTP/空态冲突与下一次成功导航恢复检查。
- 改链接回归使用 `source_id` 更新原订阅，再从原 id 读取并核对链接和两类说明清空。

### 本轮浏览器通道

先使用第 1 档 Chrome 扩展，命名 `🔎 X 审查修复验证` 后新建后台标签。
在隔离服务 `127.0.0.1:57659/review-dom` 读取实际空态页面及原 `_STATE_JS` 输出，
确认 `ready=complete, empty=true, challenge=false, articles=0`。

扩展连接和本地页面均正常，但公开 API 仅提供只读 DOM 求值、定位器和页面日志；
实际能力清单中浏览器只有 `viewport/management`，标签只有 `pageAssets`，
没有原始 CDP 命令/事件订阅或可供产品 `Session` 接入的协议传输。
因此它不能执行本轮必须补跑的 `Network.responseReceived` → `Responses` → 详情/时间线消费集成验证；
仅复用 DOM 加内存事件会重复审查指出的覆盖缺口，不能称为真实 HTTP 浏览器回归。
在记录这一协议验证能力限制后，相关既有测试改用第 2 档独立 headless shell 和临时 profile，
只访问进程内 `127.0.0.1` 夹具，不连接用户 Chrome 调试端口或读取 cookie，不启动有头浏览器。
扩展标签不接管用户已有标签、不主动激活窗口或切换标签；验证结束后关闭自建标签并清理隔离服务。

### 本轮验证结果

- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest
  tests.test_x_audit_retention tests.test_x_post_audit tests.test_x_page_policy
  tests.test_x_review_contracts tests.test_x_page_state tests.test_x_post_repair_contract -q`
  → **64 tests, OK**。新回归覆盖默认/写回入口、不同安全结果交错、失败/暂缓/过期不清零、
  旧回执续接、正文换版重置、默认结束后不再打开浏览器，以及显式重查后真实正文写回和版本递增。
- `HEADLESS_SHELL=<本机 chrome-headless-shell> uv run --offline python -m unittest
  tests.test_x_post_dom -v` → **31 tests, OK，无跳过**，4.008 秒。
  使用实际浏览器发出的本地 HTTP 请求及产品 CDP 事件链，非内存事件替身；
  核心/辅助接口、404/429/503、详情/时间线和下一次成功导航恢复均通过。
  其它测试未为此开启独立浏览器，不以协议能力限制绕过可用的扩展界面通道。
- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest discover -s tests -t . -q`
  → **2728 tests, OK (skipped=111)**，117.095 秒，退出码 0。
  该次执行 2617 项；111 项跳过中，本轮另行执行上面的 31 项真实 DOM 检查，仍有 80 项未执行，
  不把跳过项称为通过。最后补强的正文写回/版本递增断言由上述 64 项定向复跑确认。
- 原审查 `repro_audit.py` 原样运行：仅在内存恢复旧 `record` 时，两种交错模式 12 轮后仍有候选；
  恢复本轮实现后，`blocked_content/failed` 在第 3 轮、`blocked_content/identical` 在第 2 轮收口。
  新增测试另断言收口前后正文行完全不变；未用计数替代正文证据或 CAS。
- 本轮全部变更 Python 文件通过 `uvx --offline ruff check`，`git diff --check` 通过；
  变更代码文件最长 510 行，均不超过 700 行。

日志及扩展 DOM/能力清单：`/tmp/kb-x-fix-AACErx-round1/`。
本轮没有界面改动，沿用初次实现的界面截图；本轮协议验证不冒充 X 在线实测。
自建扩展标签已关闭，57659 端口已关闭，隔离数据目录已删除；
独立无头浏览器及临时 profile 由测试清理，未触碰生产库、vault、cookie 或用户现有标签。

## 第 2 轮审查修复（2026-10-20）

本轮认可并处理两条生命周期问题，不扩展账号分类、登录导入、Article 资格或历史提示迁移。

- 时间线通过 `TimelineState` 统一消费 reset、collect、scroll、settle 和收尾期间的共享错误。
  普通核心失败保留合格条目、标记未完成并冻结旧水位；核心 429/安全关卡仍停止批次。
  在正文补全或下一账号导航前再检查，导航协议调用期间暂存旧 loader 的响应，
  导航返回前消费后再释放，不能由新导航重置抹掉；迟到错误可修正尚未入库的上一账号结果。
- 审计在远端读取前持久化代次；默认重试共享同版代次，安全进度及终态单调合并。
  `--recheck`/`--url` 在实际开始时创建新代次并清除旧安全累计，旧代次迟到结果不能覆盖新意图。
  收口前已选中、收口后才开始的默认工作直接跳过；同代迟到失败/stale 不重新打开终态。
  新证据允许的正向修复和正文 CAS 仍可成功，正文换版独立从零核对，不永久忽略显式重查失败。

### 本轮浏览器通道

先选择 Chrome 扩展，命名 `🔎 X 第二轮修复验证`，仅新建后台分组标签。
扩展访问隔离服务 `127.0.0.1:58137/review-dom` 正常，实际 DOM 状态仍为
`ready=complete, empty=true, articles=0`。本轮重新取得能力清单：浏览器为
`viewport/management`、标签为 `pageAssets`，仍无可供产品 `Session` 使用的 CDP 传输和
`Network.responseReceived` 订阅。因此扩展不能完成必须验证的迟到 HTTP 事件集成链，
不是扩展断连、页面打不开或登录失败。记录此特定能力限制后，仅真实协议回归使用第 2 档独立
headless shell、全新临时 profile 和 localhost 夹具；不接用户 Chrome 调试端口、不启动有头浏览器。
本轮无界面改动，不重复操作订阅 UI，也不把内存事件测试冒充 X 线上验证。

### 本轮验证结果

- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest
  tests.test_x_late_timeline tests.test_x_audit_epochs tests.test_x_audit_retention
  tests.test_x_post_audit tests.test_x_page_policy tests.test_x_review_contracts
  tests.test_x_page_state tests.test_x_post_repair_contract tests.test_x_collectors
  tests.test_x_watermark -q` → **141 tests, OK**，4.931 秒。
- 迟到事件测试覆盖 reset、collect、scroll、settle、finish、finalize、下一导航前和导航协议调用期间；
  两种通道的核心限流均停止批次，普通失败保留部分条目/旧水位并继续后续账号，辅助错误不扩大作用域。
  临时 SQLite 中另验证实际入库、`last_note/last_error`、水位和下一轮成功恢复。
  导航命令已发出后才收到旧 loader 429 的场景，不能撤回已发导航，但不再读取该新账号或继续后续账号，
  且撤销尚未落库的上一账号成功结果；该时序由受控协议工装验证，不冒充远端时序实测。
- 审计测试以线程事件控制两个已选中核对的启动/完成顺序，覆盖 dry-run/写回、迟到失败/stale、
  已选中未开始、显式重查代次隔离、重开终态后的失败重试及迟到正向修复真实 CAS 写回。
  每次回执读取重新打开 SQLite；没有声称做过多进程压力或进程崩溃实测。
- `HEADLESS_SHELL=<本机 chrome-headless-shell> uv run --offline python -m unittest
  tests.test_x_post_dom -v` → **32 tests, OK，无跳过**，3.910 秒。
  新增在真实 collect 阶段发起 localhost UserTweets 请求，验证实际 429/503 经产品 CDP 事件链消费，
  不替换 `Responses`、错误判定或采集结果；仅将导航目标映射到隔离页面、控制请求发出时机。
- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest discover -s tests -t . -q`
  → **2741 tests, OK (skipped=112)**，113.873 秒，退出码 0。
  该次执行 2629 项；112 个跳过中另行实际执行上述 32 项，仍有 80 项未执行，不计为通过。
  定向测试是全仓子集，不重复累计。
- 内存反向验证：禁用时间线持续检查后，限流边界回归失败 14 个子用例；恢复上轮审计 `record` 后，
  重叠核对回归失败 2 个子用例，均为预期拒绝退化，产品源文件未为此改写。
- 本轮变更 Python 文件全部通过 `uvx --offline ruff check`；`git diff --check` 通过。
  变更代码文件最长 667 行，均不超过 700 行。

证据日志、扩展 DOM/能力清单和清理记录在 `/tmp/kb-x-fix-AACErx-round2/`。
自建扩展标签已关闭，隔离服务已停止、58137 无监听、临时数据目录已删除；
真实协议测试的独立浏览器、HTTP 线程和临时 profile 已由测试清理。
没有读写生产库、真实 vault、cookie 或 X 在线页面，也未接管用户标签。

## 跨导航响应归属补修（2026-10-20，iZhou9byo3Uc 第 1 轮）

仅承接本轮唯一必修项，不重新修改已通过的审计、提示字段、HTTP/DOM 空态契约。
`Page.navigate` 回复不是旧请求终结：共享接收层分别保留页面 loader 归属和未结束请求的
`sessionId/requestId` 归属。请求收到 `loadingFinished/loadingFailed` 后移除在途记录；
紧凑的 loader/消费者索引保留到本轮会话结束，兼容没有先观测到请求起始的迟到响应。
新页面只使用自己的 HTTP 状态；旧页面核心错误仍交回原账号，不能顺手扣给当前账号。
旧账号的普通失败修正尚未入库的结果并冻结原下界；429 停止余批。已失败、尚未建立
时间线消费者的页面也不能吞掉自己的迟到核心 429。

事件观察者只登记错误和修正归属结果；批次停止在协议命令回复消费完之后抛出，失败命令也经过
相同交接，不因观察者抢先抛错而遗留未消费的命令回复。关闭自有标签/浏览器时不打断清理命令。

### 本轮浏览器通道选择

先用 Chrome 扩展命名 `🔎 X 响应归属修复验证`，新建分组后台标签并重新检查能力。
浏览器仅暴露 `viewport/management`，标签仅 `pageAssets`；公共接口没有原始 CDP 传输、
`Network` 事件订阅或受控事件交付，无法验证本轮必须覆盖的产品协议接收链。
这不是扩展断连、页面打不开或登录墙。自建标签已关闭；仅协议/DOM 回归降级到第 2 档
独立 headless shell、全新临时 profile 与 localhost 夹具，不接用户 Chrome 的 CDP，
不启动有头浏览器，也不访问线上 X 或读取 cookie。

### 本轮验证结果与限制

- 11 个新增内存协议/业务测试覆盖两通道、导航回复后的旧 429/503、有无请求起始事件、
  跨两个新 loader、时间线切详情、导航失败、辅助错误、请求完成/取消、其它 session 的终结事件、
  未建立时间线的失败页面及最终关闭时收到错误。三订阅真实 `prefetch → take → _save_result`
  验证 429 不写新条目、不读 Last、旧水位保留；503 的原账号条目仍入库但留旧水位和未完成说明，
  其余账号正常前进；下一轮成功清空说明并继续推进。新账号首次 evaluate 已发出后才收到旧 429
  时不能撤回该命令，但回复消费完即停止，不再读取该账号或打开后续账号。
- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest
  tests.test_x_response_ownership tests.test_x_late_timeline tests.test_x_audit_epochs
  tests.test_x_audit_retention tests.test_x_post_audit tests.test_x_page_policy
  tests.test_x_review_contracts tests.test_x_page_state tests.test_x_post_repair_contract
  tests.test_x_collectors tests.test_x_watermark -q` → **152 tests, OK，无跳过**，5.004 秒。
- `HEADLESS_SHELL=<本机 chrome-headless-shell> uv run --offline python -m unittest
  tests.test_x_response_dom tests.test_x_post_dom -q` → **33 tests, OK，无跳过**，5.010 秒。
  新协议测试在真实临时浏览器中请求 localhost 的 UserTweets 429/503，保留 Chrome 实际产生的
  requestId、loaderId 和事件，工装仅延迟 response/终结事件交付到下一账号或详情导航回复之后。
  不伪造 HTTP/协议载荷，不替换消费者或结果；这证明受控交付时序下的真实协议集成，
  **不声称 Chrome 或线上 X 自然产生过这一时序，也不测线上发生频率**。
  四个子场景均断言自有浏览器进程退出且优雅关闭成功，HTTP 服务和临时 profile 由测试清理。
- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest discover -s tests -t . -q`
  → **2753 tests, OK (skipped=113)**，115.249 秒，退出码 0。
  本次实际执行 2640 项；113 个跳过中另行执行上述 33 项，仍有 80 项未执行，不计为通过。
  定向回归是全仓子集，不重复累计。
- 内存变异仅在导航回复处丢弃旧页面/请求索引，新增账号和详情回归捕获 **12 个失败子用例**，
  无异常子用例；原产品文件未为变异验证改写。
- 所有本轮变更 Python 文件通过 `uvx --offline ruff check`，`git diff --check` 通过；
  变更代码文件最长 452 行，均不超过 700 行。

日志在 `/tmp/kb-x-fix-AACErx-ownership/`。本轮没有界面改动，不重复截图或操作生产订阅页面。
扩展自建标签已关闭，独立浏览器和本地 HTTP 测试服务已清理；未接管、导航或切换用户已有标签，
也没有修改生产数据库、真实 vault 或登录态。

## 验收冲突合并自查（2026-10-20）

仅在任务分支 `ash/AACErxUc` 合入目标快照 `main@c9b8223`，没有切换或写入目标分支。
逐一解决 10 个冲突文件，保留主线新增的账号分类、合法空正文、正文来源约束及跳过证据，
同时保留本任务的有界欠账重查、HTTP/DOM 冲突保护、部分同步和跨导航响应归属。
同步持久化、通道元数据和前端沿用主线的 `notice_count/skip_reasons` 契约，
不将提示性说明重新混入未完成状态。结构化账号终态只在没有核心 HTTP 冲突时生效；
迟到 429 清空尚未入库条目时仍保留引用推跳过说明与不可用 URL 证据。

### 合并验证的浏览器通道

先命名 Chrome 扩展会话 `🔎 X 合并冲突验证` 并新建分组后台标签，用隔离临时库启动本地界面。
已核对三种状态：纯提示为“已同步（有跳过）”且不提示接着读；未读完为“部分同步”并独立展示
跳过说明；正常完成为“已同步”且没有跳过说明。控制台错误为空，截图已在执行轨迹展示，
并保存为 `/tmp/kb-x-merge-AACErx/ui-notices.png`。
浏览器只暴露 `viewport/management`，标签只暴露 `pageAssets`；公共接口缺少原始 CDP 传输、
`Network` 事件订阅及受控事件交付，不能完成必须的产品协议链验证。因此仅协议/DOM 测试
降级到独立 headless shell、临时 profile 和 localhost 夹具。扩展本身没有断连或登录墙，
不启动有头浏览器，不直连用户 Chrome 的 CDP，不访问线上 X 或读取 cookie。

### 合并自查结果

- 定点回归：235 tests, OK (skipped=4)，7.552 秒；4 个真实浏览器测试留待独立无头通道执行。
- 新增合并兼容回归覆盖两个通道的跳过说明、不可用证据、旧水位及迟到 429/503 的实际入库，
  并覆盖 missing/suspended/protected 与核心 404/429/503 的冲突，不把它们误判为账号永久终态。
- `HEADLESS_SHELL=/nonexistent uv run --offline python -m unittest discover -s tests -t . -q`
  → **3106 tests, OK (skipped=139)**，168.232 秒，退出码 0；实际执行 2967 项，跳过不计为通过。
- `HEADLESS_SHELL=<本机 chrome-headless-shell> uv run --offline python -m unittest
  tests.test_x_response_dom tests.test_x_post_dom tests.test_x_access_gate
  tests.test_x_challenge_verdict -q` → **66 tests, OK，无跳过**，10.478 秒。
  覆盖真实临时浏览器中的 DOM、HTTP 状态与受控迟到协议交付及相邻策略回归；不声称实测线上 X。
  定点和无头测试与全仓集合存在交集，不将各次执行数相加当作独立覆盖数。
- 本任务相对主线的所有 Python 文件通过 `uvx --offline ruff check`；
  `node --check kb/static/js/sources.js`、索引与工作区 `git diff --check` 均通过。
  冲突代码文件及本任务差异代码均不超过 700 行，`x_browser.py` 为 700 行。
- 自建扩展标签已关闭；自建界面服务进程退出，55998 端口不再监听，临时数据根已删除。
  协议测试断言自有浏览器优雅退出；其 HTTP 服务和临时 profile 由测试清理。

本轮日志目录：`/tmp/kb-x-merge-AACErx/`。没有修改生产数据库、真实 vault 或登录态。
