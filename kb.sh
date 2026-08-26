#!/usr/bin/env bash
# knowledge-base 的统一操作入口。取代原来 WeChatOA 的 start_all.sh。
#
# 原则:每条命令都是跑完就还你终端。服务起成后台守护进程,耗时的活(采集、
# 导出、索引、打标)交给已经在后台的 kb-web 去跑,命令本身立刻返回。
# 只有两个例外,因为它们的本质就是要你盯着:login(扫码)和 watch(盯凭证)。
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
WXDOWN_PORT="${WXDOWN_PROXY_PORT:-65000}"   # 跟 clash_wxdown_route 认同一个变量
API="http://127.0.0.1:$WEB_PORT"

BOOT_PY="$PY"
[ -x "$BOOT_PY" ] || BOOT_PY="$(command -v python3 || echo /usr/bin/python3)"

mkdir -p "$RUN_DIR" "$LOG_DIR"

say() { echo "$*" >&2; }
die() { echo "错误:$*" >&2; exit 1; }
cli() { (cd "$COLLECTOR" && "$PY" -m cli "$@"); }

# ── 进程 ──────────────────────────────────────────────────────────────────────

alive()    { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
# 末尾那个 echo 不能省:tr -cd '0-9' 把换行也滤掉了,而 service_pids 要把它和
# lsof 的输出接在一起——不补换行,两个 pid 会粘成一个不存在的数字,
# 于是 status 报"未运行"、start 以为没在跑又起一份。
read_pid() { [ -f "$1" ] || return 0; tr -cd '0-9' < "$1"; echo; }
port_busy(){ lsof -ti tcp:"$1" >/dev/null 2>&1; }

# 起一个真正脱离终端的后台进程:自成会话 + SIGINT 恢复默认。
# 不用 `nohup cmd &` 的原因:那样起的进程跟脚本同一个进程组,非交互 shell 会把它的
# SIGINT 设成 SIG_IGN,而这个 SIG_IGN 被子进程继承——于是 stop 时的 kill -INT 也被
# 忽略,只能降级 SIGTERM,恰好会把 wxdown 的 multiprocessing 子进程丢成孤儿。
# macOS 没有 setsid 命令,所以借 python 做 fork + setsid。
daemonize() {   # daemonize <pidfile> <logfile> <workdir> <cmd...>
  local pidfile="$1" logfile="$2" workdir="$3"; shift 3
  "$BOOT_PY" -c '
import os, signal, sys
pidfile, logfile, workdir, *cmd = sys.argv[1:]
if os.fork():
    sys.exit(0)
os.setsid()
pid = os.fork()
if pid:
    with open(pidfile, "w") as f:
        f.write(str(pid))
    sys.exit(0)
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.chdir(workdir)
fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
os.execvp(cmd[0], cmd)
' "$pidfile" "$logfile" "$workdir" "$@"
  local n=0
  while [ ! -s "$pidfile" ] && [ "$n" -lt 20 ]; do sleep 0.1; n=$((n+1)); done
}

# 从一个进程往上找到这个服务的"头"。lsof 给的常常是子进程——`uv run kb-web` 起的
# 实际 server、wxdown 的 multiprocessing worker 都是——而 kill_tree 只往下走,
# 从子进程动手会把父进程剩在那儿(端口没释放,restart 于是失败)。
# 一路向上,直到父进程不再长得像这个服务为止。
top_owner() {   # top_owner <pid> <pattern>
  local pid="$1" pattern="$2" parent
  while :; do
    parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    { [ -n "$parent" ] && [ "$parent" != "1" ] && [ "$parent" != "0" ]; } || break
    ps -o command= -p "$parent" 2>/dev/null | grep -qF -- "$pattern" || break
    pid="$parent"
  done
  echo "$pid"
}

# 一个服务当前的进程:pidfile 记的 + 占着端口的,各自往上找到头,去重。
#
# 刻意【不】用 `pgrep -f <名字>` 兜底:名字是全局匹配的,分不清端口。实测
# `pgrep -f kb-web` 会把跑在别的端口上的另一份实例也捞进来——8799 上做一次
# restart,把 8787 上的生产实例一起杀了。端口是唯一能把实例分开的东西。
service_pids() {   # service_pids <pidfile> <port> <pattern>
  local pidfile="${1:-}" port="${2:-}" pattern="${3:-}" pid
  { [ -n "$pidfile" ] && read_pid "$pidfile"
    [ -n "$port" ] && lsof -ti tcp:"$port" 2>/dev/null
    true
  } | grep -E '^[0-9]+$' | sort -un | while read -r pid; do
    alive "$pid" && top_owner "$pid" "$pattern"
  done | sort -un || true
}

# 先序收集一个进程及其全部后代。必须趁父进程还活着时调用:
# 它一旦先死,后代被 init 收养(PPID=1),就再也追不回来了。
collect_tree() {
  local pid="${1:-}" child
  [ -n "$pid" ] || return 0
  echo "$pid"
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do collect_tree "$child"; done
}

# SIGINT → SIGTERM → SIGKILL 逐级升级。先 SIGINT 是因为 wxdown 只在 SIGINT 时
# 收拾自己的 multiprocessing 子进程,直接 SIGTERM 会把它们丢成孤儿。
kill_tree() {
  local root="${1:-}" pids pid n=0
  alive "$root" || return 0
  pids="$(collect_tree "$root")"
  for pid in $pids; do kill -INT  "$pid" 2>/dev/null || true; done
  while [ "$n" -lt 25 ]; do alive "$root" || break; sleep 0.2; n=$((n+1)); done
  if alive "$root"; then
    for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
    sleep 1
  fi
  for pid in $pids; do kill -KILL "$pid" 2>/dev/null || true; done
}

stop_service() {   # stop_service <名字> <pidfile> <port> <pattern>
  local label="$1" pidfile="$2" port="$3" pattern="$4" pids pid
  pids="$(service_pids "$pidfile" "$port" "$pattern")"
  if [ -z "$pids" ]; then say "[$label] 本来就没在跑"; rm -f "$pidfile"; return 0; fi
  say "[$label] 停止 $(echo "$pids" | tr '\n' ' ')"
  for pid in $pids; do kill_tree "$pid"; done
  rm -f "$pidfile"
  local n=0
  while [ -n "$port" ] && port_busy "$port" && [ "$n" -lt 20 ]; do sleep 0.2; n=$((n+1)); done
  if [ -n "$port" ] && port_busy "$port"; then
    say "[$label] 端口 $port 仍被占用,强杀"
    lsof -ti tcp:"$port" 2>/dev/null | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
}

# ── wxdown:抓文章凭证的本地代理 ───────────────────────────────────────────────

WXDOWN_PAT="$WXDOWN_DIR/main.py"

wxdown_start() {
  if [ -n "$(service_pids "$WXDOWN_PID" "$WXDOWN_PORT" "$WXDOWN_PAT")" ]; then
    say "[wxdown] 已在运行"; return 0
  fi
  [ -x "$WXDOWN_DIR/.venv/bin/python" ] || die "找不到 wxdown-service($WXDOWN_DIR)"
  daemonize "$WXDOWN_PID" "$LOG_DIR/wxdown.log" "$WXDOWN_DIR" \
    "$WXDOWN_DIR/.venv/bin/python" "$WXDOWN_DIR/main.py"
  sleep 3
  alive "$(read_pid "$WXDOWN_PID")" \
    && say "[wxdown] 已启动(PID $(read_pid "$WXDOWN_PID"))" \
    || die "wxdown 起不来,看 $LOG_DIR/wxdown.log"
}

wxdown_stop()    { stop_service wxdown "$WXDOWN_PID" "$WXDOWN_PORT" "$WXDOWN_PAT"; }
wxdown_restart() { wxdown_stop; wxdown_start; }

wxdown_status() {
  local pids; pids="$(service_pids "$WXDOWN_PID" "$WXDOWN_PORT" "$WXDOWN_PAT")"
  [ -n "$pids" ] && echo "wxdown    运行中 (PID $(echo "$pids" | tr '\n' ' '))" \
                 || echo "wxdown    未运行"
}

# ── kb-web:知识库面板(耗时的活都在它进程里跑)────────────────────────────────

WEB_PAT="kb-web"

web_start() {
  if port_busy "$WEB_PORT"; then say "[web] 已在 $API"; return 0; fi
  # env -i 干净环境:继承外层(尤其是嵌套 claude)的环境变量会导致 Not logged in
  daemonize "$WEB_PID" "$LOG_DIR/web.log" "$ROOT" \
    env -i HOME="$HOME" PATH="$PATH" PORT="$WEB_PORT" uv run kb-web
  local n=0
  while ! port_busy "$WEB_PORT" && [ "$n" -lt 40 ]; do sleep 0.5; n=$((n+1)); done
  port_busy "$WEB_PORT" && say "[web] $API" || die "kb-web 起不来,看 $LOG_DIR/web.log"
}

# 管道跑在 kb-web 进程里,杀了它这一轮同步就断在半路(采集到一半的文章要重抓)。
# 所以默认拦一下,给一句怎么强来——而不是二话不说砍掉一个可能跑了半小时的活。
FORCE=""

web_stop() {
  if [ -z "$FORCE" ] && pipeline_running; then
    die "同步正在跑,现在停会把它断在半路。
     等它跑完(./kb.sh status 看进度),或者确实要停就加 --force"
  fi
  stop_service kb-web "$WEB_PID" "$WEB_PORT" "$WEB_PAT"
}

web_restart() { web_stop; web_start; }

web_status() {
  port_busy "$WEB_PORT" && echo "kb-web    运行中 ($API)" || echo "kb-web    未运行"
}

# ── 管道:把耗时的活丢给 kb-web,命令立刻返回 ──────────────────────────────────

pipeline_running() {
  port_busy "$WEB_PORT" || return 1
  curl -s "$API/api/pipeline/status" 2>/dev/null | "$BOOT_PY" -c '
import json, sys
try: sys.exit(0 if json.load(sys.stdin).get("running") else 1)
except Exception: sys.exit(1)'
}

# 组装 /api/pipeline/run 的请求体。stages 为空 = 全流程
pipeline_run() {   # pipeline_run <stages-json|""> [--since d --until d --targets a,b]
  local stages="$1"; shift
  local since="" until="" targets=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --since)   since="${2:-}"; shift 2 ;;
      --until)   until="${2:-}"; shift 2 ;;
      --targets) targets="${2:-}"; shift 2 ;;
      *) die "不认识的参数 $1" ;;
    esac
  done

  web_start
  if pipeline_running; then
    die "管道已经在跑了(看 $API 管道 tab,或 ./kb.sh status)"
  fi

  local body
  body="$("$BOOT_PY" -c '
import json, sys
stages, since, until, targets = sys.argv[1:5]
b = {}
if stages:  b["stages"] = json.loads(stages)
if since:   b["since_date"] = since
if until:   b["until_date"] = until
if targets: b["accounts"] = [t for t in targets.split(",") if t]
print(json.dumps(b))' "$stages" "$since" "$until" "$targets")"

  local out
  out="$(curl -s -w '\n%{http_code}' -X POST "$API/api/pipeline/run" \
    -H 'Content-Type: application/json' -d "$body")"
  case "$(echo "$out" | tail -1)" in
    200) say "已在后台开始。进度看 $API 管道 tab,或跑 ./kb.sh status" ;;
    *)   die "启动失败:$(echo "$out" | sed '$d')" ;;
  esac
}

# ── 组合动作 ──────────────────────────────────────────────────────────────────

cmd_status() {
  echo "── 服务 ──"
  web_status
  wxdown_status
  echo
  echo "── 同步进度 ──"
  if port_busy "$WEB_PORT"; then
    curl -s "$API/api/pipeline/status" 2>/dev/null | "$BOOT_PY" -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: print("(读不到)"); raise SystemExit
if not d.get("running"):
    print("空闲")
else:
    for name, s in (d.get("stages") or {}).items():
        if s.get("status") in ("running", "error"):
            print("%s: %s %s" % (name, s.get("status"), s.get("detail", "")))
' || echo "(读不到)"
  else
    echo "(面板没开)"
  fi
  echo
  echo "── 微信流量路由 ──"
  cli route 2>&1 | grep -o '"message": "[^"]*"' | cut -d'"' -f4 || echo "(读不到 Clash)"
  echo
  echo "── 扫码凭证(搜号/翻列表用)──"
  cli status 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("message",""))' \
    || echo "(采集器起不来)"
  echo
  echo "── 文章凭证(每个号各一份)──"
  cli creds --json 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
extra = "(本次新导入 %d 条)" % d["imported"] if d["imported"] else ""
print("%d/%d 个号可采%s" % (d["ready"], d["total"], extra))' \
    || echo "(读不到)"
}

# 同步前的准备。已经在跑的先杀掉重起,保证是干净的一份
cmd_prep() {
  wxdown_restart
  say "[route] 切微信文章流量到 wxdown…"
  cli route --activate >/dev/null 2>&1 || say "[route] 切换失败,自己看 ./kb.sh route"
  web_start
  echo
  cli creds 2>&1 | head -1 || true
  echo
  say "起好了,终端可以关。"
  say "接下来去 Mac 微信里逐个打开公众号的文章,在 $API"
  say "管道 tab →「管理公众号」看还差谁(自动刷新),全绿后点「同步」。"
  say "或者直接 ./kb.sh sync 也行。"
}

cmd_watch() {
  while true; do
    clear
    cli creds --missing-only 2>&1 | head -40 || true
    echo
    echo "(每 5 秒刷新,Ctrl-C 退出;服务是后台守护进程,退出不影响它们)"
    sleep 5
  done
}

cmd_logs() {
  local f="${1:-web}"
  case "$f" in web|wxdown) ;; *) die "只有 web / wxdown 两份日志" ;; esac
  tail -n "${2:-40}" "$LOG_DIR/$f.log" 2>/dev/null || say "还没有 $f 的日志"
}

usage() {
  cat >&2 <<'EOF'
knowledge-base 操作入口 —— 每条命令跑完就还你终端,不用挂着

  日常
    ./kb.sh prep                    同步前:重起 wxdown + 切路由 + 起面板
    ./kb.sh sync                    跑全流程(采集→导出→索引→打标),后台跑
    ./kb.sh status                  服务 / 同步进度 / 路由 / 凭证 一屏看全

  服务(都是后台守护进程;restart 会先杀掉在跑的那份,不管是谁起的)
    ./kb.sh start | stop | restart        两个服务一起
    ./kb.sh web    start|stop|restart|status     面板 8787
    ./kb.sh wxdown start|stop|restart|status     抓凭证的代理 65000/65001
    ./kb.sh logs [web|wxdown] [行数]
    (同步正在跑时 stop/restart 会拦一下,加 --force 强来)

  单跑某一步(都是后台跑,立刻返回)
    ./kb.sh collect [--since 2026-08-01 --until 2026-08-26] [--targets a,b]
    ./kb.sh export
    ./kb.sh index
    ./kb.sh extract

  凭证与订阅(瞬间返回)
    ./kb.sh creds [--missing-only]  每个号的文章凭证够不够(顺带从 wxdown 导入)
    ./kb.sh route [--activate]      微信文章流量有没有走 wxdown
    ./kb.sh accounts list|search|add|remove|enable|disable [名字]
    ./kb.sh setup                   新 worktree 首次准备

  这两条会占着终端,因为它们本来就是要你盯着的:
    ./kb.sh login                   扫码续期(搜号/翻列表用的那套凭证)
    ./kb.sh watch                   每 5 秒刷新,只列还没拿到凭证的号

  两套凭证别搞混:login 那套是扫码拿的、全局一份,用来搜号和翻列表;
  creds 那套是每个号各一份的文章凭证,只能靠在 Mac 微信里打开该号一篇文章、
  由 wxdown 截下来。抓正文缺的是后者。
EOF
}

# ── 分发 ──────────────────────────────────────────────────────────────────────

[ $# -gt 0 ] || { usage; exit 0; }
ACTION="$1"; shift

# --force 可以出现在任何位置,择出来后剩下的参数照常传
ARGS=()
for a in "$@"; do
  [ "$a" = "--force" ] && FORCE=1 || ARGS+=("$a")
done
set -- ${ARGS+"${ARGS[@]}"}

svc() {   # svc <前缀> <动作>
  case "$2" in
    start) "$1_start" ;; stop) "$1_stop" ;;
    restart) "$1_restart" ;; status) "$1_status" ;;
    *) die "$1 只接 start|stop|restart|status" ;;
  esac
}

case "$ACTION" in
  status)  cmd_status ;;
  prep)    cmd_prep ;;
  watch)   cmd_watch ;;
  logs)    cmd_logs "$@" ;;

  start)   wxdown_start; web_start ;;
  stop)    web_stop; wxdown_stop ;;
  restart) web_stop; wxdown_stop; wxdown_start; web_start ;;
  web)     svc web "${1:-status}" ;;
  wxdown)  svc wxdown "${1:-status}" ;;

  sync)    pipeline_run "" "$@" ;;
  collect) pipeline_run '["crawl"]' "$@" ;;
  export)  pipeline_run '["export"]' ;;
  index)   pipeline_run '["index"]' ;;
  extract) pipeline_run '["extract"]' ;;

  creds|route|login|accounts) cli "$ACTION" "$@" ;;
  setup)   "$COLLECTOR/setup.sh" ;;
  -h|--help|help) usage ;;
  *) die "不认识的命令 $ACTION(跑 ./kb.sh 看列表)" ;;
esac
