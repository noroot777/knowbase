# 新建同步入口修复（2026-09-27）

## 复现与原因

基于重建后实际检出的 `6aedaff` 排查。当前生产服务在全新浏览器会话、连接正常时可以打开配置窗口，因此不能据此还原用户原标签当时的网络状态。

已复现的故障路径：同步空闲 → 轻量状态接口返回 503 → `pipeConnected=false` → `renderPipeButtons()` 将“新建同步”禁用，但文字仍是“新建同步” → 点击后弹窗保持 `display:none`。此前交互改动将打开本地配置与后台状态连接绑在了一起。首次状态请求缓慢时，入口也一直不可操作。

另一个会让配置停住的路径是同步通道请求挂起：原来使用无超时的 `fetch`，共享的待完成请求会持续挡住列表读取；报错后也缺少就地重试入口。

## 修复

- 打开配置不再依赖状态请求成功；已确认有同步运行或正在提交操作时仍阻止重复发起。
- 在配置窗口内显示状态连接提示与重连按钮。实际启动继续校验连接、运行状态、账号选择和操作锁。
- 重连失败保留账号与日期；重连发现后台已有任务时明确说明等待原因，恢复空闲后可继续操作。
- 通道读取复用带 15 秒超时的请求方法；失败、超时或没有可用通道时给出错误与“重新读取同步通道”入口。
- 保留现有外观、同步接口和后台执行逻辑，仅调整入口及加载反馈。

## 验证

- `tests/pipeline_probe.js`：71 项通过，新增 13 项覆盖首次慢状态请求、空闲断线打开配置、禁止断线提交、重连保留配置、重连发现后台任务、手机提示布局、通道失败和超时重试。
- `tests/workspace_probe.js`：49 项通过，覆盖图鉴、搜索、问答、收藏、导航和弹窗等既有交互。
- 两组检查无未处理前端异常。所有写请求由浏览器路由模拟；预览服务额外拒绝写请求。
- 使用本分支静态文件、生产服务只读接口实测：配置窗口打开成功，原凭证通道加载 44 个公众号，开始按钮可用，无表单错误。随后取消配置，未发起真实同步。
- JavaScript 语法检查、`git diff --check` 通过；修改的代码文件均少于 700 行。

本轮证据在忽略目录 `output/playwright/`：`new-sync-disconnected-before.png`、`new-sync-disconnected-after.png`、`new-sync-real-after.png`、`new-sync-pipeline-checks.json`、`new-sync-workspace-checks.json`、`new-sync-real-results.txt`。

浏览器通道：优先检查 Chrome 扩展，返回 `browsers: []` 与 `unsupported Codex auth method: apikey`，无法建立具名后台会话。按用户要求降级为 Playwright CLI 独立无头浏览器，使用临时 profile 和具名会话 `kb-new-sync`；未连接用户普通标签、调试端口或启用有头模式。

## 回归命令

```bash
python3 tests/serve_workspace.py --port 18879
# 另一个终端运行；默认 headless，接口全量模拟
npx --yes --package @playwright/cli playwright-cli -s=sync-entry open http://127.0.0.1:18879
npx --yes --package @playwright/cli playwright-cli -s=sync-entry run-code --filename tests/pipeline_probe.js
npx --yes --package @playwright/cli playwright-cli -s=sync-entry run-code --filename tests/workspace_probe.js
npx --yes --package @playwright/cli playwright-cli -s=sync-entry close
```

预览服务不启动调度器。真实接口检查另外使用 `--upstream http://127.0.0.1:8787`，仅透传 GET；启动、停止和保存功能通过上述隔离测试验证，未重启或停止生产服务。
