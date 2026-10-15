# 明细里那一条:点得开、删得掉

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是完整的事故形态、
> 实测证据、回归清单和反向验证红数。CLAUDE.md 只留跨块的元规则和一张索引。

面板上那份失败明细里有一类条目是「这一篇一直缺正文」(公众号导出阶段的 `no_body`)。它们会被按退避
时间一轮一轮地重试,而其中确实有一些原文已经没有内容了 —— 整篇只有图片、原文被删。用户要能点开原文
核对,核对完当场说一句「别再抓了」,否则这几十条会永远挂在那里,把真正要处理的问题挤出视野。

涉及三层:采集器那边**真的删**,`kb/pipeline_failures.py` 管**别再显示**,面板上是**一条链接 + 一颗
按钮**。

## 硬约束

- **真正的删除在采集器那边,不在知识库主干**(`kb.collectors.ItemDropping` 的 `drop_item(ref)`)。
  主干不知道「这一条怎么删」—— 公众号是「删 articles 行 + 把链接身份记进 `deleted_articles`」,别的
  来源完全可能是另一回事。**语义是「永久不再采集」,不是「这一轮跳过」**:实现必须把身份拉黑,再次
  被发现时照样拦住。做不到的来源**别实现这个协议** —— 面板按「有没有这个能力」决定显不显示那颗按钮,
  假装支持的后果是用户删了一条、下一轮它又回来了。
- **顺序不许反:先让采集器删,成了之后才记账本**(`pipeline_api.pipeline_drop_failure`)。反过来写的话,
  采集器那一步失败时面板上这条已经消失了 —— 一个还在被反复重试的待处理项就此**静默丢掉**。
- **账本写不上不许把整件事报成失败。** 不可逆的那一步已经成了,报红会让用户以为没删掉而反复重试。
  如实在文案里说一句(`（面板记录没写成：…）`),那几条下一轮同步之后自然不再出现。
- **排除提交之后,后续写入不许把这个身份复活 —— 判据待在写语句自己的 WHERE 里**
  (`repository._excluded_clause`,挂在 `upsert_article_from_blog` / `set_body_text` / `set_body_html`
  三处;`cursor.rowcount == 0` 就是拒绝信号,三个函数都返回 `bool`,调用方必须跟上)。发现那一道
  (`article_identity.prepare_article`)**只是早一道,不是互斥** —— 它查完黑名单到真正写入之间要抓正文、
  要更新显示名,用户完全可能正好在这中间按下那颗按钮。键取
  `article_identity.exclusion_keys`,**发现侧和写入侧用同一份口径**:各写一遍的话两边只差一个键这件事
  在任何输出上都看不出来。
- **删除要幂等,但「库里没有这一行」不是排除凭据**(`cli._drop_missing`,三档)。缺行分三种:①名单里
  已经有它 → 幂等成功(连点、两个标签页各点一次的正常形态);②名单里没有、但调用方给了 `--link`
  → 当场把 `exclusion_keys` 整组补写进名单(`repo.exclude_identities`)再报成功 —— `add_deleted_blacklist`
  走不通,它要从 `articles` 那一行里读规范链接,而那一行已经没了;③两样都没有 → **拒**。只写一个旧档案
  ID 挡不住换个通道再发现同一篇,宣称「永久排除」就是报一个没做到的结论。
- **所以 `link` 要一路带到 CLI**(`_no_body_failure` 的 `drop.link` → `FailureDropRequest.link` →
  `drop_item` 的 `--link`)。**给不出的那两样一个都不许拼成空值**:拿空串去算身份等于给自己留一个
  「谁都命中」的键(`exclusion_keys` 自己也把空串去掉了)。
- **一轮同步跑着的时候按下按钮,统计要按实际写入结果报**(`local_runner._download`:写入被拒就记
  `excluded_articles`、`continue`)。队列是发现阶段排好的,而发现到真正写入之间隔着抓正文(几秒到
  几十秒)。那一篇被拒之后既不是新增(它没进库)、也不是失败,**更不许落进 `body_failures` 说一句
  「正文暂未获取,已安排自动重试」** —— 它不会再被抓了。面板上如实写「已永久排除 N 篇(同步期间移除)」。
- **导出 SQL 再筛一次永久排除名单,那是冗余带不是不变量**(`export_obsidian.load_articles`)。导出是
  **写进 Vault**,写出去的 md 下一次同步也不会自己消失;挡的是存量情形(库被别的路径写过、回退到旧
  备份、那次写入发生在这道不变量之前)。只能按 `a.id` 匹配 —— 另外两个键是 Python 算出来的。
- **账本的「读出来 → 改 → 整份写回」整段要在互斥里**(`pipeline_failures._exclusive`)。两条请求都读到
  旧列表、各自 append 一条、再整份替换,落盘的就只有后写的那一条,另一条**已经不可逆地删掉了内容却
  再也不会被隐藏**,而两个响应都是 `ok=true`。`_write_atomic` 只保证单次写入不是半截 JSON,保证不了
  这个。**一把 flock 就够,不另加线程锁**:flock 认的是 open file description,`_exclusive` 每次自己
  `open` 一个 fd,所以同进程两条线程(kb-web 是线程池)也互相挡得住 —— 再叠一把 `threading.Lock` 是
  同一个问题判两遍。锁文件从账本路径派生、**只创建不删除**(同 `kb/maintenance.py`)。
- **`_write_atomic` 的临时文件名每次都要是新的,不许按 pid 固定**(`pipeline_persist`)。同一个进程里
  两条线程写同一个 path 时,固定名字那份会被后进来的截断重写:先 `replace` 的搬走的是**半截**内容,
  后一条的 `replace` 还会撞上「临时文件已经被搬走了」(`FileNotFoundError`)。
- **归属对不上一律拒**(`cli.cmd_articles`:拿 id 查出真正的 `account_name`,跟调用方给的不一致就整个
  拒掉)。面板上那条明细和库里那一行必须是同一篇 —— 删错的是另一个号的文章,而那是**不可恢复**的
  (连正文一起删)。
- **过滤待在出口一处,那几份运行记录一个字节都不动**(`pipeline_failures.strip`,挂在
  `/api/pipeline/status` 和 `/api/pipeline/runs` 两个端点上)。「最新那一轮」在内存 `_state`、整批
  汇总(`pipeline_batch`)和磁盘状态文件里各有一份投影,历史文件里还有同一轮那一条 —— 挨个做外科手术
  既漏得掉(汇总那份是内存缓存,`last_result()` 优先读它),又会把「两份落盘是同一个业务结果的两个
  投影」这条不变量撕开(那条的事故形态在 `pipeline-history.md`)。
- **`strip_state` 要过两处**:当前这一轮的 `stages` 和 `last_run` 那一份。空闲时面板渲染的是
  `last_run`(`pipeResult`),跑着的时候渲染的是顶层 —— 漏掉哪一处,用户都会在某个时刻看见自己刚删掉
  的那几条又回来了。
- **`failures_total` 要跟着减,但不许掉到清单长度以下。** 面板照 `failures_total || failures.length`
  念「明细 · N 项(仅列前 M 条)」:不减的话用户删了十条、标题还写着原来那个数;压到清单长度以下,
  余下那几百条就连「还有」都不知道了(`_cap_failures` 留下这个字段正是为了这件事)。没被截断过的那
  一轮压根没有这个字段,剔完也**不许凭空长出来**。
- **账本读不出来 = 那几条重新显示,不是整份明细不显示。** 重新显示只是多看见几行(采集那边早就不抓
  它们了,再点一次也只是重复一次幂等的删除);反过来把明细藏掉才是真的丢信息。
- **账本文件从 `pipeline_state._STATE_FILE` 派生,不另立一个模块变量**(`pipeline_failures._file()`)。
  测试隔离落盘时 patch 的是那一个,另立一个的话每个走到这里的测试都得记着再 patch 一次,而漏掉的那个
  会去写**生产**那份账本,漏没漏在测试输出上一个字都看不见(同 `pipeline_state._history_file` 那条)。
- **没有身份的条目两样都不许长出来。** 来源给不出 `drop` 就不显示按钮、给不出 `url` 就不显示链接
  (`wechat_local._no_body_failure`、`pipe_view.pipeFailureRow`)—— 指着另一篇文章让人删是不可恢复的。
  **但那一条仍然要列得出来**:漏报是静默丢数据。
- **`no_body` 明细协议有三种形态,全都要认**(`wechat_local._no_body_failure`):新版 CLI 给
  `{name,id,account,title,link}`;CLI 读不到结构化报告时给**带空身份的字典**;更旧的 CLI 给一个
  `账号 · 标题` 的裸字符串。后两种都走「列得出来、不可点、不可删」。
- **`账号 · 标题` 那个字符串只够给人看,不许拿它反查是哪一篇** —— 标题会重名、`·` 本身也可能出现在
  号名或标题里。身份走 `export_obsidian.py --report` 写的那份结构化 JSON,CLI 优先读它
  (`cli._no_body_items`)。**报告写不出来不许让这几条整个消失**:那样面板上会显示「导出完成」且明细
  为空,而确实有文章没进知识库。
- **删完要作废「当前待处理数」那一路**(`invalidatePipePending` + `refreshPipe(true)`)。删一条不产生
  新的一轮,`result_version` 一个字不变 —— 在途那次待处理查询带回来的是删除**之前**的数字,却照旧对
  得上版本、照旧被应用,还顺手把欠账清掉,于是面板永久停在删前那个数(整页刷新才归零)。所以给它另
  一条身份闸:**同一轮之内条目集合也会变**。
- **不可逆操作的等待预算要盖住后端那一串**(POST 45 秒:采集器 CLI 最长 20 秒,它里面那把 SQLite 还
  配了 15 秒 busy timeout)。**超时/断线一律说「结果未确认」,不许断言「没删掉」** —— 那一步可能已经
  在后端做完了;说完顺手重读一次状态,真删成了那一条当场就消失。
- **翻历史时那颗按钮一律不出现**(`pipeFailureRow` 的 `viewing`),口径同「重试失败项」:「看到的那
  一轮」和「按下去会动的那一轮」必须是同一轮。**链接照旧能点** —— 它只是去读原文。
- **`viewing` 要进明细那段的签名**(`JSON.stringify([!!viewing, failures])`)。两屏的 failures 完全
  可能逐字相同,只比内容的话,从历史切回最新之后按钮一直不长出来。
- **事件委托挂在 `#pipe-stages` 上,不许逐个按钮绑定** —— 明细那几段 innerHTML 每轮轮询都可能重建,
  逐个绑定会在重建的那一下全部丢掉。
- **确认框走 `ui.js` 的 `confirmBox`,不是原生 `confirm`**(全站约定),而且文案里要写明这是不可撤销
  的。**标题要把按钮自己那几个字摘掉** —— `.textContent` 会把「不再抓取」一起念进来,确认框里就成了
  「某个号 · 某篇不再抓取。将把它…」。
- **外链箭头走 `icons.js` 的 `arrow('ext')`,页面里不许出现 ↗ 字符**(全站约定)。

## 为什么不去改那几份运行记录

第一版的想法是「删完把那一条从最新那一轮里剔掉」。要剔的地方有四处:内存 `_state["stages"]`、
`pipeline_batch` 的整批汇总(**内存缓存,而 `last_result()` 优先读它**)、磁盘状态文件、历史文件里
同一轮那一条。漏掉任何一处,用户都会在某个时刻看见刚删掉的那几条回来;四处都改,则两份落盘在那一刻
不再是同一个业务结果的两个投影 —— 恢复按完成序号挑中哪一份,面板上就是哪一份的内容。

出口过滤没有这些问题:记录是只读的事实(那一轮确实因为没正文跳过了几篇),而账本回答的是另一个
问题 —— **「这一条用户已经处理掉了」**。历史里同一条也跟着不再出现,那正是用户要的:「以后别让我
再看见它」。

账本按条数封顶(`_KEEP = 2000`),它只需要盖住面板显示得到的那些轮次:历史最多 `_HISTORY_KEEP`(20)
轮 × 每轮最多 `_FAILURES_KEEP`(50)条,留了一倍余量。封顶掉的是最老的那几条 —— 它们对应的轮次早被
历史的保留上限挤出去了。

## 删完之后真的不再抓吗

公众号那条链:`drop_item` → `cli articles drop` → `repo.remove_article(id, account)` →
`add_deleted_blacklist` 把 `article_identity`(`__biz + mid + idx`)记进 `deleted_articles`,再删
`articles` / `article_bodies` / `article_llm_tags` 三张表里那一行。下一轮发现时
`article_identity.prepare_article` 的第一件事就是拿 `exclusion_keys`(档案 ID、链接身份、按链接算的
稳定 ID)去查黑名单,命中就 `return None` —— 既不入库,也不进 `_download` 的正文队列。

回归 `collector/tests/test_article_drop.py::test_dropping_one_article_stops_rediscovering_it_and_is_idempotent`
钉的就是这一条:删完再 `collect()` 一次,`articles` 仍是 0 行、正文抓取一次都没调。

### 可是那一道只是「早一道」

`prepare_article` 查完黑名单,到真正写入之间隔着抓正文和更新显示名 —— 而 legacy `isolation_level` 下
那次 SELECT 不开事务、不持锁。用户正好在这中间按下按钮,那一篇照旧 upsert 进去、照旧抓正文、照旧被
导出写进 Vault,而面板上它已经不再显示:**用户再也看不到这一条**。第 1 轮审查的复现脚本把这个序列
摆了出来 —— `row_exists_after_drop_and_ingest=true`、`body_fetch_calls=1`、`export_candidates=1`。

所以判据搬进写语句自己的 WHERE 里(`_excluded_clause`),三处写入都带着它、都返回 `bool`:

```sql
INSERT INTO articles (...) SELECT ?, ?, ...
 WHERE NOT EXISTS (SELECT 1 FROM deleted_articles WHERE article_id IN (?,?,?))
 ON CONFLICT(id) DO UPDATE SET ... WHERE <同一个子句>
```

`ON CONFLICT` 那一半也要带 —— 冲突走的是 UPDATE 分支,只在 INSERT 那边加等于没加。
`exclusion_keys` 一个键都算不出来时子句退化成 `"0"`:**证明不了这一篇没被排除就别写**。

### 缺行那一档

同步记录和没关掉的旧页面会一直持有这一篇的身份,而采集库完全可能被清理、重建或回退到没有这一行的
版本。原来那一版照旧返回「以后也不会再采集」,而一个字都没写进名单(实测
`identity_blacklisted=false`、`id_blacklisted=false`)—— 主系统随即把它从各轮明细里隐藏,下一轮发现
时它照样入库。**明细一旦隐藏,这一条就再也没有入口了**,比报一次红坏得多。

现在分三档(`cli._drop_missing`):有凭据 → 幂等成功;算得出身份 → 补写进名单再成功;两样都没有 →
拒。拒掉的代价只是那一条留在明细里(下一轮同步会重新带上 `link`),比静默失约轻得多。

## 已知边界:第 50 条之后的那些删不到

每轮明细在落盘前就被截断到前 50 条(`pipeline_state._cap_failures`),出口 `strip` 只能从这个有限数组
里剔除 —— **尾部那些压根不在记录里,没有任何后补查询能把它们找回来**。于是一轮有 51 条以上待补文章
时,逐条删完前 50 条之后整个明细入口会被隐藏(`details.hidden = !failures.length`),即便
`failures_total` 还大于 0。

**这一轮没改**,理由是:每轮记录本来就有显示上限(那是它存在的理由,不是这次引入的),而再跑一次
带导出的同步就会把余下那些重新列出来 —— 代价是多跑一轮,不是丢数据。真要改得把「可操作的待处理清单」
做成一条独立的分页查询(或者在截断时留住足够的身份),那是另一件事;**只把过滤函数挪个位置找不回已经
丢弃的尾部**。

## 回归清单

- `tests/test_pipeline_failure_drop.py`(18 条):账本与过滤口径、账本的并发与互斥、端点的两步顺序、
  `link` 一路送到 CLI 的 argv、采集器契约,外加一条真浏览器探针(`@requires_headless`)。
- `tests/pipe_drop_probe.mjs`(19 项):CDP 直连 chrome-headless-shell,自带 mock 后端。链接的
  `href`/`target`/箭头 svg、有没有按钮、自绘确认框、取消不发请求、确认后请求体(含 `link`)、删完那
  一条消失、剩下的不受牵连、明细标题的计数、**待处理数不被在途那份旧回包盖回去**、翻历史时按钮消失
  而链接照旧、整轮零 JS 错误。mock 的待处理接口**故意答得慢、而且按请求到达那一刻取值**,删除就落在
  它在途期间 —— 不这么摆的话那条断言压根不敏感。
- `tests/test_pipeline_history.py`:新增一条钉 `_write_atomic` 的并发(同一个 path 两条线程,落盘必须
  是某一次的完整内容)。窗口靠「分两段写、中间歇一下」摆出来。
- `collector/tests/test_article_drop.py`(6 条):结构化报告带身份、删除的幂等与拉黑、**写入层拦得住
  已经过了检查的那一次写入**(连同导出那道冗余带)、**缺行没凭据一律拒**、**同步期间移除**(在正文
  抓取那一步里按下按钮,报告里记 `excluded_articles` 而不是 `download_failed`),外加既有的
  `test_export_keeps_old_empty_article_retryable`(改成钉**降级**那一档)。
  这一族原来在 `test_local_wechat.py` 里,那个模块到了 700 行上限 —— 夹具抽去了
  `collector/tests/local_collect_case.py`(连同 `push()`;`test_local_wechat` 仍 re-export 它,
  `test_local_index` / `test_local_coverage` 的 import 一个字没改)。

## 反向验证(定点注入,各红几条)

kb 侧 14 次,每次只改一处、跑完就还原:

| 注入 | 红 |
|---|---|
| `strip_state` 只过 `last_run`、漏掉顶层 `stages` | 1 |
| `/api/pipeline/runs` 不过滤 | 1 |
| 端点顺序反过来(先记账本再删) | 1 failure + 1 error |
| 账本写失败也报红(去掉那圈 try) | 1 error |
| 没有 id 也拼 `drop` 字段 | 1 |
| 没有 link 也给 `url` | 1 |
| 不读结构化报告,一律退回文本解析(collector) | 1 error |
| `_exclusive()` 退化成空壳(账本不加互斥) | 2 |
| `_write_atomic` 临时文件名退回按 pid 固定 | 1 |
| `drop_item` 不拼 `--link` | 1 |
| `_no_body_failure` 的 `drop` 不带 `link` | 1 |
| 端点不把 `link` 转给采集器 | 2 |
| 去掉待处理数那道身份闸(`epoch` 比较) | 1 探针项 |
| 删完不刷新待处理数(改回 `refreshPipe()`) | 1 探针项 |

collector 侧另有 6 次:归属对不上也照删(1 红)、`remove_article` 只删行不拉黑(2 红 —— 新用例加上
既有的 `test_deleted_article_does_not_return_through_other_channel`)、写入层 guard 永远放行(1 红)、
导出 SQL 去掉排除名单(1 红)、缺行一律报成功(1 红)、下载循环不认写入拒绝(1 红)。

「没有 id 也拼 `drop`」第一次注入时红 0 条:用例只摆了**裸字符串**那一档,而真实的降级形态是
**带空身份的字典**。补上那一档之后才红 —— 用例不敏感,不是判据冗余。
