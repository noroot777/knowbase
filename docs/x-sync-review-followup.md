# X 审计与同步契约承接修复

日期：2026-10-20。任务：`AACErxUciYs8`，开工版本：`39882d0`。

## 范围

已完整读取 `7DUhYZrZy1lb/round-1/report.md`，只承接用户指定的四条意见。
没有重新打开原任务，也未处理报告中的统计、退出码、报告原子性等其它事项。
账号生命周期分类、合法空正文、PostGone 计数留痕、probe/登录导入适配与完整 Article 资格均未改动。

## 四条修复

1. **审计欠账收口**：局部修复后仍先排队复查，不假称整篇修复成功。
   同版得到与欠账匹配的安全扣留结论后，把 `remaining` 转为 `retained`，默认不再反复开页。
   同版连续两次相同的安全保留结论（含 `identical`）也收口，避免出现另一种永久欠账。
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

## 验证

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

## 浏览器通道与证据

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
