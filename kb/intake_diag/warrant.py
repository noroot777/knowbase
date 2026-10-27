# -*- coding: utf-8 -*-
"""**人确认过的那份描述,怎么一路跟到真正执行、真正写入的那一下。**

**硬约束在 `docs/decisions/intake-diagnostics.md` 第一节 —— 改这块之前先读那一份。**

这块面板上每一次「立即执行」都是**先给一段承诺、再由人点下去**。两件事之间他可以
改任何东西:把订阅改指到另一个账号、切一条通道、把那条内容删掉重新收回来。

**入口再读一次、再比一次关不掉这个窗口。** 比完之后那份描述若被丢掉、只把**编号**
交给执行器,执行器就会按编号重读一份**新**配置、把它当成旧授权来跑:确认文字点名
来源 A、响应说「同步已开始」,而传下去的范围只剩 `accounts=['id:117']`,采集器于是
选中并访问了改指之后的 B,那一轮还带着「人按的、跳过退避」(审查实测)。

所以这里只有两样东西,都是**描述**而不是编号:

- `source_sig()` —— 来源那一侧的执行描述指纹。入口签它、执行器**自己重算一次再
  比**,对不上就作废这一条、不许认领当前版本。
- `life_sql()` —— 条目那一侧的那一世。人看着的那一世进请求、**AND 进每条写语句**
  (元规则:判据待在写语句自己的 WHERE 里)。旧表单停留期间那条内容被清理删掉又
  重新收回来时,这一下写不进新一世。

**空值 = 没有旧描述,照旧放行。** 「我的订阅」那颗同步、CLI、存量调用方点的就是
「现在这几条」,没有「确认时」可言 —— 要求它们一律带描述只会让那几条路跑不起来。
**反过来钉一边**:带了描述就必须比,一环对不上就拒绝或作废。
"""
from __future__ import annotations

import json


def source_channel(kind: str, settings: dict) -> str:
    """这一类来源**此刻真正会用的那条通道**。没有通道可选的来源是空串。"""
    from kb.feeds import x_channels

    return x_channels.channel_of(settings) if x_channels.supports_channels(kind) else ''


def source_floor(source: dict, kind: str, settings: dict) -> str:
    """这条订阅在**当前通道**上保留着的同步下界。读不出来是空串。

    **不许按固定顺序试几条通道、取第一个非空值。** 真跑那一轮按
    `channel_of(settings)` 取水位(`x_channels.prefetch`),而三条通道里
    `timeline` 压根没有水位键、`attach` 和 `browser` 共用一个:两条通道都留过
    水位时,按顺序试出来的是**另一条**通道的下界,于是确认框里承诺的「保留的同步
    下界是 X」和这一轮真正用的那个不是同一个值(审查第 1 轮 R2)。
    """
    from kb.feeds import x_channels

    channel = source_channel(kind, settings)
    return x_channels.watermark(source, channel) if channel else ''


def source_sig(source: dict, kind: str, settings: dict) -> str:
    """这条订阅的**执行描述指纹**:确认框承诺的那一轮,跑的是哪一条订阅。

    **不能只按 `source_id` 和问题类型签票**(`problem_id` 就是这两样拼出来的)。
    序列:确认框弹出时写着「重试来源 A」→ 人在停留期间把这条订阅改指到 B → B
    也连接失败 → 回到那个还开着的确认框点「开始」—— 编号没变、`problem_id`
    一个字没变,于是票对得上,实际重试的是 B,而且还带着「人按的、跳过退避」。

    签五样:

    - **`source_id`** —— 这份描述说的是哪一条。
    - **`rev`** —— 这条订阅的定义改到第几版(改名、改链接、停用都算)。
      **不签 `url` 本身**:地址 A→B→A 之后那个字符串逐字复原,而代次只会往前走
      (同 `fulltext.work_version` 那条「可复原的输入值不是身份」)。
    - **实际通道** —— 这一轮会走哪条通道取数。两条都配好时「都能跑」不等于「是
      同一份计划」:切一下通道,`actionable` 不变、`rev` 不变,而有效范围和执行
      路径整个换了(审查第 1 轮 R2)。
    - **当前通道的下界** —— 确认框里那句「保留的同步下界是 X」承诺的就是它
      (`source_floor`)。它由采集进度改(`merge_config`),不经
      `save_subscription`、因此不动 `rev`。承诺的下界变了,人该重看一眼。
    - **`actionable`** —— 「这一下跑不跑得起来」。它还取决于整条来源的设置
      (`reg.configuration_error` 读 `get_settings`),那一份也不在 `rev` 里。

    **刻意不签那一轮的失败原话**(`evidence`):同一条订阅每一轮失败都会重写它,
    签进去等于让人每次都重看一句没变过的话(口径同 `actions._ticket` 那条「不绑
    展示数字」)。问题**类型**变了由 `problem_id` 认得出,它已经在路由名里。

    **形状是 JSON 数组,不是冒号拼串**:水位那一项里本来就带冒号
    (`2026-10-20 08:00`),拼串的话相邻两项的边界要靠约定,而这份值是跨进程传的
    (入口签、执行器重算)—— 两边对边界的理解一旦岔开就是静默放行。
    """
    from kb.feeds import registry as reg

    return json.dumps([
        str(source['id']),
        int(source['rev'] or 0),
        source_channel(kind, settings),
        source_floor(source, kind, settings),
        bool(source['enabled']) and not reg.configuration_error(source, settings),
    ], ensure_ascii=False)


def life_sql(life, alias: str = 'feed_items') -> tuple[str, list]:
    """「人看着的那一世」那一道,直接 AND 进写语句。空值 = 不加这一道。

    返回 `('1=1', [])` 而不是空串:调用方一律写成 `AND ({cond})`,少一个分支就少
    一处「忘了拼 AND」的地方,而拼漏那一下是**静默**放行。

    `life<>''` 那一半不能省:老库里那一列可能还没种上值(迁移在
    `kb/feeds/schema.py`),两边都是空串时「对上了」是假的。
    """
    want = str(life or '')
    if not want:
        return '1=1', []
    return f"{alias}.life<>'' AND {alias}.life=?", [want]


def life_of(lives, key: str) -> str:
    """请求里带来的、这个 key 那一世。没带就是空串(照旧放行)。"""
    try:
        return str(dict(lives or {}).get(key) or '')
    except (TypeError, ValueError):
        return ''
