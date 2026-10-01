#!/usr/bin/env bash
# 把采集器跑起来所需的、不进版本库的两样东西链好:数据目录与 venv。
#
# 为什么需要这一步:data/(文章库)和 .venv/ 都在 .gitignore 里,所以
# `git worktree add` 出来的新工作树只有源码。跑一次本脚本即可,它是幂等的。
#
# 两者都指向仓库外的同一份真实目录——采集是有状态的,多个 worktree 共用同一个库
# 才对:各自持有一份几百 M 的副本既浪费,又会让"哪份是最新的"变成新问题。
# 要独立数据就设 KB_COLLECTOR_DATA_PATH 指到别处 —— 和 compose.yaml / .env 里那个
# 同名变量是同一个意思(「宿主机上的 collector 数据目录在哪」),故意共用一个名字。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${XDG_DATA_HOME:-$HOME/.local/share}/knowbase/collector"
DATA_HOME="${KB_COLLECTOR_DATA_PATH:-$STATE/data}"
VENV_HOME="${KB_COLLECTOR_VENV_PATH:-$STATE/venv}"

# 存量回退:这个采集器原来住在 WeChatOA_Aggregation 里,库和 venv 也在那边。
# 没显式指定、默认位置还空着、而老位置确实有东西时就接着用老的——搬几百 M 的库
# 是用户自己的决定,不该由一个"链好符号链接"的脚本代劳。
LEGACY="$HOME/code/gongzhonghao/WeChatOA_Aggregation"
if [ -z "${KB_COLLECTOR_DATA_PATH:-}" ] && [ ! -e "$DATA_HOME" ] && [ -d "$LEGACY/data" ]; then
  DATA_HOME="$LEGACY/data"
  echo "[setup] 沿用原位置的数据目录:$DATA_HOME" >&2
  echo "[setup]   要搬到 $STATE/data 就把目录移过去,或设 KB_COLLECTOR_DATA_PATH" >&2
fi
if [ -z "${KB_COLLECTOR_VENV_PATH:-}" ] && [ ! -x "$VENV_HOME/bin/python" ] \
   && [ -x "$LEGACY/.venv/bin/python" ]; then
  VENV_HOME="$LEGACY/.venv"
  echo "[setup] 沿用原位置的 venv:$VENV_HOME" >&2
fi

fail() { echo "[setup] 错误:$1" >&2; exit 1; }

# 空目录也行:库和表由 src/db 在首次连接时建出来
mkdir -p "$DATA_HOME"

if [ ! -x "$VENV_HOME/bin/python" ]; then
  command -v uv > /dev/null \
    || fail "没有 venv 也没有 uv。装一个 uv(https://docs.astral.sh/uv/),或用 KB_COLLECTOR_VENV_PATH 指到现成的 venv"
  echo "[setup] 建 venv:$VENV_HOME"
  uv venv --project "$HERE" "$VENV_HOME" > /dev/null
  VIRTUAL_ENV="$VENV_HOME" uv sync --project "$HERE" --active > /dev/null
fi

# `ln -sfn A link` 在 link 已经是真目录时会把链接建进那个目录里(变成 link/A),
# 而不是替换它——于是 collector/data 下多出一个 data/,库却照 DATA_DIR 建在
# collector/data 本身,采集器拿一个空库跑、`cli status` 报 0 个号而不是报错。
# 空目录(前一次误跑或 git 留下的占位)直接收掉;非空的一律停下让人看一眼。
relink() {
  local target="$1" link="$2"
  if [ ! -L "$link" ] && [ -e "$link" ]; then
    [ -d "$link" ] || fail "$link 已经存在而且不是目录,先自己挪开"
    rmdir "$link" 2> /dev/null || fail "$link 是个非空目录,不敢替换成符号链接:先确认里面的东西还要不要"
  fi
  ln -sfn "$target" "$link"
}

relink "$DATA_HOME" "$HERE/data"
relink "$VENV_HOME" "$HERE/.venv"

echo "[setup] data -> $DATA_HOME"
echo "[setup] .venv -> $VENV_HOME"
# `-m cli` 要在 collector 目录里跑才找得到模块
(cd "$HERE" && ./.venv/bin/python -m cli status > /dev/null 2>&1) \
  && echo "[setup] 自检通过:python -m cli status 可用" \
  || echo "[setup] 警告:cli status 跑不通,检查上面两个路径" >&2
