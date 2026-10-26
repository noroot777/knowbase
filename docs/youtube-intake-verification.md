# YouTube 低成本接入验证

## 范围与隔离

- 实现工作区：`/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH`。
- 频道快照沿用已提交的 52 个频道，默认启用 48 个；验证新库幂等补种、用户删除后不复活和权重保留。
- 测试使用临时数据库、vault、转录与模型替身，没有修改生产库，也没有调用真实媒体下载、ASR 或付费 LLM。
- 浏览器夹具只替换远端发现、媒体获取与模型回复；真实执行排名准入、全文分块翻译编排、笔记构建、数据库提交、导出、索引、API 和 UI。

## 追加审查第二轮：永久移除处理闸门（2026-10-24）

对应 `lSOeb3YBCdN_/round-2`。三条意见的技术结论均认可，但本轮只修复原链路内的 F3。
F1 的历史规模性能退化、F2 的极短归并尾块仍未解决，按用户明确的衍生问题边界提请转独立任务，等待用户裁定；不是三条审查意见全部通过。

### F3 的不变量与执行证据

永久移除以 `feed_item_exclusions` 的内容身份为准，独立于视频的 candidate/ready/failed 等处理状态，也不能由 force 撤销。
发现阶段不再重新创建该身份或新的订阅归属，排名、频道热度基线和选择阶段均排除它；处理端在元数据前、媒体前、内容模型前重查当前排除记录，发布端在既有写事务中再次检查。
永久移除项目的旧失败不再阻塞来源成功。Shorts 等可重探的类型排除仍允许显式重试，不与用户永久移除混淆。

哨兵 `uoJuHCxu4_3g` 正常退出，退出码 0：172 项全部通过，无跳过，耗时 12.881 秒。
其中 84 项为 YouTube 后端检查（含本轮新增 9 项），88 项为共享来源、永久移除、路径身份和预算契约。
工作目录为 `/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH`，原始命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin PYTHONPATH=/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH/tests:/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-permanent-r2 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_exclusions tests.test_youtube_admission tests.test_youtube_channels tests.test_youtube_collector tests.test_youtube_consumers tests.test_youtube_discovery tests.test_youtube_integrity tests.test_youtube_llm_request tests.test_youtube_media tests.test_youtube_notes tests.test_youtube_products tests.test_youtube_stage_cache tests.test_youtube_transcripts tests.test_failure_sources.FailureSourceTests tests.test_feed_drop.FeedDropTests tests.test_feed_drop_paths tests.test_feed_sources tests.test_source_aliases tests.test_feed_budget -v
```

完整日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/uoJuHCxu4_3g.log`。
SHA-256：`b3403600ed3c69aef1f0eb34dbb7954d6fafa3d80d94309fb340c1121be5086e`。
实际日志含上游 Starlette/pkg_resources 弃用警告，没有测试失败或模型请求管道 ResourceWarning。
这是本次新版本的执行者回归记录，不复用历史记录，也不提供独立验收或全仓 fast/probes 通过结论。

`tests/test_youtube_exclusions.py` 的 9 项新检查覆盖：

- 已发布、永久移除、再清空资格证明的旧成品，普通同步及 force 均不进入元数据、媒体或模型，也不占榜单和处理额度。
- candidate、filtered、deferred、processing、failed、ready、excluded 各状态及退避重试；永久移除事实保留，旧失败不阻塞来源成功。
- 没有现存条目的永久移除身份，再被多个订阅发现也不重建候选或归属。
- 排名及频道基线不受被移除视频影响，选择入口自身也拒绝 force 绕过。
- 排名之后才发生移除的旧快照，在元数据之前重新检查。
- 元数据执行期间移除，不再进入媒体或模型；媒体执行期间移除，不再进入后续内容模型。
- 已取得的 ready 旧快照，在发布事务中遇到永久移除后不调用写入。

### F1、F2 的边界与待裁定范围

不质疑这两条报告的正确性，转出依据是上一轮修复引出的新根因，不是工作量：

|意见|根因的引入证据|建议独立任务范围|
|---|---|---|
|F1：全量重验放大为平方级工作|`4cf56f5..f82cdd2` 将 registry.db 的频道补种改为 initialize，并把限定来源的 products.prepare 改成每次连接全局 quarantine；这是上一轮修复旧成品消费资格时新增的成本。报告亦明确记录“功能修复引入的成本”|按内容/策略版本增量维护资格，保留全存档防绕过，批量更新排名；覆盖 200 条旧视频、无新入选、跨来源读接口、写锁与工作量边界|
|F2：一字或无语义尾块无法完成归并|同一提交新增 summary_output 的逐块 `len(output) < len(source)` 约束，并将它接入原有 12000 字切块；`4cf56f5` 只有整层缩短检查，无这个逐块不可满足条件。上轮坏缓存失效已经修复，本轮报告的尾块失败是该新约束与旧切块组合引出的另一根因|归并切块、短尾块合并或保留、整层进展不变量；覆盖 24001 及其它单字、标点、空白尾块和健康模型重试，并保留合格上游缓存|

本轮没有修改 registry.db、notes 的归并实现或 quarantine 的全量校验方式，未重跑上述性能与尾块复现，也不声称它们已修好。
F3 在上一版限定来源的 prepare 与处理链上已经缺少永久排除闸门，属于原 YouTube 处理链路，本轮按共享内容身份修复，不将其误当作仅本轮才出现的衍生问题。

### 验证边界

本轮没有 UI 改动，没有启动或操作浏览器，也没有重新生成或复用截图作为本轮证据。
回归明确选择后端模块及非 UI 测试类，未运行 YouTube browser、公共布局或移除面板用例，不把历史页面结果算成本轮通过。
测试只用临时库、替身媒体和模型；模型请求协议测试使用本地 HTTP 服务，未操作生产库、真实媒体、ASR 或付费 LLM。
处理开始前已永久移除的视频不进入昂贵操作；已启动的单次媒体操作无法追回成本，若期间发生移除，只保证不再启动后续内容模型和发布。
全局历史重验、极短尾块及此前披露的其它非阻断边界仍保留；新增处理边界核验本身不是历史规模性能优化。
收尾检查覆盖共同起点以来的 45 个改动代码文件：Python AST/编译、Node 语法、diff 空白及 700 行约束全部通过，registry 仍为 699 行；未发现匹配本轮临时标识或模型请求入口的遗留进程。

## 追加审查四项修复验证（2026-10-24）

对应审查 `lSOeb3YBCdN_/round-1`，F1–F4 均认可并按原 YouTube 链路修复。
最终新执行的 284 项全部通过，包含 76 项 YouTube 检查及 13 个共享模块的 208 项检查，无跳过。
这是执行者在隔离夹具上的回归记录，不是独立验收结论；未重跑整个 fast/probes，不声称全仓通过。

### 最终命令与证据

哨兵 `sPDMdHzsjzw3` 正常退出，退出码 0，耗时 151.893 秒。
工作目录为 `/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH`，原始命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin PYTHONPATH=/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH/tests:/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-lSOe-fix-final TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests/test_youtube*.py tests.test_failure_sources tests.test_feed_sources tests.test_source_aliases tests.test_fulltext_extract_stale tests.test_doc_rewrite_roster tests.test_feed_translate tests.test_feed_trim tests.test_feed_trim_pending tests.test_feed_drop tests.test_feed_drop_paths tests.test_feed_fulltext tests.test_feed_budget tests.test_subs_layout -v
```

完整日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/sPDMdHzsjzw3.log`。
日志结尾为 `Ran 284 tests in 151.893s`、`OK`；存在上游 Starlette 和 pkg_resources 弃用警告，未见模型请求管道的 ResourceWarning。
没有复用旧结果或用代码树相等代替不可变输入证明。

### 发现、修复路径与回归

下表证据均包含在上述新日志内；本轮新增 10 项 YouTube 用例，没有跳过旧用例。

|发现|根因修复路径|执行证据与结果|
|---|---|---|
|F1：旧残缺成品可经停用、删除或改址来源绕过校验|`registry.db` 初始化事务调用 `storage.initialize`，统一迁移和 `products.quarantine`，不依赖采集范围；无资格正文改为明确待校验说明，清旧译文并置提取失效，不触发网络或模型|`tests/test_youtube_consumers.py` 覆盖启用、停用、删除、改址及孤儿内容的真实 API 与导出；均不再返回旧残缺正文，合格无归属存档仍可读。原有重建、重导出、索引及旧提取失效用例同时通过|
|F2：归属的 committed 与实际关联分离|订阅改址事务触发器清理所有旧归属，删除关联触发器撤销提交标记；查询和提交边界均核验实际关联，永久排除仍生效|真实 PATCH A→B→A 且不运行发现，之后复用成品恢复订阅关联，不调用媒体或模型；伪提交标记、历史清理和永久移除契约通过|
|F3：失败归并缓存使重试永远失败|`notes.cached` 对命中及新输出统一执行阶段校验，坏缓存只删除当前阶段；译文、分段要点、归并及最终 JSON 均先校验后保存|`tests/test_youtube_stage_cache.py` 验证 16 块长文归并失败后恢复，旧坏结果自动失效，仅重做两次归并和一次最终笔记；不重复合格翻译及 map。空、英文、超长要点和坏译文/JSON 同时覆盖|
|F4：真实 Shorts 枚举未被排除|准入层识别 yt-dlp 的 `media_type=short`，兼容旧 `shorts` 和地址后备，不依赖 watch/Shorts URL 形状|`tests/test_youtube_admission.py` 以实际单数字段覆盖 watch 和 Shorts 地址，均不进入 obtain/build；普通 60 秒视频仍可处理|

共享回归还覆盖通用来源、失败身份、来源别名、翻译、历史清理、永久移除与路径清理、全文和预算，以及公共订阅布局。
F2 的改址/关联不变量在原单表实现也存在缺口，本轮不将其误当作仅由前一轮分表修复衍生的独立任务。

### 过程失败与纠正

- `W0rwkyufdC6F`：76 项 YouTube 全部通过，退出码 0，11.690 秒。
- `EA9vv3tXkI4L`：177 项，退出码 1，3 个错误，138.707 秒。两个旧清理模块通过顶层 `feed_trim_fixture` 导入，初次命令未把 tests 目录加入 PYTHONPATH；修正最终运行环境，没有改动无关导入实现。
- 另一个错误来自通用翻译夹具：伪造的无资格 YouTube 英文成品现在按 F1 先被隔离，不应产生通用模型翻译错误。修正测试为断言待校验 provenance、没有翻译调用且 errors 为空；YouTube 的导出失败身份断言继续执行，其他来源仍验证翻译失败身份，没有跳过来源。
- 最终 `sPDMdHzsjzw3` 在夹具修正后重新执行全部 284 项，实际包含此前未成功导入的历史清理模块；未以先前的成功记录代替最终版本验证。

过程完整日志位于同一 regression 目录的 `W0rwkyufdC6F.log` 和 `EA9vv3tXkI4L.log`。

### 本次页面与剩余边界

最终合并回归真实启动隔离服务和独立无头临时 profile。
本次 `output/youtube/checks.json` 的 14 项行为断言、`output/subs-layout/results.json` 的 527 项公共布局断言全部通过，后者生成于 2026-10-24 20:15:18（Asia/Shanghai）。
人工查看本次 `output/youtube/subscriptions-desktop.png`、`chinese-reader-desktop.png`、`subscriptions-mobile.png`，以及 `output/subs-layout/manage-1440.png`、`responsive-390-844.png`：
中文阅读、免责声明、原视频时间链接可见；桌面订阅与 390px 窄屏未见新增布局遮挡或页面横向溢出。同步完成提示为夹具正常通知，不是错误弹层。

首选 Chrome 扩展具名会话的后台创建此前多次返回 `Capability is not available: visibility`，未创建验证标签；本轮沿用已证降级。
没有连接用户 Chrome/CDP、接管已有标签、激活窗口或启动有头浏览器，不访问生产库，也不运行真实媒体下载、ASR 或付费 LLM。
页面和模型替身只能证明本地编排、缓存及存储契约，不证明真实 YouTube 可用性或翻译质量。

审查列为非阻断的边界保留：强杀父进程后的子进程回收、发现阶段停止延迟、并发设置覆盖、首次排除不补位、缓存保留/磁盘配额及异常清理残留、混合/日文语言判定、完整性启发式的误拒与语义限制、榜单排序重置、归并签名未含原题。
已有日报来源范围仍需用户手动加入 YouTube；无真实 Windows、真实长视频性能或外网限流的通过结论。
历史公共浏览器 `passed=null` 记录仍只作历史说明，不作为本次通过或独立验收依据。
收尾静态检查覆盖共同起点以来的 44 个改动代码文件：Python AST/编译、Node 语法和 diff 空白检查通过，全部不超过 700 行，registry 为 699 行。
按本轮临时数据标识、页面探针、夹具服务及模型请求入口检查进程，未发现匹配的遗留进程；截图和 JSON 保留为忽略的运行产物，不加入提交。

## 第二轮审查修复验证（2026-10-24）

F1–F3 均认可并在原 YouTube 链路内修复，没有改动无关的平台或通用设置实现。
本节记录本轮新执行结果，不复用下文历史回归，也不将执行者回归当作独立验收结论。

### 最终合并回归

哨兵 `MazduTH8TScN` 正常退出，退出码 0：136 项全部通过，0 跳过，耗时 41.354 秒。
工作目录为 `/Users/fjh/code/knowledge-base/.worktrees/wnYGhulNzmEH`，原始命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-round2-final TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests/test_youtube*.py tests.test_failure_sources tests.test_feed_sources tests.test_source_aliases tests.test_fulltext_extract_stale tests.test_doc_rewrite_roster -v
```

完整日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/MazduTH8TScN.log`。
日志逐段核对，含上游依赖弃用警告，无测试失败或跳过；没有把代码树相等视为输入闭包证明。

范围为 66 项 YouTube 检查（本轮新增 12 项）及 70 项受影响契约：

- F1：旧表未发布成品、已发布残缺正文和上一版全局 ready 都不能绕过当前校验。成品持久化策略版本、完整输入快照及输入/正文指纹，ready 复用和发布边界均重验；篡改策略、输入或正文时拒绝发布。
- F1：旧正文先隔离为待校验说明，重建后更新同一个条目，不产生重复内容；已收录旧成品可跨默认新视频窗口修复。实测真实导出、索引和旧提取结果失效，清除旧摘要、实体与话题；默认提取关闭时不排队，用户开启后才重新进入待提取列表。
- F2：不同历史下界的两个有效归属分别准入、逐订阅提交，全局只处理一次。最高合格权重获选，不依赖插入顺序；不合格归属和观察名单不能抬高共享视频权重。
- F3：缺时长和首映待发布延后 120 秒、直播延后 900 秒，期满重新探测并跳过旧元数据；显式重试可提前核验并恢复。Shorts/回放的排除不占普通排名，显式重试仍可重验；旧版无类型 excluded 在升级后重新开放。
- 原有字幕、中文输出、数量与时长预算、失败覆盖、模型截止/取消以及隔离无头页面均重新执行；通用来源、失败身份、来源别名、导出旧提取失效和文档改写入口契约一并通过。

### 过程中的失败与纠正

`_odG6iJqXt-r` 首次执行 66 项，仅 1 项失败：新增测试错误假定 YouTube 默认开启知识提取。
当次真实重建正文、重导出、索引与旧提取清理的断言已经通过；修正测试为先确认默认不排队，再显式开启提取并验证重排。
没有为迎合测试更改产品默认值。相关 70 项契约由 `0IWbGNmfpkVG` 先行通过（退出码 0，30.071 秒），
上述最终 136 项又在测试修正后的版本上完整重跑，未以旧记录代替新检查。
两份过程日志分别为同一 regression 目录下的 `_odG6iJqXt-r.log` 与 `0IWbGNmfpkVG.log`。

### 页面证据与剩余边界

最终合并回归内真实启动隔离服务及独立无头临时 profile，`output/youtube/checks.json` 的 14 项行为断言通过。
已重新人工查看本次生成的 `subscriptions-desktop.png`、`chinese-reader-desktop.png` 和 `subscriptions-mobile.png`，
位置均在 `output/youtube/`；中文阅读、原视频时间链接与 390px 窄屏无新增遮挡或页面横向溢出。
扩展具名会话后的后台创建仍受既有 `Capability is not available: visibility` 限制，沿用已证降级；
未操作用户已有标签、激活 Chrome、连接用户 Chrome CDP 或启动有头浏览器。截图仅验证本地夹具，不证明真实翻译质量。

本轮未重跑整个 fast/probes，不声称全仓通过，也不拿下文后端 `passed=null` 的历史公共浏览器记录作验收依据。
未实测真实 YouTube 可访问性、真实 ASR/翻译质量、长视频性能或异常强杀后的子进程回收。
并发局部设置、首次排除不补足当轮名额及缓存全局保留/磁盘配额仍是已披露边界；未以本轮修复扩大任务范围。

## 第一轮审查修复验证（2026-10-24）

R1–R6 均按报告修复，无驳回或扩大到无关模块。实现说明见 `docs/decisions/youtube-intake.md`。

### 最终定向回归

哨兵 `KL-qkAWjftn9`，54 项全部通过，无跳过，耗时 10.709 秒。命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-review-targeted-final TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest discover -s tests -p 'test_youtube*.py' -v
```

完整日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/KL-qkAWjftn9.log`。
覆盖原有 35 项及新增 19 项，其中包括：

- R1：片尾独占、中间断层、稀疏字幕、重叠区间伪覆盖、缓存残缺均拒收；合理暂停和有效内容仍接受；逐时间标记段拒绝空译文或明显摘要化译文。
- R2：编号与 handle 别名复用一次全局处理，各自提交成品；首个订阅停用、删除或改址后其他归属仍有效；旧表迁移保留失败与归属。
- R3：数量、时长、缺模型、停止和日期范围均不能清除旧错误或上报成功覆盖；真正处理成功后才清账。
- R4：有/无字幕元数据有效期、强制刷新、字幕后来补齐及失败后刷新，均走下一轮实际恢复路径。
- R5：真实子进程取消与回收、所有管道关闭、共享绝对期限，以及本地 HTTP 服务注入故障：OpenAI 500 只发一次请求，Anthropic 持续 SSE 也不能延长总预算。
- C1/C2/C3/C5：已排除视频不占准入排名、坏作者字幕后继续尝试其他作者轨道、缓存落盘失败后复用 ASR 结果、局部设置不重置其余预算。

模型请求测试使用实际 SDK 和 `127.0.0.1` 服务、fixture key/base URL，不访问远端模型。
上一条 `pKxy6KXbc51V` 同样 54 项通过，但暴露取消后的管道未关闭警告；已修复，并以上述新记录覆盖该修改。
最终日志不再出现该 `ResourceWarning`。本文没有把旧记录、代码树相等或可变目录的输入诊断当作可复用的独立验收证明。

### 公共浏览器层

历史哨兵 `cM6GQ-tBTa99` 的原始日志显示 490 项 OK，耗时 618.837 秒，无跳过，浏览器用例实际执行 151/151。
但后端记录为 `passed=null`、`reusable=false`，不认定为后端核定通过，不具备独立验收复用资格；
这里只保留历史执行结果，不作为本轮通过依据。原始命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-review-probes TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m tests.tiers probes
```

完整日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/cM6GQ-tBTa99.log`。
R6 未跳过原布局测试，公共夹具给 YouTube 使用合法且隔离的 `@layout-fixture` 频道。
公共布局探针 `output/subs-layout/results.json` 的 527 项断言全部通过；YouTube 探针 `output/youtube/checks.json` 的 14 项断言全部通过。
本轮已人工查看 YouTube 订阅桌面、中文阅读与窄屏截图，以及公共订阅管理桌面与 390px 布局截图，未见内容遮挡或页面横向溢出。
截图位于 `output/youtube/` 和 `output/subs-layout/`，是忽略的运行产物，不加入源码提交。

### 全仓快速层

哨兵 `vCVCSK3ezSTx`：4144 项，312.148 秒，4 errors、242 skipped，命令 exit=1，不能称全仓通过。

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-review-fast TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m tests.tiers fast
```

完整日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/vCVCSK3ezSTx.log`。
4 个错误与首轮独立审查一致：负 fd 的 `test_filelock` 得到 ValueError（1 个），macOS 模拟 Windows 时
`test_wechat_setup` 缺少 `subprocess.CREATE_NO_WINDOW` 及后继断言（3 个）。这些实现未修改，未见本任务相关失败。
本次未重现初版的两个 uv/shell 失败。242 个跳过中 151 个是 fast 明确不运行的浏览器用例，其余需要平台条件。
该快速层在最后的管道关闭补丁前启动；补丁由后跑的最终 54 项定向回归验证，不将快速层冒称为补丁后的完整重测。

### 交付边界

- 已保存日报来源范围的用户，需在日报同步范围中手动勾选 YouTube，才能纳入定时追踪；不覆盖已有范围。
- 未增加视频画面理解，不保证语义无损；覆盖率和译文长度只是保守拦截明显缺漏，长时间静默内容可能被拒收。
- 尚无候选/字幕/失败音频的全局保留期限或磁盘总配额，长期运行需要关注缓存目录容量。
- 不对真实 YouTube 可访问性、真实 ASR、真实翻译质量或长视频耗时作已验证承诺；未动生产库。

## 初版定向回归（历史记录）

哨兵 `NDa6Lm4KsGJA`，退出码 0，35 项通过，无跳过。命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-targeted-v5 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest discover -s tests -p 'test_youtube*.py' -v
```

日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/NDa6Lm4KsGJA.log`。

覆盖频道管理和独立种子版本、RSS 与 handle 解析、AI 相关性和双榜排名、数量/累计时长/单条时长门槛、重试退避、旧订阅回包拒收与改址重发现。
字幕链路覆盖缓存、既有原文 SRT、作者字幕、原语言自动字幕、坏字幕回退和音频后备；验证不选择自动翻译轨道、不下载画面、ASR 线程上限与媒体清理。
中文链路覆盖长字幕分块、结尾内容进入模型输入、时间标记完整性、模型/输入缓存身份、失败不发布、完整中文转录导出以及显式日期范围。

## 浏览器通道与实测

首选 Chrome 扩展已命名会话 `🔎 YouTube 中文订阅验证`，但创建后台标签的 `visible:false` 被拒绝：`Capability is not available: visibility`。
这次调用没有创建标签；因无法保证后台操作，降级到独立无头浏览器和临时 profile，没有连接用户 Chrome 的 CDP、激活窗口或操作用户已有标签。

浏览器探针的 14 项断言全部通过，包括：

- 初始 52 个频道出现在订阅管理，热度与价值榜可切换，非 AI 候选显示过滤原因。
- 将每轮数量改成 1 后，服务端保存；新增频道的权重 1.5 持久化。
- 通过真实同步弹窗只运行抓取、导出和索引，准入一个视频，另外两个因预算保留，非 AI 视频不处理。
- 阅读区显示中文标题、笔记、完整中文转录、画面未分析提示，以及跳转原视频的时间戳；临时 vault 中恰好导出一篇中文 Markdown。
- 切换 RSS 后清除视频排名，恢复通用输入框；390 像素窄屏没有页面横向溢出，也没有浏览器脚本异常。

已人工查看以下截图，桌面排名、中文正文和窄屏内容未见遮挡或横向溢出。截图与断言 JSON 是工作区内忽略的运行产物：

- `output/youtube/subscriptions-desktop.png`
- `output/youtube/chinese-reader-desktop.png`
- `output/youtube/subscriptions-mobile.png`
- `output/youtube/checks.json`

## 初版快速回归与基线对照（历史记录）

哨兵 `7jS2LIbf0rsL` 运行快速层，退出码 1：4125 项，2 个失败、4 个错误、242 个跳过；快速层按设计不运行 151 项浏览器用例，本功能的真实浏览器验证已在定向回归单独执行。命令：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-fast-v4 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m tests.tiers fast
```

日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/7jS2LIbf0rsL.log`。

在本轮实现前的 `3bbc0c5` 归档目录 `/private/tmp/kb-yt-baseline-sg3RHH`，用相同环境运行这五个失败相关模块，哨兵 `FR0uaV4hHl27` 实际执行 67 项，同样得到 2 个失败、4 个错误：

```sh
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/private/tmp KB_DATA_DIR=/private/tmp/kb-youtube-baseline-v4 TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_filelock tests.test_wechat_setup tests.test_fulltext_held_exit tests.test_kb_grab_cmd tests.test_kb_web_start -v
```

日志：`/Users/fjh/code/harness/data/runs/wnYGhulNzmEH/regression/FR0uaV4hHl27.log`。残余失败与基线逐项一致：

- `test_fulltext_held_exit` 的两个 shell 入口用例在其隔离命令环境中找不到 `uv`。
- `test_filelock` 负文件描述符返回 `ValueError`，而用例预期其他异常。
- `test_wechat_setup` 在 macOS 上模拟 Windows 路径时缺少 `subprocess.CREATE_NO_WINDOW`，产生三个错误结果。

未修改这些与本任务无关的实现，不把全仓快速层报告为通过。归档目录不含 `.git`，哨兵输入诊断会提示不是 Git 仓库，但完整日志证明上述测试实际执行；本记录不是可自动复用的独立验收证据。

## 静态检查和未验证边界

修改与新增代码的 Python 编译、Node 语法、`git diff --check` 和单文件不超过 700 行检查通过。
尚未验证真实 YouTube 当前可访问性、字幕限流、本机模型的识别质量、真实模型翻译质量与真实长视频耗时。工具或模型缺失会明确报错，不伪造成功。
时间戳、开头/缺段/区间覆盖和逐段译文长度检查只能拒绝明显坏内容，不能证明语义无损；系统不读取画面，画面专属信息仍会缺失。
