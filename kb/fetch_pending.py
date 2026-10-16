# -*- coding: utf-8 -*-
"""「这几轮的证据还攥在我手里、没写进账本」—— 一笔独立凭证,**不走账本那把锁**。

当日抓取账本(`kb.fetch_rounds`)的每一次写都在一把旁挂的文件锁里,锁取不到就
一行都不写、攥在内存里等下次。那份内存状态随进程消失,于是剩下这一刀:**登记行
没落盘、进程又没了** —— 盘上没有任何痕迹说明那个子轮参与过,而它可能已经往 vault
落了文章。上一版把这一档记在模块开头的「只有一种情形救不回来」里,第 3 轮审查实测
它不是理论风险:真实导出里第二个子轮的登记行因为锁暂时不可用没落下去,随后 `os._exit`
—— 盘上第一个子轮的开轮行、回执、结束行全部合法,`today()` 据此报**确定的 1 条**,
而 vault 里躺着两篇。

**所以「参与过」这件事不许只存在账本里。** 每一次攥住行的时候,本进程另外留一笔
凭证:只说「这几轮(key)的证据在我手里」,不复制那些行。它和账本有三处不一样,
三处都是故意的:

- **不取那把文件锁。** 凭证按进程命名(`TAG`),写的人只有一个,所以不需要和别人
  互斥 —— 而账本锁坏掉(旁挂文件被占成目录、目录只读)正是我们要留证的那一档。
- **原子整份写**(`_write_atomic`),不追加:它表达的是「此刻我还欠哪几轮」这个
  **当前状态**,不是流水。欠的行全部落盘之后撤掉它(`put` 收到空 keys)。
- **只记轮次身份,不把那几行复制进来。** 复制等于又开一份账本:凭证里的回执脱离了
  它那条开轮行,等那一轮被轮换清掉之后,折叠会报「有成果、却找不到它那一轮」,整份
  账本从此「读不出来」。能救回条数的场合也有限 —— 进程多半是在交回执**之前**没的
  (登记行还攥着,回执压根还没产生)。把结论压到**下界**(面板写「至少 +N 条」)
  已经回答了用户那个问题:这个数不确定,想知道准数就再同步一次。

**一次写就是一代,代与代各占一个路径,路径上的内容从此不改**(`_slot`:
`<TAG>-<第几代>.json`)。上一版是「一个进程一个固定路径、每次原子替换」,第 4 轮
审查实测它漏了一整类:**过期清理按它读到的旧内容,删掉的是所有者刚替换上去的新
一代** —— 清理方 `_load` 读出 5 天前那份、判了过期,所有者这期间原子替换成当天的
身份,清理方随后按同一个路径 `unlink`,当天那笔证据就此消失,而 vault 里的文章还在。
「两边各自原子」推不出「整件事原子」。可达性也不靠撞运气:**常驻进程一直用同一个
`TAG`**,上一次撤销失败(删不掉)之后它跨过保留窗口再工作,旧内容和新内容就是同一个
路径。分代之后这一类整体消失:

- **清理只撤销自己确认过的那一代。** 删除的对象是「我刚读出来、判了过期的**那个
  路径**」,而那个路径的内容不会再变 —— 判过期和删除之间不存在「它变新了」这件事。
- **所有者只收自己写新一代**之前**就看见的那几代。** 两条线程各写一代时,后写的
  那一代一定不在先写者的名单里,所以**最新那一代永远留在盘上**。
- **新一代没落下来的时候,旧的那几代一个都不许删** —— 那是「我还欠着行」唯一的
  说法,删了就回到「盘上一个字都不说明它参与过」。

读的一侧两档分明:**读不出来算「说不清」,不算「没有」**(`held` 的 `ok=False`)。
合成一个空集的话,一次读故障就把「有一轮的证据还扣着」说成「一切都落盘了」,而
这整个模块存在的理由就是不许出现那句话。解析口径走
`pipeline_persist._load_json`(**兜住所有异常**):按异常类型在这里另列一份的后果是
实测的 —— 深嵌套 JSON 抛的 `RecursionError` 不在那张单子上,它从这里逃出去会把整次
抓取拦在采集器之前(第 4 轮审查 N5)。

**而「这一笔到底留下了没有」要读回来确认**(`mine`):`put` 写不上不出声(留着
已经落盘的旧一代只是继续报「至少」,那是安全的一侧),所以调用方不能拿它的返回值
当凭据。抓取那一侧的闸就建在 `mine` 上:**账本的登记行和这笔凭证都没留下**时不许
往下抓(口径在 `fetch_rounds.open_round`)。这两处就是闸认的全部证据 —— 它们的寿命
正好是那笔欠账的寿命(追加即不可撤 / 手里还欠行就在),而第 4 轮一度算进来的第三处
「开工标记」寿命是**那一轮运行**的(正常收尾就清掉),第 5 轮审查据此证伪了它。

硬约束、事故形态和回归清单在 `docs/decisions/daily.md`。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from kb.pipeline_persist import _dump_bytes, _load_json, _write_atomic

# 本进程那几代凭证的前缀。**pid 后面还要带一截随机**:只用 pid 的话,重启后撞上
# 同一个 pid 的新进程会把上一条命留下的凭证顶掉 —— 而那份凭证正是「有一轮的证据
# 跟着进程没了」的唯一证据。
TAG = f'{os.getpid()}-{os.urandom(4).hex()}'
SUFFIX = '.json'
# 代号和前缀之间那个分隔符。`TAG` 自己的形状是 `<pid>-<8 位十六进制>`,所以
# `TAG + SEP` 开头的名字只可能是本进程写的(别的 TAG 要同 pid 同随机数才撞得上,
# 那就是同一个 TAG)。
_SEP = '-'
_gen = 0
_gen_lock = threading.Lock()


def _slot(folder: Path, gen: int) -> Path:
    return folder / f'{TAG}{_SEP}{gen}{SUFFIX}'


def _next_slot(folder: Path) -> Path:
    """下一代的路径。代号只增不复用:同一个路径不许写第二次(见模块开头)。"""
    global _gen
    with _gen_lock:
        _gen += 1
        return _slot(folder, _gen)


def _mine_all(folder: Path) -> list[Path]:
    """盘上**本进程**写下的那几代(新的旧的都算)。列不出来按「一代都没有」算 ——
    这个判据只用来决定「要不要收旧的」,收不掉只是多留几份,安全的一侧。
    """
    paths, _ = _certs(folder)
    return [p for p in paths if p.name.startswith(TAG + _SEP)]


def _certs(folder: Path) -> tuple[list[Path], bool]:
    """这个目录里的凭证文件。目录不在 = 确定没有;列不出来 = 说不清。"""
    try:
        return sorted(p for p in folder.iterdir()
                      if p.name.endswith(SUFFIX)), True
    except FileNotFoundError:
        return [], True
    except OSError:
        return [], False


def _load(path: Path) -> dict | None:
    """读一份凭证。**读不出来、或者根不是带 keys 的字典,一律交 `None`(说不清)** ——
    半截写入不会出现(整份原子替换),但手改过、别的版本写的、坏块都会。

    解析走 `_load_json`(兜住所有异常,口径只有那一份):在这里按异常类型另列一张
    单子的话,深嵌套 JSON 的 `RecursionError` 会逃出去,而这个函数的调用方之一是
    `put` → `_prune`,于是一份坏旁文件能把 `open_round` 整个抛掉(第 4 轮审查 N5)。
    """
    obj = _load_json(path)
    if not isinstance(obj, dict) or not isinstance(obj.get('keys'), list):
        return None
    if not str(obj.get('day') or ''):
        return None                 # 没有日子的凭证排不进保留窗口
    return obj


def put(folder: Path, keys, day: str, floor: str = '') -> None:
    """登记/撤掉**本进程**那笔凭证:`keys` 非空 = 这几轮的证据还在我手里;空 = 都落盘了。

    写的是**新的一代**,不覆盖任何已有路径(理由在模块开头:覆盖和跨进程删除撞在
    同一个路径上时,过期清理会按旧内容删掉新一代)。写成之后才收自己**这次之前**
    看见的那几代 —— **新一代没写成就一代都不收**,那时旧的那几份是「我还欠着行」
    唯一的说法。

    写不上就算了:那一档另有一道闸(`fetch_rounds.open_round` 读回来核对两处证据,
    一处都没有才拦)。**撤销失败同样不出声** —— 留着一份已经落盘的凭证只会让那几轮
    继续报「至少」,那是安全的一侧,而下一次写成功会再撤一次。

    `floor` 非空时顺手把过了保留窗口的孤儿凭证删掉(某个进程没了留下的)。
    """
    older = _mine_all(folder)
    if keys:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _write_atomic(_next_slot(folder), _dump_bytes(
                {'day': day, 'keys': sorted({str(k) for k in keys if k})}))
        except OSError:
            older = []              # 新一代没落下来:旧的那几代还得替它说话
    for path in older:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if floor:
        _prune(folder, floor)


def mine(folder: Path) -> set[str]:
    """**从盘上读回来**:本进程那几代凭证此刻记着哪几轮。

    `put` 写不上不出声,所以「我这一笔留下了没有」只能读回来问 —— 抓取那一侧的闸
    用的就是它(`fetch_rounds.open_round`)。不按保留窗口筛:这里问的是「我刚写的
    那一笔在不在」,不是「盘上还有哪几轮欠着」(那是 `held`)。
    """
    keys: set[str] = set()
    for path in _mine_all(folder):
        obj = _load(path)
        if obj is not None:
            keys.update(str(k) for k in obj['keys'] if k)
    return keys


def held(folder: Path, floor: str) -> tuple[set[str], bool]:
    """盘上还扣着证据的那几轮(别的进程、或者重启前的自己),外加「读全了没有」。

    `floor` 之前那几份忽略 —— 账本自己也只留到那天(`fetch_rounds._floor`,两处
    必须同一个口径:凭证留得比账本久的话,它指的那一轮早被轮换清掉了,`today()`
    会从此永久答「读不出来」)。

    同一个进程留下好几代时取并集:旧一代记的是更早那个时刻还欠着哪几轮,**多报
    「至少」是安全的一侧**,而漏掉任何一代都是静默少记。
    """
    paths, ok = _certs(folder)
    keys: set[str] = set()
    for path in paths:
        obj = _load(path)
        if obj is None:
            ok = False              # 读不出来是「说不清」,不是「没有」
            continue
        if str(obj['day']) < floor:
            continue
        keys.update(str(k) for k in obj['keys'] if k)
    return keys, ok


def _prune(folder: Path, floor: str) -> None:
    """删掉过了保留窗口的凭证。**删的是「刚读出来、判了过期」的那一个路径**,而
    路径上的内容不会再变(分代,见模块开头)—— 所以「判过期」和「删掉」之间不存在
    「它已经被换成当天的新一代」这件事,那正是第 4 轮审查实测的那一类。

    **读不出来的那几份不删** —— 它们正是「说不清」那一档的依据(`held`),删掉
    等于把一次读故障固化成「一切都落盘了」。删不掉也不出声:留着只会多报「至少」。
    """
    paths, _ = _certs(folder)
    for path in paths:
        obj = _load(path)
        if obj is None or str(obj['day']) >= floor:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
