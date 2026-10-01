# -*- coding: utf-8 -*-
"""kb.db + vault 的维护互斥锁:一把跨进程独占锁,写这两处的人全都认它。

**为什么要有它。** kb.db 和 vault 是同一份事实的两面,改它们的有四条路:存量
清理脚本(`scripts/feed_trim_history.py`)、订阅同步的存盘与导出
(`kb.feeds.sync`)、索引(管道的 index 阶段与 `kb index`)。每一条都是
「先把一份快照读进内存 → 过一会儿再按它写盘」,两两不互斥的话,一边就能用
**删除之前**读到的那份快照把另一边刚删掉的东西复活:

- 清理把一篇旧文从 feeds.db / vault / kb.db 三处删干净,而一轮并发的 index
  拿它删除之前读到的那个 `Doc` 完成 upsert —— `documents` 回到 1、全文检索照样
  命中,可 vault 里那个文件已经不存在:搜得到、点开没有原文,重跑清理也找不到
  它(feeds 行已经没了)。下一次索引会把它收掉(`remove_missing_documents` 按
  磁盘上还在的那些算),但在那之前库里就躺着这么一行假结果
  (2026-10-01 审查实测)。
- 同步那一侧是同一个形态,只是快照换成了订阅行和待导出清单
  (见 `kb.feeds.sync._save_result` / `export` 上的说明)。

**粒度是整趟,不是只圈写的那几句。** 竞态的那一半在「读」:scan 读到的是删除
之前的文件内容,只把 upsert / remove 圈起来的话,旧快照照样写得进去。所以
scan → upsert → `remove_missing_documents` 一整段都在锁里。

**向量编码也在锁里。** `embed_pending_chunks` 先把「还没有向量的 chunk」整份
读出来(id + 正文),再分批写回 `chunk_vec`;而 `chunk_vec` 是 sqlite_vec 的
虚表,**没有外键级联**(`remove_missing_documents` 是按 chunk id 显式删它的),
清理在这中间删掉那些 chunk 的话,后面几批会按已经不存在的 chunk id 把向量插
回去 —— 这些孤儿行谁也不会再来收(「孤儿向量 0」是生产审计在看的一条不变量)。
代价是一轮大编码会把锁攥上几分钟(`--reembed` 更久),而被它挡住的只有一次性
运维脚本 —— 它本来就不该在重新编码的中途删文档。

**等不到就别做。** 等待有上限(`KB_MAINT_LOCK_WAIT`,默认 900 秒)并在等的时候
出声(`on_wait`);超时抛 `LockBusy`,调用方一律「什么都没动」收场(index 阶段
标红写明原因、`kb index` 退 2、清理脚本退 2 且一个字节没改)。无限等更坏:管道
是单例,一个卡住的持锁者会把面板上的同步、定时同步和日报一起冻死,而索引漏跑
一轮下一轮就补上了 —— 不可逆删除和"假结果留在库里"才是不能赌的那两头。

**不会抱死。** 这是系统里唯一一把跨进程维护锁,没有任何路径在持有它的时候再去
取别的锁;同线程重入直接放行(flock 认的是 open file description,同进程换一个
fd 再锁会把自己锁死)。

锁的是 `data/` 下**单独的 lockfile**,只创建不删除:unlink 会让「A 持锁 →
B 删文件 → C 新建再持锁」出现两个持锁者(和 `secrets.json` 那把锁同一套理由)。
flock 随进程退出自动释放,所以持锁者崩了也不会把别人堵死。文件名沿用 feeds
那一版的 `feeds-trim.lock`:改名等于在升级窗口里让老进程锁 A、新进程锁 B,两个
持锁者同时写同一个库 —— 名字只是一个路径,不值这个风险。
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from contextlib import contextmanager

from kb.config import DATA_DIR

LOCK_NAME = 'feeds-trim.lock'

# 等不到锁的上限(秒)。宽到正常绝不会碰到:持锁的要么是一轮索引、要么是一次
# 运维清理,量级都在分钟以内。
WAIT_SECONDS = float(os.environ.get('KB_MAINT_LOCK_WAIT') or 900)
# 多久跟调用方说一次「还在等」。面板/日志那一侧每条提示都会占一行日志(环形
# 80 行),所以别太密;第一次是立刻说的。
NOTIFY_EVERY = 60.0
_POLL = 0.25


class LockBusy(RuntimeError):
    """等到超时都没拿到锁。抛出时这一趟一个字节都还没动,这是安全的那一侧。"""


_held = threading.local()


def _acquire(fh, wait: float, on_wait) -> None:
    """拿到锁才返回;超时抛 `LockBusy`。

    flock 没有超时参数,所以用非阻塞加轮询。第一次就拿到的话不会调 `on_wait`
    —— 常态下不该有任何「正在等」的噪音。`on_wait` 可以自己抛(index 阶段用它
    响应用户按下的「停止」),异常原样穿出去。
    """
    deadline = time.monotonic() + max(wait, 0.0)
    started = time.monotonic()
    told: float | None = None
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            pass
        waited = time.monotonic() - started
        if on_wait and (told is None or waited - told >= NOTIFY_EVERY):
            told = waited
            on_wait(waited)
        if time.monotonic() >= deadline:
            raise LockBusy(
                f'另一个 kb.db 维护任务(索引或存量清理)占着维护锁,等了 '
                f'{int(waited)} 秒还没让开;这一趟什么都没做。')
        time.sleep(_POLL)


@contextmanager
def maintenance_lock(wait: float | None = None, on_wait=None):
    """持有维护锁跑这一段。整段的理由见模块说明。

    wait: 等待上限(秒),缺省 `WAIT_SECONDS`;0 = 拿不到就立刻抛。
    on_wait: `on_wait(已等秒数)`,只在真的等上了才调(立刻一次,之后每
             `NOTIFY_EVERY` 秒一次)。
    """
    if getattr(_held, 'on', False):
        yield
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = (DATA_DIR / LOCK_NAME).open('a+')
    try:
        _acquire(fh, WAIT_SECONDS if wait is None else wait, on_wait)
        _held.on = True
        yield
    finally:
        _held.on = False
        fh.close()                       # 关闭即释放 flock
