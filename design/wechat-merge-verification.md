# 绿泡泡接入：验收冲突合并核对

日期：2026-10-23。任务：WJ1Uxb6zVWbJ。

## 范围与保留依据

仅在任务工作区、任务分支 `ash/WJ1Uxb6z` 将 `main` 合入；没有切换、提交或合并到目标分支，没有删除分支或 worktree，没有重启用户服务。

- 任务原提交：`709fa8361611fe88e88e0e8e13e601a8ded0cd12`。
- 合入的主线提交：`5de502342d8f746aa9b7e1b208ac8cf3e68a5dd4`。
- 共同基线：`101618c3dbe3f43694c80d64ccd70a442cf4ecd5`。
- 主线自共同基线修改的 18 个文件均逐项核对：10 个与任务版本完全一致，另外 8 个差异均为已受审的接入修复，没有未分类的主线改动。完整 blob 对照在证据目录的 `preservation-audit.json`。

## 冲突处理

7 个文件共 18 个冲突块均已解除并暂存，不保留冲突标记或未合并索引：

| 文件 | 保留内容 |
| --- | --- |
| `design/wechat-windows-verification.md` | 主线 Windows 接入说明和任务修复的生效边界；纠正已经提交后仍写“未提交”的旧描述。 |
| `kb/wechat_runtime.py` | 主线原生命令、超时清理及编码处理；保留目录保护的 SID 超时安全错误。 |
| `kb/wechat_setup.py` | Windows 分发、跨进程锁、UTF-8 严格读取、锁故障分类、备份中断恢复、锁内重读及完整终态原子发布。 |
| `kb/wechat_windows.py` | 固定原生 Reader／获取工具与本人确认；保留材料缺失诊断、失败清理及保留材料的恢复指引，不重新引入 Mac discovery 适配器。 |
| `kb/wechat_windows_reader.py` | 直接调用原生 Reader，保留真实数据库读取与覆盖验证，不安装 Mac 适配器。 |
| `tests/test_wechat_setup.py` | 双方已有接入／超时测试；保留 backing_up 与统一 BlockingIOError 契约回归。 |
| `tests/wechat_agent_test.mjs` | 主线平台测试与任务新增的状态未知、禁止获取及恢复轮询回归。 |

自动合并的 `kb/static/js/wechat_setup.js` 也已核对，状态未知和轮询修复未丢失。解决冲突后，全部 `kb/`、`collector/` 和 `tests/` 内容与任务原提交完全一致；本次合并不新增产品行为，只更新合并关系及上述文档说明。

## 本次实际验证

- Python 定向回归 136 项：135 通过、1 项既有 Mac 可选 Reader 集成跳过。覆盖 Windows 接入、状态恢复、共享锁、Windows 入口、通道与容器 API。日志 `regression-python.log`。
- 固定 Windows Reader + 真实 SQLCipher 加密数据库集成 6/6 通过，包含错误密钥、辅助库缺覆盖、部分材料及可读未知辅助库，不访问真实聊天库或真实密钥。日志 `regression-reader.log`。
- Node 前端回归 10/10 通过；生产 JS 语法检查通过。日志 `regression-js.log`。
- 真实隔离 app/API/HTML 浏览器验证 9/9，通过实际刷新验证 4 种新终态、4 种历史 running=true 终态及真正中断的备份阶段。0 个未捕获页面异常；6 张截图逐张目检。没有点自动获取或同步。
- 7 个关键 Python 文件 AST 解析、700 行上限及空白检查通过；工作区与暂存区的 `git diff --check` 通过；无冲突标记、无未合并索引。

证据目录：`D:/ai_workspace/ash/data/runs/WJ1Uxb6zVWbJ/merge-resolution`。保留可重跑的 `browser_run.py`／`browser_probe.mjs`、状态发布结果、页面断言和截图。基础回归元数据放在证据目录的 `synthetic-*` 子目录，不使用主目录数据库或配置。

本次没有跑整仓 full、Collector 整套或真实客户端首次获取，不将定向回归描述成全量通过。

## 浏览器通道与清场

先尝试扩展具名后台标签 `🔎 接入合并验证`，传入 visible=false 后返回 `Capability is not available: visibility`，无法保证保持后台；没有去掉后台约束重试或接管普通标签。依规定降级为独立 headless-shell，明确 `--headless`、临时 profile，只连接自己创建的无头进程，不使用用户 cookie 或有头浏览器。

隔离服务 PID 20124／端口 6750、浏览器 PID 25096／端口 3813 已退出；复查相关进程、直接后代和监听均为 0。临时夹具及 profile 已清理，用户 8787 服务未动。目标主目录仍干净，`main` 提交未改变。

## 重新验收

本次合并提交只落在任务分支。完成后由统一验收动作把任务分支合回目标分支并清理工作区；不要手工操作目标分支。只要主线没有再次前进，重新点击验收即可按 fast-forward 完成。

## 2026-10-24：合入新增 Windows 入口与文件锁修复

本次仍只在任务分支合入主线。合并前任务提交为 `313fa5d20fb047ca004f128c9cc5b78f1785712d`，
主线提交为 `ac24f02ec07141dec9bdca50f3d82476cfbb7f1a`，共同基线为
`5de502342d8f746aa9b7e1b208ac8cf3e68a5dd4`。没有切换或改写目标分支，也没有操作用户服务。

### 冲突与保留审计

- `kb/filelock.py` 保留主线的常量、局部 errno、原生错误诊断及消费方说明；同时保留任务的限制：
  仅非阻塞取锁的错误 33 转为 `BlockingIOError(EAGAIN)`，解锁、权限和其他故障仍为原始 `OSError`。
- `docs/decisions/windows-entry.md` 保留主线新增的入口拆分、三态查询、Job 身份、健康就绪、停机、
  参数及编码约束，只将文件锁异常说明与上述合并契约对齐。
- 主线新增修改的 22 个文件中，20 个与主线 blob 完全相同，仅上述两处有显式冲突解决差异。
  主线未修改的任务文件也逐项对照；除本报告追加验证记录外，21 个文件与原任务 blob 完全相同。
  没有用整份 ours/theirs 覆盖任一侧改动。

### 本次实际验证与已知限制

- 文件锁、锁错误、接入恢复及登录导入定向回归：69 项全部通过。覆盖真实跨进程互斥、共享锁、
  显式解锁、句柄关闭和进程退出释放，以及权限故障和解锁错误不能被误报成忙碌。
- Windows 入口、进程身份、Job 生命周期、服务就绪、状态编码及绿泡泡接入回归：163 项中
  158 通过、5 跳过。4 项因任务检出没有本地 `.venv`，1 项为既有 Mac 可选 Reader 集成；
  原生 Job 与服务就绪用例实际执行，包含未监听、HTTP 不健康、延迟就绪重用及后代清场。
- 清理与索引锁消费者回归：37 项中 36 通过、1 失败。失败为备份路径中的 Windows 反斜杠
  与 tar 路径正斜杠比较，已在从相同主线提交导出的隔离副本上原样复现；不属于本次冲突，未改。
  首次命令遗漏 tests 夹具搜索路径产生的收集错误，在补齐 `PYTHONPATH` 后消除，原日志保留。
- 固定 Windows Reader 与新建 SQLCipher 加密数据库集成：6 项全部通过，不读取真实聊天库或密钥。
- Node 前端回归：10 项全部通过；生产 JS 语法检查通过。本轮没有前端行为改动或浏览器操作。
- 13 个本次合入的 Python 文件通过 AST 与内存编译；合入代码文件最多 695 行；空白检查、
  冲突标记和未合并索引检查通过。环境未安装 ruff，未把语法检查当成 lint 或全量测试通过。

所有回归使用证据目录内的合成数据与临时目录；原生进程夹具自行安全停止并等待退出，复查没有
残留的本轮 Python 夹具进程。没有重启 8787、读取真实聊天或执行自动获取，也未改主目录配置。

证据目录：`D:/ai_workspace/ash/data/runs/WJ1Uxb6zVWbJ/merge-filelock-resolution`。
包含 `preservation-audit.json`、`main-resolution.diff`、`static-checks.json`、各组回归日志及
`baseline-path-failure.log`；主线基线副本只在该证据目录，不在目标分支工作区。
