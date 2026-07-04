# knowledge-base

个人知识库:把 obs vault 里的内容(微信公众号、YouTube 视频摘要)统一索引,
支持关键词 + 语义混合检索和意图问答。

## 架构

```
obs vault (事实来源, Markdown + frontmatter)
  ├── source/dr-gzh/          公众号文章 (WeChatOA_Aggregation 导出)
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

1. WeChatOA_Aggregation 抓新文章 → `export_obsidian.py` 增量导出到 obs
2. `kb index` 增量索引新文档(内容 hash 判断,秒级)
3. `kb extract` 给新文章打标
4. 随便搜

## 设计决策备忘

- **obs md 是事实来源**,SQLite 只是索引(可随时删掉重建):换检索栈不锁死数据
- **中文 FTS**:FTS5 unicode61 不切中文词,入库/查询都过 jieba(`store.tokenize`)
- **RRF 融合**(k=60):不依赖两路打分量纲,免调参
- **doc 分 = 最高 chunk 分**:避免长文靠块数刷分
- **embedding 可换**:接口在 `kb/embedder.py`,换模型后 `kb index --reembed`
- 规模预估 ~15 篇/天,5 年 ~20 万 chunk,sqlite-vec 精确搜索毫秒级,不需要独立向量库
