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
- wxdown 把抓到的凭证写在 `wxdown-service/resources/data/credentials.json`,
  `cli creds` 直接读这个文件导入。原来那条 "wxdown WebSocket → 前端 :5173 →
  后端 :8000 POST" 的转发链路只是在搬运这个文件,两个常驻服务都已不需要。

## collector 采集器

- **新开的 worktree 要先跑一次 `./collector/setup.sh`**。`collector/data`(592M 文章库
  + 凭证)和 `collector/.venv` 都在 .gitignore 里,worktree 只会带源码,不跑这一步
  采集阶段会报"采集器起不来"。脚本幂等,链的是仓库外的同一份真实数据。
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
  那可能是推荐位里别人家的号。列表接口 getmsg 和 wxdown 的凭证都不带昵称,
  所以**一个号不发新文章就不会更新名字**。
- 别把 `collector/data` 或 `collector/.venv` 加进版本库:前者含 592M 库、264M 封面和
  带 token/cookie 的 `id_info.json`。它们现在是符号链接,`data/` 那条带斜杠的规则
  挡不住,所以 .gitignore 里另有不带斜杠的显式两行,别删。

## kb-web 服务

- 管道跑在 kb-web 进程内,杀 8787 前先 `curl -s http://127.0.0.1:8787/api/pipeline/status` 确认 `"running": false`。
- server.py / projects.py 改动要重启才生效;kb/static/ 下静态文件每次请求从磁盘读,免重启。
- 重启用 `env -i` 干净环境(继承嵌套 claude 的环境变量会导致 Not logged in)。
- 不要用 preview_start 托管 kb-web(会接管生命周期,杀掉管道)。

## 前端反馈(硬性)

- **禁止用原生 `alert` / `confirm` / `prompt`**,系统弹窗跟本项目的瓷白×墨蓝黑调子完全不搭,还阻塞主线程。
- 统一走 `kb/static/js/ui.js` 三个出口:
  - `toast(text, {type: err|ok|info, detail})` — 一次性操作反馈(失败/成功),右下浮起,自动消失。
  - `confirmBox({title, body, ok, cancel, danger})` — 破坏性动作确认,返回 `Promise<boolean>`,Esc / 点遮罩 = 取消。
  - `apiFail(r, fallback)` / `toastIfFail(r, text)` — 从失败的 `Response` 榨错误文案。**别再写 `(await r.json()).detail || '失败'`**:未捕获异常时 Starlette 返回的是纯文本 500,`r.json()` 会炸,原因就被吞成一句无信息的"失败"。
- 表单字段级校验不用 toast,用 `.field-err` 就地红字(错在哪写在哪个输入框下面)。
