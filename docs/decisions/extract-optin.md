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
- **`store.connect` 必须显式 `uri=True`。** ATTACH 认不认 `file:…?mode=ro` 取决于
  **连接**有没有带 `SQLITE_OPEN_URI`(Python 的 `uri=` 参数正是它),不带时只能靠
  SQLite 编译默认值 `SQLITE_USE_URI`。两份解释器实测差异:

  | 解释器 | SQLite | `USE_URI` | 不带 `uri=True` 时的 ATTACH |
  |---|---|---|---|
  | Homebrew python3 | 3.53.2 | 开 | 成功(**掩盖了这个缺陷**) |
  | `uv run --offline`(**服务实际用的那份**) | 3.50.4 | 没开 | `unable to open database`,**连普通目录都失败** |

  后果不止于 feeds:只要候选里有 feeds 来源,整个待提取查询就取不到结果,混在里面
  的绿泡泡积压也跟着不处理,面板那个待处理数变成不可取得而不是按新默认变成零
  (2026-10-11 审查实测)。
- **地址要用 `store.db_uri` 的规范 URI,不许手拼 `f"file:{path}"`。** `#` 和 `?`
  会被 URI 解析当成 fragment / query —— 不是报错,是**打开一个名字更短的别的库**,
  顺带在截断处凭空建一个文件,`mode=ro` 一起失效。实测:数据目录叫 `reader#data`
  时建出一个叫 `reader` 的文件并报 `no such table: feedsreg.subscriptions`。
  `Path.as_uri()` 把这些字符一并 percent-encode。
  **这同时是 `store.connect` 主库路径必须走 `db_uri` 的理由** —— 开了 URI 支持之后,
  主库自己的路径也要按 URI 规则写。
- **ATTACH 用参数绑定。** 今天是冗余带,如实记着:`as_uri()` 已经把单引号编成
  `%27`,把地址拼进 SQL 字符串这个注入**红 0 条**。留它是构造上的 ——「地址怎么来
  的」和「地址怎么进 SQL」是两件事,不该让后者押在前者一定会编码上。审查里
  `reader's data` 那一档报的 `near "s": syntax error` 正是两道都没有时的样子。
- **补列迁移要先于这次只读 JOIN。** `subscriptions.extract` 是 ALTER 补出来的,
  而补列只在 `reg.db()` 里跑。升级后第一条命令如果是 `kb extract`(或管道的并行
  存量提取),它压根没碰过注册库 —— JOIN 当场 `no such column: s.extract`、退出码
  1,而**先打开一次面板再跑同一条命令就好了**(2026-10-11 审查实测,那正是最难查
  的一类)。这里借一次 `reg.db()`:它幂等,口径也只有那一份(建表 + 补列 + 按修订
  号补种都在它的写锁里)。
- **借 `reg.db()` 之前先判文件在不在。** 它是「幂等地确保库在」的入口,会把一个
  空库连同默认订阅一起建出来 —— 于是「库被删了、而 vault 里还躺着 feeds 文档」
  这种该让人看一眼的状态被静默抹平成「一条开着提取的订阅都没有」,所有 feeds 文档
  从此不提取而面板一切正常。
- **ATTACH 之前先问一句「这一批候选里有没有 feeds 来源的」。** 全新安装、纯绿泡泡
  的库压根没有 feeds.db,无条件 ATTACH 会为一个不存在的故障抛异常 —— 而这个函数
  坐在面板每次刷新那条路上。实测注入「无条件 ATTACH」时
  `test_missing_registry_is_fine_without_feed_candidates` 和
  `test_non_feed_sources_are_untouched` 两条同时红。
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

- `_extract_pass`:**把原因记进 `stats["blocked"]`,让收尾去落阶段**,不在这里
  自己 `_stage` 一次就 return。它的两个调用方(`_run_extract` 单轮、管道的
  存量 + 增量双轮)随后都无条件走 `_finish_extract_stage`,而那边按
  `ok+failed+empty+no_text+stale == 0` 把阶段刷成绿色的「没有待提取的文档」——
  中途那句红字当场被盖掉。2026-10-11 审查在真页面上拍到的就是这个:右侧日志
  是红色的「读不到订阅的提取开关」,左边同时显示绿色「同步已完成」和「提取知识:
  已完成」,API 返回 `{"status":"ok","detail":"没有待提取的文档"}`。
  **`blocked` 必须单独记一档**:那几个计数数的是「处理过的文档」,而这一档下一篇
  都没看到 —— 0 在那句判断里的含义是「没有待提取的文档」,两种完全相反的情况长得
  一模一样。`_finish_extract_stage` 里认它的那一句因此**排在所有计数之前**,并单独
  给一条 failures 明细(面板上展开就是原因)。
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
- `UnreadableRegistryTests` 3 条:坏库必须抛 / **库没了而候选里还有 feeds 文档
  时也要抛,且不许顺手把库建回来** / **没有 feeds 候选时根本不碰 feeds.db**(反向)。
- `ColumnTests` 3 条:老库补 0 / 换链接不清掉用户的选择(顺带钉住采集进度该清的
  还是清)/ 新订阅默认关。
- `StageOutcomeTests` 4 条:单轮 `_run_extract` 的**最终**状态是红的 / 双轮管道
  同样 / 原因进得了 failures 明细 / **真的没有待提取文档时照旧是绿的**(反向,
  一个「一律报红」的实现在前三条里全绿)。
- `LegacyRegistryTests` 2 条:裸 sqlite3 造一个没有 `extract` 列的旧库、全程不碰
  `reg.db()`,直接 `pending_docs` 不许报错且补列要做掉 / 迁移跑完后开着的订阅
  照旧放行(反向)。
- `AwkwardDataDirTests` 3 条:数据目录名含 `'` / `#` / `?` 时整条判据照常成立,
  并钉住「没在截断处凭空建出一个文件」。
- `ReadOnlyMountTests` 2 条:挂上来的库拒绝写入 / `store.connect` 显式要 URI 支持。
  后者是**过程断言、而且在本机恒绿**(Homebrew 的 SQLite 开了 `USE_URI`,后果
  断言对它完全不敏感,定点注入红 0),跨构建的后果测不了就把构造钉住 —— 如实记着。

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

2026-10-11 第 1 轮审查的四条修复,另注入 9 处:

| 注入 | 红 |
|---|---|
| 收尾不认 `blocked`(回到被覆盖) | 3 |
| `_extract_pass` 不记 `blocked`(只中途落红) | 3 |
| `blocked` 落成绿色 | 2 |
| failures 不带原因 | 1 |
| `store.connect` 不开 `uri=True` | 1 |
| 不跑补列迁移 | 1 |
| 不做 URI 编码(裸拼 `file:{path}`) | 2 |
| 借 `reg.db()` 之前不判文件在不在 | 1 |
| 地址拼进 SQL 字符串(不绑定) | **0** |

最后一条是**真冗余**,不是用例不敏感:`as_uri()` 已经把单引号编成 `%27`,
SQL 那一层的注入面在它之后就关掉了。保留绑定的理由是构造上的(见上文)。
「借 `reg.db()` 之前不判文件在不在」最初也红 0 —— 原有两条用例一条验「没有
feeds 候选」、一条验「库是坏的」,都够不着「库没了但有候选」这一档,补了一条。

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
