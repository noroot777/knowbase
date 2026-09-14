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
任何具体来源。当前唯一实现是公众号采集器(`kb/collectors/wechat_oa.py` + `collector/`),
微信特有的一切——凭证怎么过期、反爬要等多久、文章先落哪个库——都关在
那个文件里;换来源或换抓取方式只写一个新实现,管道一行不改。

面板显示各阶段进度/待处理数/采集器健康状况(文案由采集器给,面板只渲染),
「管理公众号」弹窗可以搜索添加、停用、移除订阅,并实时显示每个号的文章凭证
够不够——采集器实现了 `TargetManager` 契约才显示这些,没实现的来源自动隐藏。

微信这个来源要逐个号授权:抓正文用的凭证得在 Mac 微信里打开该号一篇文章、
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
