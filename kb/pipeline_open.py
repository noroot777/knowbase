"""「有一轮开工了」单独落一笔盘 —— **收尾两份终态都写上了才清掉它**。

两份终态都是在 `_end_run()` 收尾时才写的。**都写失败那一档,磁盘上一个字都没有留下
这一轮的痕迹**:新身份、完成序号、终态和那笔留档诊断全在内存里,而重启把内存清空。
于是恢复看到的两份都是上一轮,`same_event` 正确地证明「这两份是同一次事件」—— 可它
证明不了「没有更新的一轮」:实测 B 轮(成功、范围是 B订阅)收尾时两份写入都注入
`ENOSPC`,重启后页头又显示 A 的 503、**空欠账、没有任何过期提示**,点「重试失败项」
导出阶段收到 `accounts: ['A订阅']`。进程在跑完之后、收尾之前被杀掉是同一档。

**现存的候选不等于「发生过的全部运行」。** 要能分出来,只能在**开工之前**就落一笔:
这一笔在、而磁盘上最新那一轮不是它,就说明有一轮跑过而结果没落下来。

判据故意只认「不是同一次事件」而不是「标记还在」:只有一份终态写失败那一档
(`scope`)标记也没清,可那一轮确实在历史里 —— 报失证就是误报。
"""
from __future__ import annotations

from pathlib import Path

from kb.pipeline_persist import _dump_bytes, _load_json, _write_atomic, event_id


def mark(path: Path, run: dict) -> str:
    """落一笔开工标记。返回诊断(空串 = 写上了)。

    **写不上不拦起跑**:磁盘满、权限不对的时候连同步都不让跑,代价比「这一轮的结果
    将来可能说不清」大得多 —— 那一档如实记一笔诊断,面板上照旧看得见。
    """
    try:
        _write_atomic(path, _dump_bytes({
            "run_id": run.get("run_id", ""),
            "seq_gen": run.get("seq_gen"),
            "started_at": run.get("started_at", ""),
            "run_kind": run.get("run_kind", ""),
        }))
        return ''
    except Exception as exc:  # noqa: BLE001 - 任何落盘坏法都只是「这一笔没留下」
        return f'开工标记（{type(exc).__name__}）'


def clear(path: Path) -> None:
    """清掉开工标记。**只在两份终态都写上之后调** —— 清不掉就留着。

    留着的后果是下一次启动报一次失证(误报,顶多多跑一轮完整同步);反过来「没写上
    终态却把标记清了」是漏报,那一轮就此查不出来。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def lost(path: Path, known) -> bool:
    """**开工标记指着的那一轮,在磁盘上有没有留下任何一份终态。**

    `known` 是两份落盘里读出来的全部候选(状态文件那一份 + 历史各轮)。判据是
    「它一份都没留下」,**不是「标记还在」、也不是「标记不是挑中的那一轮」**:
    · 标记不在 —— 上一轮正常收尾过(或者这个库从没跑过),没有失证。
    · 标记在、而且候选里找得到同一次事件 —— 那一轮至少留下了一份(只有一份写失败
      那一档就是这样;两份各写失败一次、先后又分不出来的那一局也是),不是失证。
    · 一份都找不到才是失证:那一轮跑过,磁盘上一个字都没留下。

    判据写成「标记不是挑中的那一轮」会多报一整类:先后分不出来时挑中的是状态文件
    那一份,而另一轮明明在历史里 —— 那一屏不是过期,只是先后未知(`order` 自己会说)。
    """
    mark_on_disk = _load_json(path)
    if not isinstance(mark_on_disk, dict):
        return False
    got = event_id(mark_on_disk)
    if not got:
        # 标记自己没有可信身份(写坏了、或者是别的版本写的):**宁可报失证** ——
        # 它存在就说明有一轮开工过,而我们证明不了它收尾了。
        return True
    return not any(got == event_id(one) for one in known if isinstance(one, dict))
