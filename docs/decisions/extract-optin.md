# 订阅级的提取开关(kb/extract.py 的 pending_docs + extract_optin.py)

> 决策记录。CLAUDE.md 只留硬约束和一行指针,这里是完整的事故形态、实测证据、
> 回归清单和反向验证红数。

## 为什么要这道闸

- **feeds 那几类来源(rss / social / web / digest)默认不买提取,逐条订阅自己说了
  算**(`subscriptions.extract`,默认 0)。证据是 2026-10-10 在生产库上数的:

  ```
  来源      documents  已提取   贡献的 entity/topic 标签   其中全库独有
  rss            806     778            1474                  1024
  social         298     296             234                    81
  web              5       0               0                     0
  全库标签种类 17397
  ```

  聚合类来源的长文章每篇能抽出一堆只出现过一次的细碎实体 —— rss 那 778 篇一家
  就贡献了 1024 种全库独有的标签(占全库 5.9%),把标签检索和 wiki 主题页稀释成
  噪声。短推文反而集中在几个知名公司上,social 独有标签只有 81 种。
  **污染大户是 rss,不是直觉上的 social** —— 所以口径必须是逐条订阅,整类开或
  整类关都不对:同样是 rss,技术博客值得提取,新闻聚合不值得。

- **不提取 ≠ 进不了知识库。** 索引(FTS + 向量)是独立的一步,照旧全量跑。关掉
  这个开关只少了 `entity/` `topic/` 标签、摘要和软广判定,那篇文章照样被全文和
  语义检索搜到、照样能在问答里被引用。面板上那句说明必须写这一点 —— 没有它,
  「不提取」会被读成「这条订阅的内容进不了知识库」。

- **绿泡泡和别的非 feeds 来源不受这道闸影响**,照旧全量进队列。

## 判据待在哪

- **`pending_docs` 是唯一口径**,一处改动同时治提取队列和面板那个「当前待处理
  N 项」(`pipeline.pending_counts` 数的就是它)。
- **一个条目可能同时属于几条订阅,判据是「任一归属订阅开着就提取」。** 两个方向
  不对等:漏提取可恢复(`extracted_at` 仍是 NULL,开关一开自动回队列),静默多
  提取只是多几个标签。
- **`limit` 截的是过滤之后的结果。** 反过来写的话 `retry_failed_plan` 那句
  `pending_docs(conn, limit=1)` 会在「第一条恰好是被拦下的 feeds 文档」时报
  「没有积压」,而绿泡泡那边明明还排着。

## 为什么不把两个库合成一个

订阅开关在 feeds.db、文档在 kb.db,跨库 JOIN 看起来是合并的理由。实测下来不是:

- **ATTACH 就够了。** 2026-10-10 在生产库上实测:`ATTACH + LEFT JOIN` 6ms,
  反向拼 path 的 `NOT IN` 写法 5ms,归属覆盖率 rss 806/806、social 298/298、
  web 5/5,**零孤儿**。
- **合库会当场废掉一个现有的并行写窗口。** `_run_pipeline` 里 crawl 和「存量提取」
  是并行的两个线程,安全边界正是「提取只写 kb.db、抓取写 feeds.db 且采集器在
  进程外」。合成一个库之后这两个是同一把写锁的争用者。这个并行是有意设计的 ——
  抓取被反爬 sleep 限速时 CPU 和网络全闲着。
- **向量编码攥着维护锁几分钟**(见 `maintenance-lock.md`)。现在这几分钟里采集
  照跑,合库之后一个字都写不进去。
- **两个库的恢复语义相反。** kb.db 772 MB、绝大部分是 chunks/FTS/向量,坏了可以
  **删掉重索引**(从 vault 全量重建);feeds.db 11.6 MB,装的是采集水位、etag、
  翻译成品、补抓状态,**重建不了**。塞进一个文件等于把最便宜的那条恢复路径废掉。
- 顺带的:备份体量差 70 倍;kb.db 要加载 sqlite_vec 扩展才能完整打开,而 feeds
  那侧现在裸 `sqlite3` 就能读(collector、脚本、备份都靠这个);772 MB 的库做
  结构迁移本身有不可逆风险。

### ATTACH 的硬约束

- **只用于只读 JOIN,写入绝不跨库。** WAL 下涉及多个库的事务不是原子的(SQLite
  官方行为,两个库实测都是 WAL),跨库写会在崩溃点留下半个结果。`mode=ro` 把这件
  事从「约定」变成「物理上做不到」—— 实测 `ATTACH ... ?mode=ro` 之后写入报
  `attempt to write a readonly database`。
- **ATTACH 之前先问一句「这一批候选里有没有 feeds 来源的」。** 全新安装、纯绿泡泡
  的库压根没有 feeds.db,无条件 ATTACH 会为一个不存在的故障抛异常 —— 而这个函数
  坐在面板每次刷新那条路上。实测注入「无条件 ATTACH」时
  `test_missing_registry_is_fine_without_feed_candidates` 和
  `test_non_feed_sources_are_untouched` 两条同时红。
- **URI 之前先确认真实文件在。** 万一某个 SQLite 构建没开 URI 处理,那个字符串
  会被当成字面文件名,ATTACH 一个不存在的名字等于凭空建一个空库。本机实测
  (SQLite 3.53.2)URI 生效、`mode=ro` 生效、文件不存在时 ATTACH 失败且不留垃圾
  文件 —— 但这不可移植,所以那道检查照留。
- **进来之前先 DETACH 一次**:上一次如果死在异常里没摘干净,同名 ATTACH 会报
  「already in use」,而那跟这次要回答的问题毫无关系。

## 两个库之间唯一的接缝

`source/<kind>/<item_key>.md` —— 由 `sync._export` 落盘(`root / (item['key'] +
'.md')`),由 `extract_optin.FEED_DOC_PATH` 在 SQL 里重建。**这是一条隐式契约**:
`_export` 哪天改了落盘路径(换目录、换文件名、加一层按日期分的子目录),这边会
**静默**匹配不上,后果是那条订阅即使开着提取也一篇都排不进队列。方向上是安全的
(`extracted_at` 仍是 NULL,改回来自动回队列),但没人会发现。所以回归里那一条
(`PathContractTests`)真跑一遍 `sync.export` 再按 SQL 里那个拼法反查 —— 用例自己
按约定摆的 md 只能证明「判据对我摆的盘成立」。

## 读不到 feeds.db 时

**抛,不静默放行也不静默拦死。** 静默返回空(当成「一条订阅都没开」)会让所有
feeds 来源的提取悄悄停住,而面板上「当前待处理」是 0、阶段是绿的 —— 和「真的没有
积压」长得一模一样。三条调用路径各自接住它:

- `_extract_pass`:落 `_stage("extract", "error", "读不到订阅的提取开关:…")`。
  不让异常冒出这个阶段 —— 那会把状态机留在 running。
- `pipeline.pending_counts`:本来就包在 try 里,面板那个数显示不出来。
- `retry_failed_plan`:**照样把 extract 排进计划**。这条路只在决定「要不要跑这
  一步」,真正的诊断归 `_extract_pass`。在这里吞掉的话用户收到的是 409「上次运行
  没有失败项」,而真实情况是库读不到,两者指向完全相反的下一步。

## 同步入口跟着改

`intake_api.sync` 和 `feeds/schedule.tick` 原来都写死 `['crawl','export','index']`,
feeds 来源的提取从来不跑 —— 积压全靠绿泡泡那轮全流程定时同步顺手消化。现在
**两处都加上 `extract`**:要不要花钱提取由订阅级开关回答了,入口不该再二次把关。
没开过开关的库跑到这一步是「没有待提取的文档」,一次模型都不调。

## 存量不回滚

已提取的 778 + 296 篇留着,钱已经付过,标签也已经在图谱和检索里,其中有真有用的。
想清的话要双写(`doc_tags` + md frontmatter 的 `tags`,走现成的
`remove_writeback_tag`)并重置 `extracted_at`,那是**不可恢复**的(重提要再付一次
钱)。建议新规则跑两周,去主题页看哪些 `topic/` 下只挂一两篇、是哪条订阅贡献的,
再按那个反推。

## 老库迁移

`_ADDED_COLUMNS` 里 ALTER 补 `extract INTEGER NOT NULL DEFAULT 0` —— **老库里的
订阅一律补 0**,这是一次有意的口径收紧。补成 1(「保持原样」)会让存量那几十条
聚合订阅继续往标签里灌噪声,而用户以为自己拿到的是新规则。

独立一列、不放 `config`:换链接时 `save_subscription` 的 `reset` 会把 config 整个
清成 `'{}'` —— 那是采集进度该有的下场,而这是用户对这条订阅的配置(理由逐字同
`fulltext`,见 `fulltext.md`)。

## 回归清单

`tests/test_extract_optin.py`(13 条):

- `GateTests` 7 条:默认拦住 / 开了放行 / 后开也能回队列 / **非 feeds 来源不受
  影响**(反向)/ 同类里开了的和没开的并存 / 共享条目任一开着就放行 / limit 在
  闸之后截。
- `PathContractTests` 1 条:真跑 `sync.export`,落盘路径必须等于
  `FEED_DOC_PATH` 重建出来的那一个。
- `UnreadableRegistryTests` 2 条:坏库必须抛 / **没有 feeds 候选时根本不碰
  feeds.db**(反向,顺带钉住「不许为了问一句开关把 feeds.db 建出来」)。
- `ColumnTests` 3 条:老库补 0 / 换链接不清掉用户的选择(顺带钉住采集进度该清的
  还是清)/ 新订阅默认关。

`tests/fulltext_probe.mjs` 第 3.5 组(真浏览器,CDP 直连 chrome-headless-shell):
弹窗里有这个开关 / 默认未勾 / 说明里写着「不开也能被全文和语义检索搜到」/
保存时 body 里带 `extract:true` / **同一次 PATCH 把 fulltext 原样带上** /
后端真的开了且补抓开关没被动 / **开着的订阅再打开弹窗是勾上的** / 订阅行上标出来 /
没开的那条不标(反向)/ 关回去立刻生效 / 来回切一遍补抓开关一个字没动。

**和补抓那个开关放在同一个探针文件里不是图省事**:两个开关挤在同一个弹窗、同一次
PATCH 里,「改 A 把 B 静默重置」这种缺陷只有两个开关摆进同一条用例才看得见。

## 反向验证(2026-10-10 定点注入)

后端 8 处,每一处单独注入、跑 `tests/test_extract_optin.py`:

| 注入 | 红 |
|---|---|
| 判据整个去掉 | 4 |
| 判据反着来(放行没开的) | 6 |
| 路径拼法漏掉 `source/` 前缀 | 5 |
| 无条件 ATTACH(不先问有没有 feeds 候选) | 2 |
| feeds.db 读不到时静默当成「一条都没开」 | 1 |
| 换链接时把 extract 一起清掉 | 1 |
| 老库补列默认 1 | 1 |
| 新订阅默认开 | 5 |

前端 5 处,跑 `tests/test_fulltext_browser.py`:

| 注入 | 红 |
|---|---|
| 弹窗不回显 extract(永远显示未勾) | 1 |
| 保存时漏掉 extract 字段 | 4 |
| 保存时漏掉 fulltext(改 A 把 B 重置) | 2 |
| 订阅行上不标 | 1 |
| 说明里删掉「不开也能搜到」那句 | 1 |

**第一条前端注入最初红 0 条**,因为探针只验了「默认是没勾的」—— 而摆盘时它本来
就是未勾的,一个「永远渲染成未勾」的实现在那几条下全绿。那正是最坏的那种缺陷:
用户开了提取,过几天回来改一下名字再保存,开关就被静默关掉了。补上「开着的订阅
再打开弹窗是勾上的」之后红 1 条。

## 受这道闸影响的既有测试

三处 fixture 摆的是 feeds 来源,改动后它们要验的东西会被这道闸遮掉,红出来的话
指向的地方跟它们自己要钉的事毫无关系:

- `tests/test_extract_no_body.py`:摆的是 `source/rss/*.md` 但库里没有任何订阅。
  它验的是空正文的两档终态,和内容从哪来无关 —— **换成 `dr-gzh`**(非 feeds
  来源),并在脚手架 docstring 里写明别改回去。
- `tests/test_feed_fulltext.py` 的 `FullTextCase`、`tests/feed_trim_fixture.py`
  的 `sub()`:它们真的建了 rss 订阅,而且要真走进提取里去 —— **加 `extract: True`**。
  那条订阅在语境里正是「值得提取的聚合源」,开着才对得上。
- `tests/test_feed_sources.py` 那条钉住 stages 列表的断言跟着改成四步。
