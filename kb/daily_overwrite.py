# -*- coding: utf-8 -*-
"""出报要盖掉一份已经有的日报之前,核对**用户批准的是不是那一份**。

**硬约束在 `docs/decisions/daily.md` 第一节 —— 改这块之前先读那一份。**

出报的最后一下是整份替换文件(`daily._write` 的 `tmp.replace`),所以盖掉一份日报
是**不可恢复**的。界面上那个确认框问的是「重新生成 X 日的日报?」,而**点击那一刻
的浏览器日期不是写文件时的目标身份**:

- 勾着「先同步一轮」的那条路要先跑几小时同步,出报的目标日期是**同步跑完之后**才
  由服务当前时刻定下来的(这一点不许改 —— 把 `until` 在开跑前钉死会让同步那几小时
  里发布的内容掉进两份日报的缝里,见 `daily.next_window`)。浏览器还是 10-19、服务
  已经 10-20 时,确认框问的是 19 日那一份,被盖掉的是 20 日那一份。
- 期间别人(定时轮)写过一版时,用户批准覆盖的那一版已经不在盘上了。

所以**判据落在真正要写的那一天上**,而且就在写之前核对一次。授权带两样:

- `day`:用户看着哪一天那份日报点下「重新生成」的;
- `version`:他看到的是**哪一版**。三档,少一档就有一个方向会静默盖掉 ——
  `'<时刻>'` = 核实过就是这一版;`'*'` = 界面明确告诉过他「查不到这一天有没有,
  要是有就会覆盖」(他批的是「那一天不管哪一版都盖」);`''` = **压根没问过他**
  (界面判定那一天没有日报,所以没弹确认框)—— 这一档只有「真的没有」才放行,
  期间冒出来一份就得拒,否则正好复现「一句过期的『今天没有日报』静悄悄盖掉刚出的
  那一份」。

`approval=None` 是**没有人可问**那一档(定时轮、命令行):照旧放行 —— 这道闸管的是
「问错了天」,不是「该不该出报」。
"""
from __future__ import annotations

from dataclasses import dataclass

# 文件在、版本读不出来。**不许当成「这一天没有日报」** —— 那正好放行一次覆盖。
UNKNOWN = '?'
# 「那一天不管哪一版都盖」:界面明确问过、而且说清了查不到有没有。
ANY = '*'


class OverwriteRefused(Exception):
    """要写的那一天不是用户批准的那一天(或者那一份已经换了版本)。

    **不是临时故障**:重试多少次都是同一个结果,得让用户按着真正的目标再确认一次。
    """


@dataclass(frozen=True)
class Approval:
    """用户在界面上批准覆盖的是哪一天、哪一版(含义见模块头)。"""

    day: str
    version: str = ''


def report_version(day: str) -> str | None:
    """这一天那份日报现在是**哪一版**。`None` = 这一天还没有日报。

    版本就是它 frontmatter 里的 `generated`(界面拿到的也是这一个,见
    `daily._entry`)。**判「不存在」只认 `FileNotFoundError`**,别的读不出来一律回
    `UNKNOWN`(元规则那条:「看不见」不等于「没有」,而这两件事在这里导出的结论
    正好相反)。
    """
    from kb.daily import report_dir
    from kb.scanner import parse_frontmatter
    path = report_dir() / f'{day}.md'
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except Exception:                              # noqa: BLE001 - 见 docstring
        return UNKNOWN
    return str(meta.get('generated') or '') or UNKNOWN


def _never_asked(day: str, current: str) -> str:
    """「这一天已经有一份,而压根没问过他要不要盖」这句话,**只有一份**。

    两条路都落到这一档:界面判定那一天没有日报(所以没弹确认框,批准里版本是空的)、
    以及请求里压根没带批准。
    """
    return (f'{day} 已经有一份日报(生成于 {current}),而这一次出报压根没问过要不要'
            '覆盖它 —— 这一次没有动它。请再点一次「生成日报」,确认之后它才会被覆盖')


def check_overwrite(day: str, approval: Approval | None) -> None:
    """要盖掉 `day` 那份日报之前核对一次。对不上就抛 `OverwriteRefused`。

    **口径只有这一份**,两处调用:出报前早一道(省掉那次模型钱)、以及落盘那一段
    里真正的那一道(两者之间别的进程照样能写一版进来)。**早的那一道不是互斥**,
    它只是把话早点说清楚。
    """
    current = report_version(day)
    if current is None or approval is None:
        return
    if not approval.day:
        # 请求里压根没带批准(老脚本、直打接口的摆盘)。**和「界面判定那一天没有
        # 日报」是同一档**,所以说同一句话:下面那句「你确认的是另一天」对这一档
        # 是错的 —— 他哪一天都没确认过。
        raise OverwriteRefused(_never_asked(day, current))
    if approval.day != day:
        raise OverwriteRefused(
            f'这一轮要写的是 {day} 那份日报,而你确认覆盖的是 {approval.day or "另一天"}'
            f'的那一份 —— {day} 已经有一份日报了,这一次没有动它。'
            '请按 {day} 再确认一次'.replace('{day}', day))
    if approval.version == ANY or (approval.version
                                   and approval.version == current):
        return
    if not approval.version:
        raise OverwriteRefused(_never_asked(day, current))
    raise OverwriteRefused(
        f'{day} 那份日报期间已经换了一版(你确认的是 {approval.version},'
        f'现在盘上是 {current})—— 这一次没有动它。请再确认一次')
