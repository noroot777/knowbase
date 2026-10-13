# kb.db 的维护互斥锁(kb/maintenance.py)

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是完整的事故形态、
> 实测证据、回归清单和反向验证红数。CLAUDE.md 只留跨块的元规则和一张索引。
> 硬约束和下面的展开**说的是同一条口径,要改规则两边一起改**。

## 硬约束(原 CLAUDE.md「kb.db 的维护互斥锁(kb/maintenance.py)」)

- **写 kb.db + vault 的几条路共用一把跨进程独占锁**(`maintenance_lock()`,flock 在 `data/feeds-trim.lock` 上):存量清
  理脚本、订阅同步的存盘与导出、索引的两条入口(`pipeline_stages._run_index`、`cli.cmd_index`)、条目化的就地入库、提取
  的 tag 回写(**四个**调用方:管道 `_extract_pass`、`kb extract --writeback`、`scripts/extract_parallel.py`、软广补判经
  `sync_promo_tag` 进来的)、提取结果的交付(`commit_extraction`)。
- **口径是「凡是写 kb.db 或 vault 文件的入口」,不是「两条索引入口」** —— 条目化那条自称「只增不删」,可它末尾照样调
  `embed_pending_chunks`,而那个函数补的是**全库**待编码的 chunk。判据只能是**写哪张表** + **写不写 vault 里的文章文
  件**。
- **粒度是整趟,不是只圈写的那几句**(竞态的那一半在「读」),所以 scan → upsert → remove 一整段都在锁里。**向量编码也
  在锁里**:`chunk_vec` 是 sqlite_vec 虚表、**没有外键级联**,清理在中间删掉那些 chunk 的话后面几批会按不存在的 chunk
  id 插回向量,而这些孤儿行**照样占着语义检索的候选名额**。代价是一轮大编码会攥着锁几分钟,这是统一互斥的标价。
- **等待有上限,而且等的时候要出声**(`KB_MAINT_LOCK_WAIT`,默认 900 秒;`on_wait` 立刻一次、之后每 60 秒一次)。超时抛
  `LockBusy`,三处一律「什么都没动」收场。**无限等更坏**:管道是单例,一个卡住的持锁者会把面板同步、定时同步和日报一起
  冻死。
- **等锁期间还认「停止」,而且认它的回调跟出声那个分开**(`on_poll` 每 0.25 秒一次,抛 `_StopWhileWaiting` 让阶段落
  `stopped` 而不是红)。**`flock` 一成功就返回,所以拿到锁之后还要再认一次**,但**只在真等过锁时才补这一次**(无条件检
  查会把刚放进 index 的那一轮当场顶成 stopped)。
- **提取的 tag 回写也在锁里,而且锁内要先确认这一篇还在**(`_still_indexed`,不在就返回 False、**不抛异常**)——
  `writeback_tags` 是整份读改写,清理在中间把这一篇删干净的话那次写回**把 md 文件重新创建出来**,比 index 那条更糟(文件
  回来之后谁也收不掉)。**回写的粒度是「每篇各取一次」,不是整趟提取**,模型请求留在锁外。
- **取锁之前必须先把写事务落盘,而这件事由共用入口自己负责**(`if conn.in_transaction: conn.commit()`)—— **SQLite 的写
  锁不认这把维护锁**,攥着没提交的写事务去等锁,持锁的清理就卡在自己的 DELETE 上。判据是**两层**:共用入口自己 flush +
  调用点时序照旧写明白,另有一条回归把全仓调用点的名单钉住。
- **补判等不到锁时 `LockBusy` 照常抛出去、整轮中止**(`_flag_in_vault` 只接 `OSError`)。
- **同形状但没闭合的是 `wiki_daily.step_linkify`** —— 分界线:**管道自动跑的写 vault 入口都该取这把锁**,手工一次性运维
  脚本留在外面。
- **不会抱死**:这是唯一一把跨进程维护锁,没有任何路径持着它再去取别的锁;同线程重入直接放行(flock 认的是 open file
  description)。锁的是 `data/` 下**单独的 lockfile**,只创建不删除;**文件名沿用 `feeds-trim.lock`**,改名等于在升级窗
  口里让两个持锁者同时写。
- **`test_feed_translate.ColumnBackfillTests.test_two_first_accesses_both_succeed` 是一条已知的负载敏感 flaky,红了先别当
  回归。** 判据是它自己的报错内容:`duplicate column name` 才是它要抓的回归,`database is locked` 是 `reg.db()` 在
  `BEGIN IMMEDIATE` 之前那两句 DDL/PRAGMA 上的争锁窗口。
- **测试里必须把 `maintenance.DATA_DIR` 指到 tmp**,不指的话会去 flock **生产**那个 lockfile。
- 详细见 `docs/decisions/maintenance-lock.md`。

## 事故形态与实测证据

- **写 kb.db + vault 的几条路共用一把跨进程独占锁**(`maintenance_lock()`,flock 在
  `data/feeds-trim.lock` 上):存量清理脚本、订阅同步的存盘(`sync._save_result`)与
  导出(`sync.export`)、索引的两条入口(`pipeline_stages._run_index`、`cli.cmd_index`)、
  条目化的就地入库(`pipeline_stages._ingest_pretagged_sources`)、提取的 tag 回写
  (`extract.writeback_extraction`,**四个**调用方共用它这一份:三条提取路径 +
  软广补判经 `sync_promo_tag` 进来)、**提取结果的交付**(`extract.commit_extraction`
  把校验 → 落库 → 提交 → 回写圈成一个协调区间,三条读正文的提取路径一律走它 ——
  详见「聚合源的正文补抓」那条「一次提取的结果要和它基于的那份正文绑在一起」)。
  每一条都是「先把一份快照读进内存 → 过一会儿再按它写盘」,两两不互斥就能用**删除
  之前**那份快照把另一边刚删掉的东西复活。**索引那两条是 2026-10-01 第 2 轮审查补上
  的**:清理把一篇旧文从三处删干净、进度标成完成之后,一轮并发的 index 拿它删除之前
  读到的那个 `Doc` 完成 upsert —— `documents=1`、FTS 命中 1,而 vault 里那个文件已经
  不存在:搜得到、点开没有原文,重跑清理也只会说「无事可做」(feeds 行早没了)。
- **口径是「凡是写 kb.db 或 vault 文件的入口」,不是「两条索引入口」。** 条目化
  那条是紧接着的第 1 轮审查抓出来的:它自称「只增不删」,看着跟清理没关系,可它末尾
  照样调 `embed_pending_chunks` —— 而那个函数补的是**全库**待编码的 chunk、不限于本轮
  自己写进去的那些,所以清理删掉的 chunk 会被它按旧快照插回向量(实测 151 行孤儿向量,
  语义检索当场返回空)。判据因此只能是**写哪张表**:全仓 `upsert_document` 三处
  (`_index_pass` / `_ingest_pass` / `cli._index_pass`)、`insert_vectors` 只有
  `embed_pending_chunks` 一个入口而它有三个调用点,现在全在锁里。**写 vault 文件那一半
  是第 2 轮审查补的**(见下面「tag 回写」那条):md 文件本身也是被清理删掉的东西之一,
  按「写哪张表」问压根问不到它。再添一条写入方时照这两个判据走(写不写 kb.db、写不写
  vault 里的文章文件),别照「它是不是索引」。
- **粒度是整趟,不是只圈写的那几句。** 竞态的那一半在「读」:scan 读到的是删除之前
  的文件内容,只把 upsert / `remove_missing_documents` 圈起来的话,旧快照照样写得
  进去。所以 scan → upsert → remove 一整段都在锁里(`pipeline_stages._index_pass`、
  `cli._index_pass`)。
- **向量编码也在锁里。** `embed_pending_chunks` 先把「还没有向量的 chunk」整份读出来
  (id + 正文),再分批写回 `chunk_vec`;而 `chunk_vec` 是 sqlite_vec 的虚表、**没有
  外键级联**(`remove_missing_documents` 是按 chunk id 显式删它的),清理在这中间删掉
  那些 chunk 的话,后面几批会按已经不存在的 chunk id 把向量插回去 —— 这些孤儿行谁也
  不会再来收(「孤儿向量 0」是生产审计在看的一条不变量),**而且它们照样占着语义检索
  的候选名额**(`search` 无来源过滤时先取全局 top-150、再 JOIN 当前 chunks/documents,
  孤儿候选先占名额再被 JOIN 丢弃):于是清理之后**仍然留着**的文章被一批指向不存在
  chunk 的向量挤出结果,重跑索引也不恢复(2026-10-01 第 1 轮审查实测)。代价是一轮大
  编码会把锁攥上几分钟(`--reembed` 更久),**被它挡住的不止那个一次性清理脚本**:另一
  个进程的订阅存盘与导出也在等同一把锁。这是统一互斥的标价,接受它的理由是这几条路写
  的本来就是同一个库,串起来跑才是对的。
- **等待有上限,而且等的时候要出声**(`KB_MAINT_LOCK_WAIT`,默认 900 秒;`on_wait`
  立刻一次、之后每 `NOTIFY_EVERY`(60)秒一次,第一次就拿到锁时压根不调)。超时抛
  `LockBusy`,三处一律「什么都没动」收场:index 阶段标红写明原因(漏跑一轮下一轮自己
  补 —— 扫描是全量比对)、`kb index` 退 2、清理脚本退 2 且一个字节没改。**无限等更坏**:
  管道是单例,一个卡住的持锁者会把面板上的同步、定时同步和日报一起冻死,而「拿旧快照
  写回去的那行假结果」要等到下一次索引才被收掉。不出声也不行:面板那张卡片会一直停在
  「扫描 vault」,看着像卡死。
- **等锁期间还认「停止」,而且认它的回调跟出声那个分开**(`_lock_waiter` 给
  `on_poll` + `on_wait` 两个:前者每 `_POLL`(0.25)秒一次、只问「还要不要等」,抛
  `_StopWhileWaiting` 让阶段落 `stopped` 而不是红;后者只管低频出声)。挂在同一个回调
  上的话,等锁期间按下的那一下最坏要 `NOTIFY_EVERY`(60)秒后才被看见 —— 第 1 轮审查
  把等待上限缩成 2 秒实测,那一轮等满超时、落的是 `error / 索引没做`。
- **`flock` 一成功就返回,所以拿到锁之后还要再认一次**(`waited["on"] and
  stop_requested`)。循环是「试 flock → 没拿到才问一次」,锁正好在这两步之间松开的话
  下一轮直接 return、再也不问,这一轮会照常索引到底。**但只在真等过锁时才补这一次**:
  无条件检查会把管道编排刚放进 index 的那一轮当场顶成 stopped,而按钮承诺的是「当前
  阶段结束后停止」,不是取消刚开跑的这个阶段(回归里正反两条各钉一边)。
- **提取的 tag 回写也在锁里,而且锁内要先确认这一篇还在**(`extract.writeback_extraction`)。
  这条原来被显式**排除**在外,依据是「`chunks`/`doc_tags`/`entities` 对 `documents(path)`
  都是 `ON DELETE CASCADE`,给一个已删 path 写标签会被外键当场拒掉,所以提取这条路上
  没有复活可言」—— **那个依据只看了库、没看文件,是错的**(2026-10-01 第 2 轮审查两种
  合法时序各实测一次:`kb extract --writeback` 与同步的并行存量提取)。`writeback_tags`
  是「`read_text` 拿一份全文快照 → 改 frontmatter → `write_text` 整份写回」,清理在这中间
  把这一篇从 feeds.db / vault / kb.db 三处删干净的话,那次写回**把 md 文件重新创建出来**,
  下一轮索引合法地扫到它、upsert 回库,旧文从此搜得到点得开。外键级联挡不住:标签是在
  删除**之前**写进去并提交的,级联删除照样全部成功,它管的是那几张表,管不着谁在之后
  重建文件。**而且这一条比 index 那条更糟**:index 复活的只是一行 `documents`,下一轮
  `remove_missing_documents` 自己就收掉了;文件真的回来之后谁也收不掉 —— feeds.db 里
  已经没有这一条,重跑清理只会说「无事可做」。
- **回写的粒度是「每篇各取一次」,不是整趟提取**,而且模型请求留在锁外。竞态窗口只有
  单篇的 read→write 那一下(提取那一趟里真正读磁盘的就只有这一步),把整轮圈进去等于
  攥着锁等几十次 LLM 往返,面板上的同步、定时同步和日报一起冻住。index 必须整趟是因为
  它竞态的那一半是「把整个 vault 读成一份快照」,两件事不一样。
- **锁拿到手之后先复查「这一篇还在吗」**(`_still_indexed`:文件在 + `documents` 有行),
  不在就什么都不做、返回 False(「没改文件」),**不抛异常**。等锁的这段时间里清理很
  可能正是把它删掉了;而 `_extract_pass` 把回写和 `save_extraction` 包在同一个 try 里,
  抛出去就是把这一篇记成提取失败、面板标红 —— 文章是用户主动清理掉的,那是谎报一次
  故障。判定本身早就落库了,随后被外键级联一并删掉,末态干净。
- **取锁之前必须先把写事务落盘,而这件事由共用入口自己负责**
  (`writeback_extraction` 里那句 `if conn.in_transaction: conn.commit()`)。
  **SQLite 的写锁不认这把维护锁**:攥着 `save_promo` / `save_extraction` 留下的那个
  没提交的写事务去等锁,持锁的清理就卡在自己的 DELETE 上,两边互等到 SQLite 的 busy
  超时(默认 30 秒)把清理打断 —— 清理抛 `database is locked` 退出,feeds 行和 md 都
  删了、`_drop_index` 那一步没做完:**搜索里还留着刚删掉的旧文**(2026-10-01 第 3 轮
  审查跨进程实测 32.5 秒后报错,末态 `documents=1`、全文命中 1,要再跑一次清理才收得
  干净)。提前提交不新增副作用 —— 改了文件本来也要在锁内 `conn.commit()`,这只是把
  同一个提交挪早;四个调用方的判定都是逐篇独立、可重跑的,组内那次提交没有「要么全
  要么全不」的语义。
- **这条原来只写在调用方那一侧,于是漏掉了第四个调用方。** 第 2 轮定的契约是「三条
  提取路径先 commit 再回写」(管道 `_extract_pass`、`kb extract --writeback` 本来就是,
  `scripts/extract_parallel.py` 跟着改),可软广补判经 `sync_promo_tag` 进的是同一个
  入口,它在 `save_promo` 之后、整组提交**之前**回写 —— 而那一轮的回归**按名单枚举了
  三条路径**,名单本身就是缺口,16 项定向回归和全量测试全绿照过(2026-10-01 第 3 轮
  审查发现)。所以判据换成**两层**:共用入口自己 flush(锁序正确性不取决于「每个调用
  方都记得先提交」),调用点的时序照旧写明白(`promo_backfill.cmd_run` 整组判定先
  `conn.commit()`、再逐篇 `_flag_in_vault`),各有一条回归钉着;另有一条**把全仓调用
  点的名单也钉住**,多一处会在那里红,而不是等某次清理报 `database is locked`。
- **补判等不到锁时 `LockBusy` 照常抛出去、整轮中止**(`_flag_in_vault` 只接 `OSError`)。
  那说明另一头正在做维护,而这个脚本按 `promo_level=''` 本来就可重跑;把它接住继续,
  后面每篇还要各等一次 `KB_MAINT_LOCK_WAIT` 才是更坏的一种。
- **同形状但本轮没闭合的是 `scripts/wiki/wiki_daily.step_linkify`**:它也是「读一篇
  vault 文章 → 改 → 整份写回」,也跑在管道里(wiki 阶段),所以同一条路径成立 ——
  只是它不在本轮审查范围内,没动。分界线记在这:**管道自动跑的写 vault 入口都该取这把
  锁**,`scripts/fix_note_citations.py` / `backfill_youtube_summaries.py` /
  `skill_tag_backfill.py` 那类手工一次性运维脚本留在外面(人在边上看着、不会和定时
  清理撞上)。`step_linkify` 属于前者,该单独补。
  存量提取那条并发路径在 index 之前就被 join 掉了(见 `pipeline._run_pipeline`)。
- **不会抱死**:这是系统里唯一一把跨进程维护锁,没有任何路径持着它再去取别的锁;同
  线程重入直接放行(flock 认的是 open file description,同进程换一个 fd 再锁会把自己
  锁死)。
- 锁的是 `data/` 下**单独的 lockfile**,只创建不删除(unlink 会让「A 持锁 → B 删文件
  → C 新建再持锁」出现两个持锁者,理由同 `secrets.json` 那把锁)。**文件名沿用 feeds
  那一版的 `feeds-trim.lock`**:改名等于在升级窗口里让老进程锁 A、新进程锁 B,两个持
  锁者同时写同一个库 —— 名字只是一个路径,不值这个风险。
- **`test_feed_translate.ColumnBackfillTests.test_two_first_accesses_both_succeed`
  是一条已知的负载敏感 flaky,红了先别当回归。** 它测的是补列那把写锁,却会被
  另一件事绊倒:`reg.db()` 在 `BEGIN IMMEDIATE` **之前**还有
  `executescript(SCHEMA)` 和 `PRAGMA journal_mode=WAL`,两个连接同时首访同一个
  新库时就在这两句上争锁(DDL 和切 journal 都要锁,而 PRAGMA 不走 busy handler,
  `timeout=30` 管不着它),于是偶发 `database is locked` —— 跟补列毫无关系。
  **判据是它自己的报错内容**:`duplicate column name` 才是这条用例要抓的那个
  回归,`database is locked` 是这个窗口。单独跑或只跑两三个模块时稳定绿,整组
  一起跑、机器负载上来才出现(2026-10-08 实测:HEAD 版 0/11,前面多一条带 tar
  打包的用例之后 2~3/8;把屏障超时从 1 秒压到 0.2 秒仍然 2/10,**所以诱因不是
  屏障**,压它没用)。**并发首访那个 locked 窗口是真实的产品行为**(生产里两个
  请求同时首访老库就会撞上,只是 `_migrate` 是毫秒级、窗口极窄),要修得动
  `reg.db()` 的锁序 —— 那是核心路径,得单独立项、单独审查,别挂在别的修复里顺手改。
- **测试里必须把 `maintenance.DATA_DIR` 指到 tmp**(`tests/feed_trim_fixture.py`,以及
  `test_feed_budget` / `test_feed_translate` / `test_feed_sources` 这三个会真走
  `sync.export` 的):不指的话这些用例会去 flock **生产**那个 lockfile,被 kb-web 正在
  跑的一轮索引(现在整趟持锁)挂住几分钟。
- 回归分两个文件:写 kb.db 那几条在 `tests/test_index_lock.py`(13 条),写 **vault 文件**
  那几条在 `tests/test_writeback_lock.py`(6 条)—— 第 3 轮补完之后挤在一个文件里是
  694 行,贴着 700 上限,所以按形状拆开(摆盘仍共用 `feed_trim_fixture.py`,
  `documents_without_a_file` 跟着搬进去了)。
  `test_index_lock.py`:清理持锁期间起跑的索引既不开扫也不写库、
  面板上出声说在等、末态不留指向不存在文件的 `documents` 行;清理之后那一轮索引把已删
  文档收掉;等不到锁就不做且一个字节没动;**停止那四条**(早就按下的不去排队 / 等上了
  之后才按的不等到超时 / 正好落在锁松开那一下的也不许被吃掉且一个字节没动 / 锁空着时
  一个早已按下的停止不许把这一轮顶掉);**条目化入库那两条**(清理持锁期间起跑的入库
  既不读待编码快照也不写向量、末态孤儿向量 0;等不到锁抛 `LockBusy`)外加阶段文案把
  `LockBusy` 和停止分开说;`kb index` 在另一个**真子进程**持锁时退 2、锁松了照样做完;
  编码确实在锁内;清理之后导出 + 索引照常走且嵌套取锁不抱死。
  `test_writeback_lock.py`(第 3 轮之后 7 条):`kb extract --writeback` 与管道存量提取各一条(清理持锁
  期间起跑的回写既不写文件、末态 vault 文件也没被重建出来,随后那一轮索引没有东西
  可收,而且这一篇不许被记成提取失败);**四个**调用方调回写时共享连接都不攥着没提交
  的写事务;**共用入口自己也 flush**(摆一个开着的写事务直接调 `sync_promo_tag`,看
  取锁那一刻 `in_transaction` 是不是假);**协调入口 `commit_extraction` 也自己
  flush**(同一个判据,它比回写早一步取同一把锁);**清理持锁期间起跑的软广补判不把清理卡死**
  (真跑 `promo_backfill.cmd_run`,只替掉模型回复,末态清理退 0 且旧文三处都没了、
  补判自己那篇判定和 md 标记都在);全仓调用点名单。
  **被 race 的那个线程必须在打桩还生效的时候跑完** —— 放到 `with patch` 外面等的话,
  回写读的是**生产** `VAULT_ROOT`,`_still_indexed` 当然说「这篇不在」:绿是绿了,可
  那和修没修对没关系(第 3 轮自查发现,第 2 轮那两条原来就是这个写法)。
  反向验证定点注入过十三版:index 阶段不取锁红 4 条(那条复活用例的两条观察改到末态之后
  再断言时,红的是 `(documents, searchable)` 从 `(0,0)` 变成 `(1,1)` —— 正是第 2 轮
  审查报告里那份证据;**只验「锁取了没有」的话,一个什么都没防住的实现也能全绿**)、
  `kb index` 不取锁红 1 条、把编码挪到锁外红 1 条;**条目化入库不取锁红 2 条**(红的是
  `留下了指向已删 chunk 的向量行`,所以末态那条断言排在过程观察之前 —— 反过来写只能
  看见过程、看不见后果)、取消检查挂回低频提示并去掉锁后那次检查(就是第 1 轮被审那一
  版)红 2 条、只去掉锁后那次检查红 1 条、把它改成无条件红 1 条;**回写那三版**:整段
  不取锁(就是第 2 轮被审那一版)红 2 条,红的是 `回写把清理删掉的 vault 文件重建出来了`
  —— 所以末态那条断言同样排在过程观察之前;取了锁但去掉锁内那次复查红 1 条,红的是
  `(1, set()) != (0, {...})`,也就是这一篇被记成了提取失败(文件确实没被重建,但面板
  会为一篇用户主动删掉的文章标红);`extract_parallel.py` 回写前不提交红 1 条。
  **锁序那三版**(第 3 轮):只退调用点时序红 1 条(共用入口的 flush 兜住了,所以红的
  是名单那条)、只去掉共用入口的 flush 红 1 条(`带着没提交的写事务去等维护锁了`)、
  两层都去掉(就是第 3 轮被审那一版)红 3 条 —— 其中那条 race 红的是
  `(False, False, 0, 0) != (False, False, 1, 1)`,正是审查报告里「清理半途失败、搜索
  里还留着旧文」那份证据。**清理在这种情况下是抛出来的**(`OperationalError:
  database is locked`),用例要把它收下来记成一个值,让末态那条断言先红 —— 让异常冒
  上去的话注入旧版本只看得到一个 ERROR。
