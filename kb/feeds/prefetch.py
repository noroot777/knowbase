# -*- coding: utf-8 -*-
"""**逐条取数之前先跑一遍的整批预取**:X 的登录态通道、简报通道各一份。

`kb/feeds/sync.py` 越过 700 行硬上限时从它里面拆出来的,**口径一个字没动**。
`sync` 原地再导出这两个名字和它们各自那份预算常量,所以 `sync._prefetch_x` /
`sync._prefetch_digest`(直接调的、`patch.object` 打桩的)和 `sync.X_LOCAL_SECONDS`
照旧成立。

这两个零件为什么存在、预算为什么另算,见 `docs/decisions/x-channels.md` 与
`digest.md`。
"""
from __future__ import annotations

import os

from kb.feeds import digest, registry as reg, x_channels
from kb.pipeline_flow import halted
from kb.pipeline_state import _stage

# 简报预取另算一份预算:它是串行的,每期一次 HTTP(RSS 一次 + 最多 `MAX_ISSUES`
# 期),跟 HTTP 那一侧 90 秒的并发预算不是一回事 —— 口径同 `X_LOCAL_SECONDS`。
DIGEST_SECONDS = 240
# X 的登录态通道(feedgrab / 浏览器)另算预算:它们是串行的 —— 同一个浏览器
# profile 只能被一个实例打开 —— 而单个账号要滚时间线、必要时还要等懒加载,
# 90 秒连五个号都跑不完。这条通道按天同步(见 `source_types` 里 social 的
# interval),所以给得宽;用完的账号记成"留待下次",靠 last_attempt 轮转补齐。
X_LOCAL_SECONDS = float(os.environ.get('KB_X_LOCAL_SECONDS', '1200'))


def _prefetch_x(kind, sources, settings, scope):
    """X 登录态通道的整批预取。跑在起线程池之前,所以读侧不必加锁。

    一个账号都抓不到也不抛:每个账号的失败已经落在它自己那一项上,随后
    `fetch_source` 会把它翻成这条订阅的失败 —— 一个改了名的号不该把整轮带掉。
    这一类来源没有通道概念时 `targets()` 返回空,直接空转。
    """
    x_channels.reset()
    if not x_channels.supports_channels(kind):
        return
    picked = x_channels.targets(sources, settings)
    if not picked:
        return
    name = x_channels.label(x_channels.channel_of(settings))
    _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name}:准备读取 {len(picked)} 个账号')

    def progress(done, count='', handle=''):
        # **两种形状都要接**:逐账号那条是 `(第几个, 共几个, handle)`,而整批里
        # 还有几处要报的是一句话(关掉到点的登录窗口、等限流冷却过去)。只写三参
        # 的话那几处单参调用当场 TypeError 把整条大V采集带掉,而面板上只会看到
        # 采集失败、指不到任何跟业务有关的东西。
        if not count:
            _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name}:{done}')
            return
        _stage('crawl', 'running', f'{reg.KINDS[kind]} · {name} {done}/{count} · @{handle}')

    # 区间补抓把它那段日期的起点交给采集器当下界:日常水位可能已经停在那段
    # 日期之后,按水位读的话用户点名的那几天一条都翻不到(见 `prefetch`)。
    x_channels.prefetch(sources, settings, budget=X_LOCAL_SECONDS,
                        since=scope.since if scope.ranged else '',
                        on_progress=progress,
                        should_stop=halted)


def _prefetch_digest(kind, sources, settings, scope):
    """简报通道的整批预取。跑在起线程池之前,所以读侧不必加锁。

    一期都抓不到也不抛:失败落在它自己那一项上,随后 `fetch_source` 会把它
    翻成这条订阅的失败 —— 口径同 `_prefetch_x`。

    **区间补抓那段日期要在这儿交下去**(口径同 `_prefetch_x`)。这条通道是预取
    的:`unconditional` 压根到不了它,而 `_save_result` 的日期筛选排在拿到结果
    之后 —— 预取时按日常水位筛掉的期号,后面没有任何一步救得回来(实测点名一段
    旧日期时一期页面都不请求、报"新增 0 条、同步已完成",2026-10-10 审查)。
    """
    digest.reset()
    picked = digest.targets(sources) if digest.supports(kind) else []
    if not picked:
        return
    _stage('crawl', 'running', f'{reg.KINDS[kind]}:准备读取 {len(picked)} 份简报')

    def progress(url):
        _stage('crawl', 'running', f'{reg.KINDS[kind]} · {url}')

    digest.prefetch(sources, settings, budget=DIGEST_SECONDS,
                    since=scope.since if scope.ranged else '',
                    until=scope.until if scope.ranged else '',
                    on_progress=progress,
                    should_stop=halted)
