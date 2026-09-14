# knowledge-base 项目约定

## 代码文件行数上限(硬性)

- **大模型生成的代码文件,单文件不得超过 700 行。**
- 预计超过 700 行时,必须先拆分:按职责拆成多个文件(如 CSS/JS 按面板拆、Python 按模块拆),再分别生成。
- 读取大文件同理:超过 ~700 行的文件分块读(offset/limit),不要整读。

## 仓库结构

- `kb/` 知识库本体,`collector/` 来源采集器(公众号)。两者在同一个 git 仓库里,
  就是为了改采集器时也能用 worktree 隔离——分成两个 git 的话,worktree 只会
  复制 kb 那一半,collector 仍是那唯一一份生产代码,隔离等于没有。
- 知识库主干不认识任何具体来源,只认 `kb/collectors/` 的采集器契约;微信特有的
  东西全在 `kb/collectors/wechat_oa.py` 与 `collector/` 里。新增来源写个实现即可。

## 操作入口

- **`./kb.sh` 是唯一的操作入口**(取代原 WeChatOA 的 start_all.sh)。`./kb.sh` 不带参数
  打印全部命令。日常两条:`./kb.sh prep`(同步前起 wxdown + 切路由 + 盯凭证)、
  `./kb.sh status`(服务/路由/凭证/订阅一屏看全)。
- **服务一律用 `daemonize()` 起,不用 `nohup ... &`**。后者起出来的进程跟脚本同一个
  进程组,非交互 shell 会给它把 SIGINT 设成 SIG_IGN,而这个 SIG_IGN 被子进程继承——
  于是 stop 时的 `kill -INT` 也被忽略,只能降级 SIGTERM,恰好会把 wxdown 的
  multiprocessing 子进程丢成孤儿。daemonize 借 python 做 fork+setsid(macOS 没有
  setsid 命令)并把 SIGINT 恢复默认:自成会话不受终端影响,同时 stop 能优雅收尾。
- **两套凭证别搞混**:`login` 那套是扫码拿的、全局一份,微信关掉 `appmsgpublish`
  之后它只剩「搜公众号加号」一个用途;`creds` 那套是每个号各一份的文章凭证,
  只能靠在 Mac 微信里打开该号一篇文章、由 wxdown-service 的 mitm 截下来。
  **抓文章——列表和正文都算——全靠后者**,`login` 救不了,续期方式也完全不同。
- 公众平台后台那套采集(`appmsgpublish` 翻列表)的代码已经删干净了,包括
  `main.py`、`src/processor/`、`src/web/` 和 `profile_adapter` 的 monkey-patch。
  留着它不是无害的:原来"某个号没有 profile_ext 凭证 → 悄悄退回旧接口 →
  invalid session → 整轮采集中止 → 提示去跑 login"这条路,每一步都是错的。
  现在 `WechatRequest` 本身就是 profile_ext 实现,缺凭证只影响那一个号。
- **文章凭证有四态,别再只分"有/没有"**:`missing`(没截到)、`expired`(服务端
  回过 no session,`invalidated_at>0`)、`stale`(还没被否掉,但截下来超过
  `WXDOWN_CRED_FRESH_MINUTES`,默认 20 分钟)、`valid`。判定在
  `profile_ext.credential_state()`。**stale 只影响上报,不影响放行**——
  `is_valid()` 是 `get(allow_invalid=False)` 的闸门,按年龄收紧它就等于
  "过了 20 分钟不许采",而实测 30 分钟的凭证三个里两个还能用。stale 不计入
  「几个就绪」,面板上是黄点;采集照样拿它去试,试通了 `mark_valid` 转绿。
  没有这一档的话,昨天截的 44 条今天全是绿点,面板报 44/44 就绪。
- **新鲜度算的是 `max(captured_at, verified_at)`,不是 `captured_at`。**
  `verified_at` 由 `mark_valid` 在采集实测通过时写,是独立字段——写回
  `captured_at` 会把"什么时候截的"这个事实改掉。没有它,`mark_valid` 就只清
  `invalidated_at`,一条三小时前截的凭证明明刚采成功,面板照样黄点、照样不
  计入就绪,和注释里写的"试通转绿"自相矛盾(2026-08-27 审查发现)。
  `mark_valid` 写盘有 60 秒节流:翻一页调一次,不然一个号十几次全量重写。
- wxdown 把抓到的凭证写在 `wxdown-service/resources/data/credentials.json`,
  `cli creds` 直接读这个文件导入。原来那条 "wxdown WebSocket → 前端 :5173 →
  后端 :8000 POST" 的转发链路只是在搬运这个文件,两个常驻服务都已不需要。

## collector 采集器

- **`collector/data` 和 `collector/.venv` 丢了就跑 `./collector/setup.sh`**。两者都在
  .gitignore 里、都是链到仓库外那份真实目录的软链接,所以新开的 worktree 只带源码,
  主检出被 `git clean -xd` 之类扫过一次也会没。脚本幂等,链的是同一份真实数据。
  缺链时面板顶栏报「采集器还没装好 · 缺 .venv、data——跑一次 ./collector/setup.sh」,
  抓取/导出两阶段落 skipped 而不是 error。**别再让它退回 `sys.executable`**:
  那是知识库自己的解释器,拿它跑 `-m cli` 必死在 `import requests`,用户看到的是
  一句 ModuleNotFoundError 的 traceback 尾巴,完全指不到 setup.sh 上去
  (2026-08-27 修,判定在 `wechat_oa._backend_problem()`)。
- 采集器是**一次性 CLI,不是常驻服务**:`cd collector && ./.venv/bin/python -m cli <命令>`。
  常用 `status`(健康+订阅列表) / `collect` / `export` / `accounts list|add|remove` / `login`。
  stdout 只出 NDJSON 给程序读,人看的提示走 stderr。
- `collector/api.py` **不再是 API**。它原来是跑在 :8000 的 FastAPI 后端(53 个路由),
  给 `collector/frontend` 那套 Vue UI 用;前端和常驻服务撤掉后一个路由都没人调,
  已经连同 `server.py` / `web_app.py` / `src/api/` 一起删了。现在只剩 CLI 和采集
  流程共用的底层(抓正文、下封面、搜号、查扫码凭证)。文件名没改,是因为
  `cli.py` 和 `src/collect/` 里的 `import api` 还指着它。要看旧服务翻 git 历史。
- **公众号列表**存在 `collector/data/wechatoa.db` 的 `accounts` 表,日常维护走
  kb-web 管道 tab 的「管理公众号」弹窗(搜索添加 / 停用 / 移除),不要直接改库。
  终端等价物是 `cli accounts list|search|add|remove|enable|disable`;面板走的就是它。
  搜索添加需要扫码凭证(`cli login`),停用/移除只动本地库,凭证过期也能用。
- **公众号改名只更新 `accounts.display_name`,永远不动 `name`。** `name` 是主键,
  `articles.account_name` 是指向它的外键(没有 ON UPDATE CASCADE),已导出的
  vault markdown 也按它归档——改它要么被外键拒绝,要么让 6000 多篇文章和磁盘
  上的文件对不上。新名字从正文页的 `var nickname = htmlDecode(...)` 里读,
  抓正文时顺手记下(见 `extract_account_nickname`)。别用页里裸的 `nickname:`,
  那可能是推荐位里别人家的号。列表接口 getmsg 和 wxdown 的凭证都不带昵称。
  昵称有三条来路,缺一条就有号永远改不了名:①抓到新文章时顺手读;
  ②`runner._refresh_display_name` 每隔 `GZH_DISPLAY_RECHECK_DAYS`(默认 7 天)
  核对一次(挑库里几篇旧文章单抓正文页,只读昵称)——不发新文章的号
  只有这一条路;③用户搜新名字添加时直接采信。
  挑文章要排掉 `is_wx_deleted=1`,并且读不出昵称时往下一篇退(上限
  `_NICKNAME_CANDIDATES`):已删文章的链接打开是"该内容已被发布者删除",
  页面上没有 `var nickname`,而多图文的次条被删很常见、还跟头条同一个
  create_time 排在它前面——只取一篇的话这个号就永远改不了名
  (2026-08-27 第 2 轮审查发现,`GitHub源码` 就是这个形态)。
  **节流看的是 `display_checked_at`,不是 display_name 空不空**:后者只回答
  "现在叫什么",补上一次之后就再也不看第二眼,停更号以后再改名就永远发现
  不了(2026-08-27 第 1 轮审查发现)。核对完无论名字变没变都记一笔时间,
  所以稳定状态下每个号每 7 天才多花一次请求。
- **`enable_crawl_account` 先按 fakeid 认号,认出来只更新、不插新行。**
  改名后用户必然拿新名字来搜索添加(列表上还是旧名,微信里搜不到旧名),
  照新名字插一行就会有两条同 fakeid 的订阅:同一个号的新文章分别写进两套
  主键,vault 目录裂开,外键还全合法,谁也不报错。它返回**实际入库的档案键**,
  调用方据此区分"新添加"和"这就是那个改了名的号"。前端候选的「已订阅」
  判定同理——只比名字的话,改过名的号永远显示成「添加」(2026-08-27 审查发现)。
- **「几个凭证就绪」只数参与采集的号**,而且是**三处同一个口径**:面板
  (`sources_api.list_targets` 的 `scored` 过滤 `enabled`)、`cli creds`
  (SQL 里的 `in_crawl_config=1`)、`cli accounts list` 的摘要行。停用的号不
  采集,自然不该要求它有凭证——算进总数的话,停用一个号之后「全部就绪」
  就永远到不了,弹窗还一直催你去点它的文章。这条口径在三处**分别漏过一次**
  (2026-08-27 连着两轮审查各抓出一个),`tests/test_ready_count.py` 就是拿来
  把它们钉在一起的。列表本身照样显示停用号(得能重新启用),只是不进计数、
  不进「只看未就绪」。
- 别把 `collector/data` 或 `collector/.venv` 加进版本库:前者含 592M 库、264M 封面和
  带 token/cookie 的 `id_info.json`。它们现在是符号链接,`data/` 那条带斜杠的规则
  挡不住,所以 .gitignore 里另有不带斜杠的显式两行,别删。

## kb-web 服务

- 管道跑在 kb-web 进程内,杀 8787 前先 `curl -s http://127.0.0.1:8787/api/pipeline/status` 确认 `"running": false`。
- server.py / projects.py 改动要重启才生效;kb/static/ 下静态文件每次请求从磁盘读,免重启。
- 重启用 `env -i` 干净环境(继承嵌套 claude 的环境变量会导致 Not logged in)。
- 不要用 preview_start 托管 kb-web(会接管生命周期,杀掉管道)。

## 大模型阶段配置(kb/llm_stages.py)

- **每一处会调大模型的活都是一个「阶段」**,注册表在 `kb/llm_stages.py` 的
  `STAGES`:问答回答/意图规划/研究工具循环、收藏分诊、提取打标、条目化
  gate/拆条目、wiki 摘要综述、skill 标签回填。面板在 kb-web 顶栏「模型」,
  配置落 `data/secrets.json` 的 `stages` 段。**别再在各文件里写死模型字面量**
  (原来 `EXTRACT_MODEL`/`GATE_MODEL`/`TRANSCRIBE_MODEL`/`LLM_MODEL`/
  `JUDGE_MODEL` 五处各写一份),调用方一律 `stage_model("<key>")` 取。
- **provider 是显式存下来的,不是从模型名猜的。** `_provider_of` 那套前缀规则
  (`gpt-*` → openai)只是没有别的信息时的兜底:同一个 id 在两家中转站都可能
  存在,猜错就是拿 Anthropic 的 key 去打 OpenAI 的站,报 401 还看不出为什么。
  面板下拉的 value 是 `provider:model_id`,选项按供应商分组。字符串形式的
  `openai:gpt-5.5` 在 `llm.resolve` / `--model` 参数上同样认。
- **模型标识一出 Python 就得带家**(`llm.qualify`):`/api/models` 的 option
  value、`get_default_model`/`get_plan_model`/`get_fav_model` 的返回、落进
  messages 表的那一列,全是 `provider:model_id`。这三个 getter 一度只回裸 id,
  于是「面板选 openai → 问答把裸 id 交回 `resolve()` → 按遍历顺序猜成
  anthropic」,选哪家就用哪家当场不成立;同名 id 还会让下拉出现两个一样的
  选项、`default` 两边同时为真(2026-09-14 审查发现)。界面上要显示模型名时
  过 `modelLabel()`(core.js)剥掉前缀,别把带家的值直接印给人看。
- **下拉的候选 = 勾选的模型 + 各阶段当前指向的模型**(`llm.model_pool()`)。阶段
  模型不要求在「API Key」面板勾选过(API/命令行写的、老配置迁移的、选完又被
  取消勾选的),而只要没有一个 option 匹配当前默认,浏览器就自动选中第一个候选,
  chat.js 每次发送又都带上下拉的值——面板里配好的问答模型当场被顶掉,还看不出
  是谁改的(2026-09-14 第 2 轮审查发现)。`loadModels()` 里另有一层兜底补项。
- **逐条试候选只能用 `llm.parse_model`,不能用 `llm.resolve`。** 后者认不出家时
  会去问默认模型,而默认模型自己也在走这段解析——旧配置里留一个谁也不认识的
  `default_model` 就是无限递归。`tests/test_llm_stages.py` 钉住了这条。
- 没配的阶段按 `Stage.default` 走,**那些 default 就是这套设置出现之前各文件里
  写死的那个 id**,所以「一个都不配」等于行为不变;测试里逐个对过。
- 阶段配置**读时不缓存**(每篇/每轮现取):面板改完不用重启 kb-web,也不用等
  下一次管道。批量脚本例外——transcribe 这种在整批开始时定一次,免得中途改
  面板出现半批新半批旧。
- **改 `secrets.json` 一律走 `llm.update_settings(mutate)`,别在外面 read → 改 →
  write。** 它在锁里重新读盘再交给 mutate 改,两次并发保存才会叠加:面板里
  连着改两个阶段(或一边存 key 一边改阶段)时,各自基于同一份旧内容写回的话,后写
  的那次把前一次整个吃掉,两个请求还都返回成功(2026-09-14 第 3 轮审查发现)。
  **临界区里是两把锁,少一把都不够**:进程内 RLock 挡同一个 kb-web 里的并发,
  `secrets.json.lock` 上的 flock 挡跨进程的——两个 kb-web 实例、或者服务进程
  和一条命令行脚本同时存盘时,进程内锁形同虚设,丢更新的形态和第 3 轮一模一样
  (2026-09-14 第 4 轮审查发现)。flock 锁的是**独立的 lockfile,不是
  `secrets.json` 自己**:写盘是 rename 换文件,锁着的 inode 会被换走,后来者
  锁到的就是另一个对象了;lockfile 也只创建不删除,unlink 会让
  「A 持锁 → B 删文件 → C 新建再持锁」出现两个持锁者。
  写盘是「唯一名字的临时文件 + rename」:截断重写会让并发读者读到半截 JSON,而
  `_file_secrets` 把坏 JSON 吞成 `{}`,接着一存盘就把整份 key 抹了;临时文件名不
  唯一则两个进程会抢同一个 tmp,先 rename 的把它搬走、后一个报 FileNotFoundError。
- 收藏页那个模型下拉和面板里的「收藏 · 分诊与行动卡」**是同一处配置**
  (`llm.set_fav_model` 直接写 stages)。两边各存一份的话,用户在一处改完,
  另一处的值会悄悄把它压回去。旧的顶层键(`default_model`/`plan_model`/
  `fav_model`/`agent_model`)还读得到,但一旦在面板里改过就会被清掉。

## 向量编码(bge-m3)

- **模型跑在子进程里,不在 kb-web 进程里**(`kb/embed_worker.py` 是本体,
  `kb/embed_pool.py` 管起停,`kb/embedder.py` 是门面)。理由只有一个:内存。
  bge-m3 一碰 MPS 就占 ~3.7 GB,而且批量编码时每出现一种新的 batch 形状,MPS
  的显存池就再涨一截、**永不回落**——kb-web 跑几轮 index 之后常驻 8.8 GB
  (实测峰值 11 GB),`ps` 的 RSS 还看不出来(被压缩换出了,只表现为 swap 打满)。
- **这笔内存在进程内根本收不回**(2026-09-08 实测,别再试了):
  `torch.mps.empty_cache()` 只让 phys_footprint 掉 8 MB;`del model` + gc +
  empty_cache 也只从 5901 MB 降到 4157 MB;改 fp16 只把权重从 2.96 压到 1.07 GB,
  编码峰值一模一样。**进程边界是唯一可靠的回收手段。**
- 生命周期:懒起(第一次编码才 spawn,冷启动 3.2 秒)、空闲
  `KB_EMBED_IDLE_SECONDS`(默认 600)秒自杀、批量任务结束时
  `embed_pending_chunks` 显式收掉。子进程 stdin 读到 EOF 就退出,所以父进程
  怎么死都不会留孤儿;它自己 `start_new_session=True`,不会被打给父进程组的
  信号在编码半途打断。
- **批量写向量一律走 `embedder.embed_pending_chunks(conn, progress)`**,别再抄
  「取无向量 chunk → 128 条一批 → 写回 → commit」那个循环:原来 `cli.py`、
  `pipeline_stages` 的两处各有一份,改一处漏两处。
- `KB_EMBED_BATCH_BUDGET` 决定**峰值**内存(注意力张量随文本长度平方涨,显存池
  会涨到见过的最大批次那个规模)。2026-09-08 实测 1536 条真实 chunk:
  32×768²(原值)峰值 17.0 GB / 14.7 chunk·s⁻¹,8×768²(现值)8.7 GB / 9.0,
  4×768² 8.0 GB / 8.8。取 8 是拐点。**改这个值不需要 reembed**——实测新旧预算
  出来的向量余弦相似度 min 0.99999994。
- 协议是「4 字节小端长度 + 载荷」的帧。**向量帧的长度前缀必须被吃掉**:第一版
  按 `n*dim*4` 裸读,前缀被当成第一个 float,整批向量平移 4 字节(余弦相似度
  从 1.0 掉到 0),剩的 4 字节还会把下一个请求的帧同步带歪——而错位之后 read
  会**永久阻塞**,且是攥着锁阻塞的,搜索和索引一起冻住。所以父进程侧的读带
  帧长上限(`_MAX_FRAME`)和超时(`KB_EMBED_REQUEST_TIMEOUT`),把「挂住」变成
  「报错重试」。`tests/test_embed_pool.py` 拿 stub worker 把这些钉住(不加载
  真模型,1 秒跑完);注入回原 bug 会红 10 条。
- 已知遗留:`kb-mcp` 有好几个进程,各自会起自己的 worker(都命中 3.7 GB)。
  空闲超时能兜住,但同时活跃时仍是几倍开销。真要治得让它们共用一个常驻
  worker(要 socket + 守护进程),现在没做。

## 前端反馈(硬性)

- **禁止用原生 `alert` / `confirm` / `prompt`**,系统弹窗跟本项目的瓷白×墨蓝黑调子完全不搭,还阻塞主线程。
- 统一走 `kb/static/js/ui.js` 三个出口:
  - `toast(text, {type: err|ok|info, detail})` — 一次性操作反馈(失败/成功),右下浮起,自动消失。
  - `confirmBox({title, body, ok, cancel, danger})` — 破坏性动作确认,返回 `Promise<boolean>`,Esc / 点遮罩 = 取消。
  - `apiFail(r, fallback)` / `toastIfFail(r, text)` — 从失败的 `Response` 榨错误文案。**别再写 `(await r.json()).detail || '失败'`**:未捕获异常时 Starlette 返回的是纯文本 500,`r.json()` 会炸,原因就被吞成一句无信息的"失败"。
- 表单字段级校验不用 toast,用 `.field-err` 就地红字(错在哪写在哪个输入框下面)。
