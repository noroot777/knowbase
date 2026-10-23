# 绿泡泡接入：第 1 轮审查修复

日期：2026-10-23。任务：WJ1Uxb6zVWbJ。

## 基线与范围

原始受审实现已在主目录提交 `5de5023`，而任务 HEAD 是 `101618c`。本轮将该提交的 18 个受审文件同步到任务工作区，再在其上修复；没有移动分支、提交、合并或修改主目录。复审应读取当前任务工作区，而不是继续只读主目录。

本轮处理报告中的 8 条阻塞意见。共享文件锁属于接入链路的依赖，统一其异常契约并撤销接入调用点的 Windows 特判；没有修改 X 导入模块或扩展其他业务。

## 逐项修复

| 审查项 | 处理与回归后果 |
| --- | --- |
| 1. 不能解密被当作成功 | Windows 启动器不再安装 Mac discovery 适配器，准备步骤也不再复制它。真实固定 Reader 对全错材料拒绝导入，4 个库全部打不开时 unresolved=4；核心库可读、辅助库缺材料时保持 partial，不发布配置。可读但不识别结构的辅助库仍正常计入覆盖。Mac 适配器保持不变。 |
| 2. 目录保护失败被当作忙碌 | `_LockBusy` 只代表实际锁竞争；私有目录符号链接、DACL 失败、SID 查询超时和其他 I/O 故障不进入 running 分支。状态明确显示不可用、running=null、禁止开始获取，并继续有限频率刷新。 |
| 3. 半成品阻塞后续获取 | 获取非零退出或超时时，仅清理本次新产生的 bundle。已保留的部分/不兼容材料不自动删除；失败提示列出实际托管材料路径，说明先自行备份并移走、取消显式覆盖变量、重新检查并再次确认。通过人工归档材料后能够重新到达授权页，不自动再次获取。 |
| 4. Windows 锁异常不一致 | `filelock.flock` 将非阻塞取锁的错误 33 统一翻译为 BlockingIOError(errno.EAGAIN)。权限错误和解锁错误保持 OSError；接入层不再判断 winerror。真实双句柄竞争、释放后重新获取及两类反例均通过。 |
| 5. 读状态失败假报空闲 | PermissionError 最多重试 6 次；只有 FileNotFoundError 表示 unchecked。其他读错误、损坏 JSON、无效状态结构明确报状态不可用，不确定是否运行，不停止前端恢复轮询。状态读写显式使用 UTF-8。 |
| 6. 备份阶段漏恢复 | 活动阶段补齐 backing_up；除逐阶段回归外，调用真实 `_backup` 生产状态，再验证中断识别。正常 ready、failed、partial、待授权等终态保持原样。 |
| 7. 显式材料不存在提示错误 | 在准备读取器前明确报告 KB_WECHAT_KEYS_FILE 指定的文件不存在或不是文件，不下载、不获取、不替换。补齐读取器可返回状态文案；未知状态不再凭空声称已有材料。 |
| 8. 刚完成被误报中断 | 抢到锁后、释放锁前重新读取状态；统一发布层一次写入终态及 running=false，收尾只释放锁。旧终态的残留 running=true 不触发中断恢复，正常附带字段保留。 |

界面同步显示“状态未知 / 状态暂不可用”，而非进行中或假装已结束；瞬时故障消退并重新读到合法状态后按钮重新可用。永久损坏状态的恢复入口仍待独立处理，见下文。新材料恢复提示包含较长路径，增加状态区域的任意位置换行，避免窄屏横向溢出。

## 自动回归

在任务工作区使用主目录已有 Python 3.11，KB_DATA_DIR 指向临时目录：

```text
python -m unittest tests.test_wechat_windows tests.test_wechat_setup tests.test_wechat_setup_recovery tests.test_wechat_windows_recovery tests.test_filelock_errors tests.test_filelock tests.test_win_entry tests.test_sync_channels tests.test_wechat_discovery_integration tests.test_docker.ContainerAPITests
Ran 132 tests — OK (skipped=1)

node --test tests/wechat_agent_test.mjs
10 pass / 0 fail
```

132 项中 131 通过；唯一跳过是未提供 Mac Reader 环境的既有可选 discovery 测试，不是本轮 Windows 真实测试。

另外使用已准备好的 Windows CPython 3.12 / SQLCipher 环境，设置 `KB_WECHAT_TEST_WINDOWS_RUNTIME` 指向缓存的固定 Reader 源码目录：

```text
python -m unittest -v tests.test_wechat_windows_reader_integration
Ran 6 tests — OK
```

6 项均使用真实 Windows 启动器和真实固定 Reader CLI，先核对 Reader 两个源码文件的 SHA256，再为每项独立创建 4 个全新 SQLCipher 加密数据库。没有把 json_call/discovery 打桩：全错材料导入拒绝、全错 keys 计数、缺辅助库 key、全可读未知辅助表，以及两条实际接入边界均验证。缓存源码只复制到临时目录；没有改写用户安装的读取器或真实材料。

JS 语法检查、修改 Python 文件编译、单代码文件不超过 700 行以及 git diff --check 通过。未安装或新增格式化工具，未重跑整层 fast 与 Collector 整套测试，不将此前报告里的既有失败改写为本轮通过。

## 定点反向验证

全部仅在测试进程或全新合成 Reader 目录中注入，未替换整份仓库代码：

| 注入 | 结果 |
| --- | --- |
| 给合成 Windows 启动器装回 Mac 适配器 | 3 项真实加密负例断言失败，0 异常 |
| 将所有 SetupError 都解释为锁竞争 | 2 处权限/目录失败断言失败，0 异常 |
| 状态权限重试耗尽后返回 unchecked | 1 项断言失败，0 异常 |
| 移除 backing_up 活动状态 | 真实备份生产者回归断言失败 1 项 |
| 移除锁内的第二次状态读取 | 4 个刚完成终态均被误报，4 处断言失败 |
| 不翻译 Windows 锁竞争错误 | 原生双句柄回归抛出错误类型，异常 1 项；命中预期，而非导入/环境失败 |

## 浏览器验证与通道降级

先通过 Browser/Chrome 扩展尝试创建具名会话“🔎 绿泡泡审查修复”的后台标签，传入 visible=false。接口在创建前拒绝：`Capability is not available: visibility`。因此不能满足“保持后台、不得激活窗口”的要求，没有省略该选项重试，也没有接管现有标签。

降级到第 2 级：缓存的独立 chrome-headless-shell，明确 --headless、全新临时 profile，使用项目 tests/probe_shell.mjs 的生命周期管理；只连接自己创建的无头实例，不连接用户 Chrome。验证不需要登录态，没有使用用户 cookie，没有启用有头浏览器。

隔离真实 uvicorn 服务使用内核分配的空闲回环端口，加载本任务 HTML/CSS/JS 和接入 API；KB_DATA_DIR、KB_WECHAT_RUNTIME、Reader 配置均指向临时目录。对状态文件读权限与目录保护分别定点制造故障，真实 API 返回不可用后，页面停止转圈、禁用获取、继续恢复轮询；移除故障后自动显示备份中断并恢复按钮。另以合成材料路径验证 partial 提示的桌面/390px 窄屏换行及纵向滚动后操作按钮可达。未点击真实接入或获取按钮。

页面未捕获脚本异常为 0，无横向溢出。截图逐张目检；证据目录：

```text
D:/ai_workspace/ash/data/runs/WJ1Uxb6zVWbJ/fix-round-1/
  01-status-unavailable.png
  02-acl-failure.png
  03-recovered.png
  04-partial-recovery.png
  05-partial-recovery-mobile.png
  06-partial-recovery-mobile-actions.png
  browser-result.txt
  service.log
```

验证脚本保留在忽略目录 `.run/review_service.py`、`.run/review_probe.mjs`、`.run/review_ui.py`。浏览器及隔离服务已结束，临时 profile、状态文件和合成材料随夹具清理；未重启或停止用户的 8787 服务。

## 生效边界

本轮修复仍在任务工作区，尚未合并或应用到主目录。完成确认只表示本轮修复和上述验证完成，不代表独立复审或人工验收通过。审查报告其余不阻塞项未作业务扩展。没有访问真实聊天库、扫描真实绿泡泡进程、获取真实密钥、改变客户端或提权，也不能据此承诺所有绿泡泡 4.x 客户端版本兼容。

## 终态发布补充与范围裁定

针对 U84ahkiKIET9 的第 1 轮报告，界内第 2 条是旧第 8 条未闭合的不变量，已补充修复：

- `progress` 根据统一的活动阶段集合，一次原子写入完整状态及正确的 `running`；终态首次落盘就包含 `running=false`，不依赖下一次读取才能完成。
- worker 收尾只释放作业锁，不再读取或覆写业务结果。异常同样经过终态发布出口，保留可公开的原因并继续隐藏原始异常内容。
- `snapshot` 只恢复仍处于活动阶段的记录，锁内二次读取保护保留；合法终态即使遗留 `running=true`，也保留状态、授权字段、账号候选和结果字段，并对外显示已结束。
- 本次仅改 `kb/wechat_setup.py`、`tests/test_wechat_setup_recovery.py` 和本报告；没有改 UI、Windows Reader／材料处理或主目录。

### 本次验证

- 新增回归在修复前稳定复现问题；修复后 setup 两套定向测试 45 项通过。
- 前文相同的 Python 回归命令现为 136 项，135 通过、1 项既有 Mac 可选集成跳过；JS 仍为 10/10。未把上一轮真实加密测试或 Collector 测试算成本次重新执行。
- 真实 `_work` 在持续拒绝状态读取的条件下发布四种合成终态，读回调用数为 0；发布当时即可观察到 `running=false`。单元测试同时覆盖 6 种终态、5 种活动阶段、锁竞争、锁内状态变化及安全异常。
- 真实隔离 app/API/HTML 的 9 组浏览器验证通过：4 种新终态、4 种遗留 `running=true` 终态及真正中断的备份阶段。实际点击刷新，成功徽标、待授权区及具体失败说明保留，0 个页面脚本异常；6 张截图已逐张目检。没有执行自动获取或同步。
- 两次仅在测试进程内的定点反向注入，分别恢复“终态写 true”和“按残留 running 判中断”，各触发 6 个失败子项，证明回归能拦住相应缺陷。
- Python AST、700 行上限及正常 `git diff --check` 通过。第一次静态命令因 PowerShell 的原生命令引号丢失失败，改为标准输入执行后通过；保留初次与最终日志。

证据目录：`D:/ai_workspace/ash/data/runs/WJ1Uxb6zVWbJ/fix-terminal-publication`。包括修复前／后测试日志、两种反向注入结果、可重跑的 `browser_run.py`／`browser_probe.mjs` 以及页面截图。

浏览器先尝试扩展具名后台标签 `🔎 接入终态复核`，返回 `Capability is not available: visibility`，不能满足保持后台的约束；未去掉后台选项重试。按规定改用缓存 headless-shell，明确 `--headless`、独立临时 profile，只连接自己创建的无头进程，不使用用户登录态或普通标签。隔离服务 PID 19472／端口 2546、浏览器 PID 26256／端口 10980 已结束并复查无监听、无该父进程的后代；临时夹具和 profile 已清理。主目录仍干净，用户 8787 服务未动。

基础回归还留有系统临时目录 `C:/Users/fanda/AppData/Local/Temp/kb-terminal-regression-data`，只含 0 字节的 `feeds-trim.lock`，无数据库或材料。对该明确目录的 PowerShell 清理被执行策略拒绝，未换通道绕过；浏览器临时 profile 和隔离服务夹具不在此目录中，已正常清理。

### 申请转出的两条衍生问题

两项技术意见均认可，不称作误报，也不声称已经修复；依照本轮“上一轮修复引入的衍生问题转独立任务”的边界规则提出裁定：

1. 新第 1 条：前轮旧第 5 条把读取错误当空闲，修复为严格读取及 `status_unavailable`。审查报告第 72、153 行也明确旧条目已修、新问题由该修复引出。相对 `5de5023` 的新增严格 `_read` 与禁用接入分支，导致永久损坏、非法结构或旧编码没有恢复入口。建议独立任务处理“持锁确认空闲后显式备份并重建不可解析状态”；不得自动获取、删访问材料或动配置。本次不新增状态重置协议。
2. 新第 3 条：前轮旧第 7 条已修复显式材料文件不存在的提示；补文案时在 `kb/wechat_setup.py:35` 新增 `account_selection_required` 映射，使 `kb/wechat_windows.py:185` 原来的普通 blocked 变成要求候选账号的交互状态，而固定 Reader 在上层目录输入时只返回计数。审查报告第 109、155 行确认这是新增映射的字段缺口。建议独立任务在 Reader→接入 API 层统一校验状态及必需字段，缺有效候选时提示修正目录且允许重新检查；同步核对 Mac 转换边界及授权字段，不在当前补充修复中扩展适配协议。

当前停在范围裁定，不作本轮完成确认或验收声明；转出是否成立及是否创建独立任务由用户决定。
