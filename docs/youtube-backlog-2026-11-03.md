# YouTube 历史积压一次性处理（5aRnd8NVGVZj）

## 冻结范围

- 开始：2026-11-03 15:41:08，Asia/Shanghai。
- 固定窗口：2026-10-29 00:00:00+08:00 至 2026-11-03 15:41:08+08:00，包含边界。
- 生产代码基线 `804829e`，已核实包含 `1975648`、`c7d1c4b`、`ccb4851`。
- 原始 `feed_runs.id=219` 未保存 699 个视频 ID，不能证明历史逐 ID 完全一致。
  本任务采用可复现重建：当前有效归属、未永久排除、`score_rank>0`、非 `ready`，
  按该轮结束时间 `2026-11-03T06:11:31+00:00` 判断元数据新鲜度不合格。
  恰好得到 **699 条（595 条 approximate）**：candidate 671、deferred 23、failed 5。
  开工时已经因时间推移额外过期的 8 条不纳入，其他 filtered、ready 和其他来源不处理。

## 操作与恢复依据

生产审计目录：
`/Users/fjh/code/knowledge-base/data/audits/youtube-backlog-5aRnd8NVGVZj/`。

- `manifest.json`：冻结 ID、SHA-256、完整候选与归属快照、原始同步摘要、既有正式文章快照。
- `feeds.before.sqlite3`、`index.before.sqlite3`：维护锁内通过 SQLite backup 生成的初始快照。
- `dates.json`：每条真实发布日期、精度、证据来源和错误；每完成一条原子保存。
- `feeds.pre-clean.sqlite3`：第一次清理前的额外生产快照。
- `cleanup.precommit.json`、`cleanup.json`：事务预提交和已提交删除审计，可在中断后核对重入。
- `processing.json`、`processing-batches.jsonl`：逐条认证结果、失败/续跑状态和原有预算下的批次。
- `index-verification.json`、`reconciliation.json`：定向索引、检索证据及不重叠结果对账。

可靠的非估算发现日期和缓存元数据可复用：日期不随播放量 TTL 失效。
approximate 不作日期证据，必须拿真实视频元数据。
只有 upload_date 而日期区间跨越本地窗口边界时，保留待核，不推测具体时刻。
元数据请求继续使用项目既有 yt-dlp、正常配置的登录态、单条 30 秒和上游限流熔断。
不打开浏览器，不导出 cookie，不重新发现 48 个频道。

清理在维护锁和写事务中完成，只删除冻结集合中已证实五天外且不含正式资产的候选、
归属及遗留候选表对应行；保留字幕/译文/笔记缓存与原有正式文章。
真实日期写入现有 `youtube_discovery_history`，不是用户永久排除名单；显式范围补抓仍可发现。
恢复时必须先停止本任务并取得维护锁，从备份按 `cleanup.json` 的删除 ID 定向恢复
`youtube_videos`、`youtube_video_sources`、原有 `youtube_candidates`；必要时恢复这些 ID 的
原发现历史。不得把整个旧备份直接覆盖仍在接受其他写入的生产库。

## 执行入口

下面命令在任务 worktree 执行；长操作用 ash 普通 monitor 托管，不是回归测试。

```sh
export KB_DATA_DIR=/Users/fjh/code/knowledge-base/data
export PYTHONPATH="$PWD"
PY=/Users/fjh/code/knowledge-base/.venv/bin/python
$PY scripts/youtube_backlog_20261103.py freeze
$PY scripts/youtube_backlog_20261103.py dates --budget 3300
$PY scripts/youtube_backlog_20261103.py clean
$PY scripts/youtube_backlog_process_20261103.py process --budget 3000 --attempts 2
$PY scripts/youtube_backlog_process_20261103.py deliver
$PY scripts/youtube_backlog_process_20261103.py index
$PY scripts/youtube_backlog_process_20261103.py reconcile
```

dates 可重复调用直到覆盖冻结清单；未知日期保留，不记作删除或入库。
核实失败原因后可用 `dates --retry-unknown` 重试未知项，每次先前记录保存在
`previous_attempts`；剩余总预算不足一次完整单条核验时先退出，不额外制造超时未知项。
process 每次只调度已核实窗口内的 ID，内存中的局部调度不改全局榜单或设置。
沿用完整字幕/音频、中文翻译笔记、认证、正式发布和原导出链；不伪造日期或认证。
每批仍受原 max_videos、max_minutes、max_video_minutes、run_seconds 限制。
对现有正式资产去重。超长、直播、Shorts、上游失败或认证失败原样记为未解决，
不放宽门槛，不报告全部入库。索引只 upsert 目标文章、补目标 chunk 的向量，不全库重建/清理。

最终执行结果将由本任务后续收尾记录；当前文档不表示批处理已经完成。

## 批次检查点

任务分支已更新至生产 `main` 基线 `e62cf6d`，包含 `87cc380` 的完整字幕直接翻译、
`ad37c63` 的并行字幕/标题翻译及 `75b539b` 的标题失败隔离。
已核实实际 imports 来自任务 worktree，`notes.build` 走 `build_translation`，
不再要求或调用 `youtube_summary` 完成内容成稿。后续内容处理按此实现，不恢复旧摘要管线。

2026-11-03 18:18（上海时间）收到普通哨兵 `tS1ipQEvNMtM` 的真实执行结果：
退出码 0，共持久化 457/699 条日期，old 392、recent 64、unknown 1。
唯一 unknown `b5B5I8vAZS0` 是批次总时限用尽，将在下一批显式重试；
这不是已经证实的上游不可用。此时尚未执行内容入库，生产正式 YouTube 仍为原有 7 篇。

本检查点已真实删除核实五天外的 **392 条**，不是隐藏：逐 ID 验证
`youtube_videos`、`youtube_video_sources`、`youtube_candidates` 均无对应行，
原归属的发现历史保留可靠日期。清理前 `feeds.pre-clean.sqlite3` 已生成。
原 7 篇正式文章的标题、正文、哈希及路径未改，清理前后各来源正式资产键/哈希/路径一致。
阶段对账为 **699 = 392 已删除 + 0 已入库 + 307 尚未完成**；
其中 242 条尚未核验、1 条超时待重试、64 条已核实窗口内待完整处理。

2026-11-03 19:03（上海时间）已接收普通哨兵 `1lQAE23KL8II`：
`DATES_AND_CLEAN_EXIT=0`，完整核验 **699/699，old 613、recent 86、unknown 0**。
清理累计 **613 条**，再次逐 ID 验证候选/归属/遗留候选均已消失，原 7 篇正式资产未变。
固定窗口和冻结 ID SHA-256 未变。下一阶段只处理这 86 条近期视频，不新增发现。
原有准入仍可能阻止超长等内容，此类原因逐条保留，不冒充已入库。

内容批处理把数量/累计时长/总时限造成的调度延后与真正处理失败分开计次，
未轮到的内容不消耗失败重试次数；上游限流原因在本轮熔断作用域退出前保留。
若原有管线显式发布翻译降级成品，审计会独立标注，中文完整处理仍列入未解决，
不把完整性认证通过误报为全部中文译文已完成。

### 内容首轮实际结果

2026-11-03 19:57（上海时间）接收普通哨兵 `WYAlS15xUiTP`，`PROCESS_EXIT=0`。
首轮完成 20 个批次，处理记录覆盖 60/86 条；逐条核实 **44 篇新增正式文章**
均通过 `products.valid`、正式发布且已完整导出，Markdown 文件实际存在。
正式 YouTube 总数为 **51 = 原有 7 + 新增 44**；原 7 篇标题、正文、哈希及路径未改。
新增来源为自动字幕 39 篇、作者字幕 5 篇，无翻译降级成品。
本检查点尚未执行目标定向索引和检索验证，不把导出冒充检索已完成。

窗口内未完成 **42 = 26 尚未轮到 + 6 单条时长门槛 + 9 批次预算延后 + 1 翻译覆盖失败**。
六条单视频超过原 45 分钟预算，保留候选和明确原因；九条预算延后不消耗失败次数，
下一轮继续；`x6jBcI5FtXc` 未通过中文译文覆盖校验，保留原始字幕及有效译文缓存待重试。
未扩大准入或放松覆盖认证。首轮逐项记录已保留为 `processing-pass-1.json`、
`processing-status-pass-1.json`，原始日志 `process-pass-1.log`，继续处理剩余目标。
