# -*- coding: utf-8 -*-
"""本日的抓取账本:今天抓了第几次、每一次往 vault 里**新落了**多少篇。

日报面板的 tooltip 报的就是这个事实(它在面板上怎么显示,见同一份文档的「按钮边上
那句话长在生成按钮前面」)。**每一条规则下面都压着一次实测事故,全文在
`docs/decisions/daily.md`—— 动这个模块之前先读那一份**,这里只留规则本身。

**抓取事实和日报取材是两件事,不许互相顶替**:一次抓取抓了多少是抓取自己的事,生成、
重新生成、删掉日报都改不了它;而「这一次生成能多带进去几条」(按钮边上那句「有 N 条
新内容」,口径在 `daily.pending` 的 `gain`)受名额和发布时间排序左右,两个数可以差
两个数量级 —— 抓回来 860 篇而这一次只多带得进 11 条,两句话各待各的位置。

**成果的归属由生产者的回执定,不由任何旁观者的差集猜**(`SourceCollector.
materialize()`,见 `kb.collectors`)。错过的两版都是拿旁观者的视角冒充生产者的成果
(库里头一回出现的文档数、物化前后的全 vault 差集)。采集器说不出来(返回 `None`)
就记一笔「这一段不可确认」,不拿一个看着确定的数糊过去。

**回执里每一条带两个身份:账本按内容身份去重、按抓取身份分档。** 稳定内容身份
(`rss:<key>`、`wechat_local:<id>`)由采集器自己的库给 —— 路径当不了它,标题一改
文件名就变。抓取身份是**发现**这一条的那一次业务抓取(记在 `feed_items.fetch_round`
和公众号那张 `article_fetch_rounds` 里),所以「这一次新抓到的」和「之前抓到、这一
次才补导进来的」分得开,不靠时间窗口反猜。

**`added` 报的是「这次同步之后日报能多拿到多少」**(仓库主人定的口径)。另一个口径
「远端这次新发现多少」在补导场景下必然更小,差额如实拆成 `fresh`/`carried`/`unsure`
交给面板,**不从 `added` 里悄悄减掉,也不拿 `fresh` 冒充总数**;`unsure`(抓取身份是
空串)不许并进 `carried` —— 那是把「不知道」写成「确定不是」。

**只勾了导出的那种运行不算一次抓取**(压根不开轮)。它落下来的那几篇带着**原本那次
抓取**的身份,账目上属于真抓到它们的那一轮;**不许追记回那一轮** —— 会改掉用户早就
看过的数,而那一轮多半已经滚出保留窗口,只会留下一条对不上轮次的孤行。

**「一次业务抓取」的身份是业务批次,不是管道运行**(一轮日报 = N+1 次运行、一次
抓取。没有批次的独立运行用自己的 `run_id`。算法只有一处:`pipeline_batch.round_key`,
对外那一份是 `round_key()`)。**先有 `open_round` 才认 `note_new`。** 而账本要分得清
**三件事**:确定没有、确定有 N 条、不可确认(`today()` 的 `None` / `n=0` / `partial`
—— 混成一句的后果是一次写失败之后面板一直说「今天还没有抓取」)。

**落盘是追加 + 幂等投影,所以重放不会少记、也不会多记。**

- 追加而不是整份重写(整份重写一次写坏就带走当天已经确认过的全部轮次)。
- 回执行存**内容身份集合**、折叠取并集:`fsync` 报错后重试追加同一行,存条数会翻倍
  (实测 2 篇报成 4 篇),存身份则不变;按路径取并集会把改过名的同一篇数成两篇。
- 攥在手里没落下去的行不许悄悄丢:超过 `_KEEP_UNSAVED` 时换成一条「不可确认」标记,
  **投影不完整就不许解除「不可确认」**。
- **轮换不许清走还有人欠着证据的轮次**(开轮、子轮、成果、结束、标记连那笔凭证一起
  闭合,口径在 `kb.fetch_rotate`):先清开轮行、再补写它的回执,留下的孤行会把整份账本
  **永久**判成「读不出来」。
- **账本的每一次写都在那把文件锁里**(`_locked`),锁取不到时**一行都不写**。两边的
  降级必须是同一套协议:一边绕锁追加、一边让持锁者放心覆盖,那条追加就没人保护。

**「收到过回执」不等于「这一轮交完了」**,完整性只认那条明确的结束行(`note_end`);
**而「交完了」按子轮集合算**,登记过的子轮一个不剩都交齐才给确定数。

**证据不许因为「这一刻读不出账」就消失**(`_mark` 认 `_opened` / `_gaps`),**而
「参与过」这件事也不许只存在账本里** —— 账本的写要过文件锁,锁坏掉时行攥在内存里、
进程一没盘上就没有痕迹,所以另留一笔**不走那把锁**的凭证(`kb.fetch_pending`)。

**生产者的提交边界在「文件落进 vault」那一刻**,不在它后面那次入库确认:确认失败
只意味着下一轮还会再导一次,从成果里抹掉它则再也补不回账。

**缓冲是两条线程共用的,确认只许移除自己真写下去的那几行**;锁序只有一个方向:
**文件锁 → 缓冲锁**。

**而账本和那笔凭证能一起失效 —— 那一档不许往下抓**(`open_round` 那道闸;两处证据各自
的寿命、被挡在外面的那两样、以及拒绝时那句话都在 `kb.fetch_gate`)。**不拦的只有两条**:
不物化的运行,以及账本坏着、而那笔凭证写上了。**而拒绝要干净**:两处都没留下的那次尝试,
`_drop` 把它压进缓冲的那几行成组撤掉 —— 留着的话光读一次页面就把一次压根没起跑的尝试
补写成了当天的一次抓取。

**「今天第 N 次」按业务起跑顺序排、算哪一天的按开工那天,都不按谁最后补写**:开轮行
上带一笔微秒级起跑时刻(`t`),`day`/`at`/`t` 是一份事实整份取最早那条开轮行,投影
按 `(day, t, key)` 排好再交出去(`kb.fetch_fold`),轮换整份重写时把 `t` 一起抄走。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kb import fetch_pending, fetch_rotate
from kb import filelock
from kb.config import DATA_DIR
from kb.fetch_fold import _LEGACY_SEG, _fold, answer
from kb.fetch_gate import (NO_EVIDENCE, NoEvidence,  # noqa: F401 - 再导出
                           landed, rollback)
from kb.fetch_identity import _identity
from kb.fetch_scope import _scope_prefixes, in_scope      # noqa: F401 - 再导出

TZ = ZoneInfo('Asia/Shanghai')
FILE_NAME = 'fetch_rounds.jsonl'
LOCK_NAME = 'fetch_rounds.lock'
# 「还欠着行没落盘」那几笔独立凭证放哪(每个进程一份,见 `kb.fetch_pending`)。
PENDING_DIR = 'fetch_rounds.pending'
# 账本里留几天。只回答「今天」,但不能只留今天:23:59 开的那一轮 00:01 才物化完,
# 记它的那一下得找得到它(轮次按**开工那天**归属,不在追加时重新猜今天)。
_KEEP_DAYS = 2
# 攥在手里等下次重试的行数上限。磁盘一直写不进去的话,这里挡住它无界增长 ——
# 但丢掉的那几行要换成「不可确认」标记,不许静默少记(见模块开头)。
_KEEP_UNSAVED = 64
# `_opened` / `_gaps` 各记得住几轮身份。一天就那么几轮,这个数只是挡无界增长。
_KEEP_KEYS = 64

# 落盘失败、还没写下去的那几行。**不是缓存**:它存在期间 `today()` 一律答
# 「不可确认」,因为此刻磁盘上那份账本比事实少。
_unsaved: list[dict] = []
# **本进程亲手开过的那几轮。** 读账短暂失败时拿它顶 `_is_open`:读不出来并不
# 代表这一轮不存在,而自己开的那一轮一定存在(开轮行要么已经落盘、要么还在
# `_unsaved` 里)。见 `_mark`。
_opened: list[str] = []
# **缺了一行的那几轮。** 某条回执/标记连 `_unsaved` 都没留住(读不出账、这一轮
# 又不是本进程开的)时记一笔,结束行据此带上 `partial` —— 不然读账一恢复,一个
# 结束行就把未知解除了,报出一个确定却少记的数(见模块开头)。
_gaps: list[str] = []
# **上面那三份内存状态的互斥锁。** 管道线程在写账本,而面板每 15 秒从 API 线程
# 读一次(`today()` 自己也是缓冲的消费者:它会替攥着的行补写一次),所以这三份
# 是真的被两条线程同时碰的。
#
# **锁序只有一个方向:账本那把文件锁 → 这把缓冲锁。** 反过来挂的话,一条线程
# 攥着缓冲等文件锁、另一条持着文件锁等缓冲,凑出一个死锁。所以这把锁里**只许做
# 缓冲自己的事**:搬动那几行、以及把「还欠哪几轮」写成一笔凭证(那一笔不碰账本、
# 不取任何锁,见 `kb.fetch_pending`)—— **追加账本那一下必须在锁外**,它是 I/O,
# 罩进来就把面板那一跳堵在磁盘故障后面。
_buffer_lock = threading.RLock()


def _push(bag: list[str], key: str) -> None:
    """把一个轮次身份记进 `bag`:去重、挤掉最老的,最多留 `_KEEP_KEYS` 个。

    挤出去的那一档落在安全的一侧:`_opened` 少一个 = 那一行退回「缺了一行」,
    `_gaps` 少一个 = 结束行不带 `partial`,而那时轮次本来就少了行、`_fold` 会
    自己报 `ok=False`。
    """
    with _buffer_lock:
        if key in bag:
            return
        bag.append(key)
        del bag[:-_KEEP_KEYS]


def _seen(bag: list[str], key: str) -> bool:
    """这个身份记在 `bag` 里没有 —— 读也要过缓冲锁(另一条线程正在挤它)。"""
    with _buffer_lock:
        return key in bag


def _pending() -> bool:
    """手里还攥着没落盘的行没有。**这一句就是 `today()` 答「不可确认」的判据。**"""
    with _buffer_lock:
        return bool(_unsaved)


def _pending_folder() -> Path:
    return DATA_DIR / PENDING_DIR


def _floor(at: datetime) -> str:
    """保留窗口的下沿(这一天之前的轮次清出账本)。**账本和凭证共用这一份口径**:
    凭证留得比账本久的话,它指的那一轮早被轮换清掉了,`today()` 会从此永久答
    「读不出来」;反过来凭证先过期,扣着的证据就没人提了。"""
    return _day(at - timedelta(days=_KEEP_DAYS - 1))


def _certify(at: datetime | None = None) -> None:
    """把「我还欠哪几轮的行」落成一笔独立凭证。**调用方必须已持缓冲锁。**

    凭证的全部作用是:**登记行没落盘、进程又没了**的时候,盘上仍留着「那个子轮
    参与过」这件事,于是结论压到下界而不是沿用上一个子轮的确定数(第 3 轮审查
    实测的那一刀,形态在 `kb.fetch_pending` 开头)。手里空了就撤掉它 —— 不撤的
    后果是面板永久写「至少」,所以这一句和 `_unsaved` 的每一次搬动绑在一起,
    不单独挑调用点。
    """
    keys = [str(o.get('key') or '') for o in _unsaved if o.get('key')]
    moment = _now(at)
    fetch_pending.put(_pending_folder(), keys, _day(moment), _floor(moment))


def _file() -> Path:
    return DATA_DIR / FILE_NAME


@contextmanager
def _locked(exclusive: bool):
    """账本读写的那把文件锁。读共享、写独占,**独占要一路罩住「读→轮换→追加」**。

    锁在一个旁挂的空文件上,不在账本自己身上:轮换走的是原子替换(换 inode),
    锁在被替换掉的那个 inode 上等于没锁。

    **这把锁是叶子** —— 持有它的时候不许再去取别的锁(维护锁尤其不许)。面板
    每 15 秒读一次账本,而维护锁会被一整轮导出持有好几分钟;反过来挂的话,
    面板要么被同步堵死,要么和管道线程凑出一个死锁。

    取不到锁(目录只读、fcntl 不支持、旁挂文件被占成了别的东西)**不拦抓取,
    但要如实交出「这次没锁住」** —— `yield` 的就是这个布尔值。失锁之后这一边
    **一个字都不往账本里写**:要写的行攥在 `_unsaved` 里等下次(`_flush`),
    覆盖式轮换直接跳过(`_rotate`),展示降级成「读不出来」(`today`)。

    为什么连追加也停:`O_APPEND` 的单次写确实不会互相截断,但账本的另一半协议是
    **持锁者可以按它读到的快照整份重写** —— 一边放行绕锁的追加、一边让持锁者放心
    覆盖,那条追加就是没人保护的(第 2 轮审查实测:失锁那个进程三种行全部追加成功、
    另一个持锁进程随后按旧快照轮换,那一整轮消失而剩下的行行行合法,形态在
    `docs/decisions/daily.md`)。所以只留一条口径:**没锁住就不写**。代价是那几行
    晚一点落盘、这段时间面板说「读不出来」—— 抓取那一侧照旧(`note_*` 都不抛;
    `open_round` 只在**两处证据都没留下**时才拦,那不是失锁这一档)。
    """
    fh = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(DATA_DIR / LOCK_NAME, 'a+')
        filelock.flock(fh.fileno(), filelock.LOCK_EX if exclusive else filelock.LOCK_SH)
    except OSError:
        if fh is not None:
            fh.close()
        fh = None
    try:
        yield fh is not None
    finally:
        if fh is not None:
            try:
                filelock.flock(fh.fileno(), filelock.LOCK_UN)
            except OSError:
                pass
            fh.close()


def _now(now: datetime | None = None) -> datetime:
    """项目时区的此刻(CLAUDE.md:一律 Asia/Shanghai,不按宿主机)。

    UTC 机器上按宿主机算的话,北京时间 00:00~07:59 这八小时会被记进前一天,
    面板上「今天第几次抓取」要到早上八点才归零。不带时区的按项目时区补齐。
    """
    at = now or datetime.now(TZ)
    return at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ)


def _day(at: datetime) -> str:
    return at.date().isoformat()


def _read() -> tuple[list[dict], bool]:
    """读账本。**文件不存在是「确定还没有记录」,读不出来是「不可确认」** ——
    两种都压成空的话,一次权限故障就会让面板笃定地说「今天还没有抓取」。

    `UnicodeDecodeError` 和 `OSError` 一样归到「不可确认」:账本被写成二进制
    垃圾时,原来那一版会把这个异常一路抛给调用方 —— 而第一个调用方是抓取阶段
    的 `open_round`,于是一份坏掉的展示用账本能把整次抓取拦在采集器之前
    (第 3 轮审查实测)。
    """
    raw = _raw()
    return _fold(raw)[:2] if raw is not None else ([], False)


def _raw() -> list[str] | None:
    """账本那几行的原文。**`None` = 这份文件读不出来**(权限、被写成二进制垃圾),
    空列表 = 确定还没有记录(文件不存在)。

    两者差在**许不许整份重写**:读不出来时文件里还有什么我们不知道,拿一份空快照
    写回去就是把别人的整轮抹掉;文件不存在时那份空快照是确定的。`_read()` 把两者
    压成同一个「不可确认」,所以轮换那一步要自己问这一句(`open_round`)。
    """
    try:
        return _file().read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        return None


def _append(objs: list[dict]) -> bool:
    """追加这几行。失败返回 False,**不抛** —— 展示账本写不上不该把这一轮抓取
    带红,但也不许假装成功(那是上一版吞掉 `OSError` 的那条:25 篇文章已经入库、
    tooltip 永久写 0 条,补齐文章也不会自己补回来)。

    `fsync` 报错不代表前面那次 `write` 没写进去,所以这里返回 False 之后的重试
    **必须是幂等的** —— 回执行存**内容身份**集合、折叠取并集,正是为这一刀准备的。
    """
    body = ''.join(json.dumps(o, ensure_ascii=False) + '\n' for o in objs)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_file(), 'a', encoding='utf-8') as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        return False
    return True


def _flush(objs: list[dict], locked: bool) -> bool:
    """把这几行连同之前没落下去的一起追加。**只在持锁时真写。**

    `locked=False` 一律攥着不写:账本的另一半协议是持锁者可以整份重写,绕锁追加
    的那几行没人保护(实测形态在 `_locked`)。攥着的代价是这段时间 `today()` 答
    「读不出来」,下一次操作、或者面板下一跳(`today` 自己会重试)就补上。

    磁盘满、权限被改这类故障多半是一阵子的,而它们之后的那次账本操作照样该把
    漏掉的那几行补上 —— 所以攥着重试,而不是就地丢掉。

    **攥不住的时候换标记,不静默少记。** 超过 `_KEEP_UNSAVED` 时挤出去的那几行
    里,每一轮各留一条 `partial` 标记:数据确实没了,但「这一轮的条数不全」这个
    事实留住了,下一次写成功就会落盘。原来那一版直接 `batch[-64:]` 切掉 ——
    剩下的补写成功后 `today()` 会重新给出一个确定、但已经少记的结论。

    **确认只许移除「我这一批真写下去的那几行」,不许清空整份缓冲。** 追加是 I/O、
    在缓冲锁外跑(见 `_buffer_lock`),这段时间另一条线程完全可以往缓冲里压进新的
    回执 —— 上一版一句 `_unsaved = []` 把它一起抹掉,那一篇再也补不回账(第 3 轮
    审查实测,形态在 `docs/decisions/daily.md`)。所以这里**按身份(`id`)剔**:
    同样的行可能来两条(两条 `partial` 标记长得一模一样),按值剔会多剔一条;按
    条数剔在两条线程各自确认时会剔到对方新压进来的那几行。

    重复追加不要紧:回执按内容身份取并集、子轮按集合算,折叠本来就幂等(`_fold`)。

    **而攥着的行里,父轮已经退役的那几个一起退役**(`fetch_rotate.disown`):过了保留
    窗口的那一轮被别的进程清走之后,把回执补进去就是一条谁也认不了的孤行,整份账本
    从此「读不出来」且清不掉(审查实测的 F1)。只在**持锁、而且账本读全了**时才问这
    一句 —— 答不出来时宁可攥着。
    """
    global _unsaved
    with _buffer_lock:
        held = list(_unsaved)
        if not held and not objs:
            return True
        mine = {id(o) for o in held}
    gone: list[dict] = []
    if locked and held:
        rounds, ok = _read()
        # **账本一轮都没有时一行都不退役**:那不像「别人把过期那轮清走了」(清完总还
        # 剩今天那几轮),更像账本文件被删掉或者头一次运行 —— 那时连今天的欠账都会被
        # 当成孤儿丢掉。这一档宁可写成孤行:下一次开轮的孤行清理会把它收走(`_rotate`)。
        if ok and rounds:
            held, gone = fetch_rotate.disown(held, objs, rounds)
    if locked and _append(held + objs):     # 追加在锁外:I/O 不许罩进缓冲锁
        with _buffer_lock:
            _unsaved = [o for o in _unsaved if id(o) not in mine]
            _certify()
        return True
    with _buffer_lock:
        # 这一批没落下去:重新拿一次当前的缓冲(这期间别的线程可能压进了新行,
        # 也可能确认掉了几行),把 objs 并进去再按上限收。退役掉的那几行不再回来。
        retired = {id(o) for o in gone}
        batch = [o for o in _unsaved if id(o) not in retired] + objs
        # 开轮行一律留住(一天里就那么几条):它一丢,后面那些回执就成了对不上
        # 轮次的孤行,整份账本当场变成「读不出来」—— 那比「这一轮条数不全」难用
        # 得多。
        opens = [o for o in batch if 'day' in o]
        rest = [o for o in batch if 'day' not in o]
        kept = (rest[-max(0, _KEEP_UNSAVED - len(opens)):]
                if len(opens) < _KEEP_UNSAVED else [])
        marks: list[dict] = []
        for o in rest[:len(rest) - len(kept)]:
            key = str(o.get('key') or '')
            if key and key not in {str(m['key']) for m in marks}:
                marks.append({'key': key, 'partial': 1})
        _unsaved = opens + marks + kept
        _certify()
    return False


def _owed() -> tuple[set[str], bool]:
    """此刻还有哪几轮欠着行没落盘 —— 手里攥着的,加上盘上那几笔凭证记着的。

    **轮换要靠它才敢删**:只按日期清轮次的话,过期那一轮的开轮行先被清走、它的回执
    随后从缓冲里补写出去,盘上就留下一条找不到轮次的孤行,整份账本从此永久「读不
    出来」(审查实测的 F2,形态和成组退役那条规则在 `kb.fetch_rotate`)。

    凭证**不按天筛**(所以这里压根不收 `floor`),盘上还在的每一笔都算:这里问的是
    「有没有人还要往这一轮上补行」,而不是「它排不排得进保留窗口」。筛掉过期那几笔的
    后果正是审查那一例 —— 欠着行的进程活着、它那笔凭证的日子却停在三天前,轮换照旧
    把那一轮清走。凭证本身
    的过期清理在 `fetch_pending._prune`(两边同一个 `_floor`),所以这里放宽不会让
    孤儿凭证永久扣着一轮。

    **说不清的时候不许轮换**(凭证读不出来):那时我们不知道别人还欠不欠,清走就
    可能造出那条孤行。代价只是账本长几行。
    """
    with _buffer_lock:
        keys = {str(o.get('key') or '') for o in _unsaved}
    held, ok = fetch_pending.held(_pending_folder(), '')
    return keys | held, ok


def _rotate(rounds: list[dict], stray: set[str] | None, at: datetime,
            locked: bool) -> None:
    """把过了保留窗口、而且没人再欠它证据的轮次清出账本(整份重写,见
    `kb.fetch_rotate`:那边是「该重写成什么」,这里是「现在许不许重写」)。

    **有行读不出来(`stray is None`,连文件读不出来也归这里)、没锁住、欠账说不清,
    三种都一个字不动**(各自的理由和实测形态跟着 `fetch_rotate.rotate` 那三个前提)。
    **孤行不算「读不出来」**:它认得出来是谁的、而它那一轮已经没了,留着才是永久的
    「读不出来」,所以带着它一起交给重写(口径在 `kb.fetch_fold`)。轮换失败也不出声
    —— 最坏只是文件长一点。
    """
    if stray is None or not locked:
        return
    floor = _floor(at)
    owed, seen = _owed()
    if seen:
        fetch_rotate.rotate(_file(), rounds, floor, owed, stray)


def _drop(key: str, objs: list[dict]) -> None:
    """把一次**被拒绝**的准入尝试压进缓冲的那几行成组撤掉(连那笔凭证一起更新)。

    拒绝意味着两处证据一处都没留下,所以这次尝试盘上没有任何痕迹 —— 内存里也不许
    留:留着的话故障一修好,光读一次页面(`today()` 就是缓冲的消费者)就把它补写成了
    当天第一轮抓取,而采集器一个请求都没发(审查实测的 F3)。**撤哪几行的判据在
    `kb.fetch_gate.rollback`** —— 它和「什么算参与证据」是同一条准入协议的两半。
    """
    global _unsaved
    with _buffer_lock:
        _unsaved = rollback(_unsaved, objs, key)
        _certify()


def round_key() -> str:
    """此刻属于哪一次**业务抓取** —— 当日账本的键,也是回执里的抓取身份。

    **这是对外那一份**:采集器往自己的库里记「这一条是哪一次抓到的」时问的就是
    这个问题(`feed_items.fetch_round`、公众号那张 `article_fetch_rounds`)。算法
    在 `pipeline_batch.round_key` —— 它同时给出子轮身份(管道那两个阶段两样都要),
    这里只取业务那一半。**两处各写一份迟早漂**,而漂的后果是 tooltip 把这一次新抓到的
    算成补导的,反过来也会。

    管道没在跑时是空串(CLI 导出、面板上手点的某个维护动作)。空串是「说不清」,
    照旧落进回执的 `unsure` 那一档,不冒充任何一次抓取 —— 为什么「在跑」必须由
    `running` 证明、而不能只看 `batch`/`run_id` 还在不在,见那一处。
    """
    from kb.pipeline_batch import round_key as _business

    return _business()[0]


def open_round(key: str, now: datetime | None = None,
               materializes: bool = True, seg: str = '') -> None:
    """抓取阶段开始时报到:今天又开了一次抓取(同一个 `key` 重复调是空操作)。

    多来源同步时每个来源各调一次,但它们带着同一个批次身份,所以只开一轮 ——
    这正是「不许把多来源算成多次抓取」那条落地的地方。

    `materializes`:这次运行后面还跑不跑物化阶段。**只勾了抓取的那种运行确实
    一篇都落不进 vault,它的 0 条是确定的**;而会物化却一条回执都没等到的轮次
    只能说「不可确认」—— 两者在账本上长得一模一样(都是一条开轮行、没有回执),
    所以这个事实要在开轮的时候就写下来,不能事后猜。

    `seg`:**这次运行自己**的身份(子轮,见 `pipeline_batch.round_key`)。会物化的
    每一次调用都要登记自己 —— **轮次幂等,子轮登记不幂等**:后面几次调用虽然不再
    开轮,但各自要交一行结束凭证,`today()` 认的是「登记过的子轮一个不剩都交完了」。
    少了这一步,第一个子轮的结束行就冒充了整批交完(第 2 轮审查实测)。不带 `seg`
    的调用退回老口径(整轮按一个子轮算),存量账本行同理。

    **会物化的调用在这里过一道闸:参与证据一处都留不下就抛。** 登记行追加成功就算
    留下了;没成功时再问账本和那笔凭证(`fetch_gate.landed`),都没有的话这一轮不许
    往下抓 —— 继续抓就是产生一批永远不会进账的文章(三轮审查各实测过一种绕法)。
    **抛在锁外**:拦的是抓取,不该把那把文件锁多攥一会儿。不拦的只有两条:不物化的
    运行,以及账本坏着、而那笔凭证写上了(理由见模块开头)。

    **拒绝之后这次尝试一行都不许留下**(`_drop`),而**「本进程开过这一轮」也要等
    准入定了才记**:上一版在读完账本那一刻就记上、拦住之后照旧把开轮行和子轮登记行
    攥在缓冲里,于是故障一修、面板下一跳自己把它补写成了当天第一轮抓取 —— 采集器
    一次都没跑,而真正第一次同步完成后 tooltip 写的是「第 2 次」(审查实测的 F3)。

    开轮行上那笔 `t` 是**微秒级的起跑时刻**,面板的「第 N 次」按它排序(口径在
    `kb.fetch_fold`;只有分钟级的 `at` 时顺序取的是物理行序,晚落盘的那一轮会冒充
    最后一轮)。`at` 照旧写:展示用,存量账本也只有它。
    """
    if not key:
        return
    at = _now(now)
    key = str(key)
    # 不物化的运行不登记子轮:它没有要交的东西,登记了反而永远等不到结束行。
    mark = str(seg) or _LEGACY_SEG
    segs = [{'key': key, 'seg': mark}] if materializes else []
    blocked = False
    with _locked(True) as locked:
        raw = _raw()
        rounds, ok, stray = _fold(raw) if raw is not None else ([], False, None)
        if ok and any(r['key'] == key for r in rounds):
            lines = segs                    # 顺手把攥着的那几行再试一次
        else:
            _rotate(rounds, stray, at, locked)
            # 子轮登记跟开轮行**同一行**:分成两行追加时「只落了开轮行」是一个真实
            # 的写入前缀(磁盘满、配额用尽),而那一行既够读侧算一次抓取、又不够准入
            # 认作证据 —— 于是没起跑的那次尝试占了今天第一次,当天真正第一次同步完成
            # 后 tooltip 又写成「第 2 次」(审查实测的 F2)。合成一行之后前缀只有两种:整行
            # 在(证据在盘上,该准入也该计次)、整行不在(盘上没有痕迹,两边都不算)。
            lines = [{'day': _day(at), 'key': key,
                      'at': at.strftime('%Y-%m-%d %H:%M'),
                      't': at.strftime('%Y-%m-%d %H:%M:%S.%f'),
                      'ex': 1 if materializes else 0,
                      **({'seg': mark} if materializes else {})}]
        if not _flush(lines, locked) and segs:
            # 追加没成功:**重读一次账本**再问那两处证据(`write` 成了而 `fsync`
            # 报错时那几行其实已经进了文件),一处都没留下才算拒绝。
            blocked = not landed(*_read(), _pending_folder(), key, mark)
        if blocked:
            _drop(key, lines)
        else:
            _push(_opened, key)
    if blocked:
        raise NoEvidence(NO_EVIDENCE)


def note_new(key: str, items) -> None:
    """这一轮抓取往 vault 新落了哪几篇 —— 参数是**生产者交回来的那份清单**。

    参数是一串 `kb.collectors.Fetched`(内容身份 + 抓取身份 + 这次写的路径),
    **不是一个数、也不只是路径**:

    - 不收数,调用方就没法把别的阶段算出来的产出当成抓取成果递进来(前两版正是这么
      错的,先拿日报取材结果、再拿全库首次索引数冒充它)。
    - 收**内容身份**而不是路径:折叠按它取并集,**任何重放都幂等**,而路径随标题变。
    - 收**抓取身份**,tooltip 背后那句分解才答得出来(口径见 docs/decisions/daily.md)。

    空清单要照样记:它是「这一轮确实一篇都没新落」的那条**确定**的回执。不记
    的话,它和「物化中途炸了、回执丢了」在账本上长得一样。

    回执里不在取材范围内的按 `path` 筛掉(`in_scope`)。**范围按路径判、去重按
    内容身份判**,两件事别混:前者问「这棵树算不算」,后者问「是不是同一篇」。
    **范围本身算不出来时整份回执归「不可确认」**,不拿一份空范围把它筛成空集 ——
    那等于把「答不出来」写成「确定不算」,再让结束行把这一轮定成确定的 0 条
    (判据在 `_scope_prefixes`)。按 `key` 找轮次、账本读不全那一档怎么办,
    两样都在 `_mark`。

    **清单里有读不懂的行,这一轮就报下界,不许静默少记。** 读不出形状、或者答不
    出路径(范围按路径判,答不出就判不了)的那几条照旧不计数 —— 但同时记一笔
    `partial`,面板据此说「至少 +N 条」。只跳过不记标记的后果和挑着收一份少记的
    清单一样:用户照着一个偏小的、**看着确定**的数决定不重新生成日报。
    """
    if not key:
        return
    scope = _scope_prefixes()
    if scope is None:
        note_unknown(key)
        return
    keep: dict[str, str] = {}
    torn = False
    for one in items:
        cid, fid, path = _identity(one)
        if not cid or not path:
            torn = True             # 读不懂 / 答不出路径:这一轮只是下界
            continue
        if not in_scope(path, scope):
            continue                # 不在取材范围内:这是正常的筛掉,不是缺口
        # 同一篇被报两次而只有一次带得出抓取身份时留**非空**那个(同 `_fold`):
        # 空串是「说不清」,拿它顶掉已知的身份等于把知道的事实又丢回去。
        keep[cid] = fid or keep.get(cid, '')
    lines: list[dict] = [{'items': [[c, f] for c, f in sorted(keep.items())]}]
    if torn:
        lines.append({'partial': 1})
    _mark(key, *lines)


def _mark(key: str, *lines: dict) -> None:
    """往某一轮上追加一行(回执 / 不可确认 / 子轮交完了)。**口径只有这一处。**

    一次调用里的几行**一起落**:回执和它那笔「这一份只是下界」的标记分两次写的话,
    中间那一刻盘上是一个看着确定的偏小值。

    三件事共用的那套判据:按 `key` 找轮次、**不按「今天」过滤**(23:59 开的轮次
    00:01 才物化完,按今天找会找不到它,那一轮永远停在 0 条);没开过这一轮就
    什么都不记(它不是一次抓取)。三处各写一遍的话,早晚有一处把 `_is_open`
    或者「读不全」那一档漏掉。

    **读不出账也不许把证据丢掉。** 读不全时判不了 `_is_open`,但「这一轮是不是本进程
    自己开的」是我们手里的事实(`_opened`):自己开的那一轮照旧追加这一行(它的开轮行
    要么已落盘、要么还攥在 `_unsaved` 里,孤行不会凭空多出来);别人开的那一轮只记
    一笔「缺了一行」(`_gaps`),由结束行带上 `partial` 落盘。上一版在这里直接
    return —— 一次**短暂**的读 I/O 故障就能让已落进 vault 的文章永久不进账,而随后
    的结束行照样把这一轮定成**确定的 0 条**(第 2 轮审查实测)。
    """
    key = str(key)
    with _locked(True) as locked:
        rounds, ok = _read()
        if ok:
            if not _is_open(key, rounds):
                _flush([], locked)
                return
        elif not _seen(_opened, key):
            _push(_gaps, key)
            return
        _flush([dict(one, key=key) for one in lines], locked)


def note(key: str, produce) -> None:
    """跑一次生产者,把它交回来的回执记进这一轮。

    「说不出来」和「中途炸了」对账本是同一件事:这一段的条数不全,记
    `note_unknown`。**口径只放在这一处** —— 两个采集器、重试入口、将来新增的
    来源都走它,免得某个调用点把 `None` 当成空集(那就是把「不知道」写成了
    「确定没有」)。
    """
    got = None
    try:
        got = produce()
    finally:
        if got is None:
            note_unknown(key)
        else:
            note_new(key, got)


def note_unknown(key: str) -> None:
    """这一轮有一段成果**说不出来**(采集器给不出清单、物化中途炸了)。

    记一笔标记,`today()` 据此报 partial。少了这一笔的后果是:勾了三个来源,
    前两个各交了回执、第三个炸在半路,面板会拿前两个的数当**确定**的总数 ——
    而第三个很可能已经往 vault 落下了文章。
    """
    if not key:
        return
    _mark(key, {'partial': 1})


def note_end(key: str, seg: str = '') -> None:
    """这一**子轮**的物化明确走完了 —— 它该交的回执一个不剩都结束了。

    **只有这一行能把一轮抓取变成「确定的 N 条」,而且要登记过的子轮全都交齐。**
    收到过回执不算(导出阶段进门先落一张空回执,于是「刚开始」和「全部交完」在盘上
    长得一模一样),一个子轮交完也不算整批交完(一轮日报里每个来源各跑一次导出阶段、
    共用一个业务 key)。所以结束行记在 `seg` 名下,完整性是 `segs == ends`(见
    `_fold` / `today`)。两种错法各有一次实测,形态在 `docs/decisions/daily.md`。

    **缺过一行的轮次,结束行要一并写上「不全」。** 读账短暂失败时有回执没能记下来
    (`_mark` 的 `_gaps`),光凭一个结束行解除未知,报出来就是一个确定却少记的数。

    这一行写在**逐来源循环走完之后**(`pipeline_stages._run_export`),不是写在
    某个来源的回执里。没有它的子轮,`added` 一律当下界看(面板说「至少 +N 条」)。
    """
    if not key:
        return
    line = {'seg': str(seg) or _LEGACY_SEG, 'end': 1}
    if _seen(_gaps, str(key)):
        line['partial'] = 1
    _mark(key, line)


def _is_open(key: str, rounds: list[dict]) -> bool:
    """这一轮开过没有 —— **盘上和手里都算**。

    只看盘上的后果是实测过的:数据目录短暂不可用让开轮那一行没写下去,随后
    物化真落了 2 篇,`note_new` 发现盘上没有这一轮、把那份清单整个丢掉,而
    开轮行这时正好补写成功 —— 面板从此笃定地说「第 N 次抓取 · 0 条」,刷新也
    纠不回来(第 3 轮审查实测)。
    """
    if any(r['key'] == key for r in rounds):
        return True
    with _buffer_lock:
        return any(str(o.get('key') or '') == key and 'day' in o for o in _unsaved)


def today(now: datetime | None = None) -> dict | None:
    """今天抓了第几次、最后那一次新落了多少 —— 面板那段 tooltip 要的就是这个。

    `{'n': 今天第几次, 'added': 那一次新落了多少篇, 'fresh': 其中这一次新抓到的,
    'carried': 其中之前抓到、这一次才补导进来的, 'unsure': 其中说不清是哪一次抓到的,
    'partial': 这个数全不全, 'at': 'HH:MM'}`;`n=0` 是「确定今天还没有抓取过」。
    **整份读不出来、有行没落下去、或者连共享锁都没拿到时返回 `None`(不可确认)**,
    由 API 原样交给前端。

    **`added` 报的是「这次同步之后日报能多拿到多少」,不是「远端这次新发现多少」**
    —— 仓库主人要的是前者(这个数的用处就是决定要不要再花一次模型钱重新生成
    日报),而补导进来的那几篇确实是日报这一刻才拿得到的新素材。两个数在补导场景
    下必然不等,所以把差额如实拆在 `fresh`/`carried`/`unsure` 里,而不是从 `added`
    里悄悄减掉。口径的来龙去脉在 docs/decisions/daily.md。

    `unsure` 是第三档,不许往 `carried` 里并:抓取身份是空串时我们只知道「没记过」,
    不知道「不是这一次」(存量内容、手工导入的链接、采集器还没来得及记都长这样)。
    并进去就是把"不知道"写成了"确定不是"。

    `partial=True` 的四种来源:某一段成果采集器说不出来(`note_unknown`)、这一轮
    会物化(`ex`)却没把**登记过的子轮**全交齐(`note_end`,进程在交回执或收尾
    之前就没了)、**别的进程(或者重启前的自己)还扣着这一轮的行没落盘**
    (`kb.fetch_pending` 那笔凭证),以及存量账本里一个子轮都没登记过的老轮次。
    那时 `added` 是**下界**,前端据此说「至少 +N 条」而不是「+N 条」。

    **这七个字段怎么从那几轮算出来,整份在 `kb.fetch_fold.answer`**(扣着的凭证指向
    账本里压根没有的轮次、「最后一轮」取谁、「交完了」的判据各有一次实测)。这边只管
    读:锁、凭证、攥着的行。(「那笔开工标记」不在这张单子里,也不该回来:少报那一档
    由 `open_round` 那道闸在**抓之前**挡掉,形态见模块开头。)

    **攥着没落盘的行时顺手重试一次**(走独占锁,别的读还是只拿共享锁):不重试的话
    磁盘故障恢复之后面板会一直说「读不出来」,要等下一次抓取才补写。
    """
    at = _now(now)
    if _pending():
        with _locked(True) as locked:
            _flush([], locked)
    with _locked(False) as locked:
        rounds, ok = _read()
        held, held_ok = fetch_pending.held(_pending_folder(), _floor(at))
    if not ok or not held_ok or not locked or _pending():
        return None
    return answer(rounds, held, _day(at))
