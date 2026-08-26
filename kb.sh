#!/usr/bin/env bash
# knowledge-base 的统一操作入口。取代原来 WeChatOA 的 start_all.sh
# ——那里管的三个常驻服务(后端 8000 / 前端 5173 / wxdown)现在只剩 wxdown 一个:
# 采集改成了一次性 CLI,凭证状态改在知识库面板上看。
#
#   ./kb.sh                 看所有命令
#   ./kb.sh status          一屏看全:服务 / 凭证 / 订阅 / 待处理
#   ./kb.sh prep            同步前的准备(起 wxdown + 切路由 + 盯凭证)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COLLECTOR="$ROOT/collector"
PY="$COLLECTOR/.venv/bin/python"
WXDOWN_DIR="${WXDOWN_DIR:-$HOME/code/gongzhonghao/wxdown-service}"
RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/runtime_logs"
WXDOWN_PID="$RUN_DIR/wxdown.pid"
WEB_PID="$RUN_DIR/web.pid"
WEB_PORT="${KB_WEB_PORT:-8787}"
# 代理端口跟 clash_wxdown_route 认的同一个环境变量
WXDOWN_PORT="${WXDOWN_PROXY_PORT:-65000}"

# daemonize 用的解释器,随便哪个都行,只要能 fork+setsid
BOOT_PY="$PY"
[ -x "$BOOT_PY" ] || BOOT_PY="$(command -v python3 || echo /usr/bin/python3)"

mkdir -p "$RUN_DIR" "$LOG_DIR"

say()  { echo "$*" >&2; }
die()  { echo "错误:$*" >&2; exit 1; }
cli()  { (cd "$COLLECTOR" && "$PY" -m cli "$@"); }

# ── 进程小工具 ────────────────────────────────────────────────────────────────
# 只 kill 单个 PID 不够:wxdown 的 main.py 只在 SIGINT 时收拾 multiprocessing
# 子进程,收到 SIGTERM 会直接死掉,把监听 65000/65001 的两个子进程丢成孤儿。
# 所以趁父进程还活着先把整棵树收集下来——它一旦先死,后代被 init 收养就追不回了。

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

read_pid() { [ -f "$1" ] && tr -cd '0-9' < "$1" || true; }

# 起一个真正脱离终端的后台进程。为什么不能只用 `nohup cmd &`:
#   1. 那样起的进程跟脚本同一个进程组。实测它能扛住 Ctrl-C——非交互 shell 会给
#      后台任务把 SIGINT 设成 SIG_IGN——但坏处是这个 SIG_IGN 被子进程继承下去,
#      于是 stop 时的 `kill -INT` 也一并被忽略,只能降级用 SIGTERM;而 wxdown 只在
#      SIGINT 时收拾 multiprocessing 子进程,SIGTERM 会把 65000/65001 丢成孤儿。
#   2. macOS 没有 setsid 命令,所以借 python 来 fork + setsid。
# 结果:自成会话(终端关了、Ctrl-C 都波及不到)且 SIGINT 恢复默认(stop 能优雅收尾)。
daemonize() {   # daemonize <pidfile> <logfile> <workdir> <cmd...>
  local pidfile="$1" logfile="$2" workdir="$3"; shift 3
  "$BOOT_PY" -c '
import os, signal, sys
pidfile, logfile, workdir, *cmd = sys.argv[1:]
if os.fork():                       # 父进程立刻返回,不挡住调用方
    sys.exit(0)
os.setsid()                         # 自成会话
pid = os.fork()
if pid:
    with open(pidfile, "w") as f:
        f.write(str(pid))
    sys.exit(0)
signal.signal(signal.SIGINT, signal.SIG_DFL)    # 让 stop 时的 kill -INT 有效
os.chdir(workdir)
fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
os.execvp(cmd[0], cmd)
' "$pidfile" "$logfile" "$workdir" "$@"
  # pidfile 是中间那层进程写的,比调用方返回晚一点点
  local n=0
  while [ ! -s "$pidfile" ] && [ "$n" -lt 20 ]; do sleep 0.1; n=$((n+1)); done
}

collect_tree() {
  local pid="${1:-}" child
  [ -n "$pid" ] || return 0
  echo "$pid"
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do collect_tree "$child"; done
}

kill_tree() {
  local root="${1:-}" label="${2:-}" pids pid n=0
  alive "$root" || return 0
  pids="$(collect_tree "$root")"
  say "[stop] 停止 ${label}(PID $(echo "$pids" | tr '\n' ' '))"
  for pid in $pids; do kill -INT  "$pid" 2>/dev/null || true; done
  while [ "$n" -lt 25 ]; do
    alive "$root" || return 0
    sleep 0.2; n=$((n+1))
  done
  for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
  sleep 1
  for pid in $pids; do kill -KILL "$pid" 2>/dev/null || true; done
}

port_busy() { lsof -ti tcp:"$1" >/dev/null 2>&1; }

# ── wxdown:抓文章凭证的本地代理 ───────────────────────────────────────────────

wxdown_start() {
  local pid; pid="$(read_pid "$WXDOWN_PID")"
  if alive "$pid"; then say "[wxdown] 已在运行(PID $pid)"; return 0; fi
  if port_busy "$WXDOWN_PORT"; then
    say "[wxdown] $WXDOWN_PORT 已被占用,当作已在运行(不是本脚本起的)"; return 0
  fi
  [ -x "$WXDOWN_DIR/.venv/bin/python" ] || die "找不到 wxdown-service($WXDOWN_DIR)"
  daemonize "$WXDOWN_PID" "$LOG_DIR/wxdown.log" "$WXDOWN_DIR" \
    "$WXDOWN_DIR/.venv/bin/python" "$WXDOWN_DIR/main.py"
  sleep 3
  alive "$(read_pid "$WXDOWN_PID")" \
    && say "[wxdown] 已启动(PID $(read_pid "$WXDOWN_PID"),日志 $LOG_DIR/wxdown.log)" \
    || die "wxdown 起不来,看 $LOG_DIR/wxdown.log"
}

wxdown_stop() {
  kill_tree "$(read_pid "$WXDOWN_PID")" "wxdown"
  rm -f "$WXDOWN_PID"
}

wxdown_status() {
  local pid; pid="$(read_pid "$WXDOWN_PID")"
  if alive "$pid"; then echo "wxdown    运行中 (PID $pid)"
  elif port_busy "$WXDOWN_PORT"; then echo "wxdown    运行中 (不是本脚本起的)"
  else echo "wxdown    未运行"; fi
}

# ── kb-web:知识库面板 ─────────────────────────────────────────────────────────

web_start() {
  if port_busy "$WEB_PORT"; then say "[web] $WEB_PORT 已在服务"; return 0; fi
  # env -i 干净环境:继承外层(尤其是嵌套 claude)的环境变量会导致 Not logged in
  daemonize "$WEB_PID" "$LOG_DIR/web.log" "$ROOT" \
    env -i HOME="$HOME" PATH="$PATH" PORT="$WEB_PORT" uv run kb-web
  sleep 4
  port_busy "$WEB_PORT" \
    && say "[web] http://127.0.0.1:$WEB_PORT" \
    || die "kb-web 起不来,看 $LOG_DIR/web.log"
}

web_stop() {
  # 管道跑在 kb-web 进程内,跑着的时候杀会把这一轮同步拦腰截断
  local running
  running="$(curl -s "http://127.0.0.1:$WEB_PORT/api/pipeline/status" 2>/dev/null \
    | grep -o '"running":[^,]*' | head -1 || true)"
  case "$running" in *true*) die "管道正在跑,先等它跑完(或到面板上停)";; esac
  kill_tree "$(read_pid "$WEB_PID")" "kb-web"
  rm -f "$WEB_PID"
  # uv run 起的进程可能不在我们记的 PID 树上,补一刀
  lsof -ti tcp:"$WEB_PORT" 2>/dev/null | xargs kill 2>/dev/null || true
}

web_status() {
  if port_busy "$WEB_PORT"; then echo "kb-web    运行中 (http://127.0.0.1:$WEB_PORT)"
  else echo "kb-web    未运行"; fi
}

# ── 组合动作 ──────────────────────────────────────────────────────────────────

cmd_status() {
  echo "── 服务 ──"
  web_status
  wxdown_status
  echo
  echo "── 微信流量路由 ──"
  cli route 2>&1 | grep -o '"message": "[^"]*"' | cut -d'"' -f4 || echo "(读不到 Clash)"
  echo
  echo "── 扫码凭证(搜索/翻列表用)──"
  cli status 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("message",""))' \
    || echo "(采集器起不来)"
  echo
  echo "── 文章凭证(每个号各一份)──"
  # 不用 f-string:里面要引 d["ready"],而 3.11 的 f-string 不许嵌同款引号也不许反斜杠
  cli creds --json 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
extra = "(本次新导入 %d 条)" % d["imported"] if d["imported"] else ""
print("%d/%d 个号可采%s" % (d["ready"], d["total"], extra))' \
    || echo "(读不到)"
}

# 同步前的准备:把该开的开好就退出,终端可以关掉。
# 盯凭证默认交给面板(那边自带 5 秒自动刷新);想在终端里看就加 --watch。
cmd_prep() {
  wxdown_start
  say "[route] 切微信文章流量到 wxdown…"
  cli route --activate >/dev/null 2>&1 || say "[route] 切换失败,自己看 ./kb.sh route"
  web_start
  echo
  cli creds 2>&1 | head -1 || true
  echo
  say "都起好了,这个终端可以关掉——两个服务都自成会话,不跟着终端走。"
  say "接下来在 Mac 微信里逐个打开公众号的任意一篇文章,到"
  say "  http://127.0.0.1:$WEB_PORT  管道 tab →「管理公众号」"
  say "看还差谁(每 5 秒自动把新抓到的凭证并进来,全绿了会提示)。"
  say "全绿之后点「同步」即可。想在终端里盯就跑 ./kb.sh watch。"
  if [ "${1:-}" = "--watch" ]; then echo; cmd_watch; fi
}

# 边点边看:每 5 秒把 wxdown 新抓到的凭证并进来,只列还没就绪的
cmd_watch() {
  while true; do
    clear
    # || true:一次读取失败(采集器正忙、库被锁)不该让整个盯梢循环退出,
    # 错误文案照样打在屏幕上,下一轮自然重试
    cli creds --missing-only 2>&1 | head -40 || true
    echo
    echo "(每 5 秒刷新,Ctrl-C 退出;服务是后台守护进程,退出盯梢不影响它们)"
    sleep 5
  done
}

usage() {
  cat >&2 <<'EOF'
knowledge-base 操作入口

  日常两步
    ./kb.sh prep [--watch]          同步前:起 wxdown + 切路由 + 起面板,起完就退出
                                    (服务自成会话,终端可以关。加 --watch 才在终端盯)
    ./kb.sh status                  一屏看全:服务 / 路由 / 凭证 / 订阅

  服务(都是后台守护进程,不占终端;只有 wxdown 需要常驻,采集本身是一次性任务)
    ./kb.sh web    start|stop|status      知识库面板 8787
    ./kb.sh wxdown start|stop|status      抓文章凭证的代理 65000/65001
    ./kb.sh up                            两个都起
    ./kb.sh down                          两个都停

  凭证
    ./kb.sh creds [--missing-only]  每个号的文章凭证够不够(顺带从 wxdown 导入)
    ./kb.sh watch                   每 5 秒刷新,只列还没就绪的
    ./kb.sh route [--activate]      微信文章流量有没有走 wxdown
    ./kb.sh login                   扫码续期(搜索公众号、翻文章列表用的那套)

  采集(等价于在面板上点同步)
    ./kb.sh collect [--since 2026-08-01 --until 2026-08-26] [--targets a,b]
    ./kb.sh export [--no-images]
    ./kb.sh accounts list|search|add|remove|enable|disable [名字]

  知识库
    ./kb.sh index [--no-vector]     增量索引
    ./kb.sh extract [--writeback]   LLM 打标
    ./kb.sh setup                   新 worktree 首次准备(链 data 和 .venv)

  两套凭证别搞混:login 那套是扫码拿的,全局一份,用来搜号和翻列表;
  creds 那套是每个号各一份,只能靠在 Mac 微信里打开文章、由 wxdown 截下来。
EOF
}

# ── 分发 ──────────────────────────────────────────────────────────────────────

[ $# -gt 0 ] || { usage; exit 0; }
ACTION="$1"; shift

case "$ACTION" in
  status)  cmd_status ;;
  prep)    cmd_prep "${1:-}" ;;
  watch)   cmd_watch ;;
  up)      wxdown_start; web_start ;;
  down)    web_stop; wxdown_stop ;;
  web)
    case "${1:-status}" in
      start) web_start ;; stop) web_stop ;;
      restart) web_stop; web_start ;; status) web_status ;;
      *) die "web 只接 start|stop|restart|status" ;;
    esac ;;
  wxdown)
    case "${1:-status}" in
      start) wxdown_start ;; stop) wxdown_stop ;;
      restart) wxdown_stop; wxdown_start ;; status) wxdown_status ;;
      *) die "wxdown 只接 start|stop|restart|status" ;;
    esac ;;
  creds|route|login|collect|export|accounts)
    cli "$ACTION" "$@" ;;
  index|extract|search|ask|stats)
    (cd "$ROOT" && uv run kb "$ACTION" "$@") ;;
  setup)   "$COLLECTOR/setup.sh" ;;
  -h|--help|help) usage ;;
  *) die "不认识的命令 $ACTION(跑 ./kb.sh 看列表)" ;;
esac
