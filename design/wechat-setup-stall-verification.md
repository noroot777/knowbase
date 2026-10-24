# 绿泡泡接入检查卡住：修复与验证

日期：2026-10-23。任务：WJ1Uxb6zVWbJ。

后续已增加 Windows x64 本机接入实现，见 `wechat-windows-verification.md`。
本报告保留首次卡住故障修复时的历史记录，其中 Windows 首次接入的平台限制已被后续实现取代。

## 实际故障与修复

只读检查本机现有服务，接口返回 `state=checking, running=false`，消息仍为「正在检查绿泡泡接入…」。
服务日志显示后台线程更新状态文件时，Windows 对原子替换返回 `PermissionError / WinError 5`；
随后的失败状态写入也遭遇同样错误，最终仅保存了旧状态和停止标志。前端按停止标志结束轮询，旧文案因此一直留在页面上。

- 状态文件读取与替换使用同一进程内互斥锁，避免 Windows 读句柄尚未关闭时替换失败。
- 原子替换遇到短暂拒绝访问时最多尝试 6 次，总等待 0.3 秒；永久故障仍抛出，不无限重试，不覆盖原有有效状态。
- Windows 锁冲突 `WinError 33` 转为可解释的「任务正在执行」，所有取锁失败分支关闭文件描述符；其他文件系统错误不伪装成忙碌。
- 运行阶段与停止标志不一致的旧状态，重新核对实际任务锁；任务已不存在时显示「上次接入已中断」，不自动重新获取 key。
- 没有可用 Reader 的 Windows / Linux 检查明确提示平台限制，不下载或安装仅供 Mac 接入的工具；已有可读 Reader 仍优先复用。
- 修复 Windows 命令超时清理不支持 `os.killpg` 的异常，使用进程树终止并保留固定、脱敏的超时提示；POSIX 继续按进程组清理。

## 自动回归

运行命令（使用当前机器已有的 Python 环境，无新增依赖）：

```powershell
$env:PYTHONUTF8 = '1'
python -m unittest tests.test_wechat_setup tests.test_filelock tests.test_docker.ContainerAPITests tests.test_sync_channels tests.test_wechat_discovery_integration -q
```

结果：78 项中 77 项通过、1 项跳过。跳过项需要额外准备固定版本 Reader 与 SQLCipher；本次未读取真实用户数据库或执行获取。

覆盖瞬时/永久替换失败、读取/替换互斥、旧停止状态恢复、锁冲突与句柄关闭、并发检查、重复点击、确认令牌、恢复锁、原配置保留、跨站与远程请求拒绝、Docker 能力边界、既有 Reader 复用，以及 Windows/POSIX 超时清理。

定点反向验证只在测试进程内修改单个行为，不改仓库文件：

- 去掉状态 I/O 互斥：对应回归失败 1 项、异常 0 项。
- 去掉「已停止但仍处于运行阶段」的恢复：4 种状态分别失败、异常 0 项。

额外尝试完整 Docker 运行时测试时，当前 Windows 环境有两项失败：容器入口子进程输出为空，以及另一个子进程按 GBK 读取 UTF-8 文件失败；均不在接入链路，本次未改动。容器接入 API 的 4 项回归全部通过。
当前 Python 环境未安装 Ruff，未新增依赖或更改格式化配置；使用 Python 编译与 `git diff --check` 检查修改文件。

## 浏览器验证

先检查 Chrome 扩展，能枚举浏览器；按要求尝试以具名会话「🧪 绿泡泡接入验证」新建后台标签时，
扩展返回 `Capability is not available: visibility`，无法保证后台可见性，因此未去掉该限制重试。
随后使用已有 chrome-headless-shell，以独立临时 profile、无头模式验证。
没有接管或导航用户已有标签，没有直连用户 Chrome，没有激活窗口，没有使用用户登录态。

验证服务在本任务工作区的隔离目录运行，加载真实 HTML/CSS/JS 和真实接入 API；其他业务接口使用空数据 fixture。
绿泡泡状态、配置目录和数据目录均隔离，不触碰真实接入状态、密钥或聊天库。

页面 11 项断言全部通过：旧状态显示中断、可重新检查、运行时按钮禁用、30 次并发读取均为 200、重复提交返回 409、关闭重开恢复状态、Windows 明确提示平台限制、终态按钮恢复、无错误提示、390px 无横向溢出、无未捕获脚本异常。

截图已逐张目检，桌面与窄屏均可读；产物保存在忽略目录：

- `output/wechat-stall/interrupted-desktop.png`
- `output/wechat-stall/unsupported-desktop.png`
- `output/wechat-stall/unsupported-mobile.png`
- `output/wechat-stall/results.json`

隔离验证服务与无头浏览器验证后均关闭。

## 生效边界

本次修复在任务 worktree 中，未合并、未提交；本机 8787 服务运行在另一任务目录，未修改其文件或重启该服务。
验收合入实际运行目录后，需要重启 kb-web，随后在接入页点击「刷新状态」，旧的假检查中状态会显示为中断。
Windows 本身并未因此获得首次自动获取 key 的支持；没有可用 Reader 时需在 Mac 接入并导出文章，再由当前知识库索引。
