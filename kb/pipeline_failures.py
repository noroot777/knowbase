# -*- coding: utf-8 -*-
"""失败明细里「用户已经处理掉」的那几条:记一笔身份,此后不再显示。

面板上那份明细里有一类条目是「这一篇一直缺正文」——管道会按退避时间一轮一轮
地重试它们,而其中确实有一些原文已经没有内容了(整篇只有图片、原文被删)。
用户点开原文核对完,可以在那一行上直接说「别再抓了」:真正的删除由采集器做
(`kb.collectors.ItemDropping`,它把这条的身份记成黑名单,下一轮不再发现),
这个模块只管**它从此不该再出现在面板上**。

**为什么不去改那几份运行记录。** 「最新那一轮的终态」在三个地方各有一份投影:
内存 `_state`、整批那份汇总(`pipeline_batch`)、磁盘上的状态文件,外加历史文件里
同一轮那一条。挨个做外科手术既漏得掉(汇总那份是内存缓存,`last_result()` 优先
读它),又会把「两份落盘是同一个业务结果的两个投影」这条不变量撕开 ——
那条不变量的事故形态写在 `docs/decisions/pipeline-history.md` 里。
所以这里改成在**出口**过一道:两个端点把结果交给浏览器之前各过一次 `strip()`,
上面那几份投影一个字节都不用动,历史里同一条也跟着不再出现(用户要的正是
「以后别让我再看见它」)。

账本只增不减地长在一个小文件里,按条数封顶 —— 它只需要盖住面板能显示的那些轮次
(历史最多 `_HISTORY_KEEP` 轮 × 每轮最多 `_FAILURES_KEEP` 条)。

硬约束、事故形态和回归清单在 `docs/decisions/failure-drop.md` 的第一节 ——
**动这一块之前先读那一份**。
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager

from kb import pipeline_state as ps
from kb.pipeline_persist import _dump_bytes, _load_json, _write_atomic

#: 账本里最多记多少条。面板显示得到的上限是 20 轮 × 50 条,留一倍余量。
_KEEP = 2000

#: 等锁的上限(秒)。临界区只是读一个几十 KB 的小文件再写回去,毫秒级;等上几秒
#: 就说明对面不正常,这时报「账本没写成」比攥着一条 Web 线程不放好 —— 不可逆的
#: 删除早就成了,端点那边会如实在文案里说一句。
_WAIT = 5.0
_POLL = 0.02


def _file():
    """账本就在状态文件旁边。

    **从 `ps._STATE_FILE` 派生,不另立一个模块变量** —— 理由同
    `pipeline_state._history_file`:测试隔离落盘时 patch 的是那一个,另立一个的话
    每个走到这里的测试都得记着再 patch 一次,而漏掉的那个会去写**生产**那份账本,
    漏没漏在测试输出上一个字都看不见。
    """
    f = ps._STATE_FILE
    return f.with_name(f"{f.stem}_dropped{f.suffix}")


def _lock_file():
    """账本自己的锁文件。**只创建不删除** —— unlink 会让「A 持锁 → B 删文件 →
    C 新建再持锁」出现两个持锁者(同 `kb/maintenance.py` 那条)。

    从 `_file()` 派生,所以测试隔离落盘时自动跟着搬走,理由同 `_file()`。
    """
    f = _file()
    return f.with_name(f"{f.stem}.lock")


@contextmanager
def _exclusive():
    """把「读出来 → 改 → 整份写回」整段圈起来。等不到就抛。

    **粒度是整趟,不是只圈写的那一句。** 丢更新的那一半在「读」:两条请求都读到
    删除之前的旧列表、各自 append 一条、再整份替换,最后落盘的那份只有它自己追加
    的那一条,另一条已经不可逆地删掉了内容却**再也不会被隐藏**,而两个响应都是
    `ok=true`(第 1 轮审查实测:账本里只剩 `wx-second`)。`_write_atomic` 只保证
    单次写入不是半截 JSON,保证不了这件事。

    **一把 flock 就够,不另加线程锁。** flock 认的是 open file description:
    `_exclusive` 每次自己 `open` 一个 fd,所以同一个进程里两条线程(kb-web 是线程
    池,两个标签页各点一次完全正常)也互相挡得住,跟换个进程来是同一条路。
    再叠一把 `threading.Lock` 等于同一个问题判两遍 —— 口径只有一份。
    代价是它**不可重入**:谁在持有的时候再调一次 `remember` 会把自己挡到超时。
    目前只有端点那一个调用方,不存在嵌套。
    """
    _file().parent.mkdir(parents=True, exist_ok=True)
    fh = _lock_file().open("a+")
    try:
        deadline = time.monotonic() + _WAIT
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"另一个请求正在写面板记录，等了 {_WAIT:.0f} 秒还没让开"
                    ) from None
                time.sleep(_POLL)
        yield
    finally:
        fh.close()                   # 关闭即释放 flock


def _key(channel: str, item_id: str) -> str:
    return f"{channel}:{item_id}"


def _item_key(item) -> str:
    """一条失败明细的身份,没有身份(来源没给 `drop`)返回空串。"""
    drop = item.get("drop") if isinstance(item, dict) else None
    if not isinstance(drop, dict):
        return ""
    channel, item_id = str(drop.get("channel") or ""), str(drop.get("id") or "")
    return _key(channel, item_id) if channel and item_id else ""


def dropped_keys() -> set[str]:
    """账本里记着的那些身份。**读不出来一律空集**(宽口径,同 `load_history`)。

    代价是那几条会重新出现在面板上 —— 那只是显示,采集那边早就不再抓它们了,
    再点一次也只是重复一次幂等的删除。反过来(读失败就拒绝显示明细)才是真的
    丢信息。
    **不缓存**:这份文件只有几十 KB,而缓存要么跟不上另一个写入方,要么在测试
    之间泄漏到别的数据目录去。
    """
    raw = _load_json(_file())
    got = raw.get("dropped") if isinstance(raw, dict) else None
    if not isinstance(got, list):
        return set()
    return {k for k in got if isinstance(k, str) and k}


def remember(channel: str, item_id: str) -> None:
    """记一笔「这一条已经处理掉了」。写不成会抛,由调用方如实说一句。

    **先读再写、按插入顺序封顶**:封顶要掉的是最老的那几条,而它们对应的轮次
    早被历史的保留上限挤出去了。读不出来当空账本 —— 那会让此前记过的几条重新
    显示出来,但不会让这一条记不上(「拿不准就别固化终态」管的是**终态**,这里
    的终态在采集器那边,这份账本只是显示)。

    **整段在 `_exclusive()` 里**:读、合并、写回三步之间不许有别人插进来 ——
    理由和事故形态写在那个上下文管理器的说明里。
    """
    key = _key(channel, item_id)
    with _exclusive():
        raw = _load_json(_file())
        got = raw.get("dropped") if isinstance(raw, dict) else None
        keys = ([k for k in got if isinstance(k, str) and k]
                if isinstance(got, list) else [])
        keys = [k for k in keys if k != key]
        keys.append(key)
        del keys[:-_KEEP]
        _write_atomic(_file(), _dump_bytes({"dropped": keys}))


def strip(run, keys: set[str] | None = None) -> int:
    """把一份运行结果里已处理掉的失败明细剔掉(**就地改**),返回剔了几条。

    `keys` 省略时自己读一次账本;一次要过一串记录(历史清单)时由调用方读一次
    传进来,别在循环里反复读同一个文件。

    **`failures_total` 要跟着减。** 面板照 `failures_total || failures.length` 念
    「问题明细 · N 项」,只删数组不改总数的话,删掉最后一条之后那一行会写着
    「明细 · 1 项(仅列前 0 条)」,而清单是空的。
    """
    if keys is None:
        keys = dropped_keys()
    if not keys or not isinstance(run, dict):
        return 0
    stages = run.get("stages")
    if not isinstance(stages, dict):
        return 0
    gone = 0
    for stage in stages.values():
        if not isinstance(stage, dict):
            continue
        items = stage.get("failures")
        if not isinstance(items, list):
            continue
        kept = [f for f in items if _item_key(f) not in keys]
        n = len(items) - len(kept)
        if not n:
            continue
        gone += n
        stage["failures"] = kept
        total = stage.get("failures_total")
        if isinstance(total, int):
            # 截断过的那一档:总数减掉这一次剔掉的,但不许掉到清单长度以下
            # (那会让面板念出一个比它正在显示的条数还小的总数)。
            total = max(len(kept), total - n)
            if total > len(kept):
                stage["failures_total"] = total
            else:
                stage.pop("failures_total", None)
    return gone


def strip_state(state) -> None:
    """一份 `/api/pipeline/status` 回包里所有带明细的地方都过一遍。

    **两处:当前这一轮的 `stages`,和 `last_run` 那一份。** 只过一处的话,空闲时
    面板渲染的是 `last_run`(见 `pipeResult`)、跑着的时候渲染的是顶层 —— 漏掉哪
    一处,用户都会在某个时刻看见自己刚删掉的那几条又回来了。
    """
    keys = dropped_keys()
    if not keys:
        return
    strip(state, keys)
    strip(state.get("last_run"), keys)


def strip_runs(runs) -> None:
    """历史清单整份过一遍。**账本只读一次**,别在循环里反复读同一个文件。"""
    keys = dropped_keys()
    if not keys:
        return
    for run in runs or []:
        strip(run, keys)
