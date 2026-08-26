#!/usr/bin/env bash
# 把采集器跑起来所需的、不进版本库的两样东西链好:数据目录与 venv。
#
# 为什么需要这一步:data/(含 592M 文章库和凭证)和 .venv/ 都在 .gitignore 里,
# 所以 `git worktree add` 出来的新工作树只有源码。跑一次本脚本即可,
# 它是幂等的。
#
# 两者都指向仓库外的同一份真实目录——采集是有状态的,多个 worktree 共用
# 同一个库才对:各自持有一份 592M 的副本既浪费,又会让"哪份是最新的"
# 变成新问题。要独立数据就设 GZH_DATA_HOME 指到别处。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${GZH_DATA_HOME:-$HOME/code/gongzhonghao/WeChatOA_Aggregation/data}"
VENV_HOME="${GZH_VENV_HOME:-$HOME/code/gongzhonghao/WeChatOA_Aggregation/.venv}"

fail() { echo "[setup] 错误:$1" >&2; exit 1; }

[ -d "$DATA_HOME" ] || fail "找不到数据目录 $DATA_HOME(用 GZH_DATA_HOME 指定)"
[ -x "$VENV_HOME/bin/python" ] || fail "找不到 venv $VENV_HOME(用 GZH_VENV_HOME 指定)"

ln -sfn "$DATA_HOME" "$HERE/data"
ln -sfn "$VENV_HOME" "$HERE/.venv"

echo "[setup] data -> $DATA_HOME"
echo "[setup] .venv -> $VENV_HOME"
# `-m cli` 要在 collector 目录里跑才找得到模块
(cd "$HERE" && ./.venv/bin/python -m cli status > /dev/null 2>&1) \
  && echo "[setup] 自检通过:python -m cli status 可用" \
  || echo "[setup] 警告:cli status 跑不通,检查上面两个路径" >&2
