# -*- coding: utf-8 -*-
"""这一轮是**人按的**还是**定时自己跑的** —— 口径只有一份。

为什么要有这么一份:这个事实只有一个消费方(抓取要不要跳过来源退避),却曾经在
**四个入口各判一次** —— 「重试失败项」、手动收录那颗直接起跑、起跑弹窗走管道那条、
起跑弹窗勾了「跑完出日报」那条。前三个各自写死了一句「这一轮带 force」,第四个压根
没人写,于是同一颗「开始同步」按钮,勾不勾「跑完出日报」是两种行为:不勾抓得到刚
失败的那条订阅,勾上就被静默跳过(只记一笔 `deferred`,而页面说「同步已开始」)。
这条缺口此前是一个入口一个入口补的,每次都只补了自己看得见的那一处 —— 元规则
「口径只有一份」说的正是这种形态。

所以现在入口只**声明**自己是怎么被触发的(`MANUAL` / `SCHEDULE`),判和盖都在这里。

**退避是什么**:一条订阅抓失败了,`kb.feeds.sync._save_failure` 会给它记一个
`retry_at`(最短 1800 秒)。那道闸是给**自动轮次**用的,免得一直去撞同一个坏地址;
而「失败之后手动点一次同步」恰恰是绕开它唯一的办法,所以人按的那一轮必须跳过它。

硬约束与事故形态写在 `docs/decisions/sync-entry.md`(弹窗那一侧)和
`docs/decisions/sync-schedule.md`(定时那一侧)。
"""
from __future__ import annotations

#: 人按的:界面上那几颗按钮、手动调 API 的脚本。
MANUAL = 'manual'
#: 定时自己跑的:只有 `kb.daily_run.tick` 这一条。
SCHEDULE = 'schedule'


def hand_pressed(trigger: str | None = MANUAL) -> bool:
    """这一轮是不是人按的。**没说就算手动。**

    方向是安全的那一侧(元规则那条):误判成手动顶多多抓一次,误判成定时是**静默
    不同步** —— 用户点了同步、退避中的那几条被跳过,而面板上一个字都不说。所以
    将来哪个调用方忘了声明,落在重抓那一侧。
    """
    return str(trigger or MANUAL) != SCHEDULE


def apply_force(crawl_filter: dict | None, trigger: str | None = MANUAL) -> dict | None:
    """把触发方式落到抓取范围上:人按的那一轮**跳过来源退避**。

    `force` 是整轮一份的(`kb.pipeline_stages._fetch` 从 `filters` 顶层读),不按
    来源分。`crawl_filter` 为 `None`(整库增量,不限来源)照旧返回 `None` ——
    那种形态下面那几层压根不读 `filters`。
    """
    if crawl_filter is None or not hand_pressed(trigger):
        return crawl_filter
    return {**crawl_filter, 'force': True}
