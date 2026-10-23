# Windows 绿泡泡接入：实现与验证

日期：2026-10-23。任务：WJ1Uxb6zVWbJ。

本次是对「Windows 暂不支持首次自动接入」的后续实现，不只是删除平台提示。
前一份 `wechat-setup-stall-verification.md` 记录的是首次卡住故障修复时的平台边界；
其中 Windows 不支持首次接入的结论由本报告取代。

## 实现范围

- Windows x64 本机、兼容的绿泡泡 4.x；Windows ARM、32 位和 Linux/Docker 不自动获取。
- 从文档目录、重定向文档目录及客户端配置中定位账号；支持 `KB_WECHAT_DATABASE_ROOT` 指定 `db_storage`。
  多账号先选择，不扫描网络 UNC 路径。
- 私有目录中创建 CPython 3.12 环境，准备固定 Reader、SQLCipher、Zstandard、pywin32。
  外部文件逐个校验 SHA256，首次安装仅接受有哈希的二进制 wheel。
- Windows 用原生 `python.exe + reader_launcher.py` 参数列表，不执行 Mac shell、Go、LLDB 或批处理包装。
  Collector 使用同一原生读取入口，并修正 Windows 采集器虚拟环境路径。
- 现有 Reader、配置或材料优先验证复用。旧配置不可读或材料覆盖不全时停止并保留，不覆盖、不重新获取。
  可通过 `KB_WECHAT_KEYS_FILE` 指定已有私有材料。
- 获取前等待本人勾选和有效令牌，授权绑定 Windows 平台与固定工具版本。过期或工具版本变化时拒绝获取。
  获取限时 180 秒，失败不自动重试。只调用 `extract --db-dir ... --output ...`，不用实验性断点捕获或全库导出。
- 不注入、重启、修改客户端，不自动提权或关闭安全软件。原始子进程输出仅在内存接收，不回传页面。
- 私有目录使用受保护 Windows DACL，仅当前用户、SYSTEM 和 Administrators 可访问，不把 chmod 当 Windows 权限保护。
- 材料经过真实 Reader 导入、数据库布局/覆盖验证、配置发布及最终 `doctor`，
  `live_database_read_ok=true` 才显示已验证。采集仍只处理公众号推送，不将私聊、群聊当文章。
- 保留此前状态文件互斥、短暂拒绝访问重试、旧运行状态恢复及 Windows 锁冲突修复。

## 固定来源与许可

Reader 固定来源：

```text
https://github.com/iversonzhang50-gif/wechat-intelligence-hub-windows/tree/578dddde6db3f2466ac2b41ea67d5f993dc436f7
```

获取工具固定来源：

```text
https://github.com/TANGandXUE/wcdb-key-tool/tree/79f1b5b92e12c66aa281b4a60a3c478b5f547dfa
```

运行时下载并校验 Reader 的 AGPL-3.0-only 许可证及 `WINDOWS-CHANGES.md`，获取工具的 MIT 许可证保存为 `LICENSE.key-tool`。
外部组件未直接并入本仓库 MIT 源码。摘要见 `kb/wechat_windows.py`，许可边界同步写入 `NOTICE`。
依赖固定为 CPython 3.12 / Windows x64 wheel：`sqlcipher3==0.6.2`、`zstandard==0.25.0`、`pywin32==311`；
提供 SHA256，以 `--require-hashes --only-binary :all:` 安装。

## 自动回归

```text
python -m unittest tests.test_wechat_windows tests.test_wechat_setup tests.test_filelock tests.test_docker.ContainerAPITests tests.test_sync_channels tests.test_win_entry
node --test tests/wechat_agent_test.mjs
```

主项目 112 项全部通过，其中 DACL 测试调用真实 Win32 API，PowerShell BOM 约束检查通过。
覆盖 Windows 路由、架构限制、原生参数、依赖固定、自检必须明确通过、材料保留、账号选择、
授权版本绑定、失败不配置/不重试及最后一次 Reader 检查。前端 8 项全部通过。
JS 语法、修改 Python 文件编译及 `git diff --check` 通过。未安装 Ruff，未引入格式化配置。

Collector 在独立测试环境安装其声明依赖后，`tests.test_windows_reader` 和 `tests.test_local_wechat`
共 35 项中 34 项通过；排除以下既有清理问题重跑，其余 34 项全部通过。

明确保留的测试问题，不作为本次通过项：

- `test_recovery_only_resets_affected_local_articles_and_creates_backup` 的业务断言通过，
  但 Windows 清理 `before.db` 时返回 WinError 32。该既有测试的 SQLite 上下文没有显式关闭连接，
  文件仍占用。本轮未修改该测试或恢复脚本，没有为了刷绿扩大修复范围。
- 额外运行整个 `tests.test_docker` 时，两项既有容器运行时测试在 Windows 失败：
  容器入口子进程没有预期输出，以及 POSIX 路径前缀断言遇到 Windows 反斜杠。
  Docker 接入 API 的 4 项全部通过，本轮没有修改 Docker 运行时。
- 初次用主项目环境跑 Collector 缺少 lxml/requests；随后只在 `.run` 的独立测试环境安装依赖，
  不修改用户现有项目环境。

定点反向验证只在测试进程中把当前获取工具版本伪装为已废弃版本：
授权版本绑定回归按预期失败 1 项、异常 0 项，证明它能检出错误接受旧授权。

## 原生隔离端到端

真实创建 Windows CPython 3.12 环境，完成固定依赖安装、Reader 自检、许可证与工具文件下载校验。
使用真实 SQLCipher 创建 3 个全新合成加密数据库（会话、联系人、消息表），没有用户内容。
只把获取动作替换为复制合成材料，没有执行真实进程扫描或获取工具。

通过真实后台 `start` 验证：检查进入待授权，确认前零获取、零配置；未勾选和错误令牌被拒；
一次合成获取后，由真实 Reader 完成导入、覆盖验证、setup、doctor，最终 `live_database_read_ok=true`。
再次检查复用已有配置，没有第二次获取。每次使用独立账号标识与配置目录，避免复用上次样例造成假通过。
脚本位于忽略目录 `.run/windows_fixture.py`、`.run/windows_integration.py`、`.run/windows_mutation.py`。

## 浏览器验证及降级

先尝试 Chrome 扩展，以「🧪 Windows绿泡泡接入」具名会话创建后台标签，
返回 `Capability is not available: visibility`，无法保证标签保持后台。
未取消后台约束重试，未接管用户标签、激活窗口或直连用户 Chrome。
因此降级为独立 chrome-headless-shell、临时 profile、全程无头，只连接自己启动的隔离进程。
此验证不需要用户登录态，没有使用用户 cookie。

隔离服务仅监听 `127.0.0.1:18793`，加载真实 HTML/CSS/JS、真实接入 API、真实 Reader 和合成数据库；
其他业务接口为空数据 fixture。11 项页面断言全部通过：Windows 只读说明、未确认按钮禁用、
检查不获取不配置、错误令牌 409、390px 无横向溢出、确认后按钮启用、只获取一次且验证后发布配置、
终态无忙碌/错误、重开保持已验证、重查不重复获取、无未捕获脚本异常。

以下截图逐张目检，桌面与窄屏的授权说明和已验证状态可读，窄屏长面板可内部纵向滚动：

- `output/wechat-windows/authorization-desktop.png`
- `output/wechat-windows/authorization-mobile.png`
- `output/wechat-windows/ready-desktop.png`
- `output/wechat-windows/ready-mobile.png`
- `output/wechat-windows/results.json`

产物处于忽略目录。验证后关闭自己启动的无头浏览器与隔离服务，不操作原有 8787 服务。

## 生效及未验证边界

原始 Windows 实现已包含在主分支提交 `5de5023`。第 1 轮审查修复已带入本任务工作区，
尚未提交、合并或应用到主目录运行服务；本轮修复的验证及生效边界见 `wechat-review-fixes.md`。
修复需由用户验收后合入实际运行目录并重启 kb-web 才会生效，本轮没有代为操作。
Windows 首次先运行以下命令准备采集器，再在接入页重新检查：

```powershell
.\kb.cmd setup
```

没有对用户实际绿泡泡进程、真实聊天库或具体客户端版本执行获取验证；
不能据此承诺所有 4.x 可用，不同版本内存布局可能变化。
真实客户端权限不同或版本不兼容时会停止，不自动提权、不改走实验性断点捕获。
已有兼容私有材料时应优先导入验证，而非重复获取。
