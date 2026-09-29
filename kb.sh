#!/usr/bin/env bash
# Knowbase 的统一操作入口。取代原来 WeChatOA 的 start_all.sh。
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
WEB_PORT="${KB_WEB_PORT:-8787}"
WXDOWN_PORT="${WXDOWN_PROXY_PORT:-65000}"   # 跟 clash_wxdown_route 认同一个变量
WXDOWN_WS_PORT="${WXDOWN_WS_PORT:-65001}"
# pidfile 名字里带端口。脚本自己在端口冲突时就会劝你 KB_WEB_PORT=8788 换个端口跑,
# 要是所有端口共用一个 .run/web.pid,临时端口起过一次之后,默认 8787 的
# status/stop/restart 就会把临时端口那份也当成自己的——上一轮 pgrep 跨端口误杀
# 是同一个病根。
WXDOWN_PID="$RUN_DIR/wxdown-$WXDOWN_PORT.pid"
WEB_PID="$RUN_DIR/web-$WEB_PORT.pid"
API="http://127.0.0.1:$WEB_PORT"
# 所有 curl 都带超时。没有超时的话,旧实例端口还在监听但 HTTP 卡住时,
# stop/restart 会跟着一起卡死——恰好是最需要它把旧实例杀掉的场景。
CURL=(curl -s --connect-timeout 2 --max-time 5)

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
#
# 碰到 shell 就停:交互 shell 和 `bash -c '...'` 的命令行里可能正好带着这个字样
# (比如你在一条命令里提到 kb-web),再往上走就会把自己的终端当成服务杀掉。
top_owner() {   # top_owner <pid> <pattern>
  local pid="$1" pattern="$2" parent pcmd prog
  while :; do
    parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    { [ -n "$parent" ] && [ "$parent" != "1" ] && [ "$parent" != "0" ]; } || break
    pcmd="$(ps -o command= -p "$parent" 2>/dev/null)"
    printf '%s' "$pcmd" | grep -qF -- "$pattern" || break
    prog="${pcmd%% *}"      # 第一个词才是可执行文件,后面是参数
    prog="${prog##*/}"      # 取 basename;参数里的路径不能参与判断
    case "$prog" in
      bash|zsh|sh|dash|fish|ksh|-bash|-zsh|-sh|login) break ;;
    esac
    pid="$parent"
  done
  echo "$pid"
}

# 这个 pid 的命令行像不像目标服务
pid_matches() { ps -o command= -p "${1:-0}" 2>/dev/null | grep -qF -- "$2"; }

# 一个服务当前的进程。
#
# 刻意【不】用 `pgrep -f <名字>` 兜底:名字是全局匹配的,分不清端口。实测
# `pgrep -f kb-web` 会把跑在别的端口上的另一份实例也捞进来——8799 上做一次
# restart,把 8787 上的生产实例一起杀了。端口是唯一能把实例分开的东西。
#
# 所以端口说了算:有人占着这个端口,就以占着的那个为准,pidfile 不插话——它
# 可能是上次异常退出留下的陈迹,号早被系统回收给了别的进程。只有端口上一个人
# 都没有时才回头看 pidfile:那说明服务要么还在启动、要么挂住了没释放端口,
# 那份记录(pidfile 名字里带端口,只可能是本端口这一份写的)才有用。
#
# 两条路来的 pid 都要核命令行,而且核的是【往上走到头】的那个:lsof 给的常常是
# 子进程,命令行未必带服务的字样(wxdown 的 multiprocessing worker 就只是个
# `python -c ...`)。不核则更糟——65000 被别的东西占着时 start 会说"已在运行"
# 却什么都没起,接着 route --activate 把微信流量导到一个不是 wxdown 的端口上,
# 凭证于是永远抓不到,而面板一切正常。
service_pids() {   # service_pids <pidfile> <port> <pattern>
  local pidfile="${1:-}" port="${2:-}" pattern="${3:-}" pid owner raw=""
  [ -n "$port" ] && raw="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  [ -n "$raw" ] || raw="$(read_pid "$pidfile")"
  printf '%s\n' "$raw" | grep -E '^[0-9]+$' | sort -un | while read -r pid; do
    alive "$pid" || continue
    owner="$(top_owner "$pid" "$pattern")"
    pid_matches "$owner" "$pattern" && echo "$owner"
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
  if [ -z "$pids" ]; then
    if [ -n "$port" ] && port_busy "$port"; then
      # 端口有人占,但不是我们的进程。不动它:这条路径以前是无差别 kill -9,
      # 而占着 8787 的完全可能是另一个人的东西。
      say "[$label] 没在跑。端口 $port 被别的进程占着,没动它:"
      lsof -ti tcp:"$port" 2>/dev/null | while read -r pid; do
        ps -o pid=,command= -p "$pid" 2>/dev/null | sed 's/^/       /' >&2
      done
    else
      say "[$label] 本来就没在跑"
    fi
    rm -f "$pidfile"; return 0
  fi
  say "[$label] 停止 $(echo "$pids" | tr '\n' ' ')"
  for pid in $pids; do kill_tree "$pid"; done
  rm -f "$pidfile"
  local n=0
  while [ -n "$port" ] && port_busy "$port" && [ "$n" -lt 20 ]; do sleep 0.2; n=$((n+1)); done
  if [ -n "$port" ] && port_busy "$port"; then
    # 还占着就再查一遍是不是我们的残留;是才强杀,不是就放着(见上)。
    pids="$(service_pids "" "$port" "$pattern")"
    if [ -n "$pids" ]; then
      say "[$label] 端口 $port 仍被占用,强杀"
      echo "$pids" | xargs kill -9 2>/dev/null || true
      sleep 1
    fi
  fi
}

# ── wxdown:抓文章凭证的本地代理 ───────────────────────────────────────────────

# 认这个 wxdown 安装目录,而不是只认 main.py:它的 multiprocessing worker 命令行里
# 只有 `.venv/bin/python -c ...`,不带 main.py 字样。父进程要是先死了,worker 变成
# 孤儿,只认 main.py 就再也认不出它、也就清不掉。
WXDOWN_PAT="$WXDOWN_DIR/"

wxdown_start() {
  if [ -n "$(service_pids "$WXDOWN_PID" "$WXDOWN_PORT" "$WXDOWN_PAT")" ]; then
    say "[wxdown] 已在运行"; return 0
  fi
  [ -x "$WXDOWN_DIR/.venv/bin/python" ] || die "找不到 wxdown-service($WXDOWN_DIR)"
  if port_busy "$WXDOWN_PORT"; then
    die "端口 $WXDOWN_PORT 被别的进程占着(不是 wxdown),它起不来。
     换个端口:WXDOWN_PROXY_PORT=65010 ./kb.sh wxdown start"
  fi
  # 端口要显式传给它。只用 WXDOWN_PORT 做探测和 Clash 路由、却让 wxdown 按默认的
  # 65000/65001 起,改了端口就会一边按新端口找、一边在老端口上跑。
  daemonize "$WXDOWN_PID" "$LOG_DIR/wxdown.log" "$WXDOWN_DIR" \
    "$WXDOWN_DIR/.venv/bin/python" "$WXDOWN_DIR/main.py" \
    -p "$WXDOWN_PORT" -w "$WXDOWN_WS_PORT"
  sleep 3
  alive "$(read_pid "$WXDOWN_PID")" \
    && say "[wxdown] 已启动(PID $(read_pid "$WXDOWN_PID"))" \
    || die "wxdown 起不来,看 $LOG_DIR/wxdown.log"
}

wxdown_stop()    { stop_service wxdown "$WXDOWN_PID" "$WXDOWN_PORT" "$WXDOWN_PAT"; }
wxdown_restart() { wxdown_stop; wxdown_start; }

wxdown_status() {
  local pids; pids="$(service_pids "$WXDOWN_PID" "$WXDOWN_PORT" "$WXDOWN_PAT")"
  if [ -n "$pids" ]; then echo "wxdown    运行中 (PID $(echo "$pids" | tr '\n' ' '))"
  elif port_busy "$WXDOWN_PORT"; then
    echo "wxdown    未运行(端口 $WXDOWN_PORT 被别的进程占着)"
  else echo "wxdown    未运行"; fi
}

# ── kb-web:知识库面板(耗时的活都在它进程里跑)────────────────────────────────

WEB_PAT="kb-web"

web_start() {
  if [ -n "$(service_pids "$WEB_PID" "$WEB_PORT" "$WEB_PAT")" ]; then
    say "[web] 已在 $API"; graph_stale; return 0
  fi
  # 端口有人占但不是 kb-web:说清楚,别等它"起完"再报一个莫名其妙的超时。
  if port_busy "$WEB_PORT"; then
    die "端口 $WEB_PORT 被别的进程占着,kb-web 起不来。
     换个端口:KB_WEB_PORT=8788 ./kb.sh web start"
  fi
  # env -i 干净环境:继承外层(尤其是嵌套 claude)的环境变量会导致 Not logged in
  daemonize "$WEB_PID" "$LOG_DIR/web.log" "$ROOT" \
    env -i HOME="$HOME" PATH="$PATH" PORT="$WEB_PORT" uv run kb-web
  local n=0
  while ! port_busy "$WEB_PORT" && [ "$n" -lt 40 ]; do sleep 0.5; n=$((n+1)); done
  port_busy "$WEB_PORT" || die "kb-web 起不来,看 $LOG_DIR/web.log"
  say "[web] $API"
  # 图谱页是 Vault 里的产物,不是 kb-web 现渲染的:样式改了得重新生成,提示一句
  graph_stale
}

# 管道跑在 kb-web 进程里,杀了它这一轮同步就断在半路(采集到一半的文章要重抓)。
# 所以默认拦一下,给一句怎么强来——而不是二话不说砍掉一个可能跑了半小时的活。
FORCE=""

web_stop() {
  if [ -z "$FORCE" ]; then
    local st=0
    pipeline_running || st=$?
    if [ "$st" = 0 ]; then
      die "同步正在跑,现在停会把它断在半路。
     等它跑完(./kb.sh status 看进度),或者确实要停就加 --force"
    fi
    if [ "$st" = 2 ]; then
      die "问不出同步在不在跑($API 没应答),不敢就这么停——它可能正跑到一半。
     过一会儿再试,或者确实要停就加 --force"
    fi
  fi
  stop_service kb-web "$WEB_PID" "$WEB_PORT" "$WEB_PAT"
}

web_restart() { web_stop; web_start; }

web_status() {
  local pids; pids="$(service_pids "$WEB_PID" "$WEB_PORT" "$WEB_PAT")"
  if [ -n "$pids" ]; then echo "kb-web    运行中 ($API)"
  elif port_busy "$WEB_PORT"; then echo "kb-web    未运行(端口 $WEB_PORT 被别的进程占着)"
  else echo "kb-web    未运行"; fi
}

# ── 管道:把耗时的活丢给 kb-web,命令立刻返回 ──────────────────────────────────

# 0 = 在跑,1 = 确定空闲,2 = 问不出来(超时 / 没应答 / 返回的不是 JSON)。
#
# 三态是必须的:停机保护只有在"确定空闲"时才该放行。以前问不出来一律当空闲,
# 于是状态接口慢一点,stop 就把正在跑的同步砍了——而它天然就会慢,因为要顺带
# 探采集器(冷缓存时几十秒),恰好超过 curl 那 5 秒。所以这里还要 source=false:
# 只问"在不在跑",那是内存里现成的一个布尔值,不该被来源探测拖下水。
pipeline_running() {
  port_busy "$WEB_PORT" || return 1
  local out
  out="$("${CURL[@]}" "$API/api/pipeline/status?source=false" 2>/dev/null)" || return 2
  [ -n "$out" ] || return 2
  printf '%s' "$out" | "$BOOT_PY" -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(2)
sys.exit(0 if d.get("running") else 1)'
}

# 组装 /api/pipeline/run 的请求体。stages 为空 = 全流程
pipeline_run() {   # pipeline_run <stages-json|""> [--since d --until d --targets a,b]
  local stages="$1"; shift
  local since="" until="" targets="" channel=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --since)   since="${2:-}"; shift 2 ;;
      --until)   until="${2:-}"; shift 2 ;;
      --targets) targets="${2:-}"; shift 2 ;;
      --channel) channel="${2:-}"; shift 2 ;;
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
stages, since, until, targets, channel = sys.argv[1:6]
b = {}
if stages:  b["stages"] = json.loads(stages)
if since:   b["since_date"] = since
if until:   b["until_date"] = until
if targets: b["accounts"] = [t for t in targets.split(",") if t]
if channel: b["channel"] = channel
print(json.dumps(b))' "$stages" "$since" "$until" "$targets" "$channel")"

  local out
  out="$("${CURL[@]}" -w '\n%{http_code}' -X POST "$API/api/pipeline/run" \
    -H 'Content-Type: application/json' -d "$body")"
  case "$(echo "$out" | tail -1)" in
    200) say "已在后台开始。进度看 $API 管道 tab,或跑 ./kb.sh status" ;;
    *)   die "启动失败:$(echo "$out" | sed '$d')" ;;
  esac
}

# ── 图谱页 ────────────────────────────────────────────────────────────────────
# 三张图谱是「CSS/JS 内联进单文件 HTML」的产物,住在 Vault 里,kb-web 只是把文件
# 原样发出去。所以改了 catgraph.css/js 或那几个构建脚本,**重启不会让页面变样**,
# 得重新生成。跑同步时 wiki 阶段会顺手带上;这条命令是「只想立刻看到新样式」时的
# 快捷方式:--no-llm 只吃已有归类缓存,零模型调用,实测约 6 秒(其中 5.5 秒是读
# 那棵 5530 组的条目树)。
#
# **故意没做成 restart 的一部分。** restart 现在是纯进程操作,失败了顶多服务没起来,
# 磁盘上一个字节不动;塞进去之后每次重启都在**原子替换 Vault 里三张正式页面**,而
# build_catgraph 里那一整套两阶段提交 / 发布 CAS / 质量验收,存在的理由就是这个动作
# 有风险 —— 重启一天十次就是对正式产物发布十次,还会跟正在跑的 wiki 阶段抢发布锁
# (--force 停服务时管道拦不住)。所以这里只在 web 起来之后**提示一句**,不代劳。
GRAPH_SRC=(scripts/wiki/assets/catgraph.css scripts/wiki/assets/catgraph.js
           scripts/wiki/catgraph_page.py scripts/wiki/build_catgraph.py
           scripts/wiki/graph_data.py scripts/wiki/taxo_tables.py
           scripts/wiki/build_graph_html.py)

graph_stale() {   # 源文件比图谱页新就提示一句。永远返回 0:它只是提示,不能挡住启动
  "$BOOT_PY" - "$ROOT" "${GRAPH_SRC[@]}" <<'PY' || true
import os, pathlib, sys
try:
    root, srcs = pathlib.Path(sys.argv[1]), sys.argv[2:]
    vault = pathlib.Path(os.environ.get("KB_VAULT_ROOT")
                         or pathlib.Path.home() / "obs" / "new-start").expanduser()
    pages = [vault / n for n in ("graph.html", "graph-projects.html", "graph-skills.html")]
    if all(p.is_file() for p in pages):   # 还没生成过就不催:跑一次 wiki 阶段就有了
        newest = max((root / s).stat().st_mtime for s in srcs if (root / s).is_file())
        if newest > min(p.stat().st_mtime for p in pages):
            print("[graph] 图谱页比样式/构建源旧了 —— ./kb.sh graph 重建"
                  "(约 6 秒,零模型调用)", file=sys.stderr)
except Exception:
    pass                                  # 提示而已,读不到就算了,别把启动带崩
PY
  return 0
}

cmd_graph() {
  # 数据目录跟 kb/config.py 和 scripts/wiki/taxonomy.py 一个口径:KB_DATA_DIR
  # 优先,没设才回退仓库内的 data/。两个构建器本来就按这个环境变量找库和归类
  # 缓存,前置检查要是只认 $ROOT/data,外置数据目录下就会在进构建器之前误报
  # 「缺文件」——数据明明是全的,三张图谱一张也重建不了。
  #
  # **算出来的路径要规范化之后再跟仓库 data/ 比。** `pathlib.Path` 去得掉 `./`,
  # 去不掉 `..`,也不消解符号链接 —— 拿原始字符串比的话,`$ROOT/data/../data` 和
  # 一条指回 `data/` 的软链接都是同一个目录的另一种写法,却跟 `$ROOT/data` 不相等,
  # 下面那道 worktree 闸门当场被绕过去(2026-09-29 复审第 2 轮实测:worktree 里
  # 这么写,话题图谱拿 135 KB 的测试库发布了一张 0 节点页面,还以退出码 0 报「好了」)。
  local resolved; resolved="$("$BOOT_PY" -c '
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
d = pathlib.Path(os.environ.get("KB_DATA_DIR") or (root / "data")).expanduser()
if not d.is_absolute():
    d = root / d                      # 构建器的 cwd 就是 $ROOT,相对路径按它解
d = os.path.realpath(d)               # 消解 .. 和符号链接,别名不能换个写法就变一家
print(d)
print("repo" if d == os.path.realpath(root / "data") else "external")
' "$ROOT")"
  local datadir="${resolved%%$'\n'*}" datakind="${resolved##*$'\n'}"
  # **仓库内的 data/ 只许在主检出跑。** 它在 .gitignore 里、只有主检出有;worktree
  # 里要么没有、要么是跑测试留下的空壳——照跑的后果实测过:话题图谱拿 135 KB 的
  # 测试库建出一张 0 节点的页面,**原子替换掉 Vault 里那张好的**(它自己不做质量
  # 验收,catgraph 那套闸门管不着它)。判据用 git 的 common-dir:worktree 的 .git
  # 是个文件,指向主检出那一份。KB_DATA_DIR 指到仓库外时这条前提不成立(数据是
  # 显式挂的、跟检出无关),那时只靠下面那道归类缓存检查把空目录挡住。
  if [ "$datakind" = "repo" ]; then
    local common; common="$(git -C "$ROOT" rev-parse --path-format=absolute \
                              --git-common-dir 2>/dev/null || true)"
    if [ -n "$common" ] && [ "$common" != "$ROOT/.git" ]; then
      die "这是 worktree,data/(库 + 归类缓存)只有主检出有,照跑会拿空库覆盖掉好页面。
     去主检出跑:cd ${common%/.git} && ./kb.sh graph"
    fi
  fi
  local f
  for f in taxonomy_projects.json taxonomy_skills.json; do
    [ -f "$datadir/$f" ] || die "缺 $datadir/$f —— 归类缓存不在,--no-llm 会把几千条判成「待归类」。
     先跑一次完整同步(wiki 阶段会重建缓存),或者 ./kb.sh graph 之前确认数据目录挂对了"
  done
  local since; since="$("$BOOT_PY" -c \
    'import datetime;print((datetime.date.today()-datetime.timedelta(days=14)).isoformat())')"
  say "[graph] 重建三张图谱(--no-llm:只吃已有归类缓存,不调模型)…"
  ( cd "$ROOT" && uv run python scripts/wiki/build_graph_html.py --since "$since" ) \
    || die "话题图谱没建成(原因在上面)"
  # 分类图谱会在「新图明显不如旧图」时拒绝发布并非零退出 —— 那是保护不是故障,
  # 确实该缩的那次(比如故意收紧分类规则)加 --allow-shrink。
  ( cd "$ROOT" && uv run python scripts/wiki/build_catgraph.py --kind all --no-llm "$@" ) \
    || die "分类图谱没发布(原因在上面)。拒绝发布是保护:确实该缩就加 --allow-shrink"
  say "[graph] 好了。刷新 /graph、/graph/projects、/graph/skills"
}

# ── 组合动作 ──────────────────────────────────────────────────────────────────

cmd_status() {
  echo "── 服务 ──"
  web_status
  wxdown_status
  echo
  echo "── 同步进度 ──"
  if port_busy "$WEB_PORT"; then
    "${CURL[@]}" "$API/api/pipeline/status" 2>/dev/null | "$BOOT_PY" -c '
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
  echo "── 绿泡泡流量路由 ──"
  cli route 2>&1 | grep -o '"message": "[^"]*"' | cut -d'"' -f4 || echo "(读不到 Clash)"
  echo
  echo "── 扫码凭证(只用来搜号加号)──"
  cli status 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("message", ""))
if d.get("hint"): print("  " + d["hint"])' \
    || echo "(采集器起不来)"
  echo
  echo "── 文章凭证(每个号各一份)──"
  cli creds --json 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
extra = "(本次新导入 %d 条)" % d["imported"] if d["imported"] else ""
n = lambda s: sum(1 for a in d["accounts"] if a["state"] == s)
print("%d/%d 个号凭证新鲜%s" % (d["ready"], d["total"], extra))
print("  放太久 %d · 已失效 %d · 没凭证 %d(前两类采集时照样会试一把)"
      % (n("stale"), n("expired"), n("missing")))' \
    || echo "(读不到)"
}

# 同步前的准备。已经在跑的先杀掉重起,保证是干净的一份
cmd_prep() {
  wxdown_restart
  say "[route] 切绿泡泡文章流量到 wxdown…"
  cli route --activate >/dev/null 2>&1 || say "[route] 切换失败,自己看 ./kb.sh route"
  web_start
  echo
  cli creds 2>&1 | head -1 || true
  echo
  say "起好了,终端可以关。"
  say "接下来去 Mac 绿泡泡里逐个打开公众号的文章,在 $API"
  say "管道 tab →「管理公众号」看还差谁(自动刷新),点得差不多了就「同步」。"
  say "凭证放过 20 分钟会转黄:那只是没把握,采集照样会试,试通了自己转绿。"
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
Knowbase 操作入口 —— 每条命令跑完就还你终端,不用挂着

  日常
    ./kb.sh prep                    同步前:重起 wxdown + 切路由 + 起面板
    ./kb.sh sync                    跑全流程(采集→导出→索引→打标),后台跑
    ./kb.sh sync --channel wechat_local  用绿泡泡本地数据库发现文章
    ./kb.sh sync --channel wechat_oa     用原凭证通道(默认)
    ./kb.sh status                  服务 / 同步进度 / 路由 / 凭证 一屏看全

  服务(都是后台守护进程;restart 会先杀掉在跑的那份,不管是谁起的)
    ./kb.sh start | stop | restart        两个服务一起
    ./kb.sh web    start|stop|restart|status     面板 8787
    ./kb.sh wxdown start|stop|restart|status     抓凭证的代理 65000/65001
    ./kb.sh logs [web|wxdown] [行数]
    (同步正在跑时 stop/restart 会拦一下,加 --force 强来)

  单跑某一步(都是后台跑,立刻返回)
    ./kb.sh collect [--since 2026-08-01 --until 2026-08-26] [--targets a,b]
                   [--channel wechat_local|wechat_oa]
    ./kb.sh export
    ./kb.sh index
    ./kb.sh extract
    ./kb.sh graph [--allow-shrink]  重建三张图谱页(前台约 6 秒,零模型调用)
                   图谱是 Vault 里的单文件 HTML,样式改了要重新生成——重启不管用。
                   跑同步时 wiki 阶段本来就会带上,这条是「只想立刻看到新样式」用的。

  凭证与订阅(瞬间返回)
    ./kb.sh creds [--missing-only]  每个号的文章凭证够不够(顺带从 wxdown 导入)
    ./kb.sh route [--activate]      绿泡泡文章流量有没有走 wxdown
    ./kb.sh accounts list|search|add|remove|enable|disable [名字]
    ./kb.sh setup                   新 worktree 首次准备

  这两条会占着终端,因为它们本来就是要你盯着的:
    ./kb.sh login                   扫码续期(只有搜号加号用得到)
    ./kb.sh watch                   每 5 秒刷新,只列还没拿到凭证的号

  两套凭证别搞混:login 那套是扫码拿的、全局一份,现在只剩「搜公众号加号」
  一个用途;creds 那套是每个号各一份的文章凭证,只能靠在 Mac 绿泡泡里打开该号
  一篇文章、由 wxdown 截下来。原通道取列表靠后者,login 救不了;
  本地数据库通道无需它,两通道都按文章链接获取正文。
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
  graph)   cmd_graph "$@" ;;

  creds|route|login|accounts) cli "$ACTION" "$@" ;;
  setup)   "$COLLECTOR/setup.sh" ;;
  -h|--help|help) usage ;;
  *) die "不认识的命令 $ACTION(跑 ./kb.sh 看列表)" ;;
esac
