# -*- coding: utf-8 -*-
"""让 tests 成为一个包,`unittest discover -s tests -t .` 才认它。

没有这个文件时,带 `-t .` 的那条命令直接报 `Start directory is not importable`
(discover 在 start_dir != top_level_dir 时会要求目录里有 __init__.py);而
`-t .` 是 README 和 CI 写的那条命令,也是 collector/tests 一直用的形式。
不带 `-t` 的 `discover -s tests` 两种情况都能跑,不受影响。

**顺带:进程里继承来的功能开关在这里一次清掉**(`FEATURE_ENV`)。

事故形态(实测):`KB_FEED_DOH` 一旦在 shell 里 export 过 —— 按文档打开 DoH 的人就是
这么做的 —— 101 条既有传输/预算/准入用例里有 27 条变红,红的全是跟 DNS 无关的东西
(连接失败换地址、重定向逐跳校验、8 MB 上限、截止时间、响应关卡)。病根是那些用例
默认「开关是关的」、直接打 `socket.getaddrinfo` 的桩,而 DoH 分支抢在桩前面把请求
接走了。**看起来像这一版把整条抓取链改坏了。**

清在这里而不是在 `tests.tiers._apply_tier_env`:后者只挡 `./kb.sh test`,而
`python -m unittest tests.test_feed_transport` 这条路(README、CI 和排查时都在用)
压根不经过它。**`tests` 包被 import 是两条路唯一的交汇点。**

**名单是显式的,不许写成「清掉所有 KB_*」**:`KB_DATA_DIR` 必须留着 —— 全量回归
共用它,清掉就会去读仓库里的真库。要开关的用例自己显式设(`patch.dict`,
`tests/test_feed_resolve.py` 就是这么写的)。
"""
import os

#: 进程里继承来就要清掉的功能开关。**只放「开着会改变被测代码走哪条路」的**;
#: 数据目录、时区这类环境参数不在里面(见模块 docstring)。
FEATURE_ENV = ('KB_FEED_DOH', 'KB_FEED_DOH_SECONDS')


def clear_feature_env() -> None:
    """幂等。`tests.tiers` 也调它 —— 这份名单只有一处。"""
    for name in FEATURE_ENV:
        os.environ.pop(name, None)


clear_feature_env()
