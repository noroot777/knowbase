# knowledge-base

个人知识库:把 obs vault 里的内容(微信公众号、YouTube 视频摘要)统一索引,
支持关键词 + 语义混合检索和意图问答。

## 架构

```
obs vault (事实来源, Markdown + frontmatter)
  ├── source/dr-gzh/          公众号文章 (collector/ 导出)
  ├── source/dr-youtube-dig/  YouTube 摘要 (自带 entity/topic 标签)
  └── source/gzh/             公众号实时保存 (gzh_saver)
        ↓ kb index                      ↓ kb extract
  SQLite (data/kb.db)           claude CLI (haiku)
  ├── documents / doc_tags     提取 entities/topics/summary
  ├── chunks + FTS5(jieba)       ├→ 入库 (可过滤可穷举)
  └── chunk_vec (sqlite-vec,     └→ --writeback 回写 obs frontmatter
      bge-m3 本地 embedding)
        ↓ kb search / kb ask
  FTS + 向量 → RRF 融合 → doc 聚合 [→ Claude 综合回答]
```

分工:**向量管"描述意图找内容"(语义模糊匹配),FTS+tag 管"精确命中和穷举"。**
标签提取(entity/topic)是搜索质量的大头,与是否用向量无关。

## 用法

```bash
uv run kb index                # 扫 vault,增量索引 + 补向量(首次下载 bge-m3 ~2GB)
uv run kb index --no-vector    # 只建 FTS(快,不依赖模型)
uv run kb extract --limit 20   # LLM 提取(公众号文章;youtube 自带标签免提取)
uv run kb extract --writeback  # 提取并回写 tag 到 obs md

uv run kb search "把书变成skill的工具"           # 混合检索
uv run kb search "画图" --tag topic/skill工具    # 带标签过滤
uv run kb search "xxx" --entity book-to-skill   # 按实体精确过滤
uv run kb search "xxx" --source dr-gzh --json   # 来源过滤 / JSON 输出

uv run kb ask "有没有能自动生成架构图的开源工具?"  # 检索+Claude 带引用回答
uv run kb stats

uv run kb-web                  # Web 界面: http://127.0.0.1:8787
```

## MCP(在 Claude Code / Desktop 里直接问知识库)

```bash
claude mcp add kb --scope user -- uv run --directory ~/code/knowledge-base kb-mcp
```

注册后对话里可用工具:`kb_search`(混合检索,支持 since/until 时间过滤)、
`kb_enumerate`(全量枚举实体/文章,穷举类问题用这个,不漏)、`kb_read`(读全文)、
`kb_entities`(列实体)。不提供 ask —— 调用方本身是 LLM,检索完自己综合。

## 问答的意图规划

`kb ask` / web 问答不是单纯 RAG:先用 haiku 把问题解析成查询计划——
- **穷举类**("最近一周都提过哪些开源项目")→ `mode=enumerate_entities`,
  SQL 全量聚合(时间/来源/实体类型过滤),一篇不漏,再由 Claude 分类整理
- **内容类**("X 怎么用/哪篇讲了 X")→ `mode=retrieve`,top-8 混合检索后综合
解析失败自动退回 retrieve,保底可用。

## 日常流程

web 界面(`uv run kb-web` → http://127.0.0.1:8787)的"管道"tab 一键搞定:

**抓取**(委托来源采集器,不在线则跳过)→ **导出**(采集器 → obs md)
→ **索引**(FTS + 向量,增量)→ **提取**(entity/topic/摘要,tag 自动回写 obs frontmatter)

抓取/导出两阶段只跟 `kb/collectors/` 的采集器契约打交道,知识库自身不认识
任何具体来源。当前公众号采集器有凭证和本地数据库两条通道(`kb/collectors/` + `collector/`),
微信特有的一切——凭证怎么过期、反爬要等多久、文章先落哪个库——都关在
那个文件里;换来源或换抓取方式只写一个新实现,管道一行不改。

面板显示各阶段进度/待处理数/采集器健康状况(文案由采集器给,面板只渲染),
「管理公众号」弹窗可以搜索添加、停用、移除订阅,并实时显示每个号的文章凭证
够不够——采集器实现了 `TargetManager` 契约才显示这些,没实现的来源自动隐藏。

原凭证通道要逐个号授权:获取列表用的凭证得在 Mac 微信里打开该号一篇文章、
由 wxdown-service 截下来。所以同步前先 `./kb.sh prep`,它把 wxdown 起好、
切好路由,然后一边等你点文章一边显示还差谁;面板那个弹窗也会每 5 秒自动刷新。

采集器是一次性 CLI,平时不占常驻进程——点"同步"时才起子进程,跑完就退。
唯一常驻的是抓凭证阶段的 wxdown。搜索公众号/翻列表用的那套扫码凭证过期时
跑 `./kb.sh login`(跟上面每个号的文章凭证是两回事)。

所有操作走 `./kb.sh`(不带参数看命令列表):`prep` / `status` / `creds` /
`collect` / `export` / `accounts` / `web start|stop` / `wxdown start|stop`。
服务都是后台守护进程(自成会话),起完终端就能关,不占前台。

等价的 CLI:
1. `cd collector && ./.venv/bin/python -m cli collect` 抓新文章,再 `... -m cli export` 导出到 obs
2. `kb index` 增量索引新文档(内容 hash 判断,秒级)
3. `kb extract` 给新文章打标
4. 随便搜

### 公众号双通道同步

在「管道 → 全流程同步」中选择「同步通道」，再选择时间段和公众号：

- **凭证抓取（原通道）**：默认选项，继续用 wxdown 获取每个公众号的文章凭证。
- **微信本地数据库**：通过 Rion Reader 只读本机已同步的公众号图文推送，提取文章链接后复用正文抓取。无需 wxdown 或逐号文章凭证；首次可在面板的「微信接入」中检查、准备工具并获取数据库 key。日常同步不可读时显示错误，不自动重新获取密钥或切换通道。

两通道共用知识库中的公众号订阅列表和文章库。请在 Mac 微信订阅并同步需要采集的公众号；本机没有推送的号会显示为覆盖缺口，不算请求失败。微信数据库通道先增量更新本地文章索引，再按文章发布时间和所选公众号查询，去重后只下载新文章及缺失正文；「不限时间」查询整个索引，指定区间包含结束日全天。订阅前的历史和未同步消息不能保证补齐。

手动选择会保存在当前浏览器；「每日定时」旁可单独设置定时通道。失败重试沿用上次运行的通道、公众号和时间范围。文章按 `__biz + mid + idx` 去重并保留原档案 ID，切换通道不会重新创建同一篇文章。

首次同步批量索引本机已有的公众号图文推送；后续按每个消息分片/表的消息 ID 更新新增推送，公众号匹配静默进行。索引可包含未选公众号的文章元数据，但它们不会进入正文下载和知识库导出。修改时间范围或选择公众号时直接查询索引，不重新逐号翻历史。消息 ID 与文章发布时间分开，延迟同步的旧文章也能被发现；检测到库重建、消息数量或边界改变时会重新校验，读取失败不推进对应游标。

同步进度分为「发现文章」和「下载正文」，显示推送读取数、正文完成/总数、成功/失败、已有正文及等待重试数。批量正文请求保留节流；失败从 5 分钟开始退避，最长一天，原文明确不可用则一周后复查。正文失败不影响文章索引及已完成文章发现的公众号「最近采集」时间，下次到期后直接从索引重试；这类待补正文显示在「待处理明细」，不会误报账号失败或要求立即重跑整轮同步。导出阶段同样列出缺正文的待补明细，不会因为发布时间早就标为删除；用户主动删除的文章仍不会被重新采集。

不可解析的单条推送单独记录位置、内容指纹及可提取的文章 URL 归属线索，允许正常数据继续推进增量游标。后续只核对这些异常行，内容改变后自动再解析；能确认属于未选公众号的内容异常不进入同步提示。有不可读推送的所选公众号会在待处理明细中显示名称和现存条数，即使已有历史文章也会显示。已有覆盖的账号统一以本轮发现检查完成时间更新最近采集，不因是否有新文章或旧坏推送而采用不同口径；真正读取失败的账号保留原时间，新分片失败也按同一会话关联判断。仅能唯一匹配同名会话时明确标注归属待核实，不据此采集文章；同名冲突只在一条汇总中列出候选账号，条数不分摊给各号。完全无归属线索的异常仅汇总数量，不改变其他账号的覆盖状态。正常未同步账号仍只提示「在微信关注并同步后可采集」，不列入待处理明细；若同时有归属未明的推送，则提示「未匹配到本机推送」，说明仅在未关注或未同步时需要关注/同步，那批未知推送也可能来自这些账号。数据库读取或依赖故障仍作为错误报告，不会被当成坏消息跳过。

旧版本缺少 `chksm` 导致本地文章误退休时，可先运行 `python collector/scripts/recover_local_articles.py --db <采集器数据库>` 预览，再加 `--apply --backup <备份文件>` 复位。这只恢复重试资格，正文会在使用修复版本下次同步时补抓。

```bash
./kb.sh sync --channel wechat_local
./kb.sh collect --channel wechat_local --since 2026-09-01 --until 2026-09-15 --targets 公众号名
./kb.sh sync --channel wechat_oa

# 采集器 CLI 对应选项（用于直接排查）
cd collector
./.venv/bin/python -m cli status --channel local
./.venv/bin/python -m cli collect --channel local --targets 公众号名
```

默认复用 `${CODEX_HOME:-~/.codex}/skills/wechat-cli/scripts/reader.sh` 或已安装的 `rion-wechat-cli`，需要支持只读 `schema` / `sql`；也可使用「微信接入」准备的独立运行环境，无需安装 skill。自定义安装可通过 `RION_WECHAT_CLI_BIN` 指定可执行文件。升级后在 `collector` 运行 `uv sync --frozen` 安装压缩消息所需的 `zstandard`。文章元数据及读取游标保存在采集器私有 SQLite 的 `local_wechat_articles` / `local_wechat_sources` 表，不持久化原始推送 XML、私聊或数据库密钥；旧版 JSON 匹配缓存不再参与采集。

### 首次微信接入

首页「完成首次配置」、设置菜单「微信接入」和同步管理页均可进入。先配置供应商和模型／智能体，再点「检查并接入微信」。获取 key 是独立的本机步骤，不使用供应商 API，也不向模型发送密钥。

1. 已有 Reader 配置可读时直接复用；已有 wxkey 材料则验证并导入，不覆盖旧配置和 Profile。
2. 没有 Reader 时，项目自动下载校验固定版本源码，用 uv 安装隔离的 SQLCipher / Zstandard 环境。多个账号必须在页面选择，不能自动合并。
3. 确实缺 key 时，自动构建固定版本 wxkey。页面说明进程访问、微信退出／重启及重签名副本的影响，勾选确认后点「自动获取 key 并验证」。管理员密码只在 macOS 系统授权框输入。
4. 获取前保存本机加密数据库和 sidecar 副本（活动文件副本不等于一致性备份），单次获取最多 600 秒，授权等待另有 180 秒。获取后自动验证、发布 Reader 配置并检查采集器实际使用的 Reader。成功后点「去同步公众号」。

当前自动获取限 macOS 本机访问，要求已登录自己的微信，以及 uv、Go、Apple Command Line Tools（LLDB）。Go 1.26.5 工具链可由已有 Go 自动下载。远程部署不能替浏览器所在电脑读取微信。系统授权、登录和版本不兼容不能保证无人值守完成。

独立工具位于 `~/.local/share/knowledge-base/wechat`，可用 `KB_WECHAT_RUNTIME` 更改（Web 服务和采集器需保持一致）；密钥与 Reader 配置仍位于本机私有配置目录，不回传页面。状态、构建收据和加密备份留在私有运行目录，备份可在确认不再需要后自行移除。关闭页面不取消后台工作；刷新页面恢复进度。获取失败保留上游恢复锁，不能自动重试或清除锁。已有配置失效时先修复配置，不因读取失败重新获取 key。

实现固定到 [Rion Reader `f7d60b6`](https://github.com/Rion-Wu-tech/wechat-intelligence-hub/tree/f7d60b6d94c70f09ad92b89215ce40b2dd752e7f/projects/rion-wechat-reader) 和 [wxkey `9b70eec`](https://github.com/r266-tech/wxkey/tree/9b70eecdde47a7172b19465c3f977c86b6050e8a)。下载文件校验 SHA256，wxkey 从固定源码构建并记录二进制 SHA256；系统授权路径以 root 调用并设置 `WXKEY_NO_ELEVATE=1`，该版本会跳过管理员密码 Keychain 路线，不启用修改原微信或关闭 SIP 的开关。兼容适配只修正“可读但未分类的辅助库被误计为未解密”的判断，真正读失败仍阻止接入。

**文章获取与 skill 的关系：** `wechat-intelligence-hub` skill 用于聊天情报、日报和商机分析，本项目采集文章不依赖它。数据库 key 用于读取公众号推送；全文及图片通过原文网页下载，不需要微信缓存图片的解码 key，也不保证补齐未同步到本机的历史。

## 设计决策备忘

- **obs md 是事实来源**,SQLite 只是索引(可随时删掉重建):换检索栈不锁死数据
- **中文 FTS**:FTS5 unicode61 不切中文词,入库/查询都过 jieba(`store.tokenize`)
- **RRF 融合**(k=60):不依赖两路打分量纲,免调参
- **doc 分 = 最高 chunk 分**:避免长文靠块数刷分
- **embedding 可换**:接口在 `kb/embedder.py`,换模型后 `kb index --reembed`
- **每个用到大模型的阶段各配各的**:问答回答/规划/研究、收藏分诊、提取打标、
  条目化 gate/拆条目、wiki 综述 —— 注册表在 `kb/llm_stages.py`,面板在 kb-web
  顶栏「模型」。选项按供应商分组,**选的是哪家就用哪家的 key**(同一个 id 在两家
  中转站都可能有,只存模型名就只能靠前缀猜,猜错是拿 A 家 key 打 B 家的站)。
  没配的阶段沿用注册表里的内置默认,也就是这套设置出现之前各文件里写死的那个 id。
- 规模预估 ~15 篇/天,5 年 ~20 万 chunk,sqlite-vec 精确搜索毫秒级,不需要独立向量库
