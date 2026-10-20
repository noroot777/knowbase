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
  `'<内容身份>'` = 核实过就是这一份;`'*'` = 界面明确告诉过他「查不到这一天有没有
  (或者查不到是哪一版),要是有就会覆盖」(他批的是「那一天不管哪一版都盖」);
  `''` = **压根没问过他**(界面判定那一天没有日报,所以没弹确认框)—— 这一档只有
  「真的没有」才放行,期间冒出来一份就得拒,否则正好复现「一句过期的『今天没有
  日报』静悄悄盖掉刚出的那一份」。

**版本是整份文件的内容身份,不是 frontmatter 里那行「生成于」。** 那行字只精确到
分钟、而且**不随正文变化** —— 同一分钟内再生成一份、在 Obsidian 里改掉正文、
整份换掉而留着原来那行时间,三样都造出「不同文件、相同版本」,于是核对放行、用户
确认过的那一份被盖掉(第 2 轮审查实测:同一个 `generated` 下换掉正文,`generate`
一声不响覆盖了它)。把精度从分钟改到秒救不了中间那一档。所以身份取**文件字节的
sha256**(`file_version`),「生成于」退回去只管显示(`daily._entry` 的 `generated`)。

`approval=None` 是**没有人可问**那一档(定时轮、命令行):照旧放行 —— 这道闸管的是
「问错了天」,不是「该不该出报」。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

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


def file_version(path: Path) -> str:
    """这份文件的**内容身份**(字节的 sha256 前 16 位)。`''` = 算不出来。

    **口径只有这一份**,三处消费:清单里交给界面那一格(`daily._entry` 的
    `version`)、确认请求带下来的那一版、以及落盘之前核对的这一下。三处各算一遍
    的话,某一处少读一个字节就等于「用户确认的那一份」和「我们核对的那一份」是两
    个东西,而对不上只会表现成一句「期间换了一版」。

    **读的是字节,不是 `read_text`** —— 后者会把 `\r\n` 统一成 `\n`,于是 CRLF
    那一档上同一份文件在两处算出两个身份。算不出来回空串,由调用方决定那是「没有
    这一天」还是「有、但核对不了」(两件事的结论正好相反,见 `report_version`)。
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:                              # noqa: BLE001 - 见 docstring
        return ''


def report_version(day: str) -> str | None:
    """这一天那份日报现在是**哪一版**。`None` = 这一天还没有日报。

    **判「不存在」只认 `FileNotFoundError`**,别的读不出来一律回 `UNKNOWN`
    (元规则那条:「看不见」不等于「没有」,而这两件事在这里导出的结论正好相反)。
    """
    path = _report_path(day)
    if not path.exists():
        # **只有「文件确实不在」才是 None。** `exists()` 对别的 OSError 也回 False,
        # 所以紧接着那次读还要自己分一次档:读得出来就是身份,读不出来是 UNKNOWN。
        try:
            path.read_bytes()
        except FileNotFoundError:
            return None
        except Exception:                          # noqa: BLE001 - 见上
            return UNKNOWN
    return file_version(path) or UNKNOWN


def _report_path(day: str) -> Path:
    from kb.daily import report_dir                 # 延迟:它反过来 import 本模块
    return report_dir() / f'{day}.md'


def _shown(day: str) -> str:
    """盘上那份日报**给人看的那一行**(「生成于 …」)。只进文案,不当身份。

    读不出来就照实说 —— 把它写成空串会让提示变成「已经有一份日报()」。
    """
    from kb.scanner import parse_frontmatter
    try:
        meta, _ = parse_frontmatter(_report_path(day).read_text(encoding='utf-8'))
        at = str(meta.get('generated') or '')
    except Exception:                              # noqa: BLE001 - 见 docstring
        at = ''
    return f'生成于 {at}' if at else '生成时间读不出来'


def _never_asked(day: str) -> str:
    """「这一天已经有一份,而压根没问过他要不要盖」这句话,**只有一份**。

    两条路都落到这一档:界面判定那一天没有日报(所以没弹确认框,批准里版本是空的)、
    以及请求里压根没带批准。
    """
    return (f'{day} 已经有一份日报({_shown(day)}),而这一次出报压根没问过要不要'
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
        raise OverwriteRefused(_never_asked(day))
    if approval.day != day:
        raise OverwriteRefused(
            f'这一轮要写的是 {day} 那份日报,而你确认覆盖的是 {approval.day or "另一天"}'
            f'的那一份 —— {day} 已经有一份日报了,这一次没有动它。'
            '请按 {day} 再确认一次'.replace('{day}', day))
    if approval.version == ANY:
        return
    if current == UNKNOWN:
        # **「核对不了」不许当成「核对过了」。** 身份算不出来时两边一比可能恰好
        # 相等(两个 UNKNOWN),那就等于拿一句「读不出来」当成了「就是这一份」。
        # 界面下一次会落到 `ANY` 那一档(清单给不出版本 ⇒ 确认框明说查不到是哪一
        # 版),所以这里拒了也不会把人卡死在同一句话上。
        raise OverwriteRefused(
            f'现在读不出 {day} 那份日报是哪一版,核对不了它还是不是你确认的那一份'
            ' —— 这一次没有动它。请再确认一次')
    if approval.version and approval.version == current:
        return
    if not approval.version:
        raise OverwriteRefused(_never_asked(day))
    raise OverwriteRefused(
        f'{day} 那份日报期间已经换了一版(现在盘上那一份{_shown(day)})'
        '—— 这一次没有动它。请再确认一次')
