# -*- coding: utf-8 -*-
"""「有一轮抓取开过工」—— 读那笔**起跑前提**的开工标记,按抓取/批次口径认身份。

当日抓取账本(`kb.fetch_rounds`)和那几笔独立凭证(`kb.fetch_pending`)都是**尽力
而为的旁文件**:账本的每一次写都在一把会坏的文件锁后面,凭证那一笔写不上也只是
不出声。第 4 轮审查实测这两条**能一起失效**:旁挂锁被占成目录、凭证目录路径被占成
普通文件,而数据目录本身照旧可写 —— 真实 `start_pipeline` 的开工标记落盘成功、
采集器真往 vault 写出第二篇,随后硬退出。盘上剩下的是第一个子轮那一套合法的行,
`today()` 据此报**确定的 1 条**,而 vault 里躺着两篇。「早先某一个文件写成功」推不出
「稍后另外两个文件一定可写」,所以旧版文档里那条「数据目录写不进去就不会起跑、
所以这一档不可达」的推理不成立。

**这一笔不是尽力而为的:它是起跑的前提。** 开工标记落不下来的那一轮压根不许起跑
(`kb.pipeline_open.mark`,两个起跑入口都过这道闸),所以**凡是真抓过的运行,盘上
一定有过它这一笔**。于是它正好补上另外两条都失效的那一档:账本里找不到这次抓取的
轮次/子轮时,由它说出「确实有一轮开过工」,读侧据此答「不可确认」或「至少 N 条」,
而不是沿用上一轮那个确定数。

**但判据只能是「身份对得上」,不能是「标记还在」。** 标记在收尾写上两份终态之后才
清,而一次只勾了索引/导出的运行压根不在当日账本上开轮 —— 只看「标记还在」的话,
那种运行会让面板永久报「不完整」。所以标记里记着两样东西(写在
`pipeline_open.mark`):这次抓取的业务身份(`batch`,没有批次的独立运行留空,键按
`pipeline_batch.round_key_of` 还原,**和 `round_key()` 同一条算法**)、以及这一轮到底
抓不抓(`fetches`)。老格式没有这两个字段,那是**说不清**,不是「没抓过」。

**不按日子筛。** 一是宿主机时钟和项目时区(Asia/Shanghai)可能差半天,拿标记里那个
时间串去跟账本的「今天」比,UTC 机器上有八小时会把今天的标记判成「不是今天的」,
而判错的方向正是静默回到那个少记的确定数;二是不需要:判据是「它那一轮在账本里
找不到」,而账本自己只留两天 —— 窗口外那一轮本来就被轮换清掉了,那时读侧答
「不可确认」正是实话(页头那一侧同时会显示「这一屏已经过期」,见
`pipeline_open.gap`),下一次起跑把标记一覆盖就恢复。

硬约束、事故形态和回归清单在 `docs/decisions/daily.md`。
"""
from __future__ import annotations

from pathlib import Path

from kb import pipeline_state as ps
from kb.pipeline_batch import round_key_of
from kb.pipeline_persist import _load_json_strict


def _marker(folder: Path) -> Path:
    """开工标记在**账本自己那个数据目录**里。

    **名字只有一处算**(`pipeline_state._open_file`,三份落盘都从 `_STATE_FILE`
    派生),**位置按调用方给的数据目录解析** —— 账本和标记必须是同一个库的两份记录。
    直接用 `_open_file()` 的绝对路径不行:用例只把账本那一侧指到临时目录是常态,
    那时读到的会是**另一个库**(测试共用 `KB_DATA_DIR`)那份标记,于是一条用例的
    残留能把别的用例拖成「不可确认」—— 而反过来(审查脚本只打桩账本那一侧)读不到
    本该读到的标记,表现是静默放过一次少记。
    """
    return folder / ps._open_file().name


def claim(folder: Path) -> tuple[str, str, bool]:
    """盘上有没有一轮**抓取**开过工还没交账:`(这次抓取的键, 它的子轮, 读全了没有)`。

    三档,和这个项目里所有「证据」一个口径:

    · `('', '', True)` —— **确定没有**:标记不在(上一轮正常收尾清掉了),或者标记在
      而那一轮压根不抓(`fetches=0`,只勾了索引/导出的运行)。
    · `(key, seg, True)` —— 有一轮抓取开过工,身份是这个。
    · `('', '', False)` —— **说不清**:标记读不出来(权限、I/O、半截写入、非 UTF-8、
      深嵌套),根不是字典,或者缺了判身份要用的字段(老格式)。合成「确定没有」的
      话,一次读故障就把「有一轮没交账」说成「一切都齐了」,而这个模块存在的理由
      就是不许出现那句话。
    """
    try:
        on_disk = _load_json_strict(_marker(folder))
    except Exception:  # noqa: BLE001 - 任何读不出来都是「说不清」,不是「没有」
        return '', '', False
    if on_disk is None:
        return '', '', True                 # 没有标记:上一轮收尾清掉了
    if not isinstance(on_disk, dict):
        return '', '', False
    if 'fetches' not in on_disk:
        return '', '', False                # 老格式:说不清它抓不抓
    if not on_disk.get('fetches'):
        return '', '', True                 # 这一轮不抓,它不该让面板报「不完整」
    key, seg = round_key_of(on_disk.get('batch'), on_disk.get('run_id'))
    if not key:
        return '', '', False                # 标记自己没有可信身份:宁可说不清
    return key, seg, True


def vouches(folder: Path, key: str, seg: str) -> bool:
    """这笔标记认不认**这一个子轮**(抓取那一侧的闸用它,见 `fetch_rounds.open_round`)。

    要求身份逐字对上,不是「有一笔标记」:对不上说明盘上那一笔指的是**另一轮**,
    它替不了这一轮的参与证据。
    """
    got_key, got_seg, ok = claim(folder)
    return bool(ok and got_key == str(key) and got_seg == str(seg))
