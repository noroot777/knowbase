#!/usr/bin/env bash
# 找到 knowbase 检出,把参数原样交给 `./kb.sh grab`。
#
# **这个脚本只解析路径,一个判断都不加。** 收录的全部口径在服务端
# (`kb/web/grab_api.py`,硬约束 `docs/decisions/link-grab.md` 第一节),命令行那一层
# 在 `scripts/grab_cli.py` —— 这里再判一次就是「同一个问题几处各判一遍」。
#
# **总是对主检出动手。** skill 文件随仓库走,所以它可能正躺在一个 worktree 里;
# 而用户的真数据(feeds.db、kb.db、vault)只在主检出那一份。git 的 common-dir
# 从任何 worktree 都解得出主检出,所以探到自己所在的仓库之后再换一次。
set -euo pipefail

main_checkout() {   # main_checkout <仓库内任意目录>
  local common
  common="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [ -n "$common" ] && [ -x "${common%/.git}/kb.sh" ] && { echo "${common%/.git}"; return; }
  echo "$1"
}

find_home() {
  if [ -n "${KB_HOME:-}" ]; then
    [ -x "$KB_HOME/kb.sh" ] || { echo "KB_HOME=$KB_HOME 里没有 kb.sh" >&2; return 1; }
    echo "$KB_HOME"; return
  fi
  # ① skill 住在仓库里时:.claude/skills/kb-grab/scripts/grab.sh → 往上四级
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
  [ -x "$here/kb.sh" ] && { main_checkout "$here"; return; }
  # ② 装到了 ~/.claude/skills 下:按常见检出位置找
  local c
  for c in "$HOME/code/knowledge-base" "$HOME/code/knowbase" "$HOME/knowbase" \
           "$HOME/knowledge-base"; do
    [ -x "$c/kb.sh" ] && { main_checkout "$c"; return; }
  done
  echo "找不到 knowbase 检出。设一下 KB_HOME,例如:KB_HOME=~/code/knowbase" >&2
  return 1
}

HOME_DIR="$(find_home)"
exec "$HOME_DIR/kb.sh" grab "$@"
