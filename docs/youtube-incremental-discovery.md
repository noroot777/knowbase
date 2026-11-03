# YouTube 增量发现与元数据缓存

## 现行发现规则（2026-11-03，同步失效修复）

列表粗日期不能代替最终发布日期，但也不应因此全量展开历史详情。普通同步依次执行：

1. 待查频道串行读取带登录态的 flat 列表，并启用 `youtubetab:approximate_date` 和 `--lazy-playlist`。
   按用户关注频道的低发布频率，首批读取 10 条；尾部连续 3 条粗日期均早于下界减两天余量时停止。
   不再为连续 30 条旧视频的确认成本扩大首批，也不再翻到历史末尾。
   若近期条目仍延伸到尾部，读取范围自适应扩大到 20、40……，没有固定总条数截断。
   不使用 `--break-match-filters`，不因单条旧视频就停止；频道间停 1 秒。
   明显早于请求下界且留出两天余量的粗日期只作低成本排队裁剪；边界附近或缺失日期均保留为未核实候选。
2. 普通轻量路径不传 `--download-archive`，确保日期边界判断能看见已知条目。
   已发现 ID 与仍早于窗口的历史精确检查 ID 仅在本地过滤，避免重复加入元数据队列。
   既有 `youtube_discovery_history` 保持兼容，不将估算日期写成真实历史检查日期。
3. 列表结束后，候选复用 6 小时内精确快照，只补未知或过期详情；完成/排除的不再探测。
   粗日期始终带 `approximate`，核实后按每个订阅归属持久保存的 `discovery_after` 再次裁剪，
   不能把真实早于五天起点的未知视频混进成品；老归属和已排队候选不被新订阅的起点连带过滤。
4. 所有候选继续使用原有资格判断和评分公式预排；未核实与旧快照不伪装成实时事实。
5. 再按原有视频数量、时长及处理预算取字幕、ASR、翻译和生成内容笔记。

所有远端请求仍由 yt-dlp 使用已配置浏览器的登录态，不改用匿名 RSS，不增加频道并发。
列表失败直接保留可重试错误，不用 flat 粗日期伪装精确元数据或成品日期。
单频道保留 180 秒网络兜底上限，不再把它用来逐条展开历史详情，
没有固定总条数上限；普通整轮按待查频道数 ×（180 秒 + 1 秒间隔）计额，最多 900 秒，间隔在额度内预留。
下一轮优先最久未尝试的频道，未轮到的不改尝试时间；不会因为 48 个频道把发现额度放大到 145 分钟。
显式日期补抓保留单频道 90 秒、整轮 1800 秒上限。这里只限制列表发现，其他阶段有独立预算，
终止进程和本地写库还需少量收尾时间。日期窗口实际控制是否继续翻页，不只是事后候选过滤。
尾部缺少日期时保留有限候选、标记未确认覆盖并停止扩展，不能让全部历史涌入元数据队列。
未知日期仍可能触发有限的历史核实；不能把候选数、列表 ID 数或耗时上限说成成品数和实际性能。
极密集或特别慢的频道仍可能超时：保留有效条目并标记部分完成，即使这些条目的内容都已完成，
也不推进频道 `last_success`。下一轮按旧水位重查并本地去重，不冒称保存了列表位置游标。
`--no-quiet` 保留完整播放列表结束及已在 archive 等证据；只有明确结束/跳过证据才把空输出视为空窗口。
无解释空输出或真实错误仍为可重试失败，不能把登录墙记为「没有新内容」。
日期未知的候选不作为连续旧页证据；满批尾部日期缺失记部分完成，小频道真实列尽仍可确认覆盖。
日期边界正常取消会让 yt-dlp 退出 `101`，不等于预算中断。工具层保留结构化退出码，
共用列表适配层兼容有真实日期过滤证据的正常 101；普通路径不再依赖日期截断，
退 1 但有完整播放列表结束证据、全部 ERROR 都是非登录/限流等阻断类的视频级错误时也认可覆盖完成。
不可用条目进入带 source_id 的 `notices` 和本频道 last_notice，不再让整频道永久停在「没列完」；
未列完的原因同样带 source_id，在写库时进入 last_note，卡片显示「部分同步」，不冒称有跳过的成功。
无跳过、无警告的其他频道不继承全局汇总；完成恢复会清空旧 note/notice，原失败账只在真正成功后清理。
不以 cookie、429 或 JS 挑战等警告分类否决完成；警告仍经 `cautions` / `walls` 和出口熔断单独交账。
不为完整覆盖记 `partial`，订阅保留真实警告。登录墙、未知错误、真实超时、其他退出码或无完整结束证据仍保留失败/部分完成说明；
不能只见一行日期提示就把后续真实故障掩掉。其他列表路径不改变非零退出的含义。

为避免乱序漏抓，不给窗口增加一个新的固定条数上限；固定时间预算内可能只能检查一段。
快速路径依赖 `/videos` 最新列表的总体时间顺序，保留已读范围内的乱序条目，
不承诺在任意历史深处插入的新条目也能无需全史找到；显式补抓仍保留精确路径。
大频道的近期列表仍可能部分返回；确认连续旧页边界或真实列尽才推进成功水位，
不是看到工具结束某个 10 条批次就宣称整个窗口完成。精确元数据与内容待办独立保留。
本次生产问题与验证范围见 `youtube-sync-2026-11-03.md`。

## 首次同步起点的历史实现（2026-10-31，已由五天规则替代）

以下为历史记录，不是现行默认：旧实现错误地使用三天且冻结失败请求起点。
现行无有效成功时间时，以本轮开始时上海今天零点减五天作为请求下界；审计起点保留，
失败跨日不扩大新发现窗口。已有成功水位与显式补抓规则不变。

原先 48 个频道发现 957 条并不代表抓完了全部历史：旧 flat 列表每频道最多 20 条，48 × 20 的上限为 960 条，且只拿轻量候选，不下载全部视频。
原路径使用 7 天窗口与订阅历史下界，但没有独立的首次抓取起点；flat 条目没有真实发布时间，按列表位置填的时间只供预排，不能证明它属于最近几天。

- 无有效 `last_success` 时，统一以整轮开始时间计算「上海当天零点减 3 天」，首次发现尝试前即持久化 `youtube_sync_origins.initial_fetch_at`。例如 2026-10-31 的起点为 `2026-10-28T00:00:00+08:00`，含这个时刻；同轮跨零点的频道不会差一天。
- 这是缺失抓取时间时的默认起点，不是成功记录；不伪写 `last_success`、`last_attempt`、错误或退避。已有有效成功记录后的下界取「该时间减一天」和「上海今天零点减 window_days」更早者，既覆盖列表延迟，也让调大的回看设置进入真实发现窗口；未成功前仍复用三天起点。
- 已保存的初始起点在完全失败、超时和跨日重试时均复用。后续成功使用真实成功时间，但不改初始记录；换址重置、删除级联清理、改名保留，旧条目检查进度同样处理。
- 只判断新发现是否落库；已经在队列中的视频不按该起点追溯判废，仍遵循原有 7 天窗口与订阅历史下界。新规则不修改其他订阅类型的历史深度。
- 一次性迁移仅恢复确因「早于首次同步起点（默认从今天回看 3 天）」被过滤的候选；原有错误、尝试次数、冷却保留，不恢复其他原因的排除。
- 普通同步走精确日期路径，不再全量放行 `approximate=True` 的新列表。保留安全兼容：未知日期不能冒充真实日期，也不能据此删除旧队列；存量估算条目仍需正常补元数据，但不再套新起点拒收。
- 普通强制重试不绕开日期限制；显式指定日期补抓既不初始化也不应用三天起点，仍可补抓更早内容。

`feed_runs.summary.initial_fetch_at` 记录本轮使用并保存的首次默认起点，`outside_initial_window` 记录 Python 精确过滤的新条目数。没有新增界面次级标签。

### 本轮覆盖声明

`Ruq8xJHCetPt` 四项阻断分别覆盖条目不可用与完整覆盖、自动译文不冒称完整、首次失败跨日起点冻结、旧条目后新尾部及预算续扫。
仅运行本轮修改的 notes、initial_window、windowed_discovery、window_coverage 和 browser 五个模块；
原始结果与冻结树见 `youtube-intake-verification.md` 顶部所列外部目录。其他回归留给验收，不以历史通过记录顶替。
`tests/test_youtube_windowed_discovery.py` 从真实 `discovery.fetch` 和 collector 入口验证，仅替换 yt-dlp 进程边界，不再只拿精确 `discovery.fetch` 替身证明规则生效。测试不访问 YouTube、cookie 或真实模型。

### 第二轮复修执行证据

针对固定 20 条尾部不可达、轻量预算不足及正常边界与警告混淆，新增超过 20 条的真实离线子进程、
archive 后续新增、按 4.3 秒/条虚拟计时的完整窗口、48 频道全预算以及部分完成跨轮追赶用例。
只有计时测试使用虚拟钟，不把它们冒充真实 YouTube 性能测量。同步更新受预算契约影响的定向测试。
原始命令与执行结果见 `youtube-intake-verification.md` 顶部所列本轮外部证据目录。

### 上一轮复修执行证据（e879879）

针对 `7Rzxh2K80EXE` 报告，新增真实离线子进程退出 101/1 及超时用例，经
`media.run` → 列表适配 → collector 验证正常边界无假告警、异常仍可辨。
仅运行本次修改的 notes、windowed discovery 与 browser 三个测试模块；
登记命令及实际结果记录于 `youtube-intake-verification.md` 顶部所列证据目录，不运行整套回归。

### 上一轮执行证据（83edddc）

哨兵 `lZ9UfrgEt6YJ` 登记的四个本轮修改/新增测试模块共 43 项通过、无跳过，其中
`test_youtube_windowed_discovery` 直接覆盖真实发现入口的日期及 archive 路径；无头浏览器 39 项断言通过。
原始命令、工作目录、日志、截图和首次测试参数错误记录见 `youtube-intake-verification.md` 顶部。
未执行生产同步，不能将隔离结果宣称为真实 48 个频道的降量实测。

### 历史执行证据（首次实现，已被本轮规则替代）

- ash 哨兵 `u5pxPGSRhJva`，真实退出码 `0`；`Ran 14 tests in 0.182s`，全部通过、无跳过。
- 覆盖上海时区跨日边界、状态不伪写、失败及后续成功不移动起点、已有成功记录不覆盖、精确日期包含下界、未知日期校准、多归属、改名/换址/删除、普通 force 与显式日期补抓。
- 当时隔离同步验证已知旧条目在落库前被过滤、重复同步使用同一起点；元数据校准后旧候选被判 `filtered` 是已撤回的旧预期，不是现行行为。原测试没有覆盖生产 flat 输出形态。
- 工作目录：`/Users/fjh/code/knowledge-base/.worktrees/MNW4en5L0iPP`。
- 完整原始日志：`/Users/fjh/code/harness/data/runs/MNW4en5L0iPP/regression/u5pxPGSRhJva.log`。

登记的原始命令：

```sh
/Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_initial_window -v
```

Python AST、采集器及窗口模块导入冒烟、单文件 700 行上限和 `git diff --check` 通过。未执行其他回归，也未重跑生产同步；不据此声称生产候选数量已经下降。

## 时间和完整性边界

| 环节 | 限制 | 超额处理 |
| --- | --- | --- |
| 普通轻量窗口列表 | 单频道 180 秒，整轮含间隔最多 900 秒 | 上限只作网络兜底；不逐条展开历史详情，超时明示部分完成 |
| 指定日期的详细发现 | 单频道 90 秒，整轮最多 1800 秒 | 继续保留原有部分输出和覆盖说明 |
| 增量元数据 | 整轮 120 秒，单条不超过 30 秒 | 干净且未尝试的候选保留预排，不因此阻止频道成功 |
| 精确快照刷新 | 未完成视频 6 小时后进入刷新队列 | 旧值仅作为缓存快照，不冒充实时播放量 |
| 视频深加工 | 继续使用 `youtube.run_seconds`，默认 900 秒 | 未完成内容留待下轮 |

这些是不同阶段的额度，不保证整轮或冷启动一定在几分钟内完成。
尤其首次运行或升级后旧候选没有缓存时间标记时，需要按预算逐轮补齐；
榜单不会因此大面积失去名次；缺失播放量仍显示「未知」，估算日期仍标未知。
这些条目只是预排，进入内容处理时仍要拿真实元数据重判准入，不能把估算日期固化到成品。

显式日期补抓继续使用详细列表，而不是拿估算列表证明视频位于指定日期内。
新增候选的真实日期到手后仍经过共同的窗口、订阅历史下界、未来时间和 AI 相关性判断。
真实播放量为零是有效数据；缺失、布尔、负数或非有限播放量不是零观看事实。

## 缓存与安全

- `entry.metadata_checked_at` 由存储层的 `discover`、`refresh_metadata`、`recalibrate` 统一推进。
  重复 flat 列表不会把旧快照续成新缓存，完成的视频也不会因为超过 6 小时重新消耗元数据预算。
- 元数据补齐不下载、不转录、不调用内容模型；新候选优先，并按列表位置跨频道轮转。
- 未被请求的视频不会因本轮停止或限流被登记成失败或推入失败退避。
- 单条视频的元数据错误不把正常频道推进连接失败退避；只有登录态/限流等出口级错误或
  该频道整批可用候选都探测失败才升级到频道。视频自己的失败原因、次数与冷却期照常保留。
- 未升级为频道连接失败并不代表频道同步成功。`last_error`、`failed`、`processing` 和
  本轮已尝试但未完成的视频阻止成功覆盖，不因缓存从新鲜变为过期而消失。
- 元数据准备返回分离的 `not_reached_ids` 与 `unfinished_ids`；只有前者中尚未进入
  内容处理、且没有真实错误的候选可豁免。失败冷却、取消、熔断不混作纯预算未轮到。
- 实际元数据请求后字段不完整或中断，存储层将 `metadata_pending` 置为 1；它跨轮保存，
  不因跳过 300 秒冷却而退回「未尝试」。精确元数据补齐或合格内容完成时才清除。
- 升级存量库只加列；只有 `deferred`、无准入闸代码且原因属于通用元数据说明的行
  才补种 `metadata_pending`。有重试时间时标记未完成，无重试时恢复为 `candidate`；
  直播、首映、时长、处理预算和未知原因不借共享的延后状态推断成元数据待办。
- 取消状态在元数据、处理与落账阶段连续保留；成功覆盖最后集中落在同一事务，取消时
  不清除任何频道的既有错误、次数或退避，也不把频道记入 `succeeded_ids`。
- 纯元数据待办只更新说明，不把健康候选标为 `deferred`。已有真实预算或熔断延后原因
  可跨轮保留。迁移、元数据挂起、补齐写回共用存储层 SQL 归属判据；补齐时只释放
  元数据专属延后，其他子系统的状态、原因、闸代码和冷却一律保留。
- 成功覆盖遇到维护锁或数据库异常，只记录本机保存告警、扣留本轮成功，并继续保存
  本轮历史和最终进度；不改变订阅错误账、不推退避、不加入频道失败重试名单。
  真正的频道失败仍保留在原失败名单。没有待记成功的来源时不争抢维护锁；旧提醒
  只在成功提交时清除。
- 发现落库、元数据写回、内容处理和笔记发布也共用 `faults.local_fault` 判据。
  本机维护锁、SQLite 忙/写盘资源故障只记 `local_error` / `local_ids` 与本轮告警；
  不增加视频 `attempts`、不写视频或频道失败退避，也不进入 `failed_ids`。受影响频道
  扣留成功，其他已完成频道仍可成功；批量元数据注释或预算状态写入的本机异常同样收口。
- 精确候选更新播放量不清空旧简介，不用只有日期的元数据覆盖已有精确发布时间；
  估算候选仍须取得真实时间和简介后才能完成校准，未知字段不产生永久过滤的假结论。
- 单条本地超时或事实字段缺失暂缓 5 分钟，避免每轮都由同一个坏候选占掉全部预算。
- 实际上游失败保留原始诊断；429 继续通过同一份请求级熔断账停止后续读取。
- 写回精确快照时核对旧快照未改变、仍有有效频道归属且没有永久移除记录。
- 已在元数据阶段获得的工具结果在同一轮处理时直接复用，不再重复 probe。
- 默认允许命中工具自身的短期磁盘缓存，仅显式强制同步才绕过该缓存。

## 可观测性

同步进度分别显示「频道列表」「元数据更新」「视频处理」。最终进度行单独展示三个阶段
各自耗时，以及缓存复用、新查、待更新和失败数量；`summary.message` 仍只承载告警与待办，
完全干净的同步保持为空，不把统计文字写进频道 `last_notice`。`feed_runs.summary` 额外保存：

- `timings.discovery_seconds`、`timings.metadata_seconds`、`timings.processing_seconds`。
- `metadata.reused`、`metadata.refreshed`、`metadata.pending`、`metadata.failed` 和 `metadata.ttl_seconds`。

`ranked` 包含待核实的预排候选，未知数据不会因为元数据预算不足而丢掉名次或被永久排除。
排序公式不变；未知条目保留 `approximate`，旧值以缓存快照说明，不能称为所有视频的实时播放量排名。

## 验证范围

初版新增 `tests/test_youtube_incremental.py` 并更新 `tests/test_youtube_fallback.py`。
第一轮修复修改前者并新增 `tests/test_youtube_incremental_review.py`，只执行这两个当轮
新写/改动的文件，由 ash regression 哨兵登记真实命令和结果；其余回归留给验收前置
命令，不以初版或历史结果冒充本次通过。

验证使用临时 SQLite、媒体与模型替身，不启动真实频道抓取，不读取真实 cookie，
不调用付费模型。接口核对覆盖候选排名响应中的未知标记、预排名次和频道成功状态。
不修改页面结构或样式；页面点检只访问本地隔离数据，不声称已在生产跑出新的分钟级测速结果。

## 初版验证记录（2026-10-30，修复前）

- ash 回归登记：`8HN_Zg6s0uMA`，真实退出码 `0`。
- 结果：`Ran 45 tests in 7.691s`，`OK`；仅包含本轮新增/修改的两个测试文件。
- 接口冒烟包含 FastAPI ASGI `GET /api/intake/youtube/ranking`：返回 `200`，
  待补元数据条目保持 `approximate=true`、`score_rank=0`、`state=deferred`。
- 静态验证：六个新增/改动 Python 文件的 Ruff、内存编译、700 行上限检查通过，
  `git diff --check` 通过。
- 没有执行全仓回归，没有抓真实 YouTube、读取生产 cookie、调用模型或操作浏览器。

登记命令的实际工作目录：
`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。

未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-incremental-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-incremental-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-incremental-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_incremental tests.test_youtube_fallback -v
```

完整日志：
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/8HN_Zg6s0uMA.log`。

日志 SHA-256：`2a88557982d63e0561e849ad149fd4a2daae81d3d55d094cba9587b2a6a45d00`。

## 第一轮修复验证结果（2026-10-30）

- ash 登记：`sKEv8vJelELz`，退出码 `0`；真实日志为 `Ran 34 tests in 1.957s`、`OK`。
- 仅执行本次修改的 `tests.test_youtube_incremental` 与新增的
  `tests.test_youtube_incremental_review`，没有补跑其他文件或全仓回归。
- 覆盖冷启动 48 频道/384 候选、已完成与排除条目不再探测、三个精确 entry 写入口、
  延后原因/保护状态名次保留、单条/整批/登录态失败、多归属隔离和清洁同步告警为空。
- 首次登记 `rac5QMKHSUiY` 因测试夹具缺少成品校验证据、排除原因码及浏览器字段失败；
  已补真实成品证明和必要字段后重跑，没有绕过成品隔离机制或放宽断言。

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。

未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-review1-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-review1-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-review1-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_incremental tests.test_youtube_incremental_review -v
```

日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/sKEv8vJelELz.log`。
SHA-256：`33627f44d581094f4f78473df7d3abb7c41e99b6dab2917e7898f6a8eca86a92`。

### 接口与页面冒烟

临时数据目录为 `/private/tmp/kb-J45OI3nIhEEf-review1-smoke`，独立运行本轮代码；
列频道、元数据、字幕与笔记模型均为本地替身，成品仍通过真实完整性证明。
关闭调度器后启动临时 uvicorn，仅监听 `127.0.0.1:48991`，不连接生产服务。

摆局特意把增量元数据预算设为零，深加工只许两条，验证最坏冷启动的可用性：

| 观察项 | 实际结果 |
| --- | --- |
| 启用频道 / 候选总数 | 48 / 384 |
| 预排名次数 / 页面展示前 100 条的无名次条数 | 384 / 0 |
| 成功频道 / 因真实处理预算留待后续的频道 | 46 / 2 |
| 频道错误 / 已完成并入库的中文成品 | 0 / 2 |
| `GET /api/intake/youtube/ranking` | HTTP 200，`total=384` |

真实打开「我的订阅 → YouTube → 订阅管理」并点击视图按钮，截图确认：候选保留名次，
未校准播放量仍写「未知」；普通频道显示「最近成功」，仅两个实际有处理待办的频道未记成功。
页面另含四个原始停用频道，卡片总数为 52，不能把它们算作本次 48 个启用频道的失败。

浏览器按通道顺序尝试：Chrome 扩展库存可用，但以具名会话「🔎 YouTube抓取修复」和
`visible:false` 新建后台标签时返回 `Capability is not available: visibility`。
为避免激活用户窗口或切换标签，不撤掉后台限制重试，改用独立 `chrome-headless-shell`，
临时 profile，始终无头；没有连接用户 Chrome 的调试端口或复用任何既有标签。
本地页面不需要登录态，降级不影响验证；没有点击同步、登录检查、更新工具或其他写操作。

证据归档目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-1`，含
`smoke-summary.json`、`ranking-api.json`、`page-evidence.json`、`ranking.png`、`sources.png`
及隔离服务/页面脚本。服务已停止，无头浏览器及临时 profile 已回收。

修复后的七个 Python 文件 Ruff、内存编译、700 行上限及 `git diff --check` 通过。
这些是离线替身与本地页面验证，不是生产频道抓取测速，也未调用真实 YouTube 或模型。

## 第二轮修复与验证（2026-10-30）

本轮修复的是同步完成判定，而非再次调整发现预算或评分公式：

- 同一条视频的未解决错误不随六小时快照过期而被豁免，订阅既有 `last_success`、
  `last_error`、`failures` 和 `retry_at` 原样保留；它不是一次新的频道连接失败。
- 明确分离未尝试的干净候选与已尝试不完整/失败/中断的候选；实际 `failed`、
  `last_error` 和 `processing` 的事实优先于元数据待办集合。
- 元数据、内容处理、逐频道提交以及最终成功事务中的取消都不产生新的频道成功；
  已通过证明的成品可照常保存，但错误账的清零统一延迟到未取消的成功事务。
- 真实延后原因连续三轮预排仍保留，不再因预排把状态改成 `candidate` 而次轮丢失。
- 48 频道/384 候选用例改接真实 `process`，在模型不可用时，入选内容的频道确实
  保持未完成；未入选的干净候选不拖累其他频道，不再用空处理桩宣称 48/48 完成。

### 登记的定点测试

ash monitor：`FaCFO_OJJIFV`，退出码 `0`。已读取完整真实日志：
`Ran 25 tests in 2.391s`，`OK`。
只运行本轮修改的 `tests.test_youtube_incremental_review` 与新增的
`tests.test_youtube_incremental_completion`；其余回归留给验收前置命令。

新增的 13 项用例覆盖结果集合互斥与完整性、旧失败冷却、元数据不完整、局部超时、
请求被拒、取消、缓存鲜/过期不变量，以及真实处理/成品校验/发布和两种收尾取消。
已有审查用例增加连续三轮原因保留，并校正单条视频失败的来源完成断言。

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。

未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-review2-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-review2-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-review2-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_incremental_review tests.test_youtube_incremental_completion -v
```

日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/FaCFO_OJJIFV.log`。
SHA-256：`8b23d5161025fbeb8136de6579a586fe8881a5ae4b1ab541df63c98e852b473e`。
日志含现有 Starlette/httpx 弃用提示，无失败；未为此改动无关依赖。

### 隔离接口冒烟与检查

使用全新临时数据目录与 FastAPI `TestClient`，不启动生产服务或调度器。
摆入一条快照超过六小时、视频仍在失败冷却的候选，以真实 `collect` / `process`
走完后读取来源概览和候选榜，媒体探测调用数为零。

| 观察项 | 实际结果 |
| --- | --- |
| `GET /api/intake/youtube` / `GET /api/intake/youtube/ranking` | 均为 HTTP 200 |
| 原有频道错误、3 次失败、旧成功时间 | 全部保留 |
| 视频状态 / 本轮新成功频道数 / 未完成频道数 | `failed` / 0 / 1 |
| 真实 YouTube、媒体或模型请求 | 0 |

证据：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-2/`，包含
`api-smoke.py`、`api-smoke.log`、`summary.json`、`overview.json`、`ranking.json`
与临时数据目录记录 `data-root.txt`。
本轮是后端状态契约验证，没有操作浏览器，因此没有浏览器通道降级；
没有访问用户登录态、cookie 或生产数据，也没有做真实频道抓取测速。

三个实现与两个测试文件通过 Ruff、内存编译和 700 行检查，`git diff --check` 通过。
Ruff 实际使用 `/Users/fjh/code/_deps/liveportrait/.venv/bin/ruff`；项目自己的虚拟环境
没有该命令并不等于跳过检查。所有新增/修改测试均已登记跑绿，未执行全套回归。

## 追加审查修复与验证（2026-10-30）

对应 `SqxprDLd0bEx/round-1` 的三条必须修复项，当轮只调整 YouTube 链路。
此节保留当时验证记录，其中过宽的旧库补种与收尾错误归类由后续二轮追加修复替代：

- **跨轮未完成事实**：存储层新增只增不减的 `metadata_pending` 列。实际请求后字段
  不完整或局部中断的候选，即便下一轮被五分钟冷却跳过，仍保留未完成身份，不再把
  频道判成新成功并清掉旧错误、失败次数和退避。成功补齐元数据或完成合格内容才清标。
  旧库升级对有重试时间的延后行保守保留未完成事实，无重试的纯元数据注释恢复候选。
- **收尾异常**：成功事务的取锁及数据库异常统一归入来源失败，随后继续保存同步历史、
  重试目标与最终进度。空成功列表不取维护锁；成功 ID 只在事务提交后追加。逐频道
  内容落库不再提前清提醒，停止或最终成功写入失败时保住既有提醒。
- **状态与解释分开**：数量/时间预算没有轮到的健康候选仍是 `candidate`，元数据待补
  只是说明；真实处理延后、错误、处理中以及持久的未完成事实不被覆盖。

### 登记的定点测试

本轮只修改并运行 `tests/test_youtube_incremental_completion.py`，没有补跑其他测试。
ash monitor `tOLYlrj3HD1v`，退出码 `0`；已读取完整真实日志：
`Ran 20 tests in 0.690s`，`OK`。覆盖不完整/中断请求的冷却跨轮、过期后重试、
成功清标、旧库迁移幂等、真实维护锁异常、数据库异常、空列表不取锁，以及真实处理后
三条成品与四十五条候选的状态分布；原有取消与错误账保护用例一并通过。

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。

未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-review3-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-review3-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-review3-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_incremental_completion -v
```

日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/tOLYlrj3HD1v.log`。
SHA-256：`195189ea833107d0c677503ac1a1c1fd1ef67e33787f12d62ae8171a68b0cc70`。
登记前保存的四个实现文件与本轮测试文件指纹在
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-3/tested-code.sha256`；
登记结果返回后及提交前逐项复核一致，避免只引用旧树上的通过记录。
日志含现有 Starlette/httpx 弃用提示，没有因此调整无关依赖。

### 隔离接口与真实页面冒烟

一次性临时数据/笔记/视频目录，禁用调度器；频道列表、媒体元数据、字幕及笔记生成
使用本地替身，真实 `collect`、`process`、成品校验及发布逻辑照常执行。
摆局为六频道各八候选，元数据预处理预算为零、排名及内容处理数量上限为三。
服务端拒绝非本机 socket 连接，浏览器请求也仅放行本次 loopback 服务。

| 观察项 | 实际结果 |
| --- | --- |
| `GET /api/intake/youtube` / `GET /api/intake/youtube/ranking` | 均为 HTTP 200 |
| 候选总数 / 有名次条数 | 48 / 48 |
| 接口状态分布 | `ready=3`、`candidate=45`，无 `deferred` |
| 页面点击「订阅管理」后的真实文案 | 「中文笔记已完成」3、「候选待处理」45、「留待下轮」0 |
| 页面频道卡片 / 无成功时间卡片 | 6 / 0 |
| 浏览器未处理运行时异常 | 0 |

已目视检查 `ranking.png` 与 `sources.png`：第 4 名起的未知播放量候选显示
「候选待处理：发布时间与播放量尚未核实，暂按候选信息预排，处理前再次核实」，
不再同时声称「留待下轮」。全部六张频道卡片显示本轮成功时间。

本轮重新尝试 Chrome 扩展具名会话「🔎 YouTube增量复核」，以 `visible:false`
新建后台标签时返回 `Capability is not available: visibility`，没有创建验证标签。
没有撤掉后台限制或接管用户标签，随后才降到独立 `chrome-headless-shell`，使用全新
临时 profile，始终无头。页面为本机公开页面，不需要登录态；没有连接用户 Chrome
调试端口、激活窗口、读取 cookie、点击同步或执行其他生产写操作。

证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-3/`，包含
`serve.py`、`smoke.py`、`page.mjs`、`service.log`、`smoke-summary.json`、
`overview-api.json`、`ranking-api.json`、`page.log`、`page-evidence.json`、
`ranking.png`、`sources.png`、`browser-instance.json`、`cleanup.json`。
冒烟脚本在仓库外，不属于新增回归套件。清理记录确认服务已退出、无头调试端口关闭、
临时 profile 和一次性数据目录已删除。

四个实现与一个测试文件通过 Ruff、内存编译、700 行检查，`git diff --check` 通过。
未运行整套或未改动测试，审查列出的其他回归面仍由验收前置命令覆盖；没有真实
YouTube/模型请求或生产数据写入。这些结果验证状态正确性，不代表生产抓取耗时实测。

## 二轮追加审查修复与验证（2026-10-31）

对应 `SqxprDLd0bEx/round-2` 的三条必须修复项，本轮没有调整其他频道或扩大抓取范围：

- **本机故障不罚频道**：最终成功事务的维护锁或数据库异常写入本轮 `local_error`
  和告警，扣留本轮成功；仍保存历史、刷新最终进度，但不改订阅的旧错误、失败次数、
  冷却和提醒，也不新增 `failed_ids`。真正的频道失败仍走原通道，二者不混为一类。
- **延后归属只有一份判据**：存储层 `metadata_deferral()` 提供参数化 SQL，要求
  `state='deferred'`、`gate_code=''` 且原因恰为两类元数据说明。旧库补种、元数据
  挂起和补齐写回都按写入时的当前行判断，不凭共享状态或调用前读到的行猜测归属。
- **只释放自己的延后**：旧库迁移不把直播、首映、时长、处理预算或未知原因标成
  元数据未完成；补齐时可清除独立的 `metadata_pending`，但其他子系统的状态、原因、
  闸代码和冷却保留。元数据挂起同样不能把别人的十五分钟冷却缩短成五分钟。

### 登记的定点测试

本轮只修改并运行 `tests/test_youtube_incremental_completion.py`，未运行其他测试文件。
ash monitor `TgY5blBlTTi_`，退出码 `0`；已读取真实完整日志：
`Ran 25 tests in 0.869s`，`OK`。

覆盖迁移原因/闸代码组合及幂等、两类元数据延后正常释放、并发更新为其他延后后的
SQL 写入保护、其他子系统冷却保留、锁故障不罚频道、真实成功事务中途数据库异常
全量回滚，以及本机故障与真正频道失败并存时的重试名单隔离；原有取消与跨轮错误账
保护用例仍在本轮修改文件内，一并通过。

实际工作目录：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。

未过滤的原始命令：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-review4-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-review4-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-review4-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_incremental_completion -v
```

日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/TgY5blBlTTi_.log`。
SHA-256：`c2617a04e2dcf01079f6675a921d9e5a569947da102be0a32bffe4f90cd74030`。
登记前的两个实现文件与本轮测试文件指纹保存在
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-4/tested-code.sha256`，
返回结果后及提交前逐项核对一致。日志仅有既有 Starlette/httpx 弃用提示，未改动无关依赖。

### 隔离后端接口冒烟

全新临时数据/笔记/视频目录，使用真实 FastAPI `TestClient`，未启动生产服务或调度器。
媒体、列表与模型使用本地替身，真实 `collect`、`process`、成品校验、发布和历史保存
照常执行；只在最终成功事务入口注入本机 `LockBusy`。另在同一临时库验证旧版列升级、
直播行误带元数据标记后的安全补齐，以及真正元数据延后的正常解除。

| 观察项 | 实际结果 |
| --- | --- |
| `GET /api/intake/youtube` / `GET /api/intake/youtube/ranking` | 均为 HTTP 200 |
| 真实处理并发布的成品 | 2 条 |
| 两个已正常抓取频道的旧成功时间、错误、次数、冷却和提醒 | 全部原样保留 |
| 本轮新增成功 / 频道失败 / 失败重试目标 | 均为 0 |
| 同步历史 / 最终进度 | 历史已保存；`error` 明示本机保存失败、下一轮重算 |
| 迁移后的直播行 / 真正元数据延后行 | `metadata_pending=0` / `metadata_pending=1` |
| 补齐元数据后直播状态、原因、闸代码及原冷却 | 全部保留，仅清元数据标记 |
| 真正元数据延后补齐后 | 恢复 `candidate`，元数据原因、冷却与标记清除 |
| 候选榜状态分布 | `ready=2`、`deferred=1`（真实直播闸）、`candidate=1` |

证据目录：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-4/`，包含
`api-smoke.py`、`api-smoke.log`、`api-evidence.json`、`migration.json`、
`metadata-writeback.json`、`summary.json`、`overview-api.json`、`ranking-api.json`、
`cleanup.json` 和数据目录记录。冒烟脚本在仓库外，不作为另一套回归运行。
临时数据已清理；本轮没有浏览器操作，因此没有浏览器通道降级，也没有声称完成页面点检。

两个实现与一个测试文件通过 Ruff、内存编译和 700 行检查，`git diff --check` 通过。
其余回归仍留给验收前置命令，没有为了填审查覆盖留白去运行未修改的测试。
没有读取生产库或 cookie，没有真实 YouTube/模型请求；未做生产抓取耗时实测。

## 新审查修复与验证（2026-10-31）

对应 `Os8A8bYYXGcd/round-1` 的两条必须修意见：

1. 统一 YouTube 发现落库、增量元数据、内容处理和笔记发布的本机故障判据。
   `LockBusy`、SQLite 结构化基本/扩展存储错误码、数据库忙和文件系统资源故障
   只记本轮本机告警及未完成来源；不转成视频失败、频道连接失败或新增退避。
   批量元数据注释、排名/预算状态写回和最终成功提交也使用相同收口。
   真正网络/上游失败仍走原失败账，受影响来源扣留成功，其他健康来源不受牵连。
2. 精确候选刷新播放量时保留已知简介和精确发布时间。新元数据没有简介、给出
   `None`/空白或只有 `upload_date`，不再使旧事实丢失或降为当日零点；估算条目
   仍须按真实元数据校准，未放宽资格判断或成品完整性要求。

### 登记的定点测试

本轮仅新增 `tests/test_youtube_incremental_faults.py`，未运行未修改的测试文件。
使用 `start_monitor(regression=true)` 登记的原始命令为：

```sh
env KB_DATA_DIR=/private/tmp/kb-J45OI3nIhEEf-review5-validation/data KB_VAULT_ROOT=/private/tmp/kb-J45OI3nIhEEf-review5-validation/vault KB_VIDEOS_ROOT=/private/tmp/kb-J45OI3nIhEEf-review5-validation/videos HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python -m unittest tests.test_youtube_incremental_faults -v
```

实际 cwd：`/Users/fjh/code/knowledge-base/.worktrees/J45OI3nIhEEf`。
哨兵 `4LRadQ_uzXIR` 退出码 **0**；已读取原始日志，**19 项 / 0.285 秒 / OK**。
日志：`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/regression/4LRadQ_uzXIR.log`。
SHA-256：`da792283a724947ba92be92fb578bc2e7443db7bdb6ce425a09ebb9edfd2ffba`。
三个实现文件与新增测试文件的登记前指纹保存在
`/Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-5/tested-code.sha256`；
收到结果后、提交前及提交后的代码内容逐项比对一致。
日志只有既有 Starlette/httpx 弃用提示，未改动无关依赖。

### 隔离后端接口冒烟

使用全新临时数据、笔记和视频目录，真实执行 `collect`、`process`、成品校验、
发布和同步历史保存，通过 FastAPI `TestClient` 读取接口，不启动生产服务或调度器。
频道列表、媒体与模型使用本地替身；同时禁止 socket 连接和 cookie 读取。
两个频道均有已过期的精确候选：一个仅在元数据写回时注入 `LockBusy`；另一个标题
无 AI 关键词、旧简介包含 LLM/AI 语义，本次元数据无简介且仅有 `upload_date`。

| 观察项 | 实际结果 |
| --- | --- |
| `GET /api/intake/youtube` / `GET /api/intake/youtube/ranking` | 均为 HTTP 200 |
| 故障频道的旧成功时间、错误、失败次数与冷却 | 全部原样保留 |
| 故障视频失败次数、错误与冷却 | `0` / 空 / `0`，仍为 `candidate` |
| 本轮频道失败 / 失败重试目标 / 元数据失败数 | 均为 0 |
| 健康频道 | 正常记录成功、清除旧失败账；真实处理并发布 1 条完整成品 |
| 健康视频的旧简介与精确发布时间 | 全部保留；播放量更新为 8800，未被过滤掉榜 |
| 探测与处理 | 两条各探测一次，仅健康视频读取字幕并处理 |
| 候选榜状态分布 | `ready=1`、`candidate=1` |
| 同步历史 / 最终进度 | 历史保存且接口一致；`error` 明示本机故障、下一轮重算 |

冒烟命令（cwd 同上）：

```sh
PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 /Users/fjh/code/knowledge-base/.venv/bin/python /Users/fjh/code/harness/data/runs/J45OI3nIhEEf/review-fix-5/api-smoke.py
```

脚本、`api-smoke.log`、`api-evidence.json`、`summary.json`、`overview-api.json`、
`ranking-api.json`、前后频道账快照和 `cleanup.json` 均在上述 `review-fix-5/` 证据目录。
冒烟日志 SHA-256：`bad9c41b4eb5f3c57cafb957e7c5555fce8d962f781709453f08c9ee78666f65`。
脚本在仓库外，是本轮隔离接口冒烟，不运行其他回归文件。临时目录已清理，未启动
后台服务或浏览器；没有浏览器通道降级，也不声称完成页面点检。

三个实现文件与新增测试文件通过 Ruff、内存编译和 700 行检查，`git diff --check` 通过。
未读取生产库或 cookie，未请求真实 YouTube 或模型；未实测生产抓取耗时，也未覆盖
真实磁盘耗尽或进程并发锁争用。其余未修改回归仍由验收前置命令统一执行。
