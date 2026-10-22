#!/usr/bin/env bash
# Knowbase 的统一操作入口。取代原来 WeChatOA 的 start_all.sh。
#
# 原则:每条命令都是跑完就还你终端。服务起成后台守护进程,耗时的活(采集、
# 导出、索引、打标)交给已经在后台的 kb-web 去跑,命令本身立刻返回。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COLLECTOR="$ROOT/collector"
PY="$COLLECTOR/.venv/bin/python"
RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/runtime_logs"
WEB_PORT="${KB_WEB_PORT:-8787}"
# pidfile 名字里带端口。脚本自己在端口冲突时就会劝你 KB_WEB_PORT=8788 换个端口跑,
# 要是所有端口共用一个 .run/web.pid,临时端口起过一次之后,默认 8787 的
# status/stop/restart 就会把临时端口那份也当成自己的——上一轮 pgrep 跨端口误杀
# 是同一个病根。
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
# SIGINT 设成 SIG_IGN,而这个 SIG_IGN 被子进程继承——于是 stop 时的 kill -INT 被
# 悄悄忽略,只能一路升级到 SIGTERM/SIGKILL。而「先好好说一句」在这儿是有意义的:
# kb-web 进程里可能正跑着同步,握着 kb.db 的写事务、一个采集器子进程、一个向量
# 编码子进程。macOS 没有 setsid 命令,所以借 python 做 fork + setsid。
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

# 从一个进程往上找到这个服务的"头"。lsof 给的常常是子进程(`uv run kb-web` 起的
# 实际 server 就是),而 kill_tree 只往下走,从子进程动手会把父进程剩在那儿
# (端口没释放,restart 于是失败)。
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
# 子进程,命令行未必带服务的字样。不核则更糟——8787 被别的东西占着时 start 会说
# "已在运行"却什么都没起,接着 ./kb.sh sync 把同步请求 POST 给一个根本不是 kb-web
# 的进程,而这边一切看着正常。
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

# SIGINT → SIGTERM → SIGKILL 逐级升级。先 SIGINT 是给一次收拾自己的机会:
# 服务可能正跑着同步,直接 SIGKILL 会把 sqlite 的写事务和子进程甩在那儿。
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

# ── kb-web:知识库面板(耗时的活都在它进程里跑)────────────────────────────────

WEB_PAT="kb-web"

# env -i 干净环境:继承外层(尤其是嵌套 claude)的环境变量会导致 Not logged in
CLEAN_ENV=(env -i HOME="$HOME" PATH="$PATH")

# **起服务那条命令一律 --offline。** `uv run` 每次都要先确认一遍项目环境,而
# pyproject.toml 一动(改依赖、改注释都算)它就要重建那个 editable 包 —— 重建要解析
# `build-system.requires` 里的 hatchling,不带 --offline 时**即使 uv 缓存里已经有**
# 也要去 pypi 问一次最新版。于是网络一抖启动就挂在
# 「No solution found when resolving: hatchling」上,而这事发生在 daemonize 之后的
# 后台进程里:用户看到的只有一句「kb-web 起不来,看 web.log」,而日志尾巴是一段跟
# 知识库毫无关系的 tls handshake eof(2026-10-08 实测:pyproject 10-02 改过一次,
# 10-08 起服务时撞上,服务整天起不来;同一份缓存加上 --offline 当场就建好了)。
#
# 「要不要联网装东西」因此挪到前台来问(`web_prepare_env`):离线确认不成——真的新增
# 了依赖、或者 uv 缓存被清过——才联网装一次。前台做有两个好处:失败当场说得清,
# 而不是让人去日志尾巴里猜;以及用得上当前 shell 的代理设置,env -i 把 *_proxy
# 一起清掉了,而这台机器上 pypi 常常得走代理。
#
# **不用 `uv sync` 做这件事**:它默认 exact,会卸掉 venv 里那份旧项目名的 editable
# 残留(knowledge_base),而两份 dist-info 的 RECORD 记着**同一组** bin/kb、
# bin/kb-web、bin/kb-mcp —— 卸掉它就可能把启动入口一起删了。`uv run` 默认 inexact,
# 压根不碰它。
web_prepare_env() {
  if ( cd "$ROOT" && "${CLEAN_ENV[@]}" uv run --offline python -c "" ) >/dev/null 2>&1; then
    return 0
  fi
  say "[web] 环境要补一下(依赖有变动,或 uv 缓存不全),联网装一次"
  local with_proxy=("${CLEAN_ENV[@]}") v
  for v in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
    if [ -n "${!v:-}" ]; then with_proxy+=("$v=${!v}"); fi
  done
  ( cd "$ROOT" && "${with_proxy[@]}" uv run python -c "" ) \
    || die "依赖装不上,kb-web 起不来。网络通了再跑一次 ./kb.sh web start"
}

# **跑着的进程端着的是它启动那一刻的代码,而 kb 里有一批模块是延迟 import 的。**
# Python 不热加载,所以服务运行期间一次 git checkout 就能让同一个进程里**新旧两个
# 版本的模块混跑**:2026-10-10 实测 —— kb-web 10-09 21:23 起(那时 `x_channels` 还
# 没有 `fill_bridge`),07:08 换成新代码,08:03 第一次跑同步时 `kb.feeds.sync` 才被
# import、从磁盘读到**新版**并调用 `x_channels.fill_bridge`,而 `x_channels` 早以
# **旧版**驻留在 sys.modules 里(启动后不久就被 `registry.get_settings` 拉进来了,
# 它的 .pyc 时间戳停在换代码之前,是这次定位的铁证)。七条 RSS 订阅于是一起报
# `module 'kb.feeds.x_channels' has no attribute 'fill_bridge'`、各自被推进最长
# 24 小时的退避,而面板上显示的是「连接失败」—— 那句话指不到任何跟订阅、跟网络
# 有关的东西,人只会去查源站,查不出来。
#
# 判据是 **pidfile 比 `kb/` 下任何一个 .py 旧**:`daemonize` 写 pidfile 的那一刻就是
# 进程启动那一刻,而别处只删它、不写它。**只看 .py** —— `kb/static/` 下的 js/css
# 每次请求都从磁盘读(免重启),算进来就是每改一行样式催一次重启;`scripts/` 和
# `collector/` 同理不算,它们每次都是新起的子进程,拿的本来就是磁盘上那一版。
#
# 口径同 `graph_stale`:**提示而不代劳**,也不拦。重启会把正在跑的同步断在半路,
# 什么时候重启得由人定;而且新旧混跑未必每次都撞上(两个版本之间没有互相引用的
# 新符号时一切照旧),拿它挡住命令比提示一句更坏。
code_stale() {   # 永远返回 0:它只是提示,不能挡住任何命令
  [ -s "$WEB_PID" ] || return 0
  [ -n "$(find "$ROOT/kb" -name '*.py' -newer "$WEB_PID" -print -quit 2>/dev/null)" ] || return 0
  say "[web] 代码比跑着的进程新(kb/ 下的 .py 改过)。Python 不热加载,而延迟 import
     的模块会读到新版 —— 同一个进程里新旧混跑,报出来的是一句指不到真因的
     AttributeError。要让它生效:./kb.sh restart"
}

web_start() {
  if [ -n "$(service_pids "$WEB_PID" "$WEB_PORT" "$WEB_PAT")" ]; then
    say "[web] 已在 $API"; code_stale; graph_stale; return 0
  fi
  # 端口有人占但不是 kb-web:说清楚,别等它"起完"再报一个莫名其妙的超时。
  if port_busy "$WEB_PORT"; then
    die "端口 $WEB_PORT 被别的进程占着,kb-web 起不来。
     换个端口:KB_WEB_PORT=8788 ./kb.sh web start"
  fi
  web_prepare_env
  daemonize "$WEB_PID" "$LOG_DIR/web.log" "$ROOT" \
    "${CLEAN_ENV[@]}" PORT="$WEB_PORT" uv run --offline kb-web
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
  if [ -n "$pids" ]; then echo "kb-web    运行中 ($API)"; code_stale
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

# ── 手动收录 ──────────────────────────────────────────────────────────────────
# 网页端那颗「存入知识库」的命令行版 —— 收录从此不只能在面板上做(skill `kb-grab`
# 打的也是这一条)。**判断一个都不在这儿**:抓取、查重、重抓替换、起后半条链全在
# 服务端(`kb/web/grab_api.py`,硬约束 `docs/decisions/link-grab.md` 第一节),这里
# 只负责「确认服务在」,再把参数原样交给 scripts/grab_cli.py。
#
# 先 web_start 的理由同 `./kb.sh sync`:**管道跑在 kb-web 进程内**,服务不在就只能
# 把内容留在 feeds.db 里、检索不到。
#
# **worktree 里只许借用已经跑着的那份服务。** 端口上还没人时 web_start 会起一份
# 用**这个检出自己** `data/` 的服务 —— 真文章于是收进一个测试库,而终端上写着
# 「已写入知识库」。那是谎报,代价比 cmd_graph 那道闸门挡的还隐蔽(那边是拿空库
# 覆盖好页面,这边是用户以为存下了)。出路给两条,都不代劳。
cmd_grab() {
  [ $# -gt 0 ] || die "给一条链接:./kb.sh grab <链接>(多条用空格隔开;- 表示从标准输入读,一行一条)"
  # **帮助自己就是终点,打完直接走。** argparse 的 `--help` 让子进程以 0 退出,和
  # 「参数校验通过」一模一样 —— 光靠下面那句 `|| return` 分不开它们,于是帮助打完
  # 之后 shell 照旧往下走:起服务(worktree 里更糟,先撞上那道闸门、连帮助都拿不到,
  # 还以 1 退出)。两种拼写都认,位置不限;skill 那个 wrapper 是 `exec kb.sh grab`,
  # 所以这一处管得住它。
  local a
  for a in "$@"; do
    case "$a" in -h|--help) exec "$BOOT_PY" "$ROOT/scripts/grab_cli.py" --help ;; esac
  done
  # **余下的参数校验也排在任何副作用之前。** `--parse-only` 只解析就退出:不读
  # 标准输入、不发一个请求、不起服务。它顺带把**最终生效的那个 kb-web 地址**打
  # 回来(默认值就用下面那次调用的同一个 `--api`,所以两次的参数装配是同一份)。
  local want
  want="$("$BOOT_PY" "$ROOT/scripts/grab_cli.py" --parse-only --api "$API" "$@")" || return
  # **只有目标就是本机托管那份服务时,才替它起/拦。** `--api` 指到别处(另一台机器、
  # 自己另起的一份)时,下面这道闸门和 web_start 管的是**另一个**服务:worktree 里
  # 会在连上用户给的那个地址之前就 die,主检出则多起一份用户压根不需要的守护进程
  # (第 6 轮审查 N3)。那种情况下服务的生命周期归他自己,这边只负责把参数交出去。
  # **明确认出「目标是另一个地址」才跳过。** `$want` 空着、或者将来被改成多打一行,
  # 一律**照旧按本机走** —— 管道跑在 kb-web 进程内,静默不起服务的代价是内容留在
  # feeds.db 里、检索不到(那比多起一份服务坏)。
  local local_api=1
  case "$want" in
    "${API%/}") ;;
    http://*|https://*) local_api= ;;
  esac
  if [ -n "$local_api" ]; then
    if ! port_busy "$WEB_PORT"; then
      local common; common="$(git -C "$ROOT" rev-parse --path-format=absolute \
                                --git-common-dir 2>/dev/null || true)"
      if [ -n "$common" ] && [ "$common" != "$ROOT/.git" ]; then
        die "这是 worktree,而 $WEB_PORT 上还没有服务 —— 这儿起的那份会用本检出的 data/,
     收进去的文章进的是测试库,而命令行会说「已写入知识库」。
     去主检出跑:cd ${common%/.git} && ./kb.sh grab $*
     确实要在这儿收(数据目录是显式挂的):先 ./kb.sh start,再跑这条。"
      fi
    fi
    web_start
  fi
  "$BOOT_PY" "$ROOT/scripts/grab_cli.py" --api "$API" "$@"
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
  echo "── 公众号来源 ──"
  cli status 2>/dev/null | tail -1 \
    | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("message", ""))
if d.get("hint"): print("  " + d["hint"])' \
    || echo "(采集器起不来)"
}

# 同步前的准备。已经在跑的先杀掉重起,保证是干净的一份
cmd_logs() {
  local f="${1:-web}"
  [ "$f" = "web" ] || die "只有 web 一份日志"
  tail -n "${2:-40}" "$LOG_DIR/$f.log" 2>/dev/null || say "还没有 $f 的日志"
}

# ── 回归 ──────────────────────────────────────────────────────────────────────

# 分层口径在 tests/tiers.py,这里只做两件事:给一个**全新的临时数据目录**
# (打桩范围泄漏那类事故写的就是 $KB_DATA_DIR/kb.db —— 不隔离,一次全量就可能把
# 夹具文档写进真实库,实测发生过),以及把 TZ / 离线口径钉成和 CI 一致
# (.github/workflows/ci.yml:runner 默认 UTC 会让日历日差 8 小时;HF 离线是为了
# 真有测试去拉模型时当场红,而不是安静下 2GB)。
# 临时目录的所有权在这个子 shell:取消(INT/TERM)也要把它收走 —— 长回归常被中断,
# 残留会在 /tmp 里越积越多。先收测试进程再删目录,反过来删的是正在被写的目录。
cmd_test() {
  local tier="${1:-fast}"
  (
    tmpdata="$(mktemp -d "${TMPDIR:-/tmp}/kb-test-data-XXXXXX")"
    child=""
    cleanup() {
      if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
        kill "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
      fi
      rm -rf "$tmpdata"
    }
    trap 'cleanup; exit 130' INT
    trap 'cleanup; exit 143' TERM
    trap cleanup EXIT
    cd "$ROOT"
    KB_DATA_DIR="$tmpdata" TZ=Asia/Shanghai HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
      uv run --offline python -m tests.tiers "$tier" &
    child=$!
    wait "$child"
  )
}

usage() {
  cat >&2 <<'EOF'
Knowbase 操作入口 —— 每条命令跑完就还你终端,不用挂着

  日常
    ./kb.sh start                   起面板(后台守护进程)
    ./kb.sh sync                    跑全流程(采集→导出→索引→打标),后台跑
    ./kb.sh sync --channel rss      只跑某一个来源(wechat_local|social|rss)
    ./kb.sh status                  服务 / 同步进度 / 来源 一屏看全

  服务(后台守护进程;restart 会先杀掉在跑的那份,不管是谁起的)
    ./kb.sh start | stop | restart
    ./kb.sh web   start|stop|restart|status      面板 8787
    ./kb.sh logs [行数]
    (同步正在跑时 stop/restart 会拦一下,加 --force 强来)

  单跑某一步(都是后台跑,立刻返回)
    ./kb.sh collect [--since 2026-08-01 --until 2026-08-26] [--targets a,b]
                   [--channel wechat_local|social|rss]
    ./kb.sh export
    ./kb.sh index
    ./kb.sh extract
    ./kb.sh graph [--allow-shrink]  重建三张图谱页(前台约 6 秒,零模型调用)
                   图谱是 Vault 里的单文件 HTML,样式改了要重新生成——重启不管用。
                   跑同步时 wiki 阶段本来就会带上,这条是「只想立刻看到新样式」用的。

  手动收录(网页端那颗按钮的命令行版;抓不到正文的一律不入库)
    ./kb.sh grab <链接> [<链接>...]  抓进来,并等它真写进知识库再返回
    ./kb.sh grab --state <链接>      只查这一条此刻的状态(只读,不抓不写)
    ./kb.sh grab --refetch <链接>    库里那一份不对时重抓,确认得了就把它换掉
                   ./kb.sh grab - < links.txt 从标准输入读,一行一条地址
                   --json 一行一条 JSON(给程序读)、--no-wait 投完就返回
                   Claude Code 里说「把这条存进知识库」走 skill kb-grab,同一条路。

  回归(前台跑,数据目录自动换成全新临时目录,不碰真实库)
    ./kb.sh test                    提交前的快速层:纯逻辑全量,浏览器回归显式拦下(约 3 分钟)
    ./kb.sh test probes             只跑浏览器回归 —— 改了 kb/static/ 或阅读页之后(约 10 分钟)
    ./kb.sh test full               真全量,push 前至少一次(约 12 分钟;分层见 docs/decisions/test-tiers.md)

  订阅(瞬间返回)
    ./kb.sh accounts list|discover|add|remove|enable|disable [名字]
                   discover 从本机绿泡泡的会话列表里找候选:不登录、不走网络。
    ./kb.sh setup                   新 worktree 首次准备

公众号文章从本机绿泡泡客户端已经同步下来的推送里发现(只读那个库),正文再按
链接公开抓取,所以没有任何凭证要维护。另一条「逐号文章凭证」的通道(要 wxdown
抓包、要人去逐个点开文章)在 wechat-oa 分支上,主干不带它。
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
  logs)    cmd_logs "$@" ;;

  start)   web_start ;;
  stop)    web_stop ;;
  restart) web_stop; web_start ;;
  web)     svc web "${1:-status}" ;;

  sync)    pipeline_run "" "$@" ;;
  collect) pipeline_run '["crawl"]' "$@" ;;
  export)  pipeline_run '["export"]' ;;
  index)   pipeline_run '["index"]' ;;
  extract) pipeline_run '["extract"]' ;;
  graph)   cmd_graph "$@" ;;
  grab)    cmd_grab "$@" ;;
  test)    cmd_test "$@" ;;

  accounts) cli accounts "$@" ;;
  setup)   "$COLLECTOR/setup.sh" ;;
  -h|--help|help) usage ;;
  *) die "不认识的命令 $ACTION(跑 ./kb.sh 看列表)" ;;
esac
