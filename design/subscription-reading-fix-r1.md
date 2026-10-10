# 订阅双栏阅读：第 1 轮审查修复

日期：2026-10-09，Asia/Shanghai。

## 范围与复现

完整读取本轮审查报告后，认可其中两个阻断问题。
在修改实现前，用报告提供的独立边界探针复现：

- 自动选文后专注阅读，再缩至 390 × 844，列表和正文均不可见，重新读取仍为空白。
- 选中第 30 篇并滚至 220px 后，新增第 31 篇；原有自动轮询在 14,626ms 后把正文换成第 31 篇，滚动清零。

本轮只修改阅读状态处理，补充回归测试和说明。不改导航、数据接口、抓取逻辑、纯文本显示或其他页面。

## 修复方式

### 专注阅读跨断点

进入专注模式时同时标记当前正文为已打开的阅读内容，保证移动端的显示条件与专注模式一致。
退出专注仍保留阅读意图；使用返回列表时统一清除两种显示状态。
覆盖自动选文→专注→缩窄→重新读取→返回列表，以及退出专注后再次缩窄。

### 后台刷新保留当前文章

记录选文时的来源类型、关键词和订阅筛选条件。
后台列表更新时，如果当前文章只是移出已加载窗口，且筛选上下文没变，继续保留已经打开的文章版本、语言选择和正文滚动位置。
不把保留的文章插入分页数组，不修改分页偏移或总数。

窗口外的文章显示「继续阅读」，暂时禁用无法准确定位的上下篇按钮；加载更多重新包含该文章后，更新为最新加载版本并恢复正确位置与导航，仍保留语言和滚动位置。
返回手机列表时，如果文章不在已加载列表中，将焦点交给可见的列表容器。
改变关键词（包括在订阅管理内修改）、订阅筛选或来源类型，以及出现空结果或读取失败时，不沿用不匹配的阅读缓存。

## 浏览器通道与隔离

首先尝试通过 Chrome 扩展创建名为「📖 订阅阅读修复」的后台标签，设置 `visible:false`。
扩展返回 `unsupported Codex auth method: apikey`，未能建立验证标签；按顺序降级为独立无头 Chrome。
使用命名会话 `kb-subscription-fix-r1`，进程核实包含 `--headless`、`--no-startup-window` 和独立临时 profile；未复用或直连用户普通标签，没有激活 Chrome 或启动有头浏览器。

服务为 `tests/serve_sources.py --port 8897`，数据库与 vault 均为临时数据，调度关闭，抓取只使用 example.org 替身。
边界探针只替换本地 API 响应，选文、专注、缩窄、返回和加载更多使用真实界面操作。
新增内容后的关键检查等待应用原有的 15 秒自动轮询，不点击刷新或调用选文函数制造结果。
阅读与边界探针在首次导航前读取隔离服务的来源配置并注册 API 替身，不依赖隔离库必须为空，避免已有自动选文干扰回归样例。

## 最终验证

- `tests/source_reader_boundary_probe.js`：25 项通过；新文章已进入列表窗口，但原文章、原文选择及 220px 阅读位置保持不变。覆盖移动端返回焦点、加载更多重新定位、上下篇恢复及筛选／来源切换的反向检查。
- `tests/source_reader_probe.js`：38 项通过。
- `tests/sources_review_probe.js`：15 项通过，配置保存、错误恢复、同步→Markdown→索引→全文检索不受影响。
- `.venv/bin/python -m unittest tests.test_source_post_title tests.test_fulltext_browser tests.test_feed_sources tests.test_headless_shell -v`：45 项通过，无跳过，47.879 秒。
- `node --check` 与 `git diff --check` 通过；修改的阅读脚本和新增边界探针均低于 700 行。
- 浏览器探针无未捕获页面脚本异常；错误恢复探针主动注入的 HTTP 503 为预期网络错误。

本轮证据在仓库外保存，不在 worktree 留下截图或浏览器快照：

`/Users/fjh/code/harness/data/runs/_HIlq-yx0yAk/free-review/LBJMRjsdG6tf/fix-round-1`

包含 `before-fix.log`、`boundary-fixed.log`、`reader-probe.log`、`management-probe.log`、`unit-tests.log`、`final-validation.log` 和 `browser-isolation.json`。
修复截图在该目录的 `output/playwright/` 下：`boundary-focus-mobile-fixed.png`、`boundary-refresh-before.png`、`boundary-refresh-after-fixed.png`；常规桌面、平板与手机截图也已复跑保存。

验证完成后仅关闭本轮 `kb-subscription-fix-r1` 会话和 8897 隔离服务，相关进程已退出、端口已关闭、临时浏览器 profile 已移除；清场记录见 `browser-close.log` 与 `cleanup.log`。

复跑边界检查时，在已启动隔离服务、已建立独立无头会话且当前工作目录拥有 `output/playwright/` 的前提下运行：

```bash
playwright-cli --session kb-subscription-fix-r1 run-code "$(cat /path/to/repo/tests/source_reader_boundary_probe.js)"
```
