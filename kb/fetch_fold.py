# -*- coding: utf-8 -*-
"""账本那几行怎么折成「今天开过哪几轮、各自新落了哪些文章、交完了没有」。

从 `kb.fetch_rounds` 里分出来的**纯投影**:没有 I/O、没有锁、不认任何模块状态,
输入是账本的行、输出是轮次列表外加一句「读全了没有」。那边剩下的是落盘协议
(文件锁、缓冲、凭证、轮换)和对外那几个动作;单文件 700 行上限见 CLAUDE.md。

**折叠必须幂等**,这是整份账本敢用「追加 + 重放」的全部前提:回执按**内容身份**
取并集、子轮按集合算,所以同一行追加两遍、同一轮被两条通道各报一次,结论都一样。
(按路径取并集不行:改名那一轮里同一篇内容以两个文件名各写一次,会数成两篇。)
改这里之前先读 `docs/decisions/daily.md`:`got` / `segs` / `ends` 三者的区别各有
一次实测事故,混起来的后果是面板笃定地报一个少记的数。

**「读不出来的行」和「找不到父轮的行」不是一件事**,所以 `_fold` 把后者单独交出来
(`stray`):前者的内容我们压根不知道,盘上那几行只能原样留着;后者认得出来是谁的 ——
它那一轮的开轮行已经不在了(过了保留窗口被轮换清掉),而没有开轮行的轮次本来就不
显示,所以持锁重写时可以把它清掉,账本据此从「永久读不出来」里回来。两种混成一个
布尔的后果:孤行一出现就禁掉轮换,那条孤行从此清不掉(审查实测的 F1)。

**而「这一轮算哪天的第几次」也只由这一层定。** 一个业务身份的几条开轮行先归成
**一份**起跑事实(`_start` / `_start_key`:`day`/`at`/`t` 整份取最早那一条),再按
`(day, 起跑时刻, key)` 排好序交出去;所有消费方(`today()` 的当天筛选和「最后一
轮」、轮换整份重写)只认这一份 —— **物理行序不是业务顺序,也不是日归属**:账本是
多进程追加的,一轮因为短暂写失败晚落盘,它的行就排在后开那一轮的后面,跨零点时
那两条还不同天(实测形态在 `_order` 和 `_start`)。
"""
from __future__ import annotations

from kb.fetch_identity import _pairs
from kb.pipeline_persist import _loads_json


# 没带子轮身份的结束行(存量账本、没有 run id 的调用)折成这一个身份。它**同时
# 算「登记过」和「交完了」**,所以老账本的结论一个字都不变。
_LEGACY_SEG = '-'


def _order(one: dict) -> str:
    """这一轮的**起跑时刻**,用于排序;原始行和折好的轮次都认同一套字段。

    `t` 是 `open_round` 写的微秒级时刻(定宽、零填充,所以按字符串比就是按时间比)。
    存量账本行只有分钟级的 `at`,补齐成 `:00.000000` —— 同一分钟内它排在带 `t` 的
    新行前面(那几行是更早版本写的,这个方向也对)。两样都没有的行排最前。
    """
    exact = str(one.get('t') or '')
    if exact:
        return exact
    rough = str(one.get('at') or '')
    return rough + ':00.000000' if rough else ''


def _start(one: dict) -> dict:
    """这一轮的**起跑事实**:`day` / `at` / `t` 三样,只能一起取自同一条开轮行。

    一样取最早、另一样留着物理首行的,折出来就是一份自相矛盾的事实(日期是后开
    那一条的、时刻是先开那一条的),而「今天第几次」正是按 `day` 筛的 —— 第 2 轮
    审查实测:23:59 开的那一轮跨过零点,`day` 落在 16 日而 `at` 写着 15 日 23:59,
    于是今天还没同步过、药丸却说「第 1 次抓取 · +3 条」,今天真的第一次同步之后
    它又变成「第 2 次」。
    """
    return {'day': str(one.get('day') or ''), 'at': str(one.get('at') or ''),
            't': str(one.get('t') or '')}


def _start_key(one: dict) -> tuple[int, str, str]:
    """同一轮的几条开轮行里**挑哪一条当起跑事实**:越小越优先。

    `0/1` 那一位是「知不知道起跑时刻」:**不知道的不许顶掉知道的** —— 存量行、
    手改坏的行可能连 `at` 都没有,让它赢就等于把这一轮的 `day` 也抹成空,那一轮
    从此落不进任何一天。两条都知道时取**最早**那一条(读失败之后同一个 key 会
    再写一条开轮行,「先见到」又是行序)。两条都不知道时拿 `day` 兜平局,**不拿
    物理位置兜** —— 理由同 `_order_key` 末位那一句。
    """
    exact = _order(one)
    return (0 if exact else 1, exact, str(one.get('day') or ''))


def _order_key(one: dict) -> tuple[str, str, str]:
    """排序键:`(day, 起跑时刻, key)`。

    **`day` 在最前**:存量行的 `at` 万一不是完整日期(老版本、手改过),也只会乱在
    它自己那一天里面,不会跨天错位。**末位拿 `key` 兜平局**,为的是让顺序成为**数据
    的函数**而不是文件布局的函数:同一微秒开的两轮没有真实先后可恢复,但重放、补写、
    轮换重写必须给出同一个答案 —— 拿物理位置兜平局就又把它押回行序上了。
    """
    return (str(one.get('day') or ''), _order(one), str(one.get('key') or ''))


def _enlist(got: dict, obj: dict) -> None:
    """开轮行自带的那个子轮登记(`day` 和 `seg` 同一行时)也要记进 `segs`。

    不记的后果:准入看得见这一行的 `seg`、而「这一轮交完了没有」看不见它,于是
    每一轮都少一个登记过的子轮 —— `segs == ends` 反而提前成立,一轮还没交完的
    抓取被说成确定的 N 条。
    """
    if 'seg' in obj:
        got['segs'].add(str(obj['seg'] or _LEGACY_SEG))


def _fold(lines) -> tuple[list[dict], bool, set[str] | None]:
    """把账本每一行折成「开过哪几轮、各自新落了哪些文章、走完了没有」,外加一句
    「读全了没有」。

    五种行:开轮(带 `day`/`at`/`ex`)、回执(带 `items`)、不可确认标记(带
    `partial`)、子轮登记(带 `seg`)、子轮交完了(带 `seg`+`end`)。**回执按内容
    身份取并集、子轮按集合算**,所以同一行追加两遍、同一轮被两条通道各报一次,
    折出来的结论都一样(见模块开头那条)——「交完了几次」用计数的话,一次 fsync
    重放就能把它推成完整。

    **开轮行可以自带这一轮的第一个子轮登记**(`day` 和 `seg` 在同一行):那是写侧
    为了让「准入资格」不被写入前缀劈开才合的一行(理由在 `fetch_rounds.open_round`),
    这一层只管认 —— 判据因此是「带 `day` 就是开轮行」,不是「不带那五个键」。

    `paths` 是回执的**旧形状**(只有路径、没有身份)。照旧折得出条数,但每一条的
    抓取身份都是空串 —— 升级那一下盘上正躺着这种行,不认它的话它会被当成一条没有
    `day` 的开轮行,凭空多出一次「第 N 次抓取」。

    **`got`(收到过回执)和「交完了」是两件事,不许互相顶替。** 上一版只有 `got`:
    导出阶段进门先落一张空回执,于是「刚开始」在盘上和「全部交完」长得一模一样
    —— 随后生产者真写出了文章、进程却没了,盘上的结论永久是**确定的 0 条**
    (第 1 轮审查实测两个分支:预置空回执、以及多来源里第一个交完回执后退出)。

    **而「交完了」是 `segs == ends`,不是「有谁交过」。** 一次业务抓取的物化由 N 个
    子轮各跑一段,第一个子轮的结束行证明不了后面那几个也交完(第 2 轮审查实测)。
    结束行**同时**把自己记进 `segs`:只跑导出的那种运行(重试导出阶段)压根没开过
    轮、不会登记,它交完了也该算完,否则那一轮永远「不可确认」。

    `ok=False` 表示有行没折进来(半截行、被改坏、成果对不上轮次)。那时这份账本
    只能说「不可确认」—— 拿认得出来的那部分冒充完整事实,就又回到了把写失败说成
    「今天还没有抓取」。

    第三样 `stray` 分开说「**哪几个 key 的行找不到父轮**」:`None` = 有行压根读不
    出来(那时文件里还有什么我们不知道,谁都不许整份重写)、空集 = 每一行都归了位、
    非空 = 只有孤行这一种不完整,持锁者可以把它们清掉(见模块开头)。

    **交出去之前按业务起跑顺序排一次**(`_order_key`):调用方不许再按行序理解先后,
    也不许按行序理解日归属。同一轮重复开的那几条开轮行,**起跑事实整份取最早那一条**
    (`_start`:`day`/`at`/`t` 一起换)—— 读失败之后同一个 key 会再写一条开轮行,
    折叠本来就按先见到的那条算,而「先见到」又是行序;跨零点时那两条还不同天。
    """
    rounds: list[dict] = []
    index: dict[str, dict] = {}
    ok = True
    stray: set[str] | None = set()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        # 半截行(写一半断电)、被人改坏、根不是带 key 的字典 —— 都是「这一行
        # 读不出来」。**解析口径不在这里按异常类型列一份**(`_loads_json`):深嵌套
        # JSON 抛的 `RecursionError` 不在原来那张单子上,它从这里逃出去就把整次
        # 抓取拦在采集器之前(第 4 轮审查实测,形态在 `_loads_json`)。
        obj = _loads_json(raw)
        if not isinstance(obj, dict) or not obj.get('key'):
            ok, stray = False, None     # 这一行的内容我们不知道:什么都别动
            continue
        key = str(obj['key'])
        if 'day' not in obj and {'items', 'paths', 'partial', 'end', 'seg'} & obj.keys():
            got = index.get(key)
            if got is None:
                # 有成果、却找不到它那一轮:记不全,而且这几行是**可以清掉**的
                # (它那一轮的开轮行已经不在,没有开轮行的轮次本来就不显示)。
                ok = False
                if stray is not None:
                    stray.add(key)
                continue
            if obj.get('partial'):
                got['partial'] = True
            if obj.get('end'):
                seg = str(obj.get('seg') or _LEGACY_SEG)
                got['segs'].add(seg)    # 见上:交完了的子轮一定算登记过
                got['ends'].add(seg)
            elif 'seg' in obj:
                got['segs'].add(str(obj['seg'] or _LEGACY_SEG))
            if 'items' in obj or 'paths' in obj:
                pairs = _pairs(obj)
                if pairs is None:
                    ok, stray = False, None     # 这一行的形状不认:同上
                    continue
                # 一条成果出现两次而只有一次带得出抓取身份时,留**非空**那个:
                # 空串是「说不清」,被它顶掉已知的身份等于把知道的事实又丢回去。
                for cid, fid in pairs:
                    got['items'][cid] = fid or got['items'].get(cid, '')
                got['got'] = True
            continue
        if key in index:
            # 同一轮重复开:幂等,不是第二次抓取。但**起跑事实整份取最早那一条**
            # (`day`/`at`/`t` 一起换,见 `_start`),免得「这一轮算哪天的第几次」
            # 跟着行序变(读失败后同一个 key 会再写一条,跨零点时两条不同天)。
            got = index[key]
            if _start_key(obj) < _start_key(got):
                got.update(_start(obj))
            _enlist(got, obj)
            continue
        #     `day`:这一轮算哪一天的(按**开工那天**归属,`today()` 按它筛)。
        #     `at`:分钟级起跑时刻,给人看的。
        #     `t`:微秒级起跑时刻(`fetch_rounds.open_round` 写的),排序用;存量
        #          行没有这一笔,`_order` 退回分钟级的 `at`。
        # 三样是**一份事实**,重复开轮时一起换,不许各取一处。
        got = {'key': key, **_start(obj),
               # 这次运行会不会物化。读不到(存量账本行)按会物化算 —— 那一侧
               # 的结论是「不可确认」,比笃定地说 0 条安全。
               'ex': bool(obj.get('ex', 1)), 'items': {}, 'got': False,
               # 参与这一轮物化的子轮,以及其中**明确交完了**的那几个。
               # 两个集合相等才算这一轮交完(见 `today`);存量账本行里一个
               # 子轮都没有,那几轮只能说「至少 N 条」—— 新代码确实证明不了
               # 它们完整,这比继续笃定地报一个数安全。
               'segs': set(), 'ends': set(), 'partial': False}
        _enlist(got, obj)
        rounds.append(got)
        index[key] = got
    rounds.sort(key=_order_key)
    return rounds, ok, stray


def answer(rounds: list[dict], held: set[str], day: str) -> dict | None:
    """这一天的事实该怎么答 —— `fetch_rounds.today()` 那颗药丸的全部算术。

    输入是折好的轮次、**此刻还有谁扣着行没落盘**(`held`,业务身份)和今天是哪一天;
    输出是那七个字段,或者 `None` = 不可确认。I/O、锁、凭证怎么读都在那一边,这里
    一行都不碰 —— 所以同一份快照问两遍必然同一个答案。

    **扣着的凭证指向一轮账本里压根没有的身份时,整句答「不可确认」**:今天确实抓过
    一次而盘上最新的轮次是更早那一次,拿它的数当答案就是把别人的成果说成这一次的。

    **「最后一轮」取末位,不许就地再排一次**:投影已经按业务起跑顺序排过
    (`_order_key`)—— 物理行序不是业务顺序,晚落盘的那一轮会冒充最后一轮。

    **「交完了」= 登记过的子轮一个不剩都交了结束行,而且没人扣着它的行。** 两个集合
    都空时分两种:开轮就说了不物化(`ex=0`)的那一轮确定是 0 条;存量账本那几轮压根
    没有子轮行,证明不了完整,只能算不可确认 —— 反过来把它说成确定的 0 条,就是把
    「还没交完」写成了「确定没抓到」。
    """
    if held - {r['key'] for r in rounds}:
        return None
    mine = [r for r in rounds if r['day'] == day]
    if not mine:
        return {'n': 0, 'added': 0, 'fresh': 0, 'carried': 0, 'unsure': 0,
                'partial': False, 'at': ''}
    last = mine[-1]
    done = (last['segs'] == last['ends'] and bool(last['segs'] or not last['ex'])
            and last['key'] not in held)
    # 这一轮每条成果各是**哪一次**抓到的:等于本轮 = 这次新抓到,非空而不等 =
    # 之前抓到、这次才补导进来,空串 = 说不清(不许并进 carried,口径在 `today`)。
    owners = list(last['items'].values())
    fresh = sum(1 for f in owners if f == last['key'])
    unsure = sum(1 for f in owners if not f)
    return {'n': len(mine), 'added': len(owners), 'fresh': fresh,
            'carried': len(owners) - fresh - unsure, 'unsure': unsure,
            'partial': bool(last['partial'] or not done),
            'at': last['at'][-5:]}
