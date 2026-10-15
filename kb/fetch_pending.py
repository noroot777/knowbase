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

- **不取那把文件锁。** 凭证按进程命名(`TAG`),写的人只有一个,所以不需要互斥 ——
  而账本锁坏掉(旁挂文件被占成目录、目录只读)正是我们要留证的那一档。
- **原子整份写**(`_write_atomic`),不追加:它表达的是「此刻我还欠哪几轮」这个
  **当前状态**,不是流水。欠的行全部落盘之后撤掉它(`put` 收到空 keys)。
- **只记轮次身份,不把那几行复制进来。** 复制等于又开一份账本:凭证里的回执脱离了
  它那条开轮行,等那一轮被轮换清掉之后,折叠会报「有成果、却找不到它那一轮」,整份
  账本从此「读不出来」。能救回条数的场合也有限 —— 进程多半是在交回执**之前**没的
  (登记行还攥着,回执压根还没产生)。把结论压到**下界**(面板写「至少 +N 条」)
  已经回答了用户那个问题:这个数不确定,想知道准数就再同步一次。

读的一侧两档分明:**读不出来算「说不清」,不算「没有」**(`held` 的 `ok=False`)。
合成一个空集的话,一次读故障就把「有一轮的证据还扣着」说成「一切都落盘了」,而
这整个模块存在的理由就是不许出现那句话。

剩下真救不回来的只有一档:**账本和这笔凭证一起写不进去,而进程随后就没了。**
那一档有另一道闸挡在前面 —— 数据目录写不进去时「开工标记」就落不下来,而那一笔
是起跑的前提(`kb.pipeline_open.mark`),这一轮压根不会跑,也就落不出文章来。

硬约束、事故形态和回归清单在 `docs/decisions/daily.md`。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from kb.pipeline_persist import _write_atomic

# 本进程那份凭证的文件名。**pid 后面还要带一截随机**:只用 pid 的话,重启后撞上
# 同一个 pid 的新进程会把上一条命留下的凭证顶掉 —— 而那份凭证正是「有一轮的证据
# 跟着进程没了」的唯一证据。
TAG = f'{os.getpid()}-{os.urandom(4).hex()}'
SUFFIX = '.json'


def _mine(folder: Path) -> Path:
    return folder / (TAG + SUFFIX)


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
    """
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get('keys'), list):
        return None
    if not str(obj.get('day') or ''):
        return None                 # 没有日子的凭证排不进保留窗口
    return obj


def put(folder: Path, keys, day: str, floor: str = '') -> None:
    """登记/撤掉**本进程**那笔凭证:`keys` 非空 = 这几轮的证据还在我手里;空 = 都落盘了。

    写不上就算了:那一档(账本和凭证一起写不进去)在模块开头,它另有一道闸。
    **撤销失败同样不出声** —— 留着一份已经落盘的凭证只会让那几轮继续报「至少」,
    那是安全的一侧,而下一次写成功会再撤一次。

    `floor` 非空时顺手把过了保留窗口的孤儿凭证删掉(某个进程没了留下的)。
    """
    try:
        if keys:
            folder.mkdir(parents=True, exist_ok=True)
            _write_atomic(_mine(folder), json.dumps(
                {'day': day, 'keys': sorted({str(k) for k in keys if k})},
                ensure_ascii=False).encode('utf-8'))
        else:
            _mine(folder).unlink(missing_ok=True)
    except OSError:
        pass
    if floor:
        _prune(folder, floor)


def held(folder: Path, floor: str) -> tuple[set[str], bool]:
    """盘上还扣着证据的那几轮(别的进程、或者重启前的自己),外加「读全了没有」。

    `floor` 之前那几份忽略 —— 账本自己也只留到那天(`fetch_rounds._floor`,两处
    必须同一个口径:凭证留得比账本久的话,它指的那一轮早被轮换清掉了,`today()`
    会从此永久答「读不出来」)。
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
    """删掉过了保留窗口的凭证。

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
