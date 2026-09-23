# -*- coding: utf-8 -*-
"""一轮归类的结果对象。

从 `taxonomy.py` 拆出来只因为那个文件又一次越过 700 行上限,而 `Verdicts` 是
里面最独立的一块:纯值对象 + 三个收尾动作,不碰文件、不读 `CACHE_DIR`、没有
被测试改写的模块变量(那种搬走之后 `T.CACHE_DIR = tmp` 只改到一份副本,理由
同 `taxo_tables.py`)。`taxonomy` 原样再导出这个名字。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdicts:
    """一轮归类的结果。

    `complete=False` 只在**全量重判没做完**时出现,意思是「这份 codes 是半成品,
    别拿它覆盖页面」:分类表刚改过、旧缓存整份失效,而这一轮没能给每个候选都
    拿到一个有效 code(批次抛异常,或模型返回合法 JSON 却漏了一截)。日常增量
    判定永远是 complete——那时候没判到的几条显示成「待归类」是既定行为,不该
    拦着图谱更新。

    **判完 ≠ 已提交**。重判的新结论一直攥在半成品文件里,要等调用方把新图验收
    通过、页面也落盘了,才 `commit()` 换上正式缓存:两件事不在一个事务里的话,
    「缓存已经换掉、页面校验才拦下」就成了半吊子状态——页面保住了,可那份还能
    用的旧缓存已经没了,之后每一轮都会命中新缓存、不再问模型,流水线自己爬不
    出来(2026-09-22 第 4 轮审查发现)。
    """
    codes: dict[str, str]
    complete: bool = True
    migrating: bool = False
    pending: int = 0               # 还差多少条没判(仅重判期间有意义)
    commit_fn: object = None       # 待提交的正式缓存;None = 没什么可提交的
    discard_fn: object = None      # 扔掉半成品;None = 没有半成品要扔
    keep_fn: object = None         # 这一轮不发布时:把自己判的并回暂存
    reason: str = ""               # complete=False 时:为什么不能用(给人看的)

    def commit(self) -> None:
        """新图验收通过、页面已落盘之后调:把半成品原子换成正式缓存。"""
        if self.commit_fn is not None:
            self.commit_fn()

    def discard(self) -> None:
        """新图验收**没**通过时调:连半成品一起扔掉,下一轮重新问模型。

        判定本身完整、只是结果不作数(全判成「不是 skill」之类)时,半成品留着
        的话下一轮全命中、不再问模型,同一个坏结果会一直卡在那儿——扔掉才能在
        模型恢复后自己走出来。**判到一半**那种不要扔(那是进度,下次接着判)。
        """
        if self.discard_fn is not None:
            self.discard_fn()

    def keep(self) -> None:
        """这一轮**不发布**(基线被别人换过)时调:把自己判出来的并回暂存。

        共享暂存是整份覆盖的:另一个 writer 在我们判完之后写一次,就把我们这份
        盖掉了;它发布成功还会把暂存整个删掉。所以「不发布也一条都不丢」不能靠
        「分类阶段写过一次」——那条唯一判定可能既不在正式缓存、也不在暂存,白花
        一次模型钱,日志还说留下了(2026-09-23 复审第 7 轮发现)。这里是读-改-写
        地并回去,而且跑在发布锁里。
        """
        if self.keep_fn is not None:
            self.keep_fn()
