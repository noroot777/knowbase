# Docker 支持与验证记录

日期：2026-09-28。任务：FlS9oJq68xaD。

## 实现

- 新增多阶段 Dockerfile、Compose v2 配置、构建上下文白名单及 `.env.example`。
- 应用和采集器分别从锁文件安装到独立虚拟环境。应用使用 editable 安装，保证脚本与静态资源
  仍从 `/app` 读取。PyTorch 保留锁定版本，使用官方 CPU wheel，省去 CUDA 依赖。
- 运行用户为 UID/GID 1000。应用监听容器所有接口，Compose 只发布到宿主机 localhost。
- 应用配置／索引、采集器数据、vault、模型缓存分别持久化。启动器检查目录写权限并初始化新库。
- 文章导出、条目化、Wiki 和索引统一读取环境中的目录配置；本机原有默认路径保留。
- `/api/health` 供 Docker 健康检查。Docker 绿泡泡页面引导本机采集与目录挂载，不启动 Mac key 获取。
  容器的 GET 接口返回固定能力状态，POST 返回 409；原生模式仍保留 localhost／同源限制。
- README 与 `docker/README.md` 包含启动、升级、迁移、备份、凭证目录、MCP 和资源需求说明。

## 实测

测试使用独立 Colima profile `kb-docker-test`（4 CPU、6GB 内存、Linux arm64），
独立 Compose 项目及 `127.0.0.1:18879`，未更改用户默认 Docker context。
所有文章、公众号和设置均为合成测试材料，未挂载个人 vault、密钥或采集数据库。

| 验证项 | 结果 |
| --- | --- |
| Compose 配置解析 | 通过 |
| 最终镜像实际构建 | 通过；Python 3.11、uv 0.11.26 |
| 容器启动与健康检查 | healthy；UID 1000，`kb.__file__=/app/kb/__init__.py` |
| CPU 依赖 | `torch=2.12.1+cpu`、CUDA 为 null，张量计算及 sentence-transformers 导入通过 |
| HTTP 与静态页面 | health、stats、models、stages、channels、绿泡泡状态、首页和 JS 全部 200 |
| 容器绿泡泡边界 | GET 为 `host_required`，无确认令牌；获取 POST 为 409 |
| 采集器 | 独立解释器正常执行 status；未配置凭证时返回明确未就绪状态 |
| 文章导出 | 合成采集器文章成功导出到 `/vault/new-start/source/dr-gzh` |
| 索引与关键词检索 | 两篇合成 Markdown 被索引；`--no-vector` 检索命中目标文章 |
| 强制重建与镜像更新 | 应用索引、文章、模型选择及四个卷的测试标记保留 |
| MCP | 通过 `docker compose exec -T` 初始化并列出 4 个工具 |
| 容器单元测试 | 8 项通过 |
| 原生绿泡泡接入测试 | 19 项通过 |
| 同步通道回归 | 20 项通过 |
| 采集器完整回归 | 118 项通过，包含新增挂载路径测试 |
| 绿泡泡页面交互回归 | 18 项通过，包含 Docker 状态、禁用操作、目录挂载提示 |
| 实际容器浏览器 | 桌面与 390px 手机页面通过；获取按钮禁用，无横向溢出及页面脚本异常 |
| 静态检查 | Python 编译、JavaScript 语法、git diff --check 通过；修改及新增代码均不超过 700 行 |

首次构建因测试 VM 的直连网络下载缓慢中断；配置仅用于测试构建的代理参数后成功。
Docker registry 曾返回 EOF，去掉不必要的外部 Dockerfile frontend 拉取，改用 BuildKit 内置
frontend 后继续构建。代理地址没有写入项目配置或镜像运行环境。

浏览器先检查扩展通道。CUA 返回
`failed to start codex app-server: No such file or directory (os error 2)`，浏览器列表为空，
无法创建具名扩展后台会话。随后按规范降级为 Playwright 独立临时 profile 的无头浏览器，
进程参数含 `--headless` 与 `--no-startup-window`；没有激活、接管或直连用户的普通 Chrome 标签。

截图在忽略目录 `output/playwright/docker-desktop.png`、`docker-mobile.png`，
已检查布局。构建和浏览器测试日志在 `.run/`，不纳入版本库。

## 边界

实际镜像验证平台为 Linux arm64，未进行 amd64 机器的运行测试。未下载完整 bge-m3 模型、
调用真实供应商 API 或使用真实绿泡泡凭证；本轮验证的是安装、导出、关键词索引、协议和持久化。
首次完整向量索引仍需要模型下载及足够内存，LLM 功能需要用户自己的有效供应商配置。

Mac 进程 key 获取、wxdown 配套程序和原生安装在其他仓库的辅助脚本不包含在 Linux 镜像中。
Docker 推荐消费 Mac 导出的 vault；原凭证采集可使用挂载的有效凭证，不能借此启用 Mac 数据库通道。
