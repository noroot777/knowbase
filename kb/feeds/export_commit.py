# -*- coding: utf-8 -*-
"""**一篇 md 的「可退回交付」** —— `sync._export` 那一步的文件侧动作。

硬约束在 `docs/decisions/intake-diagnostics.md` 第一节(交付那一道)。这里只管
文件,不认识 `feed_items`:谁有权交付由调用方那条带判据的 UPDATE 说。

**为什么要可退回。** 交付是两件事:vault 里那个文件、库里那条回执。顺序只有两种,
而**两种都有一段窗口**:
- 先确认再落盘 → 确认落了、进程在 `os.replace` 之前被杀掉:库里说「已交付」而
  vault 里没有这一篇,**此后任何一轮都不会再选中它**(`exported_hash=content_hash`
  把它从导出队列里永久摘掉)—— 不可恢复的那个方向(元规则:漏报是静默丢数据)。
- 先落盘再确认 → 这中间人点了「暂缓 / 不再收录」:那颗按钮承诺的是「正文、文件
  和已经入库的内容一个字都不动」,而文件已经换过了。

所以保留第二种顺序(它的每一种失败都停在「文件在、回执没占上」,下一轮重算同一份
内容、原子替换同一个路径),并且**让落盘这一下可以退回去**:覆盖之前先把旧那一份
硬链一份到旁边,拒绝时换回来;头一回落盘的拒绝时删掉。于是「撤回」不再是一句假定,
而 `os.replace` 本身照旧是原子的 —— 读者永远看到完整的一份,不是半截。

**备份做不出来就不交付。** 硬链和拷贝都失败时直接抛:那一篇这一轮记一笔失败、旧
文件一个字节都没动,比「换上去又退不回来」好。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

#: 旧那一份暂存的后缀。**不以 `.md` 结尾** —— 索引、日报取材、清理脚本都按
#: `*.md` 枚举 vault,叫 `xxx.md.prev` 就不会被它们当成一篇文章收进去。
SUFFIX = '.prev'


class Delivery:
    """`path` 这一篇的一次交付。先 `replace(temp)`,再 `settle()` 或 `refuse()`。"""

    def __init__(self, path: Path):
        self.path = path
        self.backup = path.with_name(path.name + SUFFIX)
        self.kept = False

    def replace(self, temp: Path) -> None:
        """旧那一份留一个硬链,再把新这一份原子替换上去。"""
        self.kept = False
        # 上一轮崩在中途留下的那一份:直接让位,它的内容早就不作数了(库里那条
        # 回执没占上,下一轮渲染的是当时的内容)。
        self.backup.unlink(missing_ok=True)
        if self.path.exists():
            try:
                os.link(self.path, self.backup)
            except OSError:
                # 跨设备、不支持硬链接的卷:退一步做一份真拷贝。再失败就让它抛
                # 出去 —— 调用方会把这一篇记成这一轮的失败,而旧文件没动过。
                shutil.copy2(self.path, self.backup)
            self.kept = True
        os.replace(temp, self.path)

    def settle(self) -> None:
        """获准了:备份不要了。"""
        self.backup.unlink(missing_ok=True)

    def refuse(self) -> None:
        """没获准:退回到这次交付之前的样子。

        覆盖过的换回旧那一份(**也是 `os.replace`**,原子的);头一回落盘的删掉。
        """
        if self.kept:
            os.replace(self.backup, self.path)
        else:
            self.path.unlink(missing_ok=True)
        self.backup.unlink(missing_ok=True)
