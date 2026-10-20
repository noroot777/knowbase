# -*- coding: utf-8 -*-
"""`x_browser._STATE_JS` 那份页面状态的形状 —— 测试里**只有这一处**构造它。

**为什么要集中**:`_wait_state` / `verify_timeline` 读这份状态用的是直接下标
(`last['login']`,不是 `.get`),这是有意的 —— 生产那段 JS 一定把字段都返回,
少一个就该当场炸,而不是静默当成 False(口径同元规则「拿不准就别固化终态」)。
代价是**往 JS 里加一个字段,所有手写这份 dict 的测试桩都会 `KeyError`**:
审查第 1 轮实测,加 `verify` 那一次让 `tests/test_x_collectors.py` 的 9 条
直接报错(两条日期边界 + 六条翻页/水位 + 一条"解析不出字段"),而它们钉的是和
人机验证毫无关系的旧行为。

所以构造口径收在这一个函数里:以后再加字段,改这里一处,各处桩跟着就对了。
**不要在用例里重新手写这份 dict** —— 那等于把同一个坑再埋一遍。
"""


def page_state(**over):
    """一份"正常时间线、已经加载完"的状态;`over` 覆盖要试的那几个字段。

    `articles` 缺省给 0:要"读到推文了"的用例自己传,免得默认值替用例把前提
    悄悄满足掉。
    """
    return {'url': 'https://x.com/Example', 'ready': 'complete', 'articles': 0,
            'login': False, 'verify': False, 'empty': False, 'limited': False} | over
