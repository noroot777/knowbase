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
- **微信本地数据库**：通过 `wechat-cli` 只读本机已同步的公众号图文推送，提取文章链接后复用正文抓取。无需 wxdown 或逐号文章凭证，但需已有可用的微信数据库接入；不可读时显示错误，不自动获取密钥或切换通道。

两通道共用知识库中的公众号订阅列表和文章库。请在 Mac 微信订阅并同步需要采集的公众号；本机没有推送的号会显示为覆盖缺口，不算请求失败。微信数据库通道先增量更新本地文章索引，再按文章发布时间和所选公众号查询，去重后只下载新文章及缺失正文；「不限时间」查询整个索引，指定区间包含结束日全天。订阅前的历史和未同步消息不能保证补齐。

手动选择会保存在当前浏览器；「每日定时」旁可单独设置定时通道。失败重试沿用上次运行的通道、公众号和时间范围。文章按 `__biz + mid + idx` 去重并保留原档案 ID，切换通道不会重新创建同一篇文章。

首次同步批量索引本机已有的公众号图文推送；后续按每个消息分片/表的消息 ID 更新新增推送，公众号匹配静默进行。索引可包含未选公众号的文章元数据，但它们不会进入正文下载和知识库导出。修改时间范围或选择公众号时直接查询索引，不重新逐号翻历史。消息 ID 与文章发布时间分开，延迟同步的旧文章也能被发现；检测到库重建、消息数量或边界改变时会重新校验，读取失败不推进对应游标。

同步进度分为「发现文章」和「下载正文」，显示推送读取数、正文完成/总数、成功/失败、已有正文及等待重试数。批量正文请求保留节流；失败从 5 分钟开始退避，最长一天，原文明确不可用则一周后复查。正文失败不影响文章索引及已完成文章发现的公众号「最近采集」时间，下次到期后直接从索引重试；这类待补正文显示在「待处理明细」，不会误报账号失败或要求立即重跑整轮同步。导出阶段同样列出缺正文的待补明细，不会因为发布时间早就标为删除；用户主动删除的文章仍不会被重新采集。

不可解析的单条推送单独记录位置和内容指纹，允许正常数据继续推进增量游标。后续只核对这些异常行，内容改变后自动再解析；未选公众号的内容异常不进入同步提示。数据库读取或依赖故障仍作为错误报告，不会被当成坏消息跳过。

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

默认复用 `${CODEX_HOME:-~/.codex}/skills/wechat-cli/scripts/reader.sh` 或已安装的 `rion-wechat-cli`，需要支持只读 `schema` / `sql`；自定义安装可通过 `RION_WECHAT_CLI_BIN` 指定可执行文件。升级后在 `collector` 运行 `uv sync --frozen` 安装压缩消息所需的 `zstandard`。文章元数据及读取游标保存在采集器私有 SQLite 的 `local_wechat_articles` / `local_wechat_sources` 表，不持久化原始推送 XML、私聊或数据库密钥；旧版 JSON 匹配缓存不再参与采集。

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
