# -*- coding: utf-8 -*-
"""启动接口被限流之后,同一个浏览器会话里**下一次导航**要等的那一段。

**为什么需要显式的冷却**:这一档刚被识别出来之前,每个账号都要把
`x_browser.ACCOUNT_SECONDS`(50 秒)整段耗光才报错 —— 2026-10-21 那一轮正是
前 11 段白等凑出的十分钟让限流自己过去,后面 39 个账号才正常读到。**把白等
去掉就必须把这段等待显式补回来**:识别得快了之后,一个账号不到一秒就失败,
整批会以这个速度向同一个受限接口连发几十次,预算还剩一大半就全红收场
(离线模拟:限流 30 秒结束的一轮,51 次导航 15.3 秒试完、51 个账号全失败)。
「同一轮里它自己会恢复」这个结论是在**有**那段等待的前提下观察到的,不能
搬到没有等待的实现上。

**记在会话这一层,不记在某一处异常捕获点上**:时间线、详情页正文补全、
「检查连接」三条路共用一个会话,而其中只有时间线那条会把这一档抛成异常 ——
正文补全是**暂缓**(不抛),外层换下一个账号时压根不知道刚刚撞过限流。所以
由**得出结论的那个消费者**调一次 `of(session).throttled()`,调度层
(`x_browser.harvest`)只读一处。

**为什么不干脆中止整批**(那是 `XRateLimited` 的口径):这一档实测几分钟就
自己过去,中止等于把紧接着就能读到的那几十个账号一起丢掉。所以是「慢下来、
并把没读到的排到队尾重试一次」,而不是「停」。
"""

from __future__ import annotations

import time

#: 第一次撞上之后等多久,再撞翻一倍,封顶 `MAX_WAIT`。45 秒的来头是上面那段
#: 实测:原先每个账号白等的 50 秒就是它凑出来的,去掉之后要补回同一个量级。
#: 封顶是为了别把整轮预算全睡掉 —— 等待换不来任何内容,预算闸会把余下的账号
#: 判成「留待下次同步」(水位照旧不动,下一轮重抓)。
FIRST_WAIT = 45.0
MAX_WAIT = 180.0
#: 切片长度:等待中途要能响应停止请求,不能一口气 sleep 45 秒。
SLICE = 0.5


class SessionCooldown:
    """一个浏览器会话的「下一次导航不许早于」闸门。"""

    def __init__(self, first: float = FIRST_WAIT, maximum: float = MAX_WAIT):
        self.first = first
        self.maximum = maximum
        self.next_wait = first
        self.until = 0.0
        self.hits = 0

    def throttled(self) -> None:
        """记一次启动限流,把下一次可导航的时刻往后推。

        **只在判据得出 `session_limited` 这个结论时调**,不在看见裸的 429 时调:
        启动请求被限了一次、页面照旧渲染出推文的那一种不算卡住(见
        `x_page_state.booted`),对它冷却只会把本来好用的一轮拖慢到预算之外。
        """
        self.hits += 1
        self.until = max(self.until, time.monotonic() + self.next_wait)
        self.next_wait = min(self.next_wait * 2, self.maximum)

    def cleared(self) -> None:
        """一个账号整条读下来了、期间没再撞上:退避回到起点。

        不回退的话,早先那一次会把后面无关的一次限流直接罚成封顶那么久。
        """
        self.next_wait = self.first

    def remaining(self) -> float:
        """还要多久才许再导航(0 = 现在就可以)。"""
        return max(0.0, self.until - time.monotonic())

    def wait(self, *, deadline=None, should_stop=None, announce=None) -> str:
        """等到 `until` 为止,返回给人看的一句话(不用等就是空串)。

        **切片等**:中途按下停止就立刻返回,整轮预算到点也不再往后等 —— 等过
        预算换不来任何内容,外面那道预算闸会把余下的账号判成「留待下次同步」。

        **先说再等**(`announce` 在第一次 sleep 之前调):等完才说的话,这几十秒
        到几分钟里面板上停在上一个账号的进度上,看起来就是「又卡住了」—— 而这
        一档存在的全部意义正是让它不再看起来像卡住;何况那句话紧接着就被下一个
        账号的进度盖掉,页面轮询基本抓不到。
        """
        target = self.until if deadline is None else min(self.until, deadline)
        left = target - time.monotonic()
        if left <= 0:
            return ''
        note = (f'X 限制了访问频率(本轮第 {self.hits} 次),'
                f'等 {left:.0f} 秒再试下一个账号')
        if announce:
            announce(note)
        while (remaining := target - time.monotonic()) > 0:
            if should_stop and should_stop():
                break
            time.sleep(min(SLICE, remaining))
        return note


def of(session) -> SessionCooldown:
    """这个会话的冷却闸,没有就现建一个。**登记那一侧**用(`throttled`/`wait`)。

    挂在会话对象上而不是某个模块全局变量上:整批共用一个会话,而两条通道
    (独立 profile / 直连日常浏览器)各起各的,互不该共享退避。
    """
    existing = getattr(session, '_x_cooldown', None)
    if existing is None:
        existing = SessionCooldown()
        session._x_cooldown = existing
    return existing


def cooling(session) -> str:
    """这个会话还在冷却里吗。返回拦下的原因,空串 = 可以新开页面。

    **冷却是会话级的,不是账号级的。** 一个账号内部还会导航很多次(折叠推文的
    详情页、每一篇长文的文章页),所以「换下一个账号之前等一次」挡不住它们 ——
    **账号边界不等于导航边界**。凡是要新开一个 X 页面的地方都要先问这一句。

    **问到了就扣留,不要在这里睡。** 这一步跑在账号的时间线已经读完之后,睡下去
    花的是整轮预取的预算,换来的只是一篇下一轮就能免费补上的长文正文(扣留不推进
    水位,`put_items` 按 URL 去重)。调度层在换下一个账号之前照旧等满 —— 那一处
    挡的才是真正要紧的那次导航。

    **第三方长文接口不归它管**:那不是 X 的页面,跟这次限流没有关系,而且它恰好
    是这时候唯一还走得通的那条路。

    **只读,不建** —— 不许走 `of()`。没撞过限流的会话压根没有这道闸,也就没有
    什么可等的;而这一步会被形形色色的会话对象调到(`x_post_provenance` 那条
    存量修复链传下来的就是一个裸 `object()`),顺手往人家身上挂一个属性会当场
    `AttributeError` 把整条链带掉。
    """
    gate = getattr(session, '_x_cooldown', None) if session is not None else None
    left = gate.remaining() if gate is not None else 0.0
    return f'X 刚限制了访问频率,这一轮不再新开页面(还要等 {left:.0f} 秒)' if left > 0 else ''
