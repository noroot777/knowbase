# -*- coding: utf-8 -*-
"""本日的抓取账本:今天抓了第几次、每一次往 vault 里**新落了**多少篇。

日报面板那颗药丸报的就是这个事实。它和日报的取材结果是两件事,**不许互相
顶替**:一次抓取抓了多少,是抓取自己的事,生成日报、重新生成、删掉日报都改变
不了它;而「这一次生成能选进去几条」受名额(`daily.MAX_ITEMS`/`LATE_ITEMS`)和
发布时间排序左右,算出来的数和「抓了多少」可以差两个数量级。拿后者冒充前者的
后果是:第二次同步真抓回来 9 条、按钮上写 3 条;抓回来的那条发布得早、抢不到
席位时按钮写「没有新内容」—— 而用户正是靠这个数决定要不要再花一次模型钱。

**成果的归属由生产者的回执定,不由任何旁观者的差集猜。** 数的是**物化**那一步自己
交回来的那份清单(`SourceCollector.materialize()`,见 `kb.collectors`),一条路径都不是
扫出来的 —— 错过的两版都是拿旁观者的视角冒充生产者的成果(库里头一回出现的文档数、
物化前后的全 vault 差集,形态在 `docs/decisions/daily.md`)。采集器说不出来(返回
`None`)就记一笔「这一段不可确认」,不拿一个看着确定的数糊过去。

**「一次业务抓取」的身份是业务批次,不是管道运行**(一轮日报 = N+1 次运行、一次
抓取;按运行记的话面板写「今天第 7 次抓取」而用户只点过两次。没有批次的独立运行用
自己的 `run_id`。两样身份怎么算只有一处:`pipeline_batch.round_key`)。

**先有 `open_round` 才认 `note_new`。** 轮次在**抓取阶段**开 —— 只勾了导出/索引的
那种运行压根没抓过,它落下来的东西(比如上一轮抓回来还没导的)不该凭空变出
一个「第 N 次抓取」。`note_new` 找不到那一轮就什么都不做。

**账本分得清三件事:确定没有、确定有 N 条、不可确认。**(`today()` 返回 `None` /
`n=0` / `partial`。混成一句的后果是一次写失败之后面板一直说「今天还没有抓取」。)

**落盘是追加 + 幂等投影,所以重放不会少记、也不会多记。**

- 追加而不是整份重写:整份重写一次写坏就把当天已经确认过的轮次全带走,追加
  最坏只留一条半截行,`_fold` 认得出来。
- 回执行存的是**路径集合**,折叠时取**并集**。`write` 成功而 `fsync` 报错是真会
  发生的事(那几行其实已经进了文件),重试把同一行再追加一遍 —— 存条数会翻倍
  (实测 2 篇报成 4 篇),存路径则并集不变:重试、两条通道各报一次、重放都一样。
- 攥在手里没落下去的那几行不许悄悄丢。超过 `_KEEP_UNSAVED` 时丢掉的不是数据
  而是换成一条「这一轮不可确认」的标记,**投影不完整就不许解除「不可确认」**。
- **账本的每一次写都在那把文件锁里**(`_locked`)。不然进程 A 读完旧账、进程 B
  追加、A 再按旧快照轮换,就把 B 已经确认过的行覆盖掉了。**锁取不到的时候一行
  都不写** —— 攥在手里等下次,展示降级成「读不出来」。两边的降级路径必须是同一套
  协议:一边放行绕锁的追加、一边让持锁者放心覆盖,那条追加就是没人保护的(第 2 轮
  审查实测的形态写在 `_locked` 自己那段)。

**「收到过回执」不等于「这一轮交完了」。** 完整性只认那条明确的结束行(`note_end`,
写在逐来源循环走完之后):导出阶段进门先落一张空回执,「刚开始」和「全部交完」在盘上
长得一模一样;会物化却没等到结束行的轮次一律按**下界**报。两种错法都实测过(真实导出
里 `os._exit` 落在交回执之前;多来源里第一个来源的回执让整轮看着完整)。

**而「交完了」是按子轮集合算的,不是哪一个子轮说了算**(每个子轮开工时登记自己,
`today()` 只在登记过的子轮一个不剩都交了结束行时才给确定数;两条实测的事故形态
写在 `open_round` 的 `seg` 和 `note_end` 那两段)。

**证据不许因为「这一刻读不出账」就消失。** 回执和「不可确认」标记只要生产者交过就得
留在某处:自己开的那一轮照旧追加(`_mark` 认 `_opened`),别人开的那一轮至少记一笔
「缺了一行」(`_gaps`),由结束行带上 `partial` 落盘。上一版读失败时直接丢行,一次
**短暂**的读 I/O 故障就让已落进 vault 的文章永久不进账(第 2 轮审查实测)。

**生产者的提交边界在「文件落进 vault」那一刻,不在它后面那次入库确认。**
`os.replace` 一成功就进回执,随后 `feed_items` 的确认失败只是「这一篇下一轮
还会再导一次」,不该让这篇从成果事实里消失 —— 消失之后重导时它已经不是新
文件(`fresh=False`),这一篇永远补不回账(实测)。

**而「参与过」这件事不许只存在账本里。**「没有留下参与证据」不等于「没有参与」:账本
的每一次写都要过那把文件锁,锁坏掉时那几行攥在内存里,进程一没盘上就没有痕迹说明那个
子轮参与过,而它可能已经往 vault 落了文章(第 3 轮审查实测:整轮报成**确定的 1 条**,
vault 里躺着两篇)。所以攥住行的同时另外留一笔**不走那把锁**的凭证
(`kb.fetch_pending`),`today()` 据此压到下界;凭证指向一轮账本里压根没有的身份时,
干脆答「不可确认」,而不是沿用更早那一次的确定数。

**缓冲是两条线程共用的,确认只许移除自己真写下去的那几行。** 管道在写账本,而面板
每 15 秒读一次、`today()` 自己又会替攥着的行补写 —— 追加那一下是 I/O、**不在**进程
内那把缓冲锁里,这期间另一条线程压进来的回执不许被一句「清空缓冲」带走(实测形态
在 `_flush`)。锁序只有一个方向:**文件锁 → 缓冲锁**,缓冲锁里只搬缓冲自己的东西。

**而账本和那笔凭证能一起失效 —— 那一档不许往下抓**(`open_round` 那道闸)。第 4 轮
审查实测:旁挂锁被占成目录、凭证目录被占成普通文件,数据目录照旧可写,采集器真往
vault 写出第二篇,而盘上只剩第一个子轮那一套合法的行、`today()` 报**确定的 1 条**。
继续抓就是产生一批**永远不会进账**的文章(重导时它们已经不是新文件)。

**闸只认寿命和这笔欠账绑死的那两处证据:登记行、那笔独立凭证。** 登记行追加即不可
撤(只随保留窗口轮换),凭证手里还欠行就在、行一落盘就撤 —— 两处都正好活到「这一笔
交清」为止,所以它们不在就是真的没欠。按这条口径被挡在证据集外面的有两样,各是一次
实测:那笔**开工标记**的寿命是「那一轮运行」的(正常收尾就清掉、下一次起跑又原样
覆盖,第 5 轮审查;「标记现在不在」推不出「没有尚未入账的子轮」),以及**「此刻读不
出账本」**—— 它只是让我们看不见登记行,而一次瞬时读失败恢复之后,读侧按剩下那几个
完整子轮重新给出**确定的偏小值**(第 1 轮逻辑审查,形态在 `_landed`)。两样都是把
「这一轮少报」押在别的事情上,所以一处都不留:闸不认,读侧也不看。

这一刀和「坏记录不许拦生产者」判的是两件事:**别人那份记录读不出来**是展示问题
(降级成「读不出来」,照旧抓),**自己这一笔欠账一处都记不下**是记账能力没了(拦住,
先修磁盘)。所以不拦的只有两条:不物化的运行,以及**账本坏着、而那笔凭证写上了**
(反向钉在 `tests/test_fetch_evidence.py`)。而这一刀落下去之后账本**不会撒谎**:开轮
行上记着这次运行会不会物化(`ex`),会物化却没有结束行的轮次一律算「不可确认」,
不算确定的 0 条。

**「今天第 N 次」按业务起跑顺序排,不按谁最后补写。** 开轮行上带一笔微秒级起跑时刻
(`t`),投影按 `(day, t, key)` 排好序再交出去(`kb.fetch_fold`),轮换整份重写时把
`t` 一起抄走 —— 补写、重放、重写都不改变谁是「最后一轮」。靠物理行序的后果是实测的:
A 轮 11:00 的行因为短暂写失败晚落盘、B 轮 12:00 先落,面板把 A 的条数和时间当成第 2
次抓取(第 1 轮逻辑审查)。

面板那一侧怎么显示(几种说法、自定义区间、tooltip 里那句队列积压)写在
`docs/decisions/daily.md` 的「这个计数长在生成按钮里」那一节。
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kb import fetch_pending
from kb.config import DATA_DIR
from kb.fetch_fold import _LEGACY_SEG, _fold
from kb.fetch_scope import _scope_prefixes, in_scope      # noqa: F401 - 再导出
from kb.pipeline_persist import _write_atomic

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
    **持锁者可以按它读到的快照整份重写**。一边放行绕锁的追加、一边让持锁者放心
    覆盖,那条追加就是没人保护的 —— 第 2 轮审查实测:失锁那个进程把开轮、回执、
    结束行全部追加成功(`_unsaved` 空),另一个持锁进程随后按旧快照轮换,那一整轮
    消失,剩下的 JSON 行行合法,`today()` 自称确定、报 0 条,而它写出的
    `source/rss/ack.md` 还在 vault 里。两边的降级路径必须是同一套协议,所以这里
    只留一条口径:**没锁住就不写**。代价是那几行晚一点落盘、这段时间面板说
    「读不出来」—— 抓取那一侧照旧(`note_*` 都不抛;`open_round` 只在**两处证据
    都没留下**时才拦,那不是失锁这一档,见它自己那道闸)。
    """
    fh = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(DATA_DIR / LOCK_NAME, 'a+')
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError:
        if fh is not None:
            fh.close()
        fh = None
    try:
        yield fh is not None
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
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
    try:
        text = _file().read_text(encoding='utf-8')
    except FileNotFoundError:
        return [], True
    except (OSError, UnicodeDecodeError):
        return [], False
    return _fold(text.splitlines())


def _append(objs: list[dict]) -> bool:
    """追加这几行。失败返回 False,**不抛** —— 展示账本写不上不该把这一轮抓取
    带红,但也不许假装成功(那是上一版吞掉 `OSError` 的那条:25 篇文章已经入库、
    药丸永久写 0 条,补齐文章也不会自己补回来)。

    `fsync` 报错不代表前面那次 `write` 没写进去,所以这里返回 False 之后的重试
    **必须是幂等的** —— 回执行存路径集合、折叠取并集,正是为这一刀准备的。
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

    **确认只许移除「我这一批真写下去的那几行」,不许清空整份缓冲。** 追加是 I/O:
    它在缓冲锁外跑(见 `_buffer_lock`),这段时间另一条线程完全可以往缓冲里压进
    新的回执 —— 上一版成功之后一句 `_unsaved = []` 把它一起抹掉了。第 3 轮审查
    实测:面板那一跳(`today()` 替攥着的行补写)正卡在追加里,管道线程这时记下
    第二篇的回执,追加一回来缓冲就空了,那一篇再也补不回账(重导时它已经不是新
    文件),而 `today()` 自称确定、报 1 条,vault 里躺着两篇。所以这里**按身份
    (`id`)剔**:同样的行可能来两条(两条 `partial` 标记长得一模一样),按值剔会
    多剔一条;按条数剔在两条线程各自确认时会剔到对方新压进来的那几行。

    重复追加不要紧:回执存路径集合、子轮按集合算,折叠本来就幂等(见 `_fold`)。
    所以两条线程各把同一行写一遍也只是账本长一点,结论不变。
    """
    global _unsaved
    with _buffer_lock:
        held = list(_unsaved)
        if not held and not objs:
            return True
        mine = {id(o) for o in held}
    if locked and _append(held + objs):     # 追加在锁外:I/O 不许罩进缓冲锁
        with _buffer_lock:
            _unsaved = [o for o in _unsaved if id(o) not in mine]
            _certify()
        return True
    with _buffer_lock:
        # 这一批没落下去:重新拿一次当前的缓冲(这期间别的线程可能压进了新行,
        # 也可能确认掉了几行),把 objs 并进去再按上限收。
        batch = _unsaved + objs
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


def _rotate(rounds: list[dict], ok: bool, at: datetime, locked: bool) -> None:
    """把早于保留窗口的轮次清出账本(整份重写)。

    **读不全的时候一个字都不动**:那时我们并不知道文件里还有什么,拿认得出来的
    那部分重写回去,等于把读不出来的那几行悄悄删掉。轮换失败也不出声 ——
    最坏只是文件长一点,结论不受影响。

    **没锁住的时候同样一个字都不动。** 调用方必须已经持有独占锁、而且那把锁要
    罩住它前面那次 `_read`;锁取不到时(旁挂文件被占成目录、目录只读)这一步
    直接跳过 —— 它是整份原子替换,拿一份没被保护的旧快照写回去会把另一个进程
    已经确认过的整轮抹掉,而剩下的行行行合法(第 1 轮审查实测)。反过来那一半
    (持锁的轮换者覆盖失锁的追加者)由 `_flush` 堵住:失锁时压根不追加。不轮换
    的代价只是账本多留几天的行,结论不受影响。
    """
    if not ok or not locked:
        return
    floor = _floor(at)
    live = [r for r in rounds if r['day'] >= floor]
    if len(live) == len(rounds):
        return
    lines: list[dict] = []
    for r in live:
        # `t` 要一起抄走:它是「第 N 次」的排序依据(存量行没有,照旧留空)。
        lines.append({'day': r['day'], 'key': r['key'], 'at': r['at'],
                      **({'t': r['t']} if r['t'] else {}),
                      'ex': 1 if r['ex'] else 0})
        if r['got']:
            lines.append({'key': r['key'], 'paths': sorted(r['paths'])})
        # 子轮和它们的结束行都要抄过去:漏了结束行,一轮本来完整的抓取过一次
        # 轮换就变成「至少 N 条」;漏了还没交完的那几个子轮,反过来把不完整的
        # 说成完整 —— 而轮换是台面下的维护动作,用户只会看到数字自己变了。
        for seg in sorted(r['segs'] - r['ends']):
            lines.append({'key': r['key'], 'seg': seg})
        for seg in sorted(r['ends']):
            lines.append({'key': r['key'], 'seg': seg, 'end': 1})
        if r['partial']:
            lines.append({'key': r['key'], 'partial': 1})
    try:
        _write_atomic(_file(), ''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines).encode('utf-8'))
    except OSError:
        pass


class NoEvidence(RuntimeError):
    """两处参与证据都留不下,所以这一轮没有往下抓(消息就是 `NO_EVIDENCE`)。

    **单独一个类型**,好让编排那一侧只把这一档的原因写进阶段卡片
    (`pipeline._run_pipeline` 的 crawl 分支)—— 拿 `RuntimeError` 当判据会把采集器
    自己抛的运行时错误也说成「抓取没有起跑」。
    """


# 两处证据都没留下时那一句拒绝的**唯一出处**。
NO_EVIDENCE = (
    '这一轮抓取的参与记录一处都没能留下（当日账本的登记行、独立凭证都没写上），'
    '所以没有往下抓：留不下记录的话这一轮新落的文章永远不会进账，面板会笃定地报'
    '一个少记的数，而重导时它们已经不算新文件、补不回来。先看一眼磁盘空间，以及'
    '数据目录和里面那个 fetch_rounds.* 的权限')


def _landed(key: str, seg: str) -> bool:
    """这一笔「我参与了这一轮物化」,盘上还留得住吗 —— **追加没成功时才问这一句。**

    判的其实是**读侧会不会笃定地少报**:两处证据任一还在,`today()` 就会把这一轮
    压到下界或者答「不可确认」,那时继续抓是安全的。

    **只认寿命和这笔欠账绑死的证据。** 两处都活到「这一笔交清」为止 —— 登记行追加
    即不可撤,凭证手里还欠行就在。会被别的事件撤掉的东西不算证据:第 4 轮在这里还
    认过那笔开工标记,而它正常收尾就被清掉、下一次起跑又被覆盖(第 5 轮审查实测,
    形态写在模块开头)—— 那等于把「这一轮少报」押在「没有下一轮」上。

    · 账本里已经有这一轮这一个子轮:正路(同一个子轮重复开工时走这条)。
    · 本进程那几代凭证记着这一轮:「锁坏了、行攥在手里」那一档,**读回来确认**
      (`fetch_pending.put` 写不上是不出声的,它的返回值证明不了什么)。

    **「此刻读不出账本」不算证据,一度算过。** 原来 `ok=False` 直接放行,理由是
    「读不出来不等于我那一行没落下去,读侧这时也只会答不可确认」—— 后半句只在账本
    **永久**坏在盘上时成立,而 `_read` 的 `ok=False` 还包括一次瞬时 `OSError`:它一
    恢复,读侧看见的就是此前那几个完整子轮、没有缺口凭证,于是重新给出一个**确定的
    偏小值**(第 1 轮逻辑审查实测:两条写路径都坏着、只对起跑那两次账本读注入
    `OSError`,真采集器照旧抓、真导出又落 1 篇、进程正常退出,全新进程报确定的 2 条
    而 vault 里三篇)。**「这个请求读不到」推不出「将来的读者都会知道这一轮不全」。**
    所以读失败只是让我们**看不见登记行**:这一句退到那笔凭证上,账本坏着而凭证写上
    了的那条路径照旧放行,两处都没留下才拦。
    """
    rounds, ok = _read()
    # 读得出来才谈得上「登记行在盘上」;读不出来时它只是看不见,不能当证据用。
    if ok and any(r['key'] == key and seg in (r['segs'] | r['ends']) for r in rounds):
        return True
    return key in fetch_pending.mine(_pending_folder())


def open_round(key: str, now: datetime | None = None,
               materializes: bool = True, seg: str = '') -> None:
    """抓取阶段开始时报到:今天又开了一次抓取(同一个 `key` 重复调是空操作)。

    多来源同步时每个来源各调一次,但它们带着同一个批次身份,所以只开一轮 ——
    这正是「不许把多来源算成多次抓取」那条落地的地方。

    `materializes`:这次运行后面还跑不跑物化阶段。**只勾了抓取的那种运行确实
    一篇都落不进 vault,它的 0 条是确定的**;而会物化却一条回执都没等到的轮次
    只能说「不可确认」—— 两者在账本上长得一模一样(都是一条开轮行、没有回执),
    所以这个事实要在开轮的时候就写下来,不能事后猜。

    `seg`:**这次运行自己**的身份(子轮,见 `pipeline_batch.round_key`)。会物化
    的每一次调用都要登记自己 —— **轮次幂等,子轮登记不幂等**:第二次、第三次
    调用虽然不再开轮,但它们各自要交一行结束凭证,`today()` 认的是「登记过的
    子轮一个不剩都交完了」。少了这一步,第一个子轮的结束行就冒充了整批交完
    (第 2 轮审查实测:第二个子轮写出文章后 `os._exit(23)`,整轮仍报确定的 1 条)。
    不带 `seg` 的调用退回老口径(整轮按一个子轮算),存量账本行同理。

    **会物化的调用在这里过一道闸:参与证据一处都留不下就抛。** 登记行追加成功就
    算留下了;没成功时再问账本和那笔凭证(`_landed` —— **只认寿命和欠账绑死的那两
    处**),都没有的话这一轮不许往下抓:继续抓就是产生一批永远不会进账的文章(第 4
    轮审查实测两处旁文件一起写不进去,第 5 轮实测这一档连硬退出都不需要,第 1 轮逻辑
    审查实测只要再加一次瞬时读失败就能绕过去)。**抛在锁外**:拦的是抓取,不该把那把
    文件锁多攥一会儿。不拦的只有两条:不物化的运行(它一篇都落不进 vault,没有会被
    少记的成果),以及**账本坏着、而那笔凭证写上了** —— 坏掉的展示账本不许把抓取
    拦住,但它也不能替凭证作证。

    开轮行上那笔 `t` 是**微秒级的起跑时刻**,面板的「第 N 次」按它排序(口径在
    `kb.fetch_fold`;只有分钟级的 `at` 时顺序取的是物理行序,晚落盘的那一轮会冒充
    最后一轮)。`at` 照旧写:展示用,存量账本也只有它。
    """
    if not key:
        return
    at = _now(now)
    key = str(key)
    # 不物化的运行不登记子轮:它没有要交的东西,登记了反而永远等不到结束行。
    segs = [{'key': key, 'seg': str(seg) or _LEGACY_SEG}] if materializes else []
    with _locked(True) as locked:
        rounds, ok = _read()
        _push(_opened, key)
        if ok and any(r['key'] == key for r in rounds):
            wrote = _flush(segs, locked)    # 顺手把攥着的那几行再试一次
        else:
            _rotate(rounds, ok, at, locked)
            wrote = _flush([{'day': _day(at), 'key': key,
                             'at': at.strftime('%Y-%m-%d %H:%M'),
                             't': at.strftime('%Y-%m-%d %H:%M:%S.%f'),
                             'ex': 1 if materializes else 0}] + segs, locked)
        blocked = bool(segs) and not wrote and not _landed(key, segs[0]['seg'])
    if blocked:
        raise NoEvidence(NO_EVIDENCE)


def note_new(key: str, paths) -> None:
    """这一轮抓取往 vault 新落了哪几篇 —— 参数是**生产者交回来的那份清单**。

    **参数是身份(路径集合)而不是一个数**,有两个理由:调用方没法把别的阶段算
    出来的产出当成抓取成果递进来(前两版正是这么错的,先拿日报取材结果、再拿
    全库首次索引数冒充它);而且落盘之后折叠取并集,**任何重放都幂等** ——
    `write` 成功、`fsync` 报错的那一刀之后重试一遍,2 篇还是 2 篇。

    空集合要照样记:它是「这一轮确实一篇都没新落」的那条**确定**的回执。不记
    的话,它和「物化中途炸了、回执丢了」在账本上长得一样。

    回执里不在取材范围内的路径这里筛掉(`in_scope`)。**范围算不出来时整份回执
    归「不可确认」**,不拿一份空范围把它筛成空集 —— 那等于把「答不出来」写成
    「确定不算」,再让结束行把这一轮定成确定的 0 条(判据在 `_scope_prefixes`)。

    按 `key` 找轮次、**不按「今天」过滤**:23:59 开的轮次 00:01 才物化完,按今天找
    会找不到它,那一轮永远停在 0 条。
    """
    if not key:
        return
    scope = _scope_prefixes()
    if scope is None:
        note_unknown(key)
        return
    keep = sorted({str(p) for p in paths if in_scope(str(p), scope)})
    _mark(key, {'paths': keep})


def _mark(key: str, line: dict) -> None:
    """往某一轮上追加一行(回执 / 不可确认 / 子轮交完了)。**口径只有这一处。**

    三件事共用的那套判据:按 `key` 找轮次、**不按「今天」过滤**(23:59 开的轮次
    00:01 才物化完,按今天找会找不到它,那一轮永远停在 0 条);没开过这一轮就
    什么都不记(它不是一次抓取)。三处各写一遍的话,早晚有一处把 `_is_open`
    或者「读不全」那一档漏掉。

    **读不出账也不许把证据丢掉。** 读不全时判不了 `_is_open`,但「这一轮是不是
    本进程自己开的」是我们手里的事实(`_opened`):自己开的那一轮照旧把这一行追
    加下去(它的开轮行要么已经落盘、要么还攥在 `_unsaved` 里,对不上轮次的孤行
    不会凭空多出来);别人开的那一轮只记一笔「缺了一行」(`_gaps`),由结束行
    带上 `partial` 落盘。上一版在这里直接 return —— 一次**短暂**的读 I/O 故障
    就能让已经落进 vault 的那篇文章永久不进账,而随后的结束行照样写成功、把
    这一轮定成**确定的 0 条**(第 2 轮审查实测,恢复后重导也不算新文件)。
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
        _flush([dict(line, key=key)], locked)


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
    收到过回执不算:导出阶段进门先落一张空回执(「成果从确定的空集起步」),于是
    「刚开始」和「全部交完」在盘上长得一模一样 —— 随后生产者真写出了文章、进程
    却在交回执前没了,盘上留下的是一个**确定的 0 条**,重试和刷新都纠不回来
    (第 1 轮审查实测:真实 `_run_export` 里 `os._exit(23)`,vault 多了 1 篇,
    重启读出来 `{'n':1,'added':0,'partial':False}`)。多来源那一侧同形:第一个
    来源交完回执就足以让整轮看着完整,第二个还没开口时进程没了。

    **一个子轮交完也不算整批交完。** 一轮日报里每个来源各跑一次导出阶段、共用
    一个业务 key:第一个子轮正常收尾,第二个子轮在另一个进程里写出第二篇之后
    `os._exit(23)`,盘上第一个子轮的结束行会让整轮报成**确定的 1 条**,而 vault
    里躺着两篇(第 2 轮审查实测)。所以结束行记在 `seg` 名下,完整性是
    `segs == ends`(见 `_fold` / `today`)。

    **缺过一行的轮次,结束行要一并写上「不全」。** 读账短暂失败时有回执没能记下
    来(`_mark` 的 `_gaps`),读账恢复之后光凭一个结束行解除未知,报出来就是一个
    确定却少记的数 —— 那正是第 2 轮审查那条实测的后半截。

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
    """今天抓了第几次、最后那一次新落了多少 —— 面板那颗药丸要的就是这个。

    `{'n': 今天第几次, 'added': 那一次新落了多少篇, 'partial': 这个数全不全,
    'at': 'HH:MM'}`;`n=0` 是「确定今天还没有抓取过」。**整份读不出来、有行没落
    下去、或者连共享锁都没拿到时返回 `None`(不可确认)**,由 API 原样交给前端。

    `partial=True` 的四种来源:某一段成果采集器说不出来(`note_unknown`)、这一轮
    会物化(`ex`)却没把**登记过的子轮**全交齐(`note_end`,进程在交回执或收尾
    之前就没了)、**别的进程(或者重启前的自己)还扣着这一轮的行没落盘**
    (`kb.fetch_pending` 那笔凭证),以及存量账本里一个子轮都没登记过的老轮次。
    那时 `added` 是**下界**,前端据此说「至少 +N 条」而不是「+N 条」。

    **扣着的凭证指向一轮账本里压根没有的身份时,整句答「不可确认」。** 今天确实抓过
    一次,而盘上最新的轮次是更早那一次 —— 拿它的数当今天的答案,就是把别人的成果说成
    这一次的。

    **「那笔开工标记」不在这张单子里,也不该回来**(它的寿命是那一轮运行的、不是那笔
    欠账的;少报那一档由 `open_round` 那道闸在**抓之前**挡掉,形态见模块开头)。

    **攥着没落盘的行时顺手重试一次。** 不重试的话,磁盘故障恢复之后面板会一直
    说「读不出来」,要等下一次抓取才补写(第 1 轮审查 N4)。重试走独占锁,别的
    读还是只拿共享锁。
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
    keys = {r['key'] for r in rounds}
    if held - keys:
        return None            # 有一轮扣着行,而账本里连它的轮次都没有
    day = _day(at)
    mine = [r for r in rounds if r['day'] == day]
    if not mine:
        return {'n': 0, 'added': 0, 'partial': False, 'at': ''}
    # 投影已按业务起跑顺序排过(`fetch_fold._order_key`),末位就是「最后一轮」——
    # 顺序只有一处口径:这里不许按行序理解,也不许就地再排一次。
    last = mine[-1]
    # 「交完了」= 登记过的子轮一个不剩都交了结束行,而且没人扣着它的行。两个集合都
    # 空时分两种:开轮就说了不物化(`ex=0`)的那一轮确定是 0 条;存量账本那几轮没有
    # 子轮行,证明不了完整,只能算不可确认。
    done = (last['segs'] == last['ends'] and bool(last['segs'] or not last['ex'])
            and last['key'] not in held)
    return {'n': len(mine), 'added': len(last['paths']),
            'partial': bool(last['partial'] or not done),
            'at': last['at'][-5:]}
