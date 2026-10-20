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

**凡是还显示得到的那些失败明细里实际出现的身份,一条都不许忘。** 原来是按条数封顶
(2000),靠「历史最多 20 轮 × 每轮最多 50 条」这个乘法留出余量 —— 而历史改成按天留
之后那个乘法就不成立了:三天之内可以有几百轮,账本却先把最老那几条忘掉,于是用户点过
「不再抓取」的那几篇**又出现在还翻得到的那几轮里**。
**条数、时刻都只是代理信息,证明不了「没人再引用它」。** 所以判据改成**实际引用**:
把三处还显示得到的结果(历史那几轮、最新那一轮、正在跑这一轮)里的身份收齐,在册的
一条都不回收 —— 包括升级前那些没有时刻的旧身份(实测:2000 条旧账本里最早那条仍被一
条可见历史引用,记下第 2001 条时它被挤掉,那一篇在明细里从 0 条变回 1 条)。
时刻只是额外的宽口径(最近记的留着),**读不出来就一条都不回收**(见 `_recycle`)。

硬约束、事故形态和回归清单在 `docs/decisions/failure-drop.md` 的第一节 ——
**动这一块之前先读那一份**。
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from datetime import timedelta

from kb import pipeline_state as ps
from kb.pipeline_persist import _dump_bytes, _load_json, _write_atomic
from kb.pipeline_retention import parse_stamp, run_stamp

#: 条数上限,**只削得掉「判不出时刻」那些**(升级前记下的那几条,它们没有时刻、
#: 按时间回收不掉)。带时刻而且还在射程里的一条都不动 —— 那才是这本账的本职。
_KEEP = 2000
#: 回收那一刻给墙上时钟留的余量(天)。判据本身比的是两个**各自落盘**的时刻(账本
#: 这一条记在什么时候、历史里最老那一轮结束在什么时候),两边都出自 `_now()`;留一天
#: 是怕时钟被挪过、或者两件事落在同一分钟的边上。宁可多留一天,忘掉一条是用户看得见的。
_RECYCLE_MARGIN_DAYS = 1

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


def _item_keys(item) -> set[str]:
    """包含采集器核实的同文别名；旧账本的原档案身份也继续有效。"""
    drop = item.get("drop") if isinstance(item, dict) else None
    if not isinstance(drop, dict):
        return set()
    channel, item_id = str(drop.get("channel") or ""), str(drop.get("id") or "")
    if not channel or not item_id:
        return set()
    identities = (item_id,)
    try:
        from kb.collectors import get_collector, item_aliases

        identities = item_aliases(get_collector(channel), drop)
    except Exception:
        pass
    return {_key(channel, identity) for identity in identities}


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


def _visible():
    """**还显示得到的那些结果**:返回 `(最老那一轮的时刻, 里面实际出现的身份集合)`。

    **说不清一律 `None`**,那一趟一条都不回收 —— 回收是在**拿不准的时候固化一个不可
    恢复的终态**,而代价不对称:多留几条身份只是文件大几十字节,忘掉一条是用户亲手
    处理过的那一篇又回到面板上。所以历史走**严格版**读法(读不出来、形状不对都抛),
    不走 `load_history()` 那条宽口径 —— 后者把「读不出来」和「还没有记录」返回成同一
    个空列表,而据它回收等于把一次瞬时读失败变成永久遗忘。

    **三处都要收**:历史那几轮、最新那一轮(`last_result()`)、正在跑这一轮
    (`get_state()`)。只看历史会漏掉留档失败那一档 —— 那一轮根本不在历史文件里,而
    面板正显示着它,它引用的身份一个都不能回收。

    最老那个时刻只给「最近记的留着」那道宽口径当锚点;读不出时刻的那几轮不参与取
    最小值(只要有一行坏记录就永远回收不动),它们**引用的身份照旧在册**——
    那条才是不变量。

    **那个时刻只从留档那几轮取,活着那两份只进引用集。** 它们说的是**此刻这一轮**
    (多半就是最新那一轮),而留档里一轮都没有时拿它当「最老还显示得到的时刻」,等于
    把所有更早的身份一次判死 —— 而这本账里的时刻**正是在更早那些轮次里记下的**:
    实测历史文件还不存在、状态里只有这一轮的开工时刻,一个月前记下的那条当场被回收。
    收益只是让一本还很小的账再小一点,代价不可恢复。所以留档里一轮都没有 = 下限未知
    = 这一趟不按时间回收(上限照旧削得掉判不出时刻、又没人引用的那些)。
    """
    try:
        rounds = list(ps._history_rounds_strict())
        # 引用集三处都收;下限只认 `rounds`,理由在上面
        runs = rounds + [ps.last_result() or {}, ps.get_state()]
        used = set()
        for run in runs:
            stages = run.get("stages") if isinstance(run, dict) else None
            for stage in (stages or {}).values() if isinstance(stages, dict) else ():
                items = stage.get("failures") if isinstance(stage, dict) else None
                for item in items if isinstance(items, list) else ():
                    used.update(_item_keys(item))
        stamps = [t for t in (run_stamp(r) for r in rounds) if t is not None]
    except Exception:
        return None
    return (min(stamps) if stamps else None), used


def _recycle(keys: list[str], stamps: dict, visible, *, keep: int | None = None):
    """回收:返回 `(留下的身份, 留下的时刻表)`。`keys` 按插入顺序(最老在前)。

    判据是**「它还会不会显示出来」**,不是条数也不是时刻:

    - **还显示得到的那些结果里实际出现了它** —— 留着,`_KEEP` 也不许动它。这一条是
      不变量,升级前那些没有时刻的旧身份一样受它保护。
    - 记的时刻在「最老那一轮」之后 —— 留着(额外的宽口径:刚记的那几条不碰)。
    - **判不出时刻又没人引用**(升级前记的、被手改坏的)—— 留着;条数超了只削得到
      这一类,最老的先走。
    - `visible is None`(说不清现在显示得到什么)—— 一条都不回收。
    """
    keep = _KEEP if keep is None else keep
    if visible is None:
        return keys, stamps
    floor_at, used = visible
    old = (floor_at - timedelta(days=_RECYCLE_MARGIN_DAYS)) if floor_at else None
    spare, kept = [], []
    for k in keys:
        at = parse_stamp(stamps.get(k))
        if k in used:
            kept.append(k)
        elif at is None:
            spare.append(k)          # 判不出时刻、也没人引用:条数超了先削这一类
            kept.append(k)
        elif old is None or at >= old:
            kept.append(k)
    over = len(kept) - keep
    if over > 0:
        doomed = set(spare[:over])
        kept = [k for k in kept if k not in doomed]
    live = set(kept)
    return kept, {k: v for k, v in stamps.items() if k in live}


def remember(channel: str, item_id: str) -> None:
    """记一笔「这一条已经处理掉了」。写不成会抛,由调用方如实说一句。

    **先读再写**,顺带回收一次(判据在 `_recycle`:回收得掉的是「还显示得到的那几份
    结果里压根没出现它」的那些,不是「最老的那几条」)。读不出来当空账本 —— 那会让此
    前记过的几条重新显示出来,但不会让这一条记不上(「拿不准就别固化终态」管的是
    **终态**,这里的终态在采集器那边,这份账本只是显示)。

    **每条带一个时刻**(`at`),回收要用它。老账本没有这张表,那几条按「判不出时刻」
    留着 —— 加字段不动既有那个 `dropped` 列表(元规则「只增不减」),旧读法照旧。

    **整段在 `_exclusive()` 里**:读、合并、写回三步之间不许有别人插进来 ——
    理由和事故形态写在那个上下文管理器的说明里。
    """
    key = _key(channel, item_id)
    # **那几份结果在锁外面读。** 它们是另外几份状态、只用来算回收判据,搬进临界区
    # 只会把这把锁的持有时间拉长(历史那份文件可能有几 MB)。
    visible = _visible()
    with _exclusive():
        raw = _load_json(_file())
        got = raw.get("dropped") if isinstance(raw, dict) else None
        keys = ([k for k in got if isinstance(k, str) and k]
                if isinstance(got, list) else [])
        at = raw.get("at") if isinstance(raw, dict) else None
        stamps = ({k: v for k, v in at.items() if isinstance(k, str) and isinstance(v, str)}
                  if isinstance(at, dict) else {})
        keys = [k for k in keys if k != key]
        keys.append(key)
        stamps[key] = ps._now()
        keys, stamps = _recycle(keys, stamps, visible)
        _write_atomic(_file(), _dump_bytes({"dropped": keys, "at": stamps}))


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
        kept = [f for f in items if _item_keys(f).isdisjoint(keys)]
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
