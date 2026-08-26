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

## collector 采集器

- **新开的 worktree 要先跑一次 `./collector/setup.sh`**。`collector/data`(592M 文章库
  + 凭证)和 `collector/.venv` 都在 .gitignore 里,worktree 只会带源码,不跑这一步
  采集阶段会报"采集器起不来"。脚本幂等,链的是仓库外的同一份真实数据。
- 采集器是**一次性 CLI,不是常驻服务**:`cd collector && ./.venv/bin/python -m cli <命令>`。
  常用 `status`(健康+订阅列表) / `collect` / `export` / `accounts list|add|remove` / `login`。
  stdout 只出 NDJSON 给程序读,人看的提示走 stderr。
- **公众号列表**存在 `collector/data/wechatoa.db` 的 `accounts` 表,日常维护走
  kb-web 管道 tab 的「管理公众号」弹窗(搜索添加 / 停用 / 移除),不要直接改库。
  终端等价物是 `cli accounts list|search|add|remove|enable|disable`;面板走的就是它。
  搜索添加需要扫码凭证(`cli login`),停用/移除只动本地库,凭证过期也能用。
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
