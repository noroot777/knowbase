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
