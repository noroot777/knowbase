# Knowbase

个人知识库：把绿泡泡公众号、X 大V观点、RSS 订阅、网页专栏、邮件简报、YouTube 视频摘要统一索引，支持关键词 + 语义混合检索、意图问答、每日日报、工具图鉴、收藏此段与 AI 分诊。所有内容以 Obsidian vault 里的 Markdown 为事实来源，SQLite 只是索引（随时可删可重建）。

## 免责与使用边界

**先读这一节再跑。** 这个项目为了把公众号文章收进本地知识库，会在你这台机器上做下面这几件事——它们的影响范围比「一个检索工具」大得多：

- **请求 macOS 管理员授权，并以 root 运行 wxkey**（从固定源码构建）去提取**本机微信客户端的数据库密钥**。这个过程可能让微信退出或重启，并创建一份重签名副本。
- 用 **SQLCipher 只读打开解密后的消息库**，从里面发现公众号图文推送（私聊、群聊不作为文章采集）。
- 按推送里的文章链接**批量抓取公众号文章全文与图片**，存进本地 SQLite 和 Markdown。

### 边界

- **仅限你本人的微信账号，仅在你自己的本机运行。** 不要拿它去读别人的微信，也不要在别人的机器上跑。页面上那个勾选框（「这是我本人的微信」）不是形式——它就是这条边界本身。
- **抓下来的文章仅供个人存档与检索，不得再分发。** 文章的版权属于原作者。把采集结果公开发布、打包转售、做成对外服务都超出本项目的用途。
- **使用者自行承担风险。** 批量抓取和读取本机客户端数据库可能违反微信的服务条款，也可能触及你所在地的法律法规（著作权、计算机信息系统、个人信息保护等）。是否使用、怎么用，由你自己判断并承担后果。
- **本项目不提供任何保证。** 按 [LICENSE](LICENSE) 的 MIT 条款，软件按「原样」提供，不保证可用性、正确性、数据不丢失，也不对任何直接或间接损失负责。采集依赖的上游工具和微信客户端版本随时可能变化，昨天能跑今天跑不通是常态。
- 维护者**不会**为规避平台风控、绕过授权或抓取非本人账号的内容提供帮助。

不接受上面这些，就别用这个项目——只做索引和检索（不碰微信）的那部分可以单独用：把已有的 Markdown 目录挂给它，见「[Docker 部署](#docker-部署)」。

## 没有鉴权：默认只听回环，别直接暴露到网上

**kb-web 不做任何身份验证**——全仓库没有一行鉴权代码。它唯一的保护是**默认只绑在回环地址上**（`KB_HOST` 默认 `127.0.0.1`）。

把 `KB_HOST` 改成 `0.0.0.0`、或者把 8787 端口映射出去之后，**任何能连上这个端口的人**都可以读走整个知识库的全文与笔记、调 `/api/chat` 花掉你配置的模型 API key、触发采集管道、改配置、删库里的内容。

要从别处访问就走 **SSH 端口转发**（`ssh -L 8787:127.0.0.1:8787 …`）、私有网络，或者在前面放一个**自己带鉴权的反向代理**。细节见 [SECURITY.md](SECURITY.md)。

## 快速开始

```bash
# Mac 本机（推荐，可用公众号采集）
./kb.sh start                   # 起面板 http://127.0.0.1:8787
./kb.sh sync                    # 跑全流程（采集→导出→索引→打标）

# Docker（索引和检索，不含公众号采集）
docker compose up -d --build    # http://127.0.0.1:8787
```

面板首页引导完成供应商和模型配置后即可使用。`./kb.sh` 不带参数打印全部命令。

## 功能一览

### 多来源采集

知识库主干不认识任何具体来源，只认 `kb/collectors/` 的采集器契约。新增来源写个实现即可，管道一行不改。

| 来源 | 说明 |
|---|---|
| **绿泡泡公众号** | 读本机绿泡泡客户端已同步的推送，提取文章链接后按链接抓正文。不需要凭证或代理 |
| **X 大V观点** | 首次导入 51 个 X 账号；支持增删改、停用、单源同步；默认桥接依赖外部服务，可更换全局模板或单账号专用 feed |
| **RSS 订阅** | 首次导入 9 个订阅地址；标准 RSS/Atom，内容不足全文时标注摘要 |
| **网页专栏** | 没有 RSS 的站点（如 Anthropic news），按带日期的链接组识别文章列表页 |
| **邮件简报** | TLDR AI 等汇总页式简报，从 RSS 拿期号、从落地页拿每条简讯正文 |
| **YouTube 视频** | 自带 entity/topic 标签的摘要，零成本入库 |

删除订阅保留已收录内容，重启不会恢复已删除的默认项。来源扩展方法见 [来源模块设计](docs/source-modules.md)。

### 英文内容自动翻译

订阅类来源（大V观点 / RSS / 网页专栏 / 邮件简报）抓到的英文条目在导出进 vault **之前**先整篇翻译：标题和正文都译，专有名词保留原文。中文条目直接跳过（不花钱）。一轮译不完的（成本上限 40 条/轮）留到下一轮译完再写进 vault，不会先以英文落盘；重试三次仍然译不动的按英文原文导出。翻译模型在顶栏「模型」的「来源 · 英文内容翻译」阶段里配。

### 混合检索与意图问答

```bash
uv run kb search "把书变成skill的工具"           # FTS + 向量 RRF 融合
uv run kb search "画图" --tag topic/skill工具    # 带标签过滤
uv run kb ask "有没有能自动生成架构图的开源工具?"  # 检索 + Claude 带引用回答
```

`kb ask` / web 问答不是单纯 RAG：先用模型把问题解析成查询计划——
- **穷举类**（"最近一周都提过哪些开源项目"）→ SQL 全量聚合，一篇不漏
- **内容类**（"X 怎么用 / 哪篇讲了 X"）→ top-8 混合检索后综合

### 每日日报

左侧「每日日报」：点「同步全部并生成」，或只用已入库的内容「直接生成」。日报按天写进 `<vault>/daily/YYYY-MM-DD.md`，可以直接在 Obsidian 里读。

- 覆盖的是「上一份日报的截止时刻 → 这一次的生成时刻」这段时间里**发布**的内容，不是某个自然日，也不设最长回溯上限——停服四天之后那一份就覆盖这四天
- 发布得更早、这一轮才抓进来的内容进「补录」，单独一节，不混进要点
- 可设定时（默认关闭），到点自动同步 + 生成；一天一份，临时故障当天隔半小时补两次
- 有来源没同步成功时日报照样出，但完成提示、面板横幅、日报文件本身三处都会说清楚
- 素材取窗口内入库的原始来源，综述里带 `[n]` 引用回原文；模型在「日报 · 当日综述」阶段里配

### 工具图鉴

首页默认视图。从提取阶段的 entity 数据生成条目卡片（开源项目、产品、模型、研究、公司），支持按类型 / 话题 / 来源筛选和搜索，卡片上用正字计数显示提及次数。点击打开详情抽屉，展示摘要、相关文章和标签。

### 三张图谱

`./kb.sh graph` 生成三张单文件 HTML（数据内嵌，可脱离 kb-web 用浏览器直接打开）：

- **开源项目图谱** — 按固定用途分类表分区，全量条目
- **Skill 图谱** — 工具/技能条目的用途归类
- **话题图谱** — 最近两周公众号在聊什么（实体共现网络，滚动窗口）

归类结果长期缓存，日常重生成零 LLM 调用（约 6 秒）；新条目进来才判新增的几个。同步时 wiki 阶段本来就会带上。

### 收藏此段

在知识库文章、日报正文、问答回答、订阅阅读器、图鉴条目里**选中文字即弹出「收藏此段」**，一处实现、五处共用。收藏的引文按屏幕上那份文字原样存（保留换行缩进），前后文锚点按压平空白的坐标系匹配，点收藏条目可回跳到原文对应位置。

收藏架（左侧「我的收藏」）：
- 三种状态：inbox 待试 / done 已处理 / archived 沉底
- **行动卡**：收藏瞬间后台线程调模型提炼「这条收藏要试的东西」（名称、要做什么、命令、预计时长、类型）
- **AI 分诊**：一键把所有 inbox 条目整体分成 hero / quick / deep / dup / cold / quotes，带条目指纹缓存
- 陈旧条目（待试超 60 天）提示一键沉底
- 收藏模型在「收藏 · 行动卡 / 分诊」阶段里配

### 软广判定

提取打标时顺带判定软广（promo），不单开一次调用。判定结果落 `documents.promo_level`，下游（条目化 gate、检索、wiki、日报素材）统一读这一列。hard 软广在检索和日报中被过滤。

### 管道与同步

web 界面的「管道」tab 一键搞定全流程：

**抓取** → **导出** → **索引**（FTS + 向量，增量）→ **提取**（entity/topic/摘要/软广，tag 自动回写 obs frontmatter）

- 同步历史：最近几轮运行记录保留在磁盘，面板可打开查看各轮详情
- 失败明细：缺正文的条目可点开原文核对，也能当场「不再抓取」（永久排除）
- 重试失败项：按上次运行的失败状态编排回填空正文 → 重建索引 → 重跑提取
- 手动或自动同步共用管道运行锁，单源失败独立退避，忙碌时定时任务顺延
- 面板显示各阶段进度/待处理数/采集器健康状况

### LLM 阶段配置

每个用到大模型的阶段各配各的——问答回答/规划/研究、收藏分诊/行动卡、提取打标、翻译、条目化 gate/拆条目、wiki 综述、日报——注册表在 `kb/llm_stages.py`，面板在顶栏「模型」。选项按供应商分组，选的是哪家就用哪家的 key。

### OpenAI 兼容协议

顶栏「设置 → API Key」的 OpenAI 兼容区域提供 **Responses → Chat Completions** 开关。开启时统一文本调用转为供应商的 `/chat/completions`；关闭时直接调用 `/responses`。保存后即时生效，无需重启。

## 架构

```
obs vault (事实来源, Markdown + frontmatter)
  ├── source/dr-gzh/          公众号文章 (collector/ 导出)
  ├── source/dr-youtube-dig/  YouTube 摘要
  ├── source/social/          X 大V观点
  ├── source/rss/             RSS 订阅
  ├── source/web/             网页专栏
  ├── source/digest/          邮件简报
  └── daily/                  每日日报 (不回索引)
        ↓ kb index                      ↓ kb extract
  SQLite (data/kb.db)           LLM (按阶段选模型)
  ├── documents / doc_tags     提取 entities/topics/summary/promo
  ├── chunks + FTS5(jieba)       ├→ 入库 (可过滤可穷举)
  ├── chunk_vec (sqlite-vec,     └→ --writeback 回写 obs frontmatter
  │   bge-m3 本地 embedding)
  ├── entities → 图鉴卡片
  └── favorites (收藏/行动卡)
        ↓ kb search / kb ask
  FTS + 向量 → RRF 融合 → doc 聚合 [→ Claude 综合回答]
```

分工：**向量管"描述意图找内容"（语义模糊匹配），FTS+tag 管"精确命中和穷举"。** 标签提取（entity/topic）是搜索质量的大头，与是否用向量无关。

## Docker 部署

安装 Docker Engine / Docker Desktop 和 Compose v2，在项目根目录运行：

```bash
docker compose up -d --build
# 打开 http://127.0.0.1:8787，配置供应商和各阶段模型
docker compose logs -f --tail 100
```

默认使用持久卷保存配置、索引、采集器数据、文章和模型缓存。已有文章目录可在 `.env` 中配置 `KB_VAULT_PATH`（参考 `.env.example`），再运行 `docker compose exec knowbase kb index --no-vector` 建立关键词索引。

**公众号采集只能在 Mac 本机跑。** 推荐在 Mac 完成采集和导出，Docker 挂载导出的文章目录做索引和分析。镜像已包含应用和采集器依赖，首次向量索引会额外下载约 2GB 模型。迁移、挂载和备份方法见 [Docker 部署说明](docker/README.md)。

## MCP（在 Claude Code / Desktop 里直接问知识库）

```bash
claude mcp add kb --scope user -- uv run --directory ~/code/knowbase kb-mcp
```

注册后对话里可用工具：`kb_search`（混合检索，支持 since/until 时间过滤）、`kb_enumerate`（全量枚举实体/文章，穷举类问题用这个，不漏）、`kb_read`（读全文）、`kb_entities`（列实体）。不提供 ask——调用方本身是 LLM，检索完自己综合。

## 用法

```bash
# 索引与提取
uv run kb index                # 扫 vault，增量索引 + 补向量（首次下载 bge-m3 ~2GB）
uv run kb index --no-vector    # 只建 FTS（快，不依赖模型）
uv run kb extract --limit 20   # LLM 提取（公众号文章；youtube 自带标签免提取）
uv run kb extract --writeback  # 提取并回写 tag 到 obs md

# 检索
uv run kb search "把书变成skill的工具"
uv run kb search "画图" --tag topic/skill工具
uv run kb search "xxx" --entity book-to-skill
uv run kb search "xxx" --source dr-gzh --json

# 问答与统计
uv run kb ask "有没有能自动生成架构图的开源工具?"
uv run kb stats

# Web 界面
uv run kb-web                  # http://127.0.0.1:8787
```

### 操作入口（`./kb.sh`）

```bash
# 日常
./kb.sh start                   # 起面板（后台守护进程）
./kb.sh sync                    # 全流程（采集→导出→索引→打标），后台跑
./kb.sh sync --channel rss      # 只跑某一个来源
./kb.sh status                  # 服务/同步进度/来源 一屏看全

# 服务
./kb.sh start | stop | restart
./kb.sh logs [行数]

# 单跑某一步
./kb.sh collect [--since 2026-08-01 --until 2026-08-26] [--targets a,b]
./kb.sh export / index / extract
./kb.sh graph                   # 重建三张图谱页（前台约 6 秒，零模型调用）

# 订阅管理
./kb.sh accounts list|discover|add|remove|enable|disable [名字]
```

## 公众号采集

通过 Rion Reader 只读本机绿泡泡客户端的公众号图文推送，提取文章链接后按链接抓正文。不需要凭证、代理或逐号授权。

- 首次在面板的「绿泡泡接入」窗口里检查工具、获取数据库 key
- 发现和正文是两步：先增量更新本机文章索引，再去重后只下载新文章和缺失正文
- 同步范围（这次抓哪些公众号）的手动选择保存在当前浏览器
- 失败从 5 分钟开始退避，最长一天，原文不可用则一周后复查
- 消息 ID 与发布时间分开，延迟同步的旧文章也能被发现

请先在 Mac 绿泡泡里订阅并同步需要采集的公众号——本机没有推送的号会显示为覆盖缺口。

### 首次绿泡泡接入

首页「完成首次配置」、设置菜单「绿泡泡接入」和同步中心均可进入。

1. 已有 Reader 配置可读时直接复用
2. 没有 Reader 时，项目自动下载校验固定版本源码，用 uv 安装隔离的 SQLCipher / Zstandard 环境
3. 确实缺 key 时，自动构建固定版本 wxkey；页面说明影响，勾选确认后点「自动获取 key 并验证」
4. 获取前保存本机加密数据库备份，获取后自动验证、发布 Reader 配置

当前自动获取限 macOS 本机访问，要求已登录自己的绿泡泡，以及 uv、Go、Apple Command Line Tools（LLDB）。

实现固定到 [Rion Reader `f7d60b6`](https://github.com/Rion-Wu-tech/wechat-intelligence-hub/tree/f7d60b6d94c70f09ad92b89215ce40b2dd752e7f/projects/rion-wechat-reader) 和 [wxkey `9b70eec`](https://github.com/r266-tech/wxkey/tree/9b70eecdde47a7172b19465c3f977c86b6050e8a)。

> 另一条采集方式（逐号文章凭证，靠 mitm 从 Mac 绿泡泡里截）在 `wechat-oa` 分支上，不做推荐、不再跟进主干。

## 跑测试

两侧各自一套，都是标准库 `unittest`，不需要联网、不需要模型 key（用到大模型的地方走本地桩）：

```bash
# 知识库侧（约 530 条）
uv run python -m unittest discover -s tests -t . -q

# 采集器侧（约 160 条）。没有 .venv 先跑一次 ./collector/setup.sh
cd collector && ./.venv/bin/python -m unittest discover -s tests -t . -q
```

有些测试按环境条件自动 skip：
- **浏览器回归**要 `node` 和一个 `chrome-headless-shell`，按 `HEADLESS_SHELL=<路径>` 指定
- **不需要 bge-m3**：测试把向量编码打了桩，整套跑完不会下载模型

lint：`uvx ruff@0.16.7 check .`（规则集在 `pyproject.toml`）。CI 在 ubuntu 上跑，见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。

## 设计决策备忘

- **obs md 是事实来源**，SQLite 只是索引（可随时删掉重建）
- **中文 FTS**：FTS5 unicode61 不切中文词，入库/查询都过 jieba
- **RRF 融合**（k=60）：不依赖两路打分量纲，免调参
- **doc 分 = 最高 chunk 分**：避免长文靠块数刷分
- **embedding 可换**：接口在 `kb/embedder.py`，换模型后 `kb index --reembed`
- 规模预估 ~15 篇/天，5 年 ~20 万 chunk，sqlite-vec 精确搜索毫秒级，不需要独立向量库

## 许可

**MIT**
