# 订阅双栏阅读：第 2 轮审查修复

日期：2026-10-10，Asia/Shanghai；复现与主要回归检查于 2026-10-09 启动。

## 范围与独立复现

完整读取第二轮审查报告，认可「更换订阅网址后仍显示旧来源正文」的阻断问题。
修改实现前，启动报告提供的隔离服务，并通过真实界面编辑、保存、同步及重新读取，独立复现：
接口与左侧列表只有新文章，右侧却继续显示旧文章，提示「继续阅读」，重试仍不恢复。
证据为 `membership-before-fix.log`，结果为 `REPRODUCED`，无页面脚本异常。

该问题由上一轮缓存保留逻辑引入，仍属于阅读区与当前订阅筛选一致性的原需求边界。
本轮仅修改阅读缓存有效性判定，补充测试和说明；不修改后端关联数据、采集行为、最左侧导航或正文展示形式。

## 修复规则

- 阅读上下文除来源类型、关键词与订阅 ID 外，还包含当前筛选订阅的 `url`、`feed_url` 和 `mode`。
- 这三项与后端重置订阅文章关联的配置字段一致；发生变化时，不把旧地址的正文误当成同一结果集中的分页外文章。
- 只在 `sourceOffset < sourceTotal`、仍有未加载结果且上下文不变时保留窗口外的文章。
- 已加载完整结果且其中不存在当前文章时，转为当前有效结果的第一篇，恢复选中状态、导航位置并重置正文滚动。
- 不把名称、同步时间或条目数量作为配置指纹，避免正常同步与单纯改名再次打断仍有效的阅读。

生产改动集中在 `kb/static/js/source_reader.js`，不新增接口或改变分页数组。

## 浏览器通道与隔离

首先尝试通过 Chrome 扩展创建名为「📖 订阅缓存二轮修复」的后台会话标签，设置 `visible:false`。
扩展返回 `unsupported Codex auth method: apikey`，未能创建验证标签；按规定降级到独立无头 Chrome。
Playwright CLI 会话为 `kb-subscription-fix-r2`，进程核实包含 `--headless`、`--no-startup-window` 和独立临时 profile。
没有接管、复用或直连用户普通标签，没有激活 Chrome，也没有使用有头浏览器。

- 8899：修改前使用审查提供的隔离服务；修复后使用新增的 `tests/serve_reader_membership.py`，每次从新临时库开始。
- 8900：使用 `tests/serve_sources.py`，运行既有边界、阅读和订阅管理回归。
- 两种服务均采用临时数据库与 vault，关闭调度及向量模型，远端采集仅使用 example.org 替身，不读取或改动真实订阅数据。

真实配置变更探针 `tests/source_reader_membership_probe.js` 不路由替换概览或条目响应，不修改前端状态来制造结果。
编辑、保存、配置变更后的同步和返回列表均使用真实界面。
正向保留场景通过真实单源同步 API 添加新内容，等待前端原有轮询，再通过分页 API 核实保留文章确实仍属于筛选结果。

## 最终验证

- 真实配置变更探针：19 项通过；覆盖完整结果换网址、30／35 未完整分页换网址、主页不变但专用订阅链接变化、重读恢复，以及单纯改名不打断有效阅读。
- 真实后台到来场景：轮询在 14,888ms 后将列表从 30／35 更新为 30／36；原文章仍在分页 API 的后续结果中，正文与 220px 阅读位置保持不变。
- `tests/source_reader_boundary_probe.js`：28 项通过；保留上一轮 25 项场景，并新增完整结果排除当前文章后正文、选中状态及导航恢复的检查。
- `tests/source_reader_probe.js`：38 项通过，覆盖桌面／平板／手机布局、语言切换、独立滚动、待准备条目、错误恢复和内容安全。
- `tests/sources_review_probe.js`：15 项通过，订阅配置、失败恢复、旧响应隔离及同步→Markdown→索引→全文检索不受影响。
- `.venv/bin/python -m unittest tests.test_source_post_title tests.test_fulltext_browser tests.test_feed_sources tests.test_headless_shell -v`：45 项通过，无跳过，47.580 秒。
- 浏览器共 100 项检查通过，无未捕获页面脚本异常；错误恢复用例注入的 HTTP 503 为预期网络错误。
- JavaScript 语法、Python 编译与 `git diff --check` 均通过；所有改动的代码文件低于 700 行。

边界探针删除样例文章后，筛选结果数量会相应减少；测试改为按当前夹具计算匹配条数，不再依赖原来的固定 30 条。

## 证据与复跑

证据保存在仓库外：

`/Users/fjh/code/harness/data/runs/_HIlq-yx0yAk/free-review/LBJMRjsdG6tf/fix-round-2`

主要记录为 `membership-before-fix.log`、`membership-fixed.log`、`boundary-fixed.log`、`reader-probe.log`、`management-probe.log`、`unit-tests.log`、`final-validation.log` 和 `browser-isolation.json`。
截图位于该目录的 `output/playwright/`，包含完整结果换网址、未完成分页换网址、专用订阅链接变化，以及仍有效的分页外阅读。

验证后只关闭本轮 `kb-subscription-fix-r2` 会话及 8899／8900 隔离服务，相关进程退出、端口关闭，临时浏览器 profile 与真实编辑回归的临时库已清理。
工作树未留浏览器产物；清场与控制台记录为 `browser-close.log`、`cleanup.log` 和 `browser-console.log`。

在证据目录创建 `output/playwright/` 后，启动新临时服务并使用独立无头会话执行：

```bash
/path/to/repo/.venv/bin/python /path/to/repo/tests/serve_reader_membership.py --port 8899
playwright-cli --session kb-subscription-fix-r2 open http://127.0.0.1:8899/#rss
playwright-cli --session kb-subscription-fix-r2 run-code "$(cat /path/to/repo/tests/source_reader_membership_probe.js)"
playwright-cli --session kb-subscription-fix-r2 close
```

真实编辑探针会修改隔离数据；再次执行前须重启服务，使用新的临时数据库。
