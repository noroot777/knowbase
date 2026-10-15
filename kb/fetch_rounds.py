# -*- coding: utf-8 -*-
"""本日的抓取账本:今天抓了第几次、每一次往 vault 里**新落了**多少篇。

日报面板那颗药丸报的就是这个事实。它和日报的取材结果是两件事,**不许互相
顶替**:一次抓取抓了多少,是抓取自己的事,生成日报、重新生成、删掉日报都改变
不了它;而「这一次生成能选进去几条」受名额(`daily.MAX_ITEMS`/`LATE_ITEMS`)和
发布时间排序左右,算出来的数和「抓了多少」可以差两个数量级。拿后者冒充前者的
后果是:第二次同步真抓回来 9 条、按钮上写 3 条;抓回来的那条发布得早、抢不到
席位时按钮写「没有新内容」—— 而用户正是靠这个数决定要不要再花一次模型钱。

**成果的归属由生产者的回执定,不由任何旁观者的差集猜。** 数的是**物化**那一步
自己交回来的那份清单(`SourceCollector.materialize()` 的返回值,见
`kb.collectors`),一条路径都不是扫出来的。这里错过两版,两版都是"拿旁观者的
视角冒充生产者的成果":

- 拿"库里头一回出现这个路径"的文档数当抓取成果 —— 空抓取碰上 3 篇历史未索引的
  旧文报成「+3 条」,反过来真抓到 2 篇而这一轮不索引则报 0、之后单独索引也补不
  回来(两种都是实测)。
- 拿物化前后的全 vault 路径差集当抓取成果 —— 物化那段时间里**别的进程**往
  非衍生树落的文件会被算进这一轮(实测:本轮确定空抓,另一个进程手工放进一篇
  `source/rss/manual.md`,药丸报「+1 条」)。差集是旁观者的视角,它分不出
  "谁写的"。

所以现在只收回执:采集器说不出来(返回 `None`)就记一笔「这一段不可确认」,
宁可面板上写「条数读不出来」,也不拿一个看着确定的数糊过去。

**「一次业务抓取」的身份是业务批次,不是管道运行。** 一轮日报 = N+1 次管道运行
(每个来源各一次),它们共用一个 `pipeline_batch.open_batch()` 发的身份;勾了三个
来源手动同步也是一次抓取、三次运行。按运行记的话面板上会写成「今天第 7 次抓取」
而用户只点过两次。没有批次身份的独立运行(单来源同步、重试失败项)各算一次,
那时它自己的 `run_id` 就是身份。

**先有 `open_round` 才认 `note_new`。** 轮次在**抓取阶段**开 —— 只勾了导出/索引的
那种运行压根没抓过,它落下来的东西(比如上一轮抓回来还没导的)不该凭空变出
一个「第 N 次抓取」。`note_new` 找不到那一轮就什么都不做。

**账本分得清三件事:确定没有、确定有 N 条、不可确认。** 读不出来时 `today()`
返回 `None`;`n=0` 是「确定今天还没有抓取过」;`partial` 是「这一轮的条数不全」。
三件事混成一句的后果是,一次写失败之后面板会一直笃定地说「今天还没有抓取」。

**落盘是追加 + 幂等投影,所以重放不会少记、也不会多记。**

- 追加而不是整份重写:整份重写一次写坏就把当天已经确认过的轮次全带走,追加
  最坏只留一条半截行,`_fold` 认得出来。
- 回执行存的是**路径集合**,折叠时取**并集**。`write` 成功而 `fsync` 报错是真会
  发生的事(那几行其实已经进了文件),重试把同一行再追加一遍 —— 存条数就会
  翻倍(实测 2 篇报成 4 篇),存路径则并集不变。同一轮被重试、被两条通道各报
  一次、被同一个进程重放,结论都一样。
- 攥在手里没落下去的那几行不许悄悄丢。超过 `_KEEP_UNSAVED` 时丢掉的不是数据
  而是换成一条「这一轮不可确认」的标记,**投影不完整就不许解除「不可确认」**。
- **账本的每一次写都在那把文件锁里**(`_locked`)。不然进程 A 读完旧账、进程 B
  追加、A 再按旧快照轮换,就把 B 已经确认过的行覆盖掉了。**锁取不到的时候一行
  都不写** —— 攥在手里等下次,展示降级成「读不出来」。上一版是「失锁只许追加、
  不许轮换」:那只挡住了"失锁者自己轮换",挡不住**另一个持锁的进程**按它读到的
  旧快照轮换,把失锁者已经追加成功的整轮抹掉(第 2 轮审查实测:失锁那个进程写出
  `source/rss/ack.md`、开轮/回执/结束行全部落盘,另一个进程轮换之后那一轮整个
  消失,结论从「+1 条」变成**确定的 0 条**)。两边的降级路径是同一套协议,不能
  一边放行绕锁的写、一边让持锁者放心覆盖。

**「收到过回执」不等于「这一轮交完了」。** 完整性只认那条明确的结束行
(`note_end`,写在逐来源循环走完之后):导出阶段进门先落一张空回执,所以
「刚开始」和「全部交完」在盘上长得一模一样;会物化却没等到结束行的轮次一律按
**下界**报(面板说「至少 +N 条」)。错过的两种形态都是实测的:真实导出里
`os._exit` 落在交回执之前,vault 多了 1 篇而重启后盘上写着确定的 0 条;多来源
里第一个来源交完回执就让整轮看着完整,第二个还没开口时进程没了。

**而「交完了」是按子轮集合算的,不是哪一个子轮说了算。** 一次业务抓取的物化由
N 个子轮各跑一段(一轮日报里每个来源一次管道运行,共用一个批次身份),所以
每个子轮开工时把自己登记进来(`open_round(seg=...)`),`today()` 只在**登记过的
子轮一个不剩都交了结束行**时才给确定数。少了这一层的后果是实测的:第一个子轮
正常交完,第二个子轮在另一个进程里写出第二篇之后 `os._exit(23)`,盘上第一个子轮
的结束行让整轮报成**确定的 1 条**,而 vault 里躺着两篇。

**证据不许因为「这一刻读不出账」就消失。** 回执和「不可确认」标记只要生产者交过,
就得留在某处:自己开的那一轮照旧追加(`_mark` 认 `_opened`),别人开的那一轮至少
记一笔「缺了一行」(`_gaps`),由结束行带上 `partial` 落盘。上一版在读失败时直接
丢掉那一行,于是一次**短暂**的读 I/O 故障就能让已经落进 vault 的文章永久不进账,
而随后的结束行照样写成功、把这一轮定成确定的 0 条(第 2 轮审查实测:恢复后重导
也不算新文件,这一篇再也补不回来)。

**生产者的提交边界在「文件落进 vault」那一刻,不在它后面那次入库确认。**
`os.replace` 一成功就进回执,随后 `feed_items` 的确认失败只是「这一篇下一轮
还会再导一次」,不该让这篇从成果事实里消失 —— 消失之后重导时它已经不是新
文件(`fresh=False`),这一篇永远补不回账(实测)。

**只有一种情形救不回来**:写失败之后进程就没了 —— 那几行既没落盘也不在内存里。
但这一刀落下去之后账本**不会撒谎**:开轮行上记着这次运行会不会物化(`ex`),
会物化却没有结束行的轮次一律算「不可确认」,而不是算确定的 0 条。

面板那一侧怎么显示(几种说法、自定义区间、tooltip 里那句队列积压)写在
`docs/decisions/daily.md` 的「这个计数长在生成按钮里」那一节。
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kb.config import DATA_DIR
from kb.pipeline_persist import _write_atomic

TZ = ZoneInfo('Asia/Shanghai')
FILE_NAME = 'fetch_rounds.jsonl'
LOCK_NAME = 'fetch_rounds.lock'
# 账本里留几天。只回答「今天」,但不能只留今天:23:59 开的那一轮 00:01 才物化完,
# 记它的那一下得找得到它(轮次按**开工那天**归属,不在追加时重新猜今天)。
_KEEP_DAYS = 2
# 攥在手里等下次重试的行数上限。磁盘一直写不进去的话,这里挡住它无界增长 ——
# 但丢掉的那几行要换成「不可确认」标记,不许静默少记(见模块开头)。
_KEEP_UNSAVED = 64
# `_opened` / `_gaps` 各记得住几轮身份。一天就那么几轮,这个数只是挡无界增长。
_KEEP_KEYS = 64
# 没带子轮身份的结束行(存量账本、没有 run id 的调用)折成这一个身份。它**同时
# 算「登记过」和「交完了」**,所以老账本的结论一个字都不变。
_LEGACY_SEG = '-'

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


def _push(bag: list[str], key: str) -> None:
    """把一个轮次身份记进 `bag`:去重、挤掉最老的,最多留 `_KEEP_KEYS` 个。

    挤出去的那一档落在安全的一侧:`_opened` 少一个 = 那一行退回「缺了一行」,
    `_gaps` 少一个 = 结束行不带 `partial`,而那时轮次本来就少了行、`_fold` 会
    自己报 `ok=False`。
    """
    if key in bag:
        return
    bag.append(key)
    del bag[:-_KEEP_KEYS]


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
    「读不出来」—— 抓取那一侧完全不受影响(`open_round` / `note_*` 都不抛)。
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


def _scope_prefixes() -> list[tuple[tuple[str, ...], frozenset]] | None:
    """算「抓到的新内容」的那些树,各自的 vault 相对前缀 + 要跳过的子目录。

    一次算好交给 `in_scope` 复用:一轮回执有上百条路径,每条都把全部来源 resolve
    一遍是上千次 syscall,而这份清单一轮之内不会变。

    **没读全就交 `None`,不许交一份少了几棵树的清单。** `resolve()` 会因为权限、
    符号链接层数、目录临时不可用而抛 `OSError` —— 那时「这条路径算不算成果」是
    **答不出来**,不是「确定不算」。上一版把这两种压成一种(`return []` / 跳过
    那棵树),于是一次临时故障能把一份真回执筛成空集,再让结束行把这一轮定成
    确定的 0 条(第 2 轮审查静态项)。`ValueError` 是另一件事:那棵树**确定**
    不在 vault 底下,跳过它才是对的。
    """
    from kb.config import SOURCES, VAULT_ROOT

    try:
        root = Path(VAULT_ROOT).resolve()
    except OSError:
        return None
    out = []
    for src in SOURCES:
        if src.derived or not src.enabled:
            continue
        try:
            out.append((Path(src.root).resolve().relative_to(root).parts,
                        src.skip_dirs))
        except OSError:
            return None                 # 这棵树没读全 → 整份范围都不可信
        except ValueError:
            continue                    # 确定不在 vault 底下
    return out


def in_scope(rel: str, prefixes) -> bool:
    """这个路径**算不算「抓到的新内容」** —— 范围口径只有这一份。

    和日报取材认的那批树一致(`daily._scope`):非衍生、启用中、不在 `skip_dirs`
    里。回执由采集器交,但"哪些树算"是知识库自己的事,不能交给每个采集器各判
    一遍 —— 条目化衍生树(`dr-gzh-projects` 等)和原文是同一批内容,日报本来就
    排除它们,算进来的话一轮条目化就能让药丸写出三位数而日报那边一条新的都没有;
    停用的树同理不再进库。

    `prefixes` 由调用方先算好(`_scope_prefixes`)。**算不出来时不许拿空范围
    当答案** —— 那一档归「不可确认」,判据在 `note_new`。
    """
    parts = Path(rel).parts
    for prefix, skip in prefixes:
        if parts[:len(prefix)] != prefix:
            continue
        rest = parts[len(prefix):]
        return not (rest and rest[0] in skip)
    return False


def _fold(lines) -> tuple[list[dict], bool]:
    """把账本每一行折成「开过哪几轮、各自新落了哪些文章、走完了没有」,外加一句
    「读全了没有」。

    五种行:开轮(带 `day`/`at`/`ex`)、回执(带 `paths`)、不可确认标记(带
    `partial`)、子轮登记(带 `seg`)、子轮交完了(带 `seg`+`end`)。**回执取并集、
    子轮按集合算**,所以同一行追加两遍、同一轮被两条通道各报一次,折出来的结论都
    一样(见模块开头那条)——「交完了几次」用计数的话,一次 fsync 重放就能把它
    推成完整。

    **`got`(收到过回执)和「交完了」是两件事,不许互相顶替。** 上一版只有 `got`:
    导出阶段进门先落一张空回执,于是「刚开始」在盘上和「全部交完」长得一模一样
    —— 随后生产者真写出了文章、进程却没了,盘上的结论永久是**确定的 0 条**
    (第 1 轮审查实测两个分支:预置空回执、以及多来源里第一个交完回执后退出)。

    **而「交完了」是 `segs == ends`,不是「有谁交过」。** 一次业务抓取的物化由 N 个
    子轮各跑一段,第一个子轮的结束行证明不了后面那几个也交完(第 2 轮审查实测)。
    结束行**同时**把自己记进 `segs`:只跑导出的那种运行(重试导出阶段)压根没开过
    轮、不会登记,它交完了也该算完,否则那一轮永远「不可确认」。

    `ok=False` 表示有行读不出来(半截行、被改坏、成果对不上轮次)。那时这份账本
    只能说「不可确认」—— 拿认得出来的那部分冒充完整事实,就又回到了把写失败说成
    「今天还没有抓取」。
    """
    rounds: list[dict] = []
    index: dict[str, dict] = {}
    ok = True
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            ok = False                  # 半截行(写一半断电)或被人改坏
            continue
        if not isinstance(obj, dict) or not obj.get('key'):
            ok = False
            continue
        key = str(obj['key'])
        if {'paths', 'partial', 'end', 'seg'} & obj.keys():
            got = index.get(key)
            if got is None:
                ok = False              # 有成果、却找不到它那一轮:记不全
                continue
            if obj.get('partial'):
                got['partial'] = True
            if obj.get('end'):
                seg = str(obj.get('seg') or _LEGACY_SEG)
                got['segs'].add(seg)    # 见上:交完了的子轮一定算登记过
                got['ends'].add(seg)
            elif 'seg' in obj:
                got['segs'].add(str(obj['seg'] or _LEGACY_SEG))
            if 'paths' in obj:
                if not isinstance(obj['paths'], list):
                    ok = False
                    continue
                got['paths'].update(str(p) for p in obj['paths'])
                got['got'] = True
            continue
        if key in index:
            continue                    # 同一轮重复开:幂等,不是第二次抓取
        got = {'key': key, 'day': str(obj.get('day') or ''),
               'at': str(obj.get('at') or ''),
               # 这次运行会不会物化。读不到(存量账本行)按会物化算 —— 那一侧
               # 的结论是「不可确认」,比笃定地说 0 条安全。
               'ex': bool(obj.get('ex', 1)), 'paths': set(), 'got': False,
               # 参与这一轮物化的子轮,以及其中**明确交完了**的那几个。
               # 两个集合相等才算这一轮交完(见 `today`);存量账本行里一个
               # 子轮都没有,那几轮只能说「至少 N 条」—— 新代码确实证明不了
               # 它们完整,这比继续笃定地报一个数安全。
               'segs': set(), 'ends': set(), 'partial': False}
        rounds.append(got)
        index[key] = got
    return rounds, ok


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
    """
    global _unsaved
    batch = _unsaved + objs
    if not batch:
        return True
    if locked and _append(batch):
        _unsaved = []
        return True
    # 开轮行一律留住(一天里就那么几条):它一丢,后面那些回执就成了对不上轮次
    # 的孤行,整份账本当场变成「读不出来」—— 那比「这一轮条数不全」难用得多。
    opens = [o for o in batch if 'day' in o]
    rest = [o for o in batch if 'day' not in o]
    kept = rest[-max(0, _KEEP_UNSAVED - len(opens)):] if len(opens) < _KEEP_UNSAVED else []
    marks: list[dict] = []
    for o in rest[:len(rest) - len(kept)]:
        key = str(o.get('key') or '')
        if key and key not in {str(m['key']) for m in marks}:
            marks.append({'key': key, 'partial': 1})
    _unsaved = opens + marks + kept
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
    floor = _day(at - timedelta(days=_KEEP_DAYS - 1))
    live = [r for r in rounds if r['day'] >= floor]
    if len(live) == len(rounds):
        return
    lines: list[dict] = []
    for r in live:
        lines.append({'day': r['day'], 'key': r['key'], 'at': r['at'],
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
            _flush(segs, locked)        # 顺手把攥着的那几行再试一次
            return
        _rotate(rounds, ok, at, locked)
        _flush([{'day': _day(at), 'key': key,
                 'at': at.strftime('%Y-%m-%d %H:%M'),
                 'ex': 1 if materializes else 0}] + segs, locked)


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
        elif key not in _opened:
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
    if str(key) in _gaps:
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
    return any(str(o.get('key') or '') == key and 'day' in o for o in _unsaved)


def today(now: datetime | None = None) -> dict | None:
    """今天抓了第几次、最后那一次新落了多少 —— 面板那颗药丸要的就是这个。

    `{'n': 今天第几次, 'added': 那一次新落了多少篇, 'partial': 这个数全不全,
    'at': 'HH:MM'}`;`n=0` 是「确定今天还没有抓取过」。**整份读不出来、有行没落
    下去、或者连共享锁都没拿到时返回 `None`(不可确认)**,由 API 原样交给前端。

    `partial=True` 的三种来源:某一段成果采集器说不出来(`note_unknown`)、这一轮
    会物化(`ex`)却没把**登记过的子轮**全交齐(`note_end`,进程在交回执或收尾
    之前就没了),以及存量账本里一个子轮都没登记过的老轮次。那时 `added` 是
    **下界**,前端据此说「至少 +N 条」而不是「+N 条」。

    **攥着没落盘的行时顺手重试一次。** 不重试的话,磁盘故障恢复之后面板会一直
    说「读不出来」,要等下一次抓取才补写(第 1 轮审查 N4)。重试走独占锁,别的
    读还是只拿共享锁。
    """
    if _unsaved:
        with _locked(True) as locked:
            _flush([], locked)
    with _locked(False) as locked:
        rounds, ok = _read()
    if not ok or not locked or _unsaved:
        return None
    day = _day(_now(now))
    mine = [r for r in rounds if r['day'] == day]
    if not mine:
        return {'n': 0, 'added': 0, 'partial': False, 'at': ''}
    last = mine[-1]
    # 「交完了」= 登记过的子轮一个不剩都交了结束行。两个集合都空时分两种:开轮
    # 就说了不物化(`ex=0`)的那一轮确定是 0 条;存量账本那几轮没有子轮行,
    # 证明不了完整,只能算不可确认。
    done = last['segs'] == last['ends'] and bool(last['segs'] or not last['ex'])
    return {'n': len(mine), 'added': len(last['paths']),
            'partial': bool(last['partial'] or not done),
            'at': last['at'][-5:]}
