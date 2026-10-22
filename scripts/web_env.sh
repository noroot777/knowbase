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
# 那个库,是另一件事。**空值不透传**(`NO_PROXY=''` 和「没设」不是一回事;httpx 读到空
# 代理会当成配了一个)。完整形态与实测见 docs/decisions/dns-egress.md。
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

# 配了 DoH 却一个代理变量都没有时提一句。**提示而不代劳**(口径同 code_stale /
# graph_stale):那种配法下端点多半也连不上,而后果是每条订阅都报「域名解析服务不可用」。
warn_egress_gap() {
  if [ -n "${KB_FEED_DOH:-}" ] \
     && [ -z "${HTTPS_PROXY:-}${https_proxy:-}${ALL_PROXY:-}${all_proxy:-}" ]; then
    say "[web] KB_FEED_DOH 配着,但这个 shell 里没有代理变量 —— DoH 端点多半连不上"
  fi
}
