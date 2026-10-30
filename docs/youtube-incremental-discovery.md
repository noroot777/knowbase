# YouTube 增量发现与元数据缓存

## 本轮改变

普通同步不再为每个频道展开最近 10 条视频的详细信息。现在依次执行：

1. 全部待查频道串行读取带登录态的 flat 列表，每频道最多 20 条；频道间仍停 1 秒。
2. 列表全部结束后，已完成/已排除视频不再探测；其他视频复用 6 小时内快照，只补新增或过期详情。
3. 所有候选继续使用原有资格判断和评分公式预排；未核实与旧快照保留明确说明，不伪装成实时事实。
4. 再按原有视频数量、时长及处理预算取字幕、ASR、翻译和生成内容笔记。

所有远端请求仍由 yt-dlp 使用已配置浏览器的登录态，不改用匿名 RSS，不增加频道并发。
列表失败直接保留可重试错误，不再隐式启动另一轮昂贵的逐条展开。

## 时间和完整性边界

| 环节 | 限制 | 超额处理 |
| --- | --- | --- |
| 普通频道列表 | 单频道 30 秒，整轮最多 900 秒 | 未轮到的频道下轮继续 |
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
- 取消状态在元数据、处理与落账阶段连续保留；成功覆盖最后集中落在同一事务，取消时
  不清除任何频道的既有错误、次数或退避，也不把频道记入 `succeeded_ids`。
- 已有预算或熔断延后原因跨轮保留；仅有预排名次不把 `deferred` 改成已恢复的 `candidate`。
  元数据补齐或实际内容处理推进后，才按真实结果更新该状态与原因。
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
