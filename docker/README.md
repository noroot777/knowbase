# Docker 部署

需要 Docker Engine / Docker Desktop 与 Compose v2。建议至少给 Docker 4 个 CPU、
6GB 内存和 15GB 可用磁盘；大批量向量索引需要更多资源。镜像使用 CPU 版 PyTorch，
不需要 GPU。默认只将服务发布到宿主机 `127.0.0.1:8787`。

## 启动与升级

在项目根目录执行：

```bash
# 可选：修改端口、时区及已有目录的挂载位置
cp .env.example .env
docker compose up -d --build --wait
docker compose ps
docker compose logs -f --tail 100
```

打开 <http://127.0.0.1:8787>，在供应商和模型设置中保存 API key、API 地址及各阶段模型。
无需在容器里安装 Claude / Codex CLI。key 与模型配置保存在应用数据卷中，重建容器后保留。
项目没有通用登录鉴权；如需通过服务器域名访问，应由带鉴权的反向代理接入本机端口。

更新源码后再执行 `docker compose up -d --build --wait`。`docker compose down` 只停止并移除
容器，保留命名卷；**`docker compose down -v` 会删除命名卷及其中的数据**。

## 数据与挂载

| 内容 | 容器路径 | 默认命名卷 | 可选 `.env` 宿主机目录 |
| --- | --- | --- | --- |
| SQLite 索引、模型配置、收藏和进度 | `/app/data` | `kb-data` | `KB_DATA_PATH` |
| 采集器文章库、订阅和凭证 | `/app/collector/data` | `collector-data` | `KB_COLLECTOR_DATA_PATH` |
| Markdown vault、图片与 Wiki | `/vault/new-start` | `kb-vault` | `KB_VAULT_PATH` |
| Hugging Face 模型缓存 | `/home/kb/.cache/huggingface` | `model-cache` | — |

Compose 会给命名卷加上项目名前缀。目录挂载请使用已存在的绝对路径，例如：

```dotenv
KB_PORT=8787
KB_VAULT_PATH=/Users/yourname/obs/new-start
```

挂载的是 vault 根目录，里面应包含 `source/dr-gzh/`、`source/gzh/` 或其他已支持的来源目录。
不是直接挂载 `dr-gzh`。索引、文章导出、条目化和 Wiki 使用同一份路径配置。

容器以 UID/GID `1000:1000` 运行。Linux 上的目录挂载须提前给予该用户读写权限，
如对专门新建的空数据目录执行 `sudo chown 1000:1000 /srv/kb/data`。
不要对现有个人 vault 盲目递归改所有者；可通过组权限或 ACL 授予访问权限。
启动器会在挂载不可写时给出具体目录和 UID，而不会静默写到其他位置。
Mac Docker Desktop 通常会处理文件共享映射，仍须在其设置中允许共享该目录。

迁移原生安装时，先停止原服务及采集任务，再复制或挂载原 `data`、`collector/data`
和 vault。不要同时启动两个 Web 服务操作同一份应用／采集器数据库。
仅共享 vault 时，安排 Mac 导出和容器索引先后执行，避免索引半写入文件。

## 导入、索引与检索

使用目录挂载后，直接建立索引；也可以将准备好的 `source` 目录复制进默认卷：

```bash
docker compose cp ./my-vault/source knowledge-base:/vault/new-start/
docker compose exec knowledge-base kb index --no-vector
docker compose exec knowledge-base kb search '你的关键词' --no-vector
docker compose exec knowledge-base kb stats
```

若 `compose cp` 后文件属于 root，给复制的目录授权后再运行需要写回的任务。
关键词索引不下载模型；完整 `kb index` 和语义检索第一次会下载 `BAAI/bge-m3`
（约 2GB），需要能访问 Hugging Face。模型缓存在独立持久卷中，后续无需重复下载。
完整同步仍包含向量阶段，并且提取、条目化、Wiki 和问答需要有效的供应商配置。

导出、Wiki 等子进程直接使用镜像已安装的 Python 依赖，不在运行时重新解析 uv 锁文件。
应用支持 `KB_DATA_DIR`、`KB_VAULT_ROOT`、`KB_VIDEOS_ROOT`；Compose 已设置前两个。
若需处理本机视频项目，在 Compose override 中将视频目录只读挂载到 `/videos`，
并设置 `KB_VIDEOS_ROOT=/videos`。

## 微信接入与文章采集

Docker 内无法从 Mac 微信进程提取数据库 key。页面会显示“请在 Mac 本机接入”并禁用
获取按钮，API 也拒绝启动获取。不要通过特权容器或挂载 Docker socket 绕过此限制。

推荐流程：

1. 在 Mac 原生运行本项目，完成供应商／模型配置和“微信接入”。已有可用 Reader 可复用，
   无需安装 `wechat-intelligence-hub` skill。
2. 在 Mac 通过本地微信数据库通道同步公众号，并导出 Markdown 到 vault。
3. 将 vault 通过 `KB_VAULT_PATH` 挂载到 Docker，执行索引；检索、问答与后续分析在容器运行。

镜像也包含原有凭证通道的采集器，可使用已保存的 `collector/data` 及有效公众号凭证。
凭证获取／更新仍在宿主机完成；镜像不包含 wxdown 桌面配套程序或 Mac Reader。
如需同步宿主机 wxdown 的凭证文件，可增加 `compose.override.yaml`：

```yaml
services:
  knowledge-base:
    environment:
      WXDOWN_CRED_FILE: /run/wechat/credentials.json
    volumes:
      - /absolute/path/to/wxdown/resources/data:/run/wechat:ro
```

挂载整个凭证目录可以跟随文件原子替换；路径须存在且容器用户可读。只读挂载不会阻止
采集器把凭证副本保存到自己的私有数据卷。凭证仍可能过期，状态页会报告实际可用性。
这不使“微信本地数据库”通道在 Linux 容器中可用。

```bash
# 使用采集器自己的 Python 环境
docker compose exec -w /app/collector knowledge-base .venv/bin/python -m cli status
docker compose exec -w /app/collector knowledge-base .venv/bin/python -m cli export
```

## MCP 客户端

容器内页面显示的 `/app` 是容器路径。宿主机的 MCP 客户端请使用 Docker 命令连接，
将下面的项目目录替换为实际绝对路径，服务须已启动：

```json
{
  "mcpServers": {
    "kb": {
      "command": "docker",
      "args": ["compose", "--project-directory", "/absolute/path/to/knowledge-base",
               "exec", "-T", "knowledge-base", "kb-mcp"]
    }
  }
}
```

客户端与命令行须使用同一个 Docker context／Compose 项目名；`-T` 保持标准输入输出可用于 MCP。

## 备份与排查

停止写入后备份应用数据、采集器数据和 vault；仅备份 SQLite 索引无法恢复 API 配置、
收藏、采集状态和文章原件。模型缓存可重新下载，不是唯一数据源。

`docker compose ps` 的 healthy 来自 `/api/health`，只表示 Web 进程可响应，
不代表供应商、网络或微信凭证已就绪。初始化失败先看 `docker compose logs`；
端口冲突修改 `.env` 的 `KB_PORT`。镜像构建需要访问 Docker registry、PyPI、
PyTorch CPU wheel 源和 Debian 包源，网络代理请在 Docker 的构建环境中配置，勿写入镜像。
