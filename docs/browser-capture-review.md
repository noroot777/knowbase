# 浏览器补抓审查修复记录

## 修复范围

- 来源安全：在 `SourceType` 增加默认关闭的 `body_from_link` 能力，仅 RSS、网页专栏和手动收录开启。浏览器补抓的文章入口、绑定请求和保存都读取它，不能用第三方外链覆盖简报摘要。
- HTTP 反馈：从原 `ui.js` 提取唯一的 `apiFail` 到扩展目录的共用 `api.js`，页面与扩展共同调用。先判断 HTTP 结果，再解析成功 JSON；错误保留文本或结构化原因以及 HTTP 状态。
- 确认交互：两处原生确认框改用 `confirmBox`；共享组件使用 `textContent`，独立补抓页无需加载整个主页面才能显示确认框。
- 视觉：补抓页复用 `base.css`，扩展的独立 token 子集逐值对齐并互相注明位置；页脚使用 `icons.js` 图标，大标题使用显示字体 900 字重。
- 回执：审计表进入 `registry.SCHEMA`，按持久审计恢复保存结果；到期、内存清理或服务重启后仍能继续。恢复时校验来源、排除状态、修订号及正文摘要，不复活旧正文。
- 额度：只统计 `pending` 请求；保存、取消及陈旧请求不阻塞新待办。

## 实际验证（2026-10-26）

遵守项目规则，执行者**没有执行任何回归测试**。新增 Python 和 JavaScript 用例仅做语法检查，统一留待验收执行。本轮实际运行的是针对报告缺陷的隔离最小复现、浏览器点检及编译检查，不声称回归通过。

### 浏览器通道与隔离

优先尝试 Chrome 扩展具名会话 `🧭 补抓审查修复`，以 `visible:false` 新建后台标签，返回 `Capability is not available: visibility`，无法保证后台约束。因此未移除限制重试、未使用用户 Chrome 调试端口、未读取或导航任何用户标签、未激活窗口。

按顺序降级到独立无头 Chrome for Testing（临时 profile），只访问本机 `127.0.0.1:8913` 和此临时 profile 中的扩展页面。数据及 vault 隔离在 `/tmp/knowbase-capture-review.ImLbtD/`；夹具仅挂页面和补抓路由，不启动生产调度器。服务用 `kb.sh` 的 `daemonize` 和 `uv run --offline --no-sync` 启动。没有生产数据、登录态或模型调用，未使用有头浏览器，未向用户 Chrome 安装扩展。

夹具中的后续处理入口只记录参数并返回 `fixture-dispatch-only-no-model`，没有实际运行导出、索引或提取。真实网站工具栏/快捷键授予 `activeTab`、登录文章提取覆盖率及完整模型链路仍留待验收，不能用空 profile 的结果冒充验证过用户登录态。

### 点检结果

| 场景 | 真实结果 |
| --- | --- |
| 简报订阅已开 `fulltext=1`，走真实绑定路由 | 目标读取和请求创建均 409，补抓页按钮禁用，原摘要不变；catalog 的简报能力为 false |
| 页面目标读取及扩展待办接口抛数据库异常 | 两处均显示 `Internal Server Error(HTTP 500)`，没有 `Unexpected token`；移除扩展故障后刷新待办恢复 |
| 真实扩展提取、预览、确认保存 | 提取 1444 字符，导航、表单、隐藏节点和页脚哨兵均未进入正文；确认后写入同一条目 |
| 重新采集及继续处理确认框 | 页面 DOM 中出现项目确认框，取消和 Escape 均关闭；继续处理点检中原生 dialog 事件为 0 |
| 保存后令内存请求过期，再刷新 | 内存请求为 0、审计回执仍为 1，页面恢复「已持久保存」 |
| 杀停并重新启动本轮隔离服务，再刷新和继续 | 持久回执恢复，后续路由收到 export/index/extract 和 `channel:rss`；仅记录调度，不运行管道 |
| 60 个取消请求后再创建 41 个等待请求 | 第 101 个总请求可创建，等待数为 41；100 个等待时仍拒绝第 101 个，取消一个即可复用名额 |
| 桌面及 390×844 移动端 | 移动端文档宽度/滚动宽度均 390；灰色为 `rgb(102,114,124)`，标题字重 900，页脚有 SVG 箭头 |
| 扩展下载 | ZIP 含七个文件，包括共用 `api.js`，不依赖扩展目录以外的文件 |

截图位于工作树忽略目录 `output/playwright/`：`review-saved-desktop.png`、`review-saved-mobile.png`、`review-confirm-desktop.png`、`review-confirm-mobile.png`、`review-popup-preview.png`、`review-popup-500.png`、`review-page-500.png`、`review-digest-blocked.png`。已检查确认框桌面/移动端和扩展预览截图。夹具及浏览器均在本轮结束前关闭，截图不纳入产品提交。

### 静态检查

- Python 编译：`kb/browser_capture.py`、`kb/web/browser_capture_api.py`、`kb/source_types.py`、`kb/feeds/registry.py`、`tests/test_browser_capture.py`。
- JavaScript 语法：补抓页、来源阅读器、共享反馈组件、扩展 popup、共用 HTTP helper 及 `tests/browser_capture_feedback.mjs`。
- `git diff --check` 及修改文件行数核对；代码文件均未超过 700 行。
- 验收待运行：`tests/test_browser_capture.py`、`tests/browser_capture_feedback.mjs` 以及既有订阅阅读器相关回归。上述用例不提供本轮执行结论。

## 建议转独立任务的旧路径

报告第 1 条末尾的「类问题」要求旧的自动补抓资格和订阅编辑勾选框也接入能力位。这部分技术判断成立，但不属于本次「用户在已打开文章页主动采集并补回原条目」的新链路：

- 开工基线 `f97482a` 已有 `kb/feeds/fulltext.py:97` 的资格查询（只要求订阅 `fulltext=1`）。
- `kb/static/index.html:395` 的订阅勾选框和 `kb/static/js/source_forms.js:11`、`:29` 的读写同样在基线存在。
- `git diff f97482a..2fb0122 -- kb/feeds/fulltext.py kb/static/js/source_forms.js kb/static/index.html kb/source_types.py` 无输出；本轮对 `index.html` 只增加共享反馈脚本，不修改订阅设置行为。

本轮在来源类型层建立了新能力，并使浏览器补抓整条链遵守它；没有顺手修改旧自动补抓及订阅设置。建议独立任务统一处理旧资格查询、订阅设置服务端约束和不合适来源的勾选框，并覆盖既有补抓/翻译/挂起口径。此范围转出需用户裁定，不把它描述成报告错误或已修复。
