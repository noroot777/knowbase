# shellcheck shell=bash
# 「提示而不代劳」那一族:跑着的东西比磁盘上的旧时说一句,但**绝不替人动手**。
#
# **拆成单独一份是因为 kb.sh 顶到了 700 行那条线**(全局约定)。这两个函数本来就是同
# 一件事的两面:一个看服务进程端着的代码,一个看 Vault 里那三张图谱页;口径也一样 ——
# **永远返回 0**(返回非 0 会把它挂着的那条命令整个带掉),读不到东西就安静。
#
# 由 kb.sh 顶层 source 进来,所以它自己也在 code_stale 的比较范围里(被 source 的 .sh
# 决定服务进程拿到哪一份环境)。改了它要重启服务才生效 —— 那一条正是 code_stale 在说
# 的事。回归:tests/test_kb_code_stale.py、tests/test_kb_graph_guard.py。

code_stale() {   # 永远返回 0:它只是提示,不能挡住任何命令
  [ -s "$WEB_PID" ] || return 0
  # 两类输入都算:**Python 侧**是 kb/ 下的 .py(延迟 import 会读到新版,同一个进程里
  # 新旧混跑);**shell 侧**是 kb.sh 自己和它 source 进来的 .sh —— 那几份决定了服务
  # 进程拿到哪一份环境(出口变量、DoH 开关),改完不重启,跑着的进程端的还是旧那份。
  # 「scripts/ 每次都是新起的子进程」那条只对 .py 成立,对被 source 的 .sh 不成立。
  # 两段**紧挨着**拼,中间不许有换行:夹着换行的话三段全空时字符串也非空,那句提示
  # 就永远在打(实测:`test_kb_code_stale` 的「该安静」那两条当场红)。
  local hits
  hits="$(find "$ROOT/kb" -name '*.py' -newer "$WEB_PID" -print -quit 2>/dev/null)$(find "$ROOT" "$ROOT/scripts" -maxdepth 1 -name '*.sh' -newer "$WEB_PID" -print -quit 2>/dev/null)"
  [ -n "$hits" ] || return 0
  say "[web] 代码比跑着的进程新(kb/ 下的 .py,或者 kb.sh / scripts 下的 .sh 改过)。
     Python 不热加载,而延迟 import 的模块会读到新版 —— 同一个进程里新旧混跑,报出来
     的是一句指不到真因的 AttributeError;shell 那几份决定服务拿到哪份环境(出口变量、
     DoH 开关),改完不重启照旧是旧的。要让它生效:./kb.sh restart"
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
