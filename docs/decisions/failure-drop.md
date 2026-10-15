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
- **删除要幂等。** 用户会连点,两个标签页也会各点一次,库里已经没有那一行时照样返回成功
  (`cli.cmd_articles` 的 `row is None` 分支)。
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
`article_identity.prepare_article` 的第一件事就是拿 `(id, identity, stable_article_id(link))` 三个键
去查黑名单,命中就 `return None` —— 既不入库,也不进 `_download` 的正文队列。

回归 `collector/tests/test_local_wechat.py::test_dropping_one_article_stops_rediscovering_it_and_is_idempotent`
钉的就是这一条:删完再 `collect()` 一次,`articles` 仍是 0 行、正文抓取一次都没调。

## 回归清单

- `tests/test_pipeline_failure_drop.py`(13 条):账本与过滤口径、端点的两步顺序、采集器契约,外加
  一条真浏览器探针(`@requires_headless`)。
- `tests/pipe_drop_probe.mjs`(18 项):CDP 直连 chrome-headless-shell,自带 mock 后端。链接的
  `href`/`target`/箭头 svg、有没有按钮、自绘确认框、取消不发请求、确认后请求体、删完那一条消失、
  剩下的不受牵连、明细标题的计数、翻历史时按钮消失而链接照旧、整轮零 JS 错误。
- `collector/tests/test_local_wechat.py`:新增两条(结构化报告带身份、删除的幂等与拉黑),既有的
  `test_export_keeps_old_empty_article_retryable` 改成钉**降级**那一档。

## 反向验证(定点注入,各红几条)

kb 侧 7 次,每次只改一处、跑完就还原:

| 注入 | 红 |
|---|---|
| `strip_state` 只过 `last_run`、漏掉顶层 `stages` | 1 |
| `/api/pipeline/runs` 不过滤 | 1 |
| 端点顺序反过来(先记账本再删) | 1 failure + 1 error |
| 账本写失败也报红(去掉那圈 try) | 1 error |
| 没有 id 也拼 `drop` 字段 | 1 |
| 没有 link 也给 `url` | 1 |
| 不读结构化报告,一律退回文本解析(collector) | 1 error |

collector 侧另有 2 次:归属对不上也照删(1 红)、`remove_article` 只删行不拉黑(2 红 —— 新用例加上
既有的 `test_deleted_article_does_not_return_through_other_channel`)。

「没有 id 也拼 `drop`」第一次注入时红 0 条:用例只摆了**裸字符串**那一档,而真实的降级形态是
**带空身份的字典**。补上那一档之后才红 —— 用例不敏感,不是判据冗余。
