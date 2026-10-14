"""调度账本:一条定时**这一轮的事实** —— 起过没有、下一次最早什么时候、补了几次、
今天到此为止没有 —— 记在进程里。

盘上那份配置(`last_fired` / `next_run` / `fail_count`)是这几格的**持久化副本,不是
判据的唯一来源**:它写不进去是常事(配置一时读不出、满盘、进程被杀在写之前),而
只认它就等于拿"回写没成功"去证明三件没发生的事:

- 证明"今天还没跑过" → 同一天又自动跑一整轮(日报那一轮还要再花一次模型钱);
- 证明"没到次数上限" → 每次失败都算第一次,`MAX_RETRIES` 永远到不了;
- 证明"已经可以再试了" → 退避整个丢掉,失败的日报每 30 秒重起一轮(实测 3 分钟 7 轮)。

所以**起跑受理和账本推进必须一致**:账本先推进,再去写盘;写盘失败只丢持久化,不许
放宽预算。重启之后这几格空了,那时才回头问盘上那份和**已经落盘的产物**(日报那边是
`_report_done`;页头那条没有产物可问,只能认盘上那一格,写失败时会留一句提示)。

两条定时共用这一份实现(口径只有一份):日报用全部四格,页头那条只用 `done`。
硬约束、事故形态与回归清单见 `docs/decisions/sync-schedule.md`。
"""

from __future__ import annotations

from typing import Any, Callable


class Ledger:
    """一条定时的账本,**按设置文件 + 运行日期归属**。

    跟着设置文件走:设置文件指向别处(切数据目录、测试打桩)时整本作废 —— 它是
    **那一份配置**的回写缓存,换了库就跟换了定时一样。口径同
    `pipeline_state._aggregate_file`。不作废的后果是另一个库的"今天跑完了"把这个
    库今天那一轮吃掉,而且只在同一个进程里出现,查起来毫无线索。

    跟着日期走:换天清零 —— 否则昨天补满的次数会把今天那一轮永久按住。
    """

    def __init__(self, settings_file: Callable[[], Any]):
        self._settings_file = settings_file
        self._file: Any = None
        self.day = ''           # 这几格属于哪一天
        self.next_due = ''      # 下一次最早什么时候(iso 到分),空=按配置里那个时刻
        self.fails = 0          # 今天补了几次
        self.done = False       # 今天到此为止(出报了 / 补满次数了 / 用户按了停止)

    def book(self, day: str = '') -> 'Ledger':
        """对齐归属之后返回自己。给了 `day` 就把账本挪到那一天。

        **只往后挪**:`day` 比账本里那一天更早时(跨零点的那一轮在第二天才收尾)原样
        不动 —— 挪回去会把今天已经推进的 attempt 和退避清掉,于是今天这一轮又能每 30
        秒起一次。
        """
        current = self._settings_file()
        if self._file != current:
            self._file, self.day = current, ''
            self.next_due, self.fails, self.done = '', 0, False
        if day and day > self.day:
            self.day, self.next_due, self.fails, self.done = day, '', 0, False
        return self

    def attempt(self, next_due: str) -> None:
        """起了一轮:把下一次最早时刻推到退避之后。

        **不在这里记"今天到此为止"** —— 起过 ≠ 出报:一次模型 502 之后同一天还要补
        几次(`MAX_RETRIES`),在起跑那一刻就记完成的话用户一整天等不到日报。

        **只许往后推。** 记 attempt 的那一刻,这一轮可能已经收尾了:那一步写的退避
        才是最新的,往回推等于把它作废。**这一条是带的:** 已经收尾成"今天到此为止"
        那一档由调用方的 `done` 守卫挡住(`daily_run._mark_attempt`),剩下的只有
        "补跑那一轮抢在记 attempt 之前收尾、而且跨了整分钟"这一种 —— 产品里起跑那
        一步是另起线程、记 attempt 紧接着就执行,所以去掉这个 `max` 回归红 0 条,
        如实记着:它挡的是以后有人把记账挪到工作线程里去。
        """
        self.next_due = max(self.next_due, next_due)

    def finish(self, fails: int, done: bool, next_due: str) -> None:
        """这一轮收尾:补到第几次、今天到此为止没有、下一次最早什么时候。

        `done` **只进不退**:已经到此为止之后再来一次收尾(重复回调、补跑的残留线程)
        不许把它退回"还能再起"。
        """
        self.fails, self.done, self.next_due = fails, self.done or done, next_due

    def retries(self, persisted: int) -> int:
        """今天已经补了几次:盘上那个和账本里这个**取大的那一个**。"""
        return max(persisted, self.fails)

    def due_at(self, persisted: str) -> str:
        """到期时刻:盘上那个和账本里这个**取更晚的那一个**(退避不许因为写失败而丢)。"""
        return max(persisted or '', self.next_due)
