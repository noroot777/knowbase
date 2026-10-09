# 我的订阅：列表与常驻阅读区

验证日期：2026-10-09。

## 参考与边界

参考用户截图的中、右两列，把「发现内容」和「阅读正文」分开，不改最左侧导航。
本次改动作用于我的订阅中 RSS、大V观点的内容视图；公众号连接与关注列表保持原样。
沿用现有瓷白、墨蓝黑、松绿配色与字体，不另造一套视觉系统。

## 已实现

- 中列为紧凑内容列表，保留搜索、来源筛选、作者、时间、标题／观点摘要、处理状态和加载更多。
- 选中卡片使用白底、细边框与松绿侧线；标题最多两行，文章摘要三行、观点摘要四行。
- 右列常驻显示正文，列表与正文独立滚动；默认选中第一篇可读内容。
- 已译内容默认显示中文，并提供原文标题、中文译文／原文切换和外链入口。
- 支持上一篇、下一篇、专注阅读与键盘选文；后台刷新和加载更多保留选中内容。
- 手机采用列表／正文切换，进入正文自动定位到阅读区，返回列表保留选中项；正文尽量占满可用视口。
- 持续收集设置移至订阅管理视图；添加、编辑、启停、同步和每日定时同步继续使用原有接口。

## 内容真实性与安全

不加入后端尚未提供的热门、未读计数、缩略图或收藏功能。
已有译文不是重新生成的「中文改写」，语言切换只显示已经保存的原文／译文。
待翻译、首次待补抓的条目仍不可展开；已入库且等待覆盖的条目仍可读，并标明真实处理状态。
大V观点不重复显示由正文截取的伪标题；订阅摘要明确提示全文需打开原文。
正文继续通过 `textContent` 显示纯文本，保留段落与特殊符号，不把外部内容解释为可执行 HTML。

## 浏览器通道与隔离

优先检查 Chrome 扩展通道，并尝试创建名为「📖 订阅双栏验证」的后台标签。
扩展枚举和具名后台标签创建均返回 `unsupported Codex auth method: apikey`，因此未能创建验证标签。
随后降级为独立、默认无头的 Playwright Chrome，使用命名会话 `kb-subscription-reader` 和临时 profile。
没有连接或复用用户普通标签，没有使用现有 Chrome 的调试端口，也没有启动有头浏览器或激活 Chrome 窗口。

服务运行于 `tests/serve_sources.py` 的临时数据库与 vault，关闭真实调度。
阅读探针用 34 条隔离样例替换条目响应与 RSS 概览，截图内容不是用户真实订阅数据。
管理探针使用真实本地接口完成配置保存、错误恢复、单源同步及检索；采集使用 example.org 测试替身，不请求真实订阅。
错误恢复用例主动返回 HTTP 503，浏览器对应的网络错误为预期现象，所有探针均无未捕获页面脚本异常。

## 验证结果

- `node --check`：sources、source_reader、icons 和新阅读探针全部通过。
- `tests/source_reader_probe.js`：38 项检查全部通过，覆盖双栏、语言切换、独立滚动、选中状态、翻页、筛选、失败重试、内容安全和移动端。
- `tests/sources_review_probe.js`：15 项检查全部通过，覆盖配置、旧请求隔离、自动／手动重试，以及同步→Markdown→索引→全文检索。
- `python -m unittest tests.test_source_post_title tests.test_fulltext_browser tests.test_feed_sources tests.test_headless_shell -v`：45 项测试通过，无跳过。
- `git diff --check` 通过；所有修改／新增的代码文件均小于 700 行。

运行阅读探针时，先在已安装项目依赖的环境启动隔离服务，再使用独立无头会话：

```bash
python tests/serve_sources.py --port 8894
playwright-cli --session kb-subscription-reader open http://127.0.0.1:8894/#rss
playwright-cli --session kb-subscription-reader run-code "$(cat tests/source_reader_probe.js)"
playwright-cli --session kb-subscription-reader close
```

本地验证产物（不纳入 Git）：

- `output/playwright/subscription-reader-desktop.png`：1440 × 1000 桌面。
- `output/playwright/subscription-reader-tablet.png`：1024 × 900 平板。
- `output/playwright/subscription-reader-mobile.png`：390 × 844 手机。
- `output/playwright/subscription-reader-results.log`、`subscription-management-results.log`、`subscription-regressions.log`：检查结果。

## 第 1 轮审查修复

专注阅读跨断点和新内容挤出已加载窗口时的阅读状态问题已修复，补充边界检查见 `design/subscription-reading-fix-r1.md`。
原轮验证产物已由审查回合移至其证据目录的 `pre-existing-artifacts/`；本轮复测证据在新修复报告指定的仓库外目录保存。

## 第 2 轮审查修复

阅读缓存现在区分配置变更、完整结果排除与真正分页外的文章；真实接口回归及证据见 `design/subscription-reading-fix-r2.md`。
