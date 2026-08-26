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
  if port_busy 65000; then
    say "[wxdown] 65000 已被占用,当作已在运行(不是本脚本起的)"; return 0
  fi
  [ -x "$WXDOWN_DIR/.venv/bin/python" ] || die "找不到 wxdown-service($WXDOWN_DIR)"
  ( cd "$WXDOWN_DIR" && \
    PYTHONUNBUFFERED=1 nohup "$WXDOWN_DIR/.venv/bin/python" "$WXDOWN_DIR/main.py" \
      >> "$LOG_DIR/wxdown.log" 2>&1 & echo $! > "$WXDOWN_PID" )
  sleep 3
  alive "$(read_pid "$WXDOWN_PID")" \
    && say "[wxdown] 已启动(日志 $LOG_DIR/wxdown.log)" \
    || die "wxdown 起不来,看 $LOG_DIR/wxdown.log"
}

wxdown_stop() {
  kill_tree "$(read_pid "$WXDOWN_PID")" "wxdown"
  rm -f "$WXDOWN_PID"
}

wxdown_status() {
  local pid; pid="$(read_pid "$WXDOWN_PID")"
  if alive "$pid"; then echo "wxdown    运行中 (PID $pid)"
  elif port_busy 65000; then echo "wxdown    运行中 (不是本脚本起的)"
  else echo "wxdown    未运行"; fi
}

# ── kb-web:知识库面板 ─────────────────────────────────────────────────────────

web_start() {
  if port_busy "$WEB_PORT"; then say "[web] $WEB_PORT 已在服务"; return 0; fi
  # env -i 干净环境:继承外层(尤其是嵌套 claude)的环境变量会导致 Not logged in
  ( cd "$ROOT" && env -i HOME="$HOME" PATH="$PATH" PORT="$WEB_PORT" \
      nohup uv run kb-web >> "$LOG_DIR/web.log" 2>&1 & echo $! > "$WEB_PID" )
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

# 同步前的准备:把该开的开好,然后盯着凭证一个个变绿
cmd_prep() {
  wxdown_start
  say "[route] 切微信文章流量到 wxdown…"
  cli route --activate >/dev/null 2>&1 || say "[route] 切换失败,自己看 ./kb.sh route"
  web_start
  echo
  say "接下来:在 Mac 微信里逐个打开公众号的任意一篇文章,下面会实时显示还差谁。"
  say "(也可以到面板 http://127.0.0.1:$WEB_PORT 管道 tab →「管理公众号」看,那边会自动刷新)"
  say "按 Ctrl-C 结束盯梢。"
  echo
  cmd_watch
}

# 边点边看:每 5 秒把 wxdown 新抓到的凭证并进来,只列还没就绪的
cmd_watch() {
  while true; do
    clear
    cli creds --missing-only 2>&1 | head -40
    echo
    echo "(每 5 秒刷新,Ctrl-C 退出)"
    sleep 5
  done
}

usage() {
  cat >&2 <<'EOF'
knowledge-base 操作入口

  日常两步
    ./kb.sh prep                    同步前:起 wxdown + 切路由 + 起面板,然后盯着
                                    凭证变绿(你去 Mac 微信里逐个点文章)
    ./kb.sh status                  一屏看全:服务 / 路由 / 凭证 / 订阅

  服务(只有 wxdown 是常驻的,采集本身是一次性任务)
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
  prep)    cmd_prep ;;
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
