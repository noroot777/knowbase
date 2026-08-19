# knowledge-base 项目约定

## 代码文件行数上限(硬性)

- **大模型生成的代码文件,单文件不得超过 700 行。**
- 预计超过 700 行时,必须先拆分:按职责拆成多个文件(如 CSS/JS 按面板拆、Python 按模块拆),再分别生成。
- 读取大文件同理:超过 ~700 行的文件分块读(offset/limit),不要整读。

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
