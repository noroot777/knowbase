# 任务分支合入 main 的验证

日期：2026-09-28。任务：`NX_wnSr6_APn`。

在任务工作区的 `ash/NX_wnSr6` 分支执行 `git merge main`，合入
`768097a5995d65dfe9c7fb7ea7231d78e0019cb6`。未切换或写入目标分支。

## 冲突处理

- `kb/config.py`：保留来源注册表，同时采用 main 的空环境变量回退、`expanduser()` 和视频目录配置。
- `kb/server.py`：同时挂载信息源和微信接入路由，保留 Knowbase 名称、健康检查与可配置监听地址。
- `kb/static/index.html`：同时加载两个模块的样式和脚本，保留来源导航与微信接入页面。
- `kb/static/js/workspace.js`：同时登记来源编辑／阅读和微信接入弹窗的关闭处理，保留模块初始化及 Knowbase 标题。

检查了自动合并的 README、模型工具、管道、依赖清单与锁文件；两边的来源模块、Docker 部署和微信接入改动均保留。

## 验证结果

- 应用全量 unittest：305 项，304 通过；1 项可选 SQLCipher 集成测试因未配置 `KB_WECHAT_TEST_RUNTIME` 跳过。
- 采集器全量 unittest：118 项通过。工作区原无采集器虚拟环境，先离线执行 `uv sync --frozen --offline`，再用该环境运行测试。
- `uv lock --check --offline`、`uv build --offline` 通过，生成 Knowbase wheel 与源码包。
- Python 编译、全部前端 JavaScript 语法、`git diff --check` 通过；冲突索引清空，涉及的代码文件均不超过 700 行。
- `tests/sources_review_probe.js`：15 项通过，包含默认账号、错误恢复、编辑、采集、导出、索引与全文检索。
- `tests/wechat_setup_probe.js`：18 项通过，包含确认门槛、进度恢复、Docker 能力边界及 Esc 关闭。
- 合并专项浏览器检查：8 项通过；健康、来源、微信接入与同步通道的真实 HTTP 路由全部返回 200，三个来源入口同时存在，两个模块的弹窗均可关闭，Knowbase 标题保留。
- 已检查 1440px 桌面及 390px 手机截图，页面无横向溢出、无未捕获脚本异常。

浏览器首选扩展通道，但 CUA 返回 `Browsers: Error: unsupported Codex auth method: apikey`，浏览器列表为空，无法创建具名分组后台标签。随后使用独立临时 profile 的无头 Chrome / Playwright，明确设置 `headless: true`，进程含 `--headless` 和 `--no-startup-window`。未接管或激活用户 Chrome，未启动有头浏览器。

使用 `KB_CONTAINER=1` 启动 `tests/serve_sources.py --port 18879`，数据库与 vault 位于临时目录，远端 feed 和向量模型为替身；原生微信接入交互使用 API fixture，不执行真实 key 获取。未访问生产 8787 服务、真实微信数据库或个人 vault，也未重新构建 Docker 镜像。

日志、专项脚本和截图保存在本地忽略目录 `output/playwright/merge-*`。验证结束后关闭本次浏览器和隔离服务。

## 2026-09-29：合入 main 的后续预算修复

再次在任务分支执行 `git merge main`，合入 `c50f2b772ec2eebcd4d4c91caf44bdc0c1ef63dc`。未切换或修改目标分支。

- `kb/feeds/sync.py`：保留 main 的完整预算准入、工作线程复查及 `CollectionBudgetExceeded` 独立处理；同时保留本分支仅在真实来源错误时标红的修复。
- `tests/test_feed_sources.py`：按 main 的拆分合入，把预算回归集中到 `tests/test_feed_budget.py`。保留其全部 9 项回归，补充成功／失败阶段断言，并将原尾批 HTTP 用例调整为符合新准入规则的第 70 秒启动、第 89.9 秒完成；24 个成功、1 个跳过，订阅不被误罚退避。
- `docs/source-modules.md`：同步最终预算规则，把之前允许尾批跨越总预算的策略明确标为历史，保留当时的验证记录。

验证：41 项 feed 回归通过；全量非浏览器后端回归运行 307 项，306 项通过，1 项因未配置 `KB_WECHAT_TEST_RUNTIME` 跳过。范围包含来源、传输、预算、同步通道、停止、定时与 Docker / 微信接入回归。`uv lock --check --offline`、`uv build --offline`、Python 编译和差异检查均通过，修改的代码文件均少于 700 行。

相对 main，采集传输实现与拆分后的来源测试保持一致，生产代码只补回本分支的零错误不标红规则；前端、微信接入和 Docker 集成未变。本轮预算测试使用临时数据库、受控时钟和 HTTP 替身，无真实订阅或生产数据写入。因本轮没有页面改动，未运行无关的图谱浏览器测试，也未操作浏览器，不涉及浏览器通道降级。

本轮日志位于忽略目录 `output/playwright/budget-merge-feed.log` 与 `budget-merge-backend.log`。
