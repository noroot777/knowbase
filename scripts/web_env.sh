# shellcheck shell=bash
# kb-web 进程拿到的那份环境。由 kb.sh 在定义 CLEAN_ENV 之后 source 进去。
#
# **拆成单独一份是因为 kb.sh 顶到了 700 行那条线**(全局约定),而「服务进程的环境怎么
# 拼」本来就是一件独立的事:它有自己的名单、自己的理由、自己的回归
# (tests/test_egress_env.py 直接 source 本文件)。scripts/ 下其余都是 python —— 它们和
# 这一份是同一类东西:被 kb.sh 调用的实现。
#
# **透传这一族的理由,别记错。**
# - `KB_FEED_DOH` 是**硬需求**:不透传的话服务里永远读不到它,可信解析那一层等于没有开关。
# - 代理那几个是**让出口变显式**,不是「否则就没有出口」—— 实测 macOS 上 `env -i` 并不会
#   让抓取失去代理:httpx 的 trust_env 走 `urllib.request.getproxies()`,它在 macOS 上读
#   系统代理设置(SystemConfiguration),环境变量清光了照旧拿得到。但那是 macOS 独有的:
#   Linux / 容器 / CI 上 `getproxies()` 只看环境变量,**那里 `env -i` 才真的把出口断掉**,
#   而那正是开源交付要走的那条路。
# - `env -i` 防的是继承外层(尤其嵌套 claude)的登录态变量,那条理由对出口配置不成立 ——
#   装依赖那一步(`web_prepare_env`)本来就在透传同一族,只是起服务那一行一直没用上。
#
# **名单只有一份**:谁再手写第二遍循环,这件事就又变成两处各判一遍(那正是本轮的形态)。
# **只透传出口这一族,不是整份 KB_ 开头的变量**:`KB_DATA_DIR` 跟进去会静默换掉服务读的
# 那个库,是另一件事。**空值不透传**:理由不是「httpx 会把空值当成配了一个代理」——
# 那一条实测**不成立**(urllib 的 getproxies_environment() 跳过空值);理由是服务进程
# 的环境该只有真正配过的东西,空壳变量会让「到底配没配」这个问题在排查时多一层噪声。
# 完整形态与实测见 docs/decisions/dns-egress.md。
EGRESS_VARS=(HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
             http_proxy https_proxy all_proxy no_proxy
             KB_FEED_DOH KB_FEED_DOH_SECONDS)

# 数组只能由函数填回全局:macOS 自带的是 bash 3.2,没有 mapfile。
WEB_ENV=()
build_web_env() {
  WEB_ENV=("${CLEAN_ENV[@]}")
  local v
  for v in "${EGRESS_VARS[@]}"; do
    if [ -n "${!v:-}" ]; then WEB_ENV+=("$v=${!v}"); fi
  done
}

# 服务进程**自己**那一份出口环境(`ps eww` 把目标进程的环境打在命令行后面)。
# 填进全局 `SERVICE_ENV`;读不到返回 1。
#
# **为什么非得问进程、不能问当前 shell**:服务可能是几天前、在另一个终端里起的 —— 那时
# 有没有 `HTTPS_PROXY`、`KB_FEED_DOH` 指到哪,和你现在这个 shell 没有关系。按当前 shell
# 算出来的那两行会**精确地反掉**:你刚 export 了一个代理,状态里就显示「出口代理:…」,
# 而真正在抓取的那个进程里一个代理都没有,面板上照旧超时。**这几行存在的全部理由就是
# 回答「跑着的那个到底在用什么」**,按 shell 算等于把问题换成了「我现在这个 shell 会用
# 什么」—— 而那个问题没人问。
#
# 名单仍然只有 `EGRESS_VARS` 一份(所以不会和透传那一侧错开)。按空格切:这一族的值
# (代理地址、DoH 端点)里不会有空格。
SERVICE_ENV=()
service_env() {   # service_env <pid>
  SERVICE_ENV=()
  local pid="${1:-}" blob v hit
  [ -n "$pid" ] || return 1
  blob="$(ps eww -p "$pid" 2>/dev/null | tail -n +2)"
  # pid 没了的时候 ps 只打一行表头,管道又把它的退出码吃掉了 —— 所以判据是**有没有
  # 内容**,不是退出码。
  [ -n "$blob" ] || return 1
  for v in "${EGRESS_VARS[@]}"; do
    hit="$(printf '%s\n' "$blob" | tr ' ' '\n' | grep -E "^${v}=." | tail -1 || true)"
    if [ -n "$hit" ]; then SERVICE_ENV+=("$hit"); fi
  done
  return 0
}

# 起服务/看状态时打的那两三行:可信解析开没开、端点是什么、出口代理在哪。
#
# **内容一个字都不在这儿判,全问 `kb.feeds.egress.summary()`。** 理由是主次反过来了:
# 端点和代理现在**以设置页为主、环境变量兜底**,shell 再自己 `[ -n "$KB_FEED_DOH" ]`
# 判一遍的话,「设置里填了、环境变量没填」就会打「关」—— 而服务其实是开着的。那条
# 「探不到代理」的提示同理(判据是真问一次 `urllib.request.getproxies()`,macOS 上它读
# 系统代理设置,按环境变量判是误报),也一并搬进 Python 了。
#
# **带 pid 调 = 报那个进程在用什么**(`web_status`、`web_start` 的「已在」分支:服务是
# 以前起的,环境得问它自己);**不带 pid = 刚用 `WEB_ENV` 把它起起来的那一下**,那份
# 环境就是服务的环境,按构造成立,不必再 ps 一次去和一个刚 exec 的进程赛跑。
# **取不到就什么都不打**:venv 还没装好时 uv 会失败,那一步自己会说 —— 这里再喊一遍
# 只是噪声。口径同 code_stale / graph_stale:**提示而不代劳,也不拦**。
# 端点脱敏(path / query 里可能有 token)也在 Python 那一侧,见 egress.mask_endpoint。
egress_lines() {   # egress_lines [服务 pid]
  local out line pid="${1:-}"
  local run_env  # 下面按数组赋值(bash 3.2 没有 local -a)
  if [ -n "$pid" ]; then
    if service_env "$pid"; then
      run_env=("${CLEAN_ENV[@]}" ${SERVICE_ENV+"${SERVICE_ENV[@]}"})
    else
      # **不许退回当前 shell** —— 那正是这几行要避免的那个错答案。说清只按设置文件算。
      say "[web] 读不到服务进程(pid $pid)的环境,下面只按设置文件算"
      run_env=("${CLEAN_ENV[@]}")
    fi
  else
    run_env=("${WEB_ENV[@]}")
  fi
  # **先看退出码再决定要不要说话**:只挡 stderr 是不够的,一个半路失败的解释器照旧
  # 可能在 stdout 上留下半行东西,原样打出来像是出口配置出了问题。
  out="$(cd "$ROOT" && "${run_env[@]}" uv run --offline python -c \
      'from kb.feeds import egress
for line in egress.summary():
    print(line)' 2>/dev/null)" || return 0
  while IFS= read -r line; do
    if [ -n "$line" ]; then say "[web] $line"; fi
  done < <(printf '%s\n' "$out")
  # **末尾这两处都不能省**:kb.sh 开着 `set -e`,而「一行都没打」是正常末态。
  # 写成 `[ -n "$line" ] && say ...` 的话,空输出那一轮它返回 1、整个 while 跟着返回 1,
  # `web_start` 当场被砍 —— 服务其实起来了,命令却报错退出(`tests/test_kb_web_start.py`
  # 的离线那条实测红过)。显式 `return 0` 是同一件事的第二道。
  return 0
}
