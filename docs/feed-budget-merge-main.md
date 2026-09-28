# 采集预算任务：验收合并冲突处理

在任务分支 `ash/lvn0HGXl` 执行 `git merge main`，合入目标提交 `768097a5995d65dfe9c7fb7ea7231d78e0019cb6`。合并前任务提交为 `6ddbaf2`。未切换、提交或合并到目标分支。

四处冲突均为可同时保留的改动：

| 文件 | 合并结果 |
| --- | --- |
| `kb/config.py` | 保留 main 的空环境变量回退、`expanduser()` 和视频目录配置，同时保留 RSS / 大V 来源注册。 |
| `kb/server.py` | 同时注册 `intake_api` 与 `wechat_api`，保留 Knowbase 名称、健康检查与 `KB_HOST` 配置。 |
| `kb/static/index.html` | 同时加载来源模块与微信接入的 CSS / JS，保留双方页面、弹窗和 Knowbase 品牌。 |
| `kb/static/js/workspace.js` | 同时保留来源编辑、来源阅读及微信接入的 Escape 关闭处理，保留来源初始化和 Knowbase 页面标题。 |

本任务的 `kb/feeds/` 和 `tests/test_feed_budget.py` 与合并前逐字一致。没有扩展预算修复范围。

## 验证

- 非浏览器后端回归：运行 306 项，305 项通过，1 项按既有条件跳过（`KB_WECHAT_TEST_RUNTIME` 未配置，无法运行可选的真实 SQLCipher Reader 回归）。耗时 20.673 秒。
- `uv lock --check --offline` 通过，99 个依赖的项目声明与锁文件一致。
- `workspace.js`、`wechat_setup.js`、`sources.js`、`source_forms.js` 的 `node --check` 全部通过。
- 独立无头浏览器 14 项检查通过：品牌、来源导航、两组真实 API 路由、微信弹窗、三种弹窗 Escape、RSS 添加→同步→导出→索引→阅读→全文检索、390px 无横向溢出、静态资源成功加载、无未捕获页面异常。
- 测试服务使用临时数据库和 vault、订阅抓取替身，并设置容器能力模式以禁止实际微信获取。未启动生产服务或访问用户微信数据。
- 4 个冲突文件分别为 149、60、472、193 行，均低于 700 行。无未解决索引项，`git diff --cached --check` 通过。

浏览器通道先检查扩展，`cua.getState()` 返回 `browsers: []` 与 `unsupported Codex auth method: apikey`，因此无法建立具名后台扩展会话。按用户规则降级为临时 profile 的独立 `chrome-headless-shell`，Playwright 明确设置 `headless: true`。未接管用户标签、未激活 Chrome、未启动有头浏览器。临时服务、浏览器和 profile 均在验证结束后清理。

本地验证产物位于忽略目录 `output/playwright/`：`merge-main-result.json`、`merge-main-server.log`、`merge-main-initial.yml` 与三张 `merge-main-*.png` 截图；验证脚本为 `merge-main-probe.cjs`。
