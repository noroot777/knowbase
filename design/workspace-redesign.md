# 知识库工作区重设计

2026-09-26 · ash task `ZHHoqY-1y74k`

本次以主页及图鉴、全文检索、问答、收藏、同步五个工作区为范围。采用侧栏导航、浅雾灰底和靛蓝重点色，重做内容层级与主要交互。图谱保持独立页面，通过侧栏进入。

## 视觉方向

- 主色：雾灰 `#F5F6FA`、白 `#FFFFFF`、墨蓝 `#25293D`、靛蓝 `#5153C9`、浅靛蓝 `#EBEBFC`、灰 `#70758A`。
- 标题用 Avenir Next / PingFang SC，正文用系统字体，计数与日期用系统等宽字体，无远程字体或图标请求。
- 档案卡片以类型图标、折角式色块、摘要、主题、提及次数和日期分层。保留正字计数作为来源证据的识别元素。
- 主导航、图谱、设置分别分组；桌面固定侧栏，手机折叠导航。大屏三/四列、中屏双列、手机单列。

## 交互变化

- 图鉴支持卡片/列表切换并记住偏好；筛选写入 URL，刷新和浏览器后退可恢复。
- 主题默认收起，可展开；选中主题仍可见；一键清空筛选。
- 每批 48 项，支持滚动增量加载与显式加载按钮。
- 搜索取消过期请求，区分没有结果和请求失败，支持直接重试；只选来源也可检索。
- `⌘/Ctrl K` 与 `/` 快速搜索，弹窗支持焦点循环、Esc 逐层关闭、返回触发位置；阅读器正文内也可用 Esc。
- 手机端导航关闭后不可被键盘误聚焦；问答历史可展开，中文输入法确认不会误发送。
- 切换工具时隔离旧问答请求，避免旧回答污染新工具；跨工作区保留滚动位置。
- 修复真实收藏数据里的长文本、长命令和行动卡在手机端的横向溢出。

## 验证

- 41 项隔离浏览器回归检查通过：`tests/workspace_probe.js`，所有 API 均模拟，收藏与问答不写入真实数据。
- 18 项真实数据浏览检查通过：图鉴、详情与阅读器、三类设置弹窗、同步六阶段、移动导航、问答及收藏布局。
- 真实搜索返回 20 条结果。1440、768、375px 布局检查通过，无未处理前端异常。
- 所有前端 JS 通过 `node --check`；`git diff --check` 通过；新增/修改代码文件均低于 700 行。
- 只读预览服务已验证静态资源、路径边界和写请求拦截。未启动新的采集/同步任务，未修改真实配置。
- 问答模型输出与收藏写入在隔离响应下验证交互；本次未调用真实 LLM 或向真实知识库写测试记录。

浏览器降级说明：首先检查 Chrome 扩展，返回 `unsupported Codex auth method: apikey`，无法建立具名后台会话；随后通过 Playwright CLI 启动独立无头 Chrome，使用临时 profile。未复用或直连用户普通标签，未激活 Chrome 窗口，未启用有头模式。

## 复现

```bash
# 只读预览当前 worktree 的静态页面，GET 请求复用现有后端
python3 tests/serve_workspace.py --upstream http://127.0.0.1:8787

# 完全隔离测试也可省略 --upstream；始终使用 headless 默认模式
playwright-cli -s=workspace open http://127.0.0.1:18877
playwright-cli -s=workspace run-code --filename tests/workspace_probe.js
playwright-cli -s=workspace close
```

本次实现位于当前 worktree；没有合并主分支或重启运行中的生产服务。预览工具不启动调度线程，拒绝转发 POST、PUT、PATCH、DELETE。

本地验收截图和机器可读结果保存在 `output/playwright/`（不加入 Git）：

- `after-desktop.png`、`after-tablet.png`、`after-mobile.png`
- `detail-desktop.png`、`search-results-desktop.png`
- `ask-mobile.png`、`fav-mobile.png`、`pipe-mobile.png`
- `interaction-checks.json`、`real-data-checks.json`、`visual-checks.json`
