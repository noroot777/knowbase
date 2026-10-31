# collector 采集器

> **这一份的第一节就是这一块的硬约束(不许怎样)—— 动这块代码之前先读它**,后面是完整的事故形态、
> 实测证据、回归清单和反向验证红数。CLAUDE.md 只留跨块的元规则和一张索引。
> 硬约束和下面的展开**说的是同一条口径,要改规则两边一起改**。

## 硬约束(原 CLAUDE.md「collector 采集器」)

- **`materialize()` 要交回这一轮新写了哪几篇(成果回执),而且每一篇**带着身份**;交不出就返回
  `None`。** 口径和事故形态在 `docs/decisions/daily.md` 的「归属只能由生产者给」和「『这一次抓到的』和
  『补导进来的』分不开」两节 —— 动导出那条链(导出脚本逐篇打 `  [item] {id, round, name}`、`cmd_export`
  连同输出目录收成事件、`_receipt` 换算成 vault 相对路径、超时降级那一支返回 `None`)之前先读它们。
  上层**没有别的办法知道"这些文件是谁写的"**:扫全库拿差集会把同期别的进程落下的文件算进这一轮
  (实测)。`round` 是**发现这一篇的那一次业务抓取**(`article_fetch_rounds`,migrations v19),空串是
  「说不清」、不是「不是本次」;**那张表单开一张、不给 `articles` 加列**,理由同 v17/v18(库被
  `wechat-oa` 分支同时打开)。**它只许在 `upsert_article_from_blog` 里、和首次元数据写进同一个
  savepoint**(`INSERT OR IGNORE` 的 `rowcount == 1` 就是「首次插入」的确证)。**「两条写在同一个事
  务里」还不够,得能整篇一起撤回**:来源那一条写失败时,调用方是逐篇接住异常接着往下跑的,收尾那次
  commit 会把半条记录落了盘 —— 一篇在库、没有来源,下一轮来补正文时它已经不是首次插入,来源
  **再也补不回来**。所以失败要 `ROLLBACK TO SAVEPOINT` 把这一篇的文章行一起撤掉(只撤这一篇,调用方
  同一个事务里别的业务写入原样留着),异常原样抛上去;**不许在正文抓完之后补记** ——
  那样既会把这一次的批次号盖到一篇早就在库里、只是来补正文的文章头上,也会在正文之后崩一次时留下
  一篇有正文、没来源的文章,而它再也不进采集队列。没有证据的首次来源**保持未知**,谁也不许替它补。
  **回执的边界是「文件已经落进 vault」,不是「这一篇的后续记账也成了」** —— 文件写完就算它,随后
  那次已导标记/入库确认失败只意味着下一轮还会再导一次;算在后面的话,重导时它已经不是新文件,
  这一篇永远补不回账(实测形态在 `daily.md` 的「交完了要有一行明确的凭证」)。
  **清单里一行读不懂就整份作废**(不给 `items` 这个键),挑着收等于交上去一份少记的清单而上层把它
  当确定值用。
- **公众号只有一条通道 `wechat_local`(读本机绿泡泡数据库)。** `wechat_oa` 在另一分支、主干注册表里压
  根没有它,所以「两条通道互斥、选哪条是个设置」那一整套全删了。手动请求、定时配置和失败重试照旧显式
  携带 `channel`,不修改进程全局环境。
- **退役的通道名要留成别名,不能当「不认识的来源」丢掉**(`_RETIRED_CHANNELS`
  = `{"wechat_oa": "wechat_local"}`,`resolve_channel` / `known_channels` 是唯一口径)—— 存量
  `daily.json` 里写的正是旧名字,丢掉的话**公众号从此不再定时同步、不再进日报,而面
  板上完全看不出少了什么**。配套:①退役名也要能翻成人话(`channel_labels()`,API 的 `labels`);②别名
  表要发到浏览器(`/api/pipeline/channels` 的 `aliases`),前端不许写死 `wechat_oa`。
- **「我的订阅」的公众号视图不信 `kb-sync-channel` 那份全局选择**(`sources.js` 的
  `wechatSubsChannel()` 按来源目录声明的通道过别名表解析)。**检查/重试入口同理**自己按目录现解析
  (`refreshPipeSource()`,「检查连接」重跑整个 `openWechatSubs`)。
- **逐号「就绪」那套在主干上永久是空的**(一律 `ready=None` / `state=""`)。
  `Target.ready/tone/state` 和前端那条 `ready === null` 分支留着 —— 契约是通用的。别再把「刷新凭证」
  「去 Mac 绿泡泡里各点开一篇文章」那类文案加回来。
- **「翻过、确实没有图文」和「这一次没翻」不是一件事**(`local_accounts.discover` 给每行一个 `probe`:
  `index`/`live`/`none`/`unchecked`/`error`;文案是 `cli._NO_BIZ_STATE`(哪一档)+ `_NO_BIZ_FIX`(下一
  步)两半)。`discover` 裸列时一个会话都不翻,给了查询词也只翻前 `RESOLVE_LIMIT`(12)个。**「加号失
  败」这条提示也归这份口径管**(`_accounts_add` 的 `_worst_probe`):只有 `none` 那档该去同步一篇,另两档给「缩小关键词,或用 `--fakeid` 直接指定」。
- **同名会话里只要有一条没解析出归属,按名字添加就是歧义,一律拒掉**(`_accounts_add` 的 `blind`)。
  **归属只认 `__biz`,同名不是线索**,无 biz 的同名会话只生成「归属待核实」诊断、不能成为身份映射。配套
  两条在 `targets.js` 的 `renderTgtCands`:①归属是空的候选不许点添加(按钮禁用 + 写清下一步);②**「已
  订阅」只按身份(`byId`)认,名字一律不作数**。
- **一个档案名不许改挂到另一个 `__biz` 上**(`enable_crawl_account` 抛 `AccountNameTaken`)—— `name` 是
  主键、`articles.account_name` 是外键、vault 目录也按它归档,碰上同名就是把第一个号的**全部历史文章连
  同导出目录**接到第二个号身上。判据按元规则待在那条 UPSERT 自己的 `DO UPDATE … WHERE` 里。**四句豁免
  缺一不可**:①`excluded.fakeid = ''`;②`accounts.fakeid = ''`;③`accounts.fakeid = excluded.fakeid`;
  ④`import:` 开头的占位键。
- **本地库只负责文章发现,正文继续按链接抓取。** 本地读取只能通过已配置的 `rion-wechat-cli`,须确认
  `live_database_read_ok`,不允许通知预览替代、自动获取密钥或操作绿泡泡。归属最终按推送 URL 的 `__biz`
  核实,不能以同名自动匹配。
- **文章身份是 `article_identity` 的 `__biz + mid + idx`,保留已有文章 ID 和归属**(存量是凭证通道采
  的、ID 另一套规则,本地库再发现同一篇时必须认出来、不能新开一行);用户删除时同时记进链接身份黑名单。
  不要把推送接收时间或带跟踪参数的完整 URL 当成唯一键。
- **「这个身份被永久排除了吗」的判据待在写语句自己的 WHERE 里**
  (`_repo_exclusion._excluded_clause`,挂在 `upsert_article_from_blog` / `set_body_text` /
  `set_body_html` / `set_fetch_state`;四者都返回 `bool`,调用方必须跟上 —— 连**附属状态**那一笔也要
  带,它的外键指向 `articles`,无判据的写入在文章刚被删掉时会抛外键错误)。
  `prepare_article` 查黑名单那一下**只是早一道,不是互斥** —— 它和真正写入之间隔着抓正文和更新显示
  名;**最后一道在导出的发布那一步**(`export_obsidian.publish_one`,写事务里复核再写 md)。
  四处用同一组键(`article_identity.exclusion_keys`),事故形态和回归在 `failure-drop.md`。
- **本机推送链接须保留正文校验参数 `chksm`,仅剔除跟踪参数。** 发现阶段用分片/表级 `local_id` 游标 ——
  不要用 timeline 的 after-local-id 做增量分页(读取器是在 LIMIT 后过滤 ID),也不能拿发布时间当消息水
  位;**分页/读取失败不推进对应游标**。未选公众号只进本地发现索引,不下载正文、不写共享 articles、不导
  出。正文失败存 `article_fetch_state` 按退避重试,**不能用发布日期判断删除**;本地正文暂不可读走
  `body_failures` 明细,不进 `errors`、不使抓取阶段判红。`is_wx_deleted` 的历史自动退休记录允许重新入
  库,用户删除及身份黑名单仍拦截。其余(源指纹重扫、异常表、XML 内存解码、`no_body` 明细协议)见决策
  文档。
- **`collector/data` 和 `collector/.venv` 丢了就跑 `./collector/setup.sh`**(两者都是指向仓库外的软链
  接,新开的 worktree 只带源码;脚本幂等)。缺链时面板顶栏报「采集器还没装好」,抓取/导出落 skipped 而
  不是 error。**别再让它退回 `sys.executable`**(判定在 `wechat_local._backend_problem()`)。
- 采集器是**一次性 CLI,不是常驻服务**:`cd collector && ./.venv/bin/python -m cli <命令>`(常用 `status` / `collect` / `export` /
  `accounts list|add|remove` / `login`)。stdout 只出
  NDJSON 给程序读,人看的提示走 stderr。
- **公众号列表**存在 `collector/data/wechatoa.db` 的 `accounts` 表,日常维护走 kb-web「我的订阅」页,
  **不要直接改库**;终端等价物是 `cli accounts list|discover|add|remove|enable|disable`。**添加候选来自
  本机绿泡泡的会话列表**(`cli accounts discover`):不登录公众平台、不走网络。
- **公众号改名只更新 `accounts.display_name`,永远不动 `name`**(主键 + 外键 + vault 目录都按它)。**主
  路是本机会话列表**(`local_accounts.refresh_display_names`,每次同步在发现阶段之后跑一次)。抓正文时
  读到的 `var nickname` 照旧写回;别用页里裸的 `nickname:`(那可能是推荐位里别人家的号)。**按 biz 一一
  对应,不许「只有一个就配上」**,否则两个号的名字会互换。`GZH_DISPLAY_RECHECK_DAYS` 和
  `display_checked_at` 都没了,别再加回来。
- **`enable_crawl_account` 先按 fakeid 认号,认出来只更新、不插新行**(否则同一个号两套主键、vault 目录
  裂开,而外键全合法)。它返回**实际入库的档案键**,调用方据此区分「新添加」和「这就是那个改了名的号」。
- 别把 `collector/data` 或 `collector/.venv` 加进版本库 —— 它们是符号链接,`data/` 那条带斜杠的规则挡不
  住,.gitignore 里另有不带斜杠的显式两行,别删。
- 详细见 `docs/decisions/collector.md`。

## 事故形态与实测证据

- **公众号只有一条通道 `wechat_local`(读本机绿泡泡数据库)。** 原来那条逐号文章凭证的
  `wechat_oa` 在 `wechat-oa` 分支上,主干的注册表里压根没有它,所以「两条通道互斥、
  选哪条是个设置」那一整套(`collectors.wechat_channel`、`POST /api/pipeline/channels/wechat`、
  面板上的单选、浏览器里的 `kb-wechat-channel` 广播)全部删掉了。手动请求、定时配置和
  失败重试照旧显式携带 `channel`,不修改进程全局环境。
- **退役的通道名要留成别名,不能当「不认识的来源」丢掉**(`_RETIRED_CHANNELS`
  = `{"wechat_oa": "wechat_local"}`,`resolve_channel` / `known_channels` 是唯一口径,
  `daily_run.load_settings`/`save_settings`/`start` 都过它)。
  存量 `daily.json` 里写着的正是旧名字,丢掉的话**公众号从此不再
  定时同步、不再进日报,而面板上完全看不出少了什么**。两条配套的:
  ①**退役名也要能翻成人话**(`channel_labels()` 把别名一并给出,API 的 `labels`
  字段,前端 `channelLabel()` 回落查它)——历史运行记录里写着的可能正是它,面板上
  那行「上次来源」不该显示成一个裸 id;
  ②**别名表要发到浏览器**(`/api/pipeline/channels` 的 `aliases`,前端 `adoptChannels`
  照它改写 `kb-sync-channels` / `kb-daily-channels`)。localStorage 在后端的射程之外,
  而前端不该把 `wechat_oa` 这个名字写死——那等于同一份退役清单维护两遍。不改写的话
  只要还勾着 RSS,「一个都没剩才补默认」那句兜底就不触发,升级后打开新建同步只剩
  RSS,公众号被静默地漏掉、一句提示都没有。
  回归在 `tests/test_retired_channel.py`(12 条)+ `tests/retired_channel_probe.mjs`
  (53 项,自带 mock 后端、CDP 直连 chrome-headless-shell):存着
  `["wechat_oa","rss"]` 的 localStorage 打开新建同步要勾出公众号、localStorage 被
  改写、而且**看真的发出去的 `/api/pipeline/run` body** 里是 `wechat_local`;另有
  几项钉住删掉的那些东西没留下残骸(没有 `pipeWechat`/`saveWechatChannel` 这类
  全局、没有 `tgt-ready-bar`/`tgt-refresh` 这些节点、页面上不出现「凭证」二字、
  删掉的那两个端点一次都没被请求)。
  **「我的订阅」的公众号视图不信 `kb-sync-channel` 里那份全局选择**
  (`sources.js` 的 `wechatSubsChannel()`:按来源目录声明的通道过别名表解析)——
  那是旧版同步中心记「上次看哪个来源」用的,存量值可能是 rss/social(都是合法
  通道,别名迁移不会动它们),沿用的话公众号视图的连接卡、订阅列表、「同步全部」
  全跟着串到别的来源上。探针里存量 rss/social/未知值各走一遍:通道绑回
  `wechat_local`、连接卡和订阅列表的请求都点名它、「同步全部」提交的 body 是
  公众号、新建同步的多来源草稿(`kb-sync-channels`)不被改写。
  **检查/重试入口也一样,不依赖「某次成功初始化改过全局选择」**:首次读取通道
  失败时 `openWechatSubs` 在绑定之前就返回了,连接卡那颗「检查连接」原来直连
  `refreshPipeSource()`、拿残留的全局 `pipeChannel` 去查 —— 存量 rss + 首次 503 +
  恢复后点检查,公众号页顶着「公众号」标题显示 RSS 的「已连接」,订阅列表还停在
  原错误上(2026-10-08 第 2 轮审查实测)。现在 `refreshPipeSource()` 自己按目录
  现解析通道(这张连接卡只在公众号视图上),「检查连接」改为重跑整个
  `openWechatSubs`(连接 + 订阅列表一起恢复),失败文案里留「重新读取」入口。
  探针 6.8 节钉住:503 恢复后点检查,请求全部点名公众号、列表重新读出来。
- **逐号「就绪」那套在主干上永久是空的。** 采集器一律返回 `ready=None` /
  `state=""`(写成同一句话挂在 43 行上等于什么也没区分),`refresh_readiness`
  从 `TargetManager` 契约里摘掉了,
  `POST /api/sources/targets/refresh` 也没了。`Target.ready/tone/state` 这三个字段
  和前端那条 `ready === null` 的渲染分支留着——契约是通用的,别的来源用得上。
  别再把「刷新凭证」「去 Mac 绿泡泡里各点开一篇文章」那类文案加回来:它指的是
  另一条分支上的流程。
- **「翻过、确实没有图文」和「这一次没翻」不是一件事**(`local_accounts.discover`
  给每行一个 `probe`:`index`/`live`/`none`/`unchecked`/`error`,文案口径是
  `cli._NO_BIZ_STATE`(哪一档)+ `_NO_BIZ_FIX`(下一步怎么办)这两半,`_NO_BIZ_NOTE`
  由它们拼出来给终端那一列的标记和面板的 `signature`)。`discover` 裸列时
  一个会话都不翻、给了查询词也只翻前 `RESOLVE_LIMIT`(12)个,剩下的 `fakeid` 是空
  的——原来把所有空 fakeid 一律说成「本机暂无可解析的图文推送」,那是在报一个没做过
  的结论,而用户看到它就不会再试了。审查实测:15 个都有有效图文推送的候选,宽泛搜
  索下排在预算之外的 3 个被这么标出来,缩小关键词再搜立刻解出 biz、加得进去
  (2026-10-01)。单会话读不动也别含糊成「没有图文」。
  **「加号失败」这条提示也归这份口径管**(`_accounts_add` 的 `_worst_probe`):
  它原来对所有空 fakeid 一律报「没有能解析出归属的图文推送」+「去绿泡泡里同步一篇
  图文之后再试」,于是搜索那一行明明显示着「本机这条会话读不动」,点添加却换了个
  说法,还在建议一件白跑的事——读不动和排在预算之外都不是"没有图文",号本身可能
  早就有了(2026-10-01 第 2 轮审查实测)。只有 `none` 那档该去同步一篇;另两档给
  「缩小关键词再搜,或者用 --fakeid 直接指定」。拆成状态 + 处置两半就是为了这条:
  列表里一行放得下 `状态(怎么办)`,错误提示里还要接着说别的,原样塞进去是括号
  套括号。
- **同名会话里只要有一条没解析出归属,按名字添加就是歧义,一律拒掉**
  (`_accounts_add` 里那个 `blind`)。原来只拿「已经解析出 `__biz` 的那几个」判歧义,
  于是两个同名会话、一个解出来了、另一个读不动(或排在解析预算之外)时,按名字添加
  会**把解出来的那个拿去入库**:退出码 0、提示「已添加」,订阅的却是另一个号
  (2026-10-01 第 3 轮审查复现——面板上点那条写着「本机这条会话读不动」的候选,
  前端发上去的正是空 id)。**归属只认 `__biz`,同名不是线索**:一个名字对上两个
  会话时,少一个归属就等于不知道用户要的是哪一个。
  配套的两条在前端(`targets.js` 的 `renderTgtCands`):①**归属是空的候选不许点
  添加**(`blind`,按钮禁用 + 写清下一步),这里能发上去的只有名字和一个空 id,
  点下去就是走上面那条路;②**「已订阅」只按身份(`byId`,公众号是 `__biz`)认,
  名字一律不作数**——它回答的是"这个名字订过没有",要显示的却是"这一条候选订过
  没有",而同名的两个会话是两个号。第 1 轮收成「这个名字在本次结果里只出现一次
  才按名字认」还不够:拿候选下面显示的那个编号单独再搜一次,结果只剩一条,于是
  又按名字认成「已订阅」,而真正订着的是同名的另一个号——候选 id 为空的、id 明确
  是另一个 `__biz` 的,两种写法都这样(2026-10-01 第 2 轮审查两张截图)。按 id 认
  本来就能管住改过名的号,名字兜底整条删掉。
  回归:`collector/tests/test_local_accounts.py` 那条混合同名的——钉的不是提示文案,
  而是**库里一个字都没多**(只验提示的话,挑错号那条路也"看起来对");
  `tests/retired_channel_probe.mjs` 里**真鼠标点**一下禁用的那颗按钮,确认一个
  请求都没发出去(disabled 上直接调 `.click()` 本来就不触发,证明不了页面上点下去
  会怎样),外加加完之后另一条同名候选仍然不是「已订阅」、以及缩小搜索只剩一条时
  那两种写法各一条。
- **一个档案名不许改挂到另一个 `__biz` 上**(`enable_crawl_account` 抛
  `AccountNameTaken`)。`accounts.name` 是主键、`articles.account_name` 是指向它的
  外键、导出的 vault 目录也按它归档,所以原来那句
  `ON CONFLICT(name) DO UPDATE SET fakeid=excluded.fakeid` 碰上"两个号同名"就是把
  第一个号的**全部历史文章连同导出目录**接到第二个号的身份上:外键全都合法、提示
  还是「已添加」,订阅列表看起来只是少了一个号(2026-10-01 第 2 轮审查实测:两条
  同名候选都解析出归属之后点第二条,HTTP 200、列表仍只有一条、id 换成第二个号,
  而那篇旧文章的链接里还是第一个 `__biz`)。**拒掉是唯一不丢东西的做法**——要支持
  同时订阅两个同名号,就得给第二个号另起一个档案键,而那个键会成为新的 vault 目录
  名,不是写库这一步能顺手定的。
  **拒绝不是死路**:没有历史文章的号移除时整行就真删了(`remove_account_from_list`),
  所以"同名两个号、第一次点错了"自己有退路,不需要在写库这一步替用户判断谁该让位。
  **判据就在那条 UPSERT 自己的 `DO UPDATE … WHERE` 里,不许在 Python 那边先
  SELECT 一次。** 先查后写是 TOCTOU:两个请求都读到"这个名字没人占"(面板上那个
  `tgtBusy` 只管单个页面,管不到另一个标签页、更管不到独立的 CLI 进程),随后第二
  条 UPSERT 照样把第一条刚写进去的 fakeid 盖掉,两边都报「已添加」——和这条规则
  本来要挡的事故一模一样,只是把窗口从"永远"缩成了"同时"(2026-10-01 第 3 轮审查
  实测:两个线程各一条连接抢同一个名字 50/50 复现,真实 HTTP→CLI→仓储一条链也
  复现)。python 的 `sqlite3` 默认 `isolation_level=''` 下 **SELECT 压根不开事务、
  不持任何锁**,所以那次预检和随后的写入之间没有任何东西把它们绑在一起;而 SQLite
  把写入串行化,判据待在写语句的 WHERE 里才和那次写入同生共死,放行不了就是 0 行
  (`cursor.rowcount == 0` 就是拒绝信号,由调用方抛 `AccountNameTaken`)。
  **不走"`BEGIN IMMEDIATE` 包住读+写"**:这个连接可能已经在别人开的事务里,而 WAL
  下拿不到快照报的是 `SQLITE_BUSY_SNAPSHOT`,`busy_timeout` 管不着它。`name_held_by`
  降级成**只给错误文案用的读取**——规则只有 WHERE 那一份,它只回答"现在挂在谁名下"。
  四句豁免缺一不可,**每一句各有一条回归钉着**:①`excluded.fakeid = ''`,空 fakeid
  的调用不许把已有归属抹成空(`fakeid=CASE WHEN excluded.fakeid <> '' …`)——那种
  调用只是在说"确保这个名字在采集列表里",没有在声明身份;②`accounts.fakeid = ''`,
  归属还没核实的名字不算"另一个号",真 `__biz` 认出来要填得进去;③
  `accounts.fakeid = excluded.fakeid`,同一个号重新订阅只更新;④`import:` 开头的
  占位键不算"另一个号"(`upsert_import_account` 留下的,本来就等着被真归属填上;
  当成另一个号会让这类存量账号永久订阅不了)。**②那句是逐句反向验证补出来的**:
  去掉它整套测试照旧全绿,而它一旦没了,空 fakeid 的行就永远订阅不了。
  回归:`collector/tests/test_account_subscription.py` 的 `SameNameDifferentAccountTests`
  (拒掉之后第一个号和它那篇文章一个字没动、外键干净 / 移除之后名字让得出来 /
  四句豁免各一条 / **两条独立连接同时抢同一个名字,8 组各一次,只能一个赢、库里
  只留赢家那个 fakeid** / **抢输的那一边不许把赢家的历史文章带走** / **两个真
  subprocess 跑 `cli accounts add`,退出码必须是一个 0 一个 1**)+
  `test_local_accounts.py` 里走真 CLI 的那条;探针那边**把 mock 后端按名字主键改成
  会拒**——只往数组里 push 的话,"同名第二个号"这条路在页面上看起来永远是成功的
  (第 2 轮审查指的就是这个 mock 不够真)。并发那三条**必须是真的两个执行体**:
  同进程两条连接就足以证明闸门不靠进程内状态(sqlite3 不共享 cache),再加两个真
  进程那一条,是因为"先 SELECT 后写"那一版在两个进程下同样 50/50 —— 注入回去时它
  报的正是 `[0, 0]`(两个都说成功)。
- **本地库只负责文章发现,正文继续按链接抓取。** 本地读取只能通过已配置的
  `rion-wechat-cli`,须确认 `live_database_read_ok`,不允许通知预览替代、自动获取
  密钥或操作绿泡泡。归属最终按推送 URL 的 `__biz` 核实,不能以同名自动匹配。
- **文章身份是 `article_identity` 的 `__biz + mid + idx`,保留已有文章 ID 和归属。**
  库里那 6000 多篇存量是凭证通道采的(ID 另一套规则),本地库再发现同一篇时必须认出
  来、不能新开一行;用户删除时同时记进链接身份黑名单,免得换一种发现方式又把它复活。
  不要把推送接收时间或带跟踪参数的完整 URL 当成唯一键。
- 本地推送链接须保留正文校验参数 `chksm`，仅剔除跟踪参数。发现阶段经 Reader
  只读 SQL 批量读取公众号 Msg 表，使用分片/表级 local_id 游标；不要用 timeline
  的 after-local-id 做增量分页（读取器是在 LIMIT 后过滤 ID），也不能拿发布时间
  当消息水位。先维护 `local_wechat_articles`，再按发布时间和启用/所选 biz 查询。
  未选公众号只进入本地发现索引，不下载正文、不写共享 articles、不导出，匹配不打印名称。
  `local_wechat_sources` 保存数量及首尾指纹，检测源变化后重扫；分页/读取失败不推进
  对应游标。内容异常保存在 `local_wechat_message_issues`（位置、指纹、原因），不阻塞
  游标；后续定向核对异常行，变化后再解析。能确认属于未选公众号的异常不进入同步提示。
  整条图文解析失败仍从完整文章 URL 提取唯一 biz，另存 `local_wechat_issue_biz`，
  不依赖成功文章才能诊断覆盖。无 biz 的同名会话只生成「归属待核实」诊断，不能
  成为文章身份映射；有不可读推送的所选账号均列入待处理明细（含已有历史文章），
  存量条数明确标注，账号诊断排在正文待补之前。同名冲突/无归属线索的异常只汇总
  一次，不重复归入各号；只将有具体候选关联的缺口标为覆盖待核实，并在汇总中列
  候选名称。完全无关联线索的消息不能改变其他账号状态。正常未同步账号仅汇总
  「在绿泡泡关注并同步后可采集」，不生成永久待办；同时存在无归属推送时，提示改为
  「未匹配到本机推送」，附条件性的关注/同步指引和「可能属于其中某个号」说明。
  原始 XML 只在内存中解码；压缩消息用锁定依赖 zstandard。旧 JSON 映射
  不再用于同步。进度 phase=discovery/download，正文以篇数计数，保留原通道事件兼容。
  正文失败存 `article_fetch_state`，以退避控制重试，不能用文章发布日期判断删除。
  本地正文暂不可读走 `body_failures` 待处理明细，不进 `errors`、不使抓取阶段判红；
  `latest_crawl_at` 对已有覆盖的账号统一记录最近完成发现检查的时间，不以新文章
  或隔离坏推送决定是否更新。读取失败按稳定的会话 Msg 表名跨分片追溯 biz，
  必要时仅用同名线索保守阻止对应账号更新时间，不建立文章归属；新分片没有成功
  文章也能关联到旧分片。未知且无关联的失败不冻结其他账号。导出保留
  `无正文,跳过` 明细协议供 CLI 生成 `no_body`，正文缺失不建议标记绿泡泡删除；那份明细还要带
  **身份**（`export_obsidian.py --report` 写的结构化 JSON，`cli._no_body_items` 优先读它），面板
  据此点开原文、按 `cli articles drop` 把这一篇永久排除，见 `failure-drop.md`。
  那条删除要带 `--link`:库里已经没有那一行时,**缺行不是排除凭据**,只有链接算得出跨通道也认的
  那个身份。
  `is_wx_deleted` 的历史自动退休记录允许重新入库，用户删除及身份黑名单仍拦截。

- **`collector/data` 和 `collector/.venv` 丢了就跑 `./collector/setup.sh`**。两者都在
  .gitignore 里、都是链到仓库外那份真实目录的软链接,所以新开的 worktree 只带源码,
  主检出被 `git clean -xd` 之类扫过一次也会没。脚本幂等,链的是同一份真实数据。
  缺链时面板顶栏报「采集器还没装好 · 缺 .venv、data——跑一次 ./collector/setup.sh」,
  抓取/导出两阶段落 skipped 而不是 error。**别再让它退回 `sys.executable`**:
  那是知识库自己的解释器,拿它跑 `-m cli` 必死在 `import requests`,用户看到的是
  一句 ModuleNotFoundError 的 traceback 尾巴,完全指不到 setup.sh 上去
  (2026-08-27 修,判定在 `wechat_local._backend_problem()`)。
- 采集器是**一次性 CLI,不是常驻服务**:`cd collector && ./.venv/bin/python -m cli <命令>`。
  常用 `status`(健康+订阅列表) / `collect` / `export` / `accounts list|add|remove` / `login`。
  stdout 只出 NDJSON 给程序读,人看的提示走 stderr。
- `collector/api.py` **不再是 API**。它原来是跑在 :8000 的 FastAPI 后端(53 个路由),
  给 `collector/frontend` 那套 Vue UI 用;前端和常驻服务撤掉后一个路由都没人调,
  已经连同 `server.py` / `web_app.py` / `src/api/` 一起删了。现在只剩 CLI 和采集
  流程共用的底层——现在只剩三个函数(数正文字数、把一篇文章写进库、抓正文)。
  文件名没改,是因为 `cli.py` 和 `src/collect/` 里的 `import api` 还指着它。
  要看旧服务(或者封面下载、公众平台搜号、扫码凭证那几个)翻 git 历史。
- **公众号列表**存在 `collector/data/wechatoa.db` 的 `accounts` 表,日常维护走
  kb-web「我的订阅」页的公众号视图(搜索添加 / 停用 / 移除,就地在页面上,不再是
  弹窗),不要直接改库。
  终端等价物是 `cli accounts list|discover|add|remove|enable|disable`;面板走的就是它。
  **添加候选来自本机绿泡泡的会话列表**(`cli accounts discover`):不登录公众平台、
  不走网络。裸列时归属从发现索引里查(一条推送都不翻),给了查询词才现翻索引外那几个
  会话、并且有上限——280 个会话逐个翻一遍是几分钟。
- **公众号改名只更新 `accounts.display_name`,永远不动 `name`。** `name` 是主键,
  `articles.account_name` 是指向它的外键(没有 ON UPDATE CASCADE),已导出的
  vault markdown 也按它归档——改它要么被外键拒绝,要么让 6000 多篇文章和磁盘
  上的文件对不上。新名字从正文页的 `var nickname = htmlDecode(...)` 里读,
  抓正文时顺手记下(见 `extract_account_nickname`)。别用页里裸的 `nickname:`,
  那可能是推荐位里别人家的号——本机推送的 XML 里同样没有昵称。
  **主路是本机会话列表**(`local_accounts.refresh_display_names`,每次同步在
  发现阶段之后跑一次):公众号改名后绿泡泡的会话列表立刻显示新名字,这是本机现成的
  事实,零请求、不限于发了新文章的号,所以也不需要节流。实测 44 个订阅号里能对上
  会话的 42 个。另外两条顺手的:抓正文时读到的 `var nickname` 照旧写回
  (`api.py`),用户 discover 添加时直接采信会话列表上的名字。
  **它删掉了原来那两条凑出来的路**:①只在抓到新文章时读——而「改了名」和「不发
  新文章」常常是同一批号,恰恰是用户在绿泡泡里搜不到、最需要看见新名字的那批;
  ②`GZH_DISPLAY_RECHECK_DAYS`(7 天)定期单抓一篇旧文章的正文页只为读那一行昵称,
  还要排掉 `is_wx_deleted=1`、读不出就往下一篇退。这些连同 `display_checked_at`
  列(migration 16)一起没了,别再加回来。
  **按 biz 一一对应,不许「只有一个就配上」**:`__biz` 对不上的会话一律跳过,
  否则两个号的名字会互换(回归在 `collector/tests/test_account_rename.py`,
  两个号、两个会话、顺序故意反着摆)。
- **`enable_crawl_account` 先按 fakeid 认号,认出来只更新、不插新行。**
  改名后用户必然拿新名字来搜索添加(列表上还是旧名,绿泡泡里搜不到旧名),
  照新名字插一行就会有两条同 fakeid 的订阅:同一个号的新文章分别写进两套
  主键,vault 目录裂开,外键还全合法,谁也不报错。它返回**实际入库的档案键**,
  调用方据此区分"新添加"和"这就是那个改了名的号"。前端候选的「已订阅」
  判定同理——只比名字的话,改过名的号永远显示成「添加」(2026-08-27 审查发现)。
- 别把 `collector/data` 或 `collector/.venv` 加进版本库:前者是 832M 的库,外加几份
  存量垃圾——326M `covers`(封面早就不下载了,见 `8cc380d`)、654M `backups`、凭证
  通道留下的 `profile_credentials.json`。它们现在是符号链接,`data/` 那条带斜杠的
  规则挡不住,所以 .gitignore 里另有不带斜杠的显式两行,别删。
