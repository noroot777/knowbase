# 操作入口

> 从 CLAUDE.md 外移的决策记录。CLAUDE.md 只留硬约束和一行指针,
> 这里是完整的事故形态、实测证据、回归清单和反向验证红数。原文逐字搬运。

- **`./kb.sh` 是唯一的操作入口**。不带参数打印全部命令。日常两条:
  `./kb.sh sync`(采集→导出→索引→打标,后台跑)、`./kb.sh status`
  (服务/同步进度/公众号来源一屏看全)。**没有「同步前的准备」这一步了**——
  原来的 `prep`(起 wxdown + 切路由 + 盯凭证)连同整条凭证通道一起搬去了
  `wechat-oa` 分支,主干采集只读本机绿泡泡数据库,开面板就能同步。
- **`restart` 只重启进程,不重建任何产物,尤其不重建三张图谱页。** 图谱是 Vault 里
  的单文件 HTML,kb-web 只是把文件原样发出去,所以改了 `catgraph.css/js` 重启看不出
  任何变化。重建走 `./kb.sh graph`(`--no-llm`,零模型调用,实测约 6 秒,其中 5.5 秒
  是读那棵 5530 组的条目树);跑同步时 wiki 阶段本来就会带上。**没把它塞进 restart**:
  restart 现在是纯进程操作,失败了顶多服务没起来、磁盘一个字节不动,塞进去之后每次
  重启都在原子替换三张正式页面——`build_catgraph` 里那一整套两阶段提交 / 发布 CAS /
  质量验收,存在的理由就是这个动作有风险。改成 `web_start` 之后比一次 mtime,旧了就
  打一行提示(`graph_stale`),提示而不代劳。
- **`./kb.sh graph` 用仓库内的 `data/` 时只许在主检出跑**,`cmd_graph` 第一件事就是用
  `git rev-parse --git-common-dir` 把 worktree 挡掉。`data/`(库 + 归类缓存)在
  .gitignore 里、只有主检出有;worktree 里要么没有、要么是跑测试留下的空壳,而
  **`build_graph_html.py` 自己不做质量验收**——2026-09-29 实测:从 worktree 跑,它拿
  135 KB 的测试库建出一张 0 节点的 `graph.html`,原子替换掉 Vault 里那张好的
  (`build_catgraph` 那套闸门只管它自己那两张)。
- **`cmd_graph` 的前置检查按 `KB_DATA_DIR` 解析数据目录**(没设才回退仓库 `data/`),
  跟 `kb/config.py` 与 `scripts/wiki/taxonomy.py` 一个口径。两个构建器本来就认这个
  环境变量,前置检查只认 `$ROOT/data` 的话,外置数据目录下数据明明是全的、却在进
  构建器之前就误报「缺归类缓存」,三张图谱一张也重建不了(2026-09-29 审查发现)。
  数据目录指到仓库外时上面那道 worktree 闸门不再适用(数据是显式挂的、跟检出无关),
  空目录由归类缓存那道检查挡住。
- **「是不是仓库内的 `data/`」要 `realpath` 之后再比,不能比原始字符串。**
  `pathlib.Path` 去得掉 `./`,**去不掉 `..`,也不消解符号链接** —— 于是
  `$ROOT/data/../data`、`data/../data`、一条指回 `data/` 的软链接都是同一个目录的
  另一种写法,却跟 `$ROOT/data` 不相等,worktree 那道闸门换个写法就绕过去了。
  实测后果和闸门本来要挡的事故一模一样:worktree 里这么写,话题图谱拿 135 KB 的
  测试库发布出一张 0 节点页面,还以退出码 0 报「好了」(2026-09-29 复审第 2 轮发现)。
  回归在 `tests/test_kb_graph_guard.py`:真建一个 git 仓库 + linked worktree,
  把 kb.sh 原样拷进去跑,四种别名写法各一条,外加外置目录放行、缺缓存报外置路径、
  主检出不受影响。构建器用 PATH 上的 `uv` 桩挡住,判定逻辑一行都不打桩。
- **服务一律用 `daemonize()` 起,不用 `nohup ... &`**。后者起出来的进程跟脚本同一个
  进程组,非交互 shell 会给它把 SIGINT 设成 SIG_IGN,而这个 SIG_IGN 被子进程继承——
  于是 stop 时的 `kill -INT` 也被忽略,只能降级 SIGTERM——而 kb-web 进程里跑着
  管道,SIGTERM 会把正在写 kb.db 的那一步砍在半路。daemonize 借 python 做
  fork+setsid(macOS 没有 setsid 命令)并把 SIGINT 恢复默认:自成会话不受终端
  影响,同时 stop 能优雅收尾。
- **凭证通道整条都不在主干上了。** 「每个号一份文章凭证、靠 wxdown 的 mitm 从 Mac
  绿泡泡里截下来」那一套(四态判定、新鲜度、`cli creds`、`cli login` 扫码、Clash 路由
  切换)连同它依赖的上游代码一起留在 `wechat-oa` 分支,**两个分支始终并行、不打算
  合并**。主干采集只有一条路:读本机绿泡泡数据库(`kb/collectors/wechat_local.py`
  + `collector/src/crawler/local_wechat.py`),没有逐号授权、没有要盯的凭证面板,
  正文照旧按文章链接抓。碰到提凭证的历史说法(注释、提交信息、`wechat-oa` 分支上
  的文档),那说的是另一条分支。
- **两个分支共用同一份 `collector/data`,所以主干不许在那个库上做破坏性的 schema
  变更。** 分叉的是代码,不是库:两边 setup.sh 默认链的是同一个目录,而
  `wechat-oa` 是**冻结**的,改不动——它照旧往 `articles.cover_path` 里写,核对
  改名照旧读写 `accounts.display_checked_at`。v15/v16 当时按"主干零个读取方"把这
  两列 DROP 了,实测后果是:按主干跑一次(自动迁到 v16)、再切回保留分支采集,
  第一篇新文章就 `OperationalError: table articles has no column named cover_path`
  ——那条分支从此写不进任何文章,「保留」名存实亡(2026-10-01 审查复现,用户那份
  832M 的库当时已经停在这个状态)。旧分支自己救不回来:它只认到 v13,看到
  `MAX(version)=16` 什么都不跑,它 schema.sql 里的 `CREATE TABLE IF NOT EXISTS`
  对已存在的表也是空操作。所以 v15/v16 退役成空操作、**v17 把两列补回来**(存量库
  只有这一条出路),**要停用一列就停止读写它、别 DROP**。两个空列几十字节,换的是
  另一条分支还能跑。
  **表和列一视同仁,口径收成一句:主干的迁移只增不减。** 这里原来给 v14 删
  `article_fts` 开了个口子,理由是"整张表旧分支的 `CREATE VIRTUAL TABLE IF NOT
  EXISTS` 会重建它,写入不会失败"——**那句话只对重启成立**。`ensure_database_ready`
  的缓存是进程内的:旧分支正在同步(等下一篇、等网络)、主干在另一个进程里第一次
  把这个库迁过 v14,它那个连接早就初始化完了,不会因为别人删了张表再跑一遍 schema,
  于是采下一篇时 `OperationalError: no such table: article_fts`,那一批的后续文章
  连带记成账号同步失败(2026-10-01 第 2 轮审查复现,走的是旧分支真实的
  `api._ingest_crawl_article` → `fts.upsert_article_fts`)。所以 **v14 也退役成空
  操作、v18 把那张表建回来**(空表,主干一行 fts 代码都不调;定义照抄冻结分支的
  schema.sql,列名和 tokenize 一字不差,因为它自己那句 `IF NOT EXISTS` 建错了也
  改不回来)。判据是「另一条分支**不重启**会不会当场坏」,不是「它重启之后能不能
  自愈」。`ADD COLUMN` 对在途连接是安全的(SQLite 会重新编译语句),删东西不是。
  **反过来不要去"隔离数据"**:主干那份
  8850 篇文章就在这个库里,换个默认目录等于主干从一个空库开始。
  回归在 `collector/tests/test_shared_db_compat.py`(11 条):钉的不是"列在不在",
  而是**把那条冻结分支的 SQL 原样跑一遍**(含它那两句 FTS 读写),外加两道闸门:
  ①一个**升级前就建好、全程不重开**的连接,在另一个连接迁完之后照样写得进去;
  ②**逐版**比表和列的集合,只增不减——只比首尾的话"v14 删掉、v18 又建回来"看起来
  毫无变化,那个口子就又开了。
- **`setup.sh` 要继续认旧变量名 `GZH_DATA_HOME` / `GZH_VENV_HOME`。** 脚本改名成
  `KB_COLLECTOR_DATA_PATH` / `KB_COLLECTOR_VENV_PATH` 之后只读新名字,于是用旧名字
  **显式隔离过**数据目录的人升级后被静默换到别处(默认位置,或者存量回退那个原项目
  目录),脚本照旧返回 0 报成功——用户看到的订阅和文章变成另一批、或者看起来全没了
  (2026-10-01 审查复现)。换掉的是「打开哪个库」,不是一个链接的写法。口径在
  `pick_path`:新名字优先、两个都设了打一行说明、只设旧的就沿用并提一句新名字;
  **旧变量算显式选择**,不该被"原位置还有东西"那条存量兜底顶掉。
