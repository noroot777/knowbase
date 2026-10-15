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

**回执里每一条带两个身份,账本按内容身份去重、按抓取身份分档。** 稳定内容身份
(`rss:<key>`、`wechat_local:<article id>`)由采集器自己的库给,路径当不了它 ——
标题一改文件名就变,同一篇会连着几天被报成"刚抓到"。抓取身份是**发现**这一条的
那一次业务抓取,记在采集器自己的持久化记录里(`feed_items.fetch_round`、公众号那
张 `article_fetch_rounds`),所以「这一次新抓到的」和「之前抓到、这一次才补导进来
的」分得开,而不是靠时间窗口反猜。

**`added` 报的是「这次同步之后日报能多拿到多少」。** 这是仓库主人定的口径:这个数
的用处就是决定要不要再花一次模型钱重新生成日报,而补导进来的那几篇确实是日报这
一刻才拿得到的新素材。另一个口径(「远端这次新发现多少」)在补导场景下必然更小,
它不是 `added` 要回答的问题 —— 差额如实拆成 `fresh`/`carried`/`unsure` 交给面板,
不从 `added` 里悄悄减掉,也不反过来拿 `fresh` 冒充总数。

**只勾了导出的那种运行不算一次抓取**(它压根不开轮,`note_new` 找不到轮次就什么
都不做)。它落下来的那几篇不会因此失踪:每一条都带着**原本那次抓取**的身份,所以
账目上它们属于真抓到它们的那一轮,而不是属于"没有抓取的这一次"。反过来把它们追记
回那一轮也不行 —— 一是会改掉用户早就看过的数,二是那一轮多半已经滚出保留窗口,
追记只会留下一条对不上轮次的孤行,把整份账本拖成「不可确认」。

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
- 回执行存的是**内容身份集合**,折叠时取**并集**。`write` 成功而 `fsync` 报错是真
  会发生的事(那几行其实已经进了文件),重试把同一行再追加一遍 —— 存条数就会
  翻倍(实测 2 篇报成 4 篇),存身份则并集不变。同一轮被重试、被两条通道各报
  一次、被同一个进程重放,结论都一样。**而且必须是内容身份、不能是路径**:
  改名那一轮里同一篇内容以两个文件名各写一次,按路径取并集会数成两篇。
- 攥在手里没落下去的那几行不许悄悄丢。超过 `_KEEP_UNSAVED` 时丢掉的不是数据
  而是换成一条「这一轮不可确认」的标记,**投影不完整就不许解除「不可确认」**。
- 读、追加、轮换全在同一把文件锁里(`_locked`)。不然进程 A 读完旧账、进程 B
  成功追加、A 再按旧快照轮换,就把 B 已经确认过的行覆盖掉了。

**只有一种情形救不回来**:写失败之后进程就没了 —— 那几行既没落盘也不在内存里。
但这一刀落下去之后账本**不会撒谎**:开轮行上记着这次运行会不会物化(`ex`),
会物化却一条回执都没有的轮次一律算「不可确认」,而不是算确定的 0 条。

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

# 落盘失败、还没写下去的那几行。**不是缓存**:它存在期间 `today()` 一律答
# 「不可确认」,因为此刻磁盘上那份账本比事实少。
_unsaved: list[dict] = []


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

    取不到锁(目录只读、fcntl 不支持)就直接放行:账本是个展示用的东西,
    为它把抓取拦下来不划算 —— 并发竞争的后果由上面那几条(追加、并集、
    不可确认)兜着。
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
        yield
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


def _scope_prefixes() -> list[tuple[tuple[str, ...], frozenset]]:
    """算「抓到的新内容」的那些树,各自的 vault 相对前缀 + 要跳过的子目录。

    一次算好交给 `in_scope` 复用:一轮回执有上百条路径,每条都把全部来源 resolve
    一遍是上千次 syscall,而这份清单一轮之内不会变。
    """
    from kb.config import SOURCES, VAULT_ROOT

    try:
        root = Path(VAULT_ROOT).resolve()
    except OSError:
        return []
    out = []
    for src in SOURCES:
        if src.derived or not src.enabled:
            continue
        try:
            out.append((Path(src.root).resolve().relative_to(root).parts,
                        src.skip_dirs))
        except (OSError, ValueError):
            continue
    return out


def in_scope(rel: str, prefixes=None) -> bool:
    """这个路径**算不算「抓到的新内容」** —— 范围口径只有这一份。

    和日报取材认的那批树一致(`daily._scope`):非衍生、启用中、不在 `skip_dirs`
    里。回执由采集器交,但"哪些树算"是知识库自己的事,不能交给每个采集器各判
    一遍 —— 条目化衍生树(`dr-gzh-projects` 等)和原文是同一批内容,日报本来就
    排除它们,算进来的话一轮条目化就能让药丸写出三位数而日报那边一条新的都没有;
    停用的树同理不再进库。
    """
    parts = Path(rel).parts
    for prefix, skip in (_scope_prefixes() if prefixes is None else prefixes):
        if parts[:len(prefix)] != prefix:
            continue
        rest = parts[len(prefix):]
        return not (rest and rest[0] in skip)
    return False


def _pairs(obj: dict) -> list[tuple[str, str]] | None:
    """一条回执行里的 `(内容身份, 抓取身份)`。读不出来返回 None(= 记不全)。

    认两种形状:`items` 是 `[[内容身份, 抓取身份], …]`,`paths` 是旧形状的
    `[路径, …]` —— 后者的抓取身份一律空串,因为那时还没记过它。
    """
    if 'items' in obj:
        raw = obj['items']
        if not isinstance(raw, list):
            return None
        out = []
        for one in raw:
            if not isinstance(one, (list, tuple)) or not one or not str(one[0]):
                return None
            out.append((str(one[0]), str(one[1]) if len(one) > 1 else ''))
        return out
    raw = obj.get('paths')
    if not isinstance(raw, list):
        return None
    return [(f'path:{p}', '') for p in (str(x) for x in raw) if p]


def _fold(lines) -> tuple[list[dict], bool]:
    """把账本每一行折成「开过哪几轮、各自新落了哪些文章」,外加一句「读全了没有」。

    三种行:开轮(带 `day`/`at`/`ex`)、回执(带 `items`)、不可确认标记(带
    `partial`)。**回执按内容身份取并集**,所以同一行追加两遍、同一轮被两条通道
    各报一次,折出来的结论都一样(见模块开头那条)。

    一条成果出现两次而只有一次带得出抓取身份时,留**非空**那个:空串是「说不清」,
    被它顶掉已知的身份等于把知道的事实又丢回去。

    `paths` 是回执的**旧形状**(只有路径、没有身份)。照旧折得出条数,但每一条的
    抓取身份都是空串 —— 升级那一下盘上正躺着这种行,不认它的话它会被当成一条
    没有 `day` 的开轮行,凭空多出一次「第 N 次抓取」。

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
        if 'items' in obj or 'paths' in obj or 'partial' in obj:
            got = index.get(key)
            if got is None:
                ok = False              # 有成果、却找不到它那一轮:记不全
                continue
            if obj.get('partial'):
                got['partial'] = True
            if 'items' in obj or 'paths' in obj:
                pairs = _pairs(obj)
                if pairs is None:
                    ok = False
                    continue
                for cid, fid in pairs:
                    got['items'][cid] = fid or got['items'].get(cid, '')
                got['got'] = True
            continue
        if key in index:
            continue                    # 同一轮重复开:幂等,不是第二次抓取
        got = {'key': key, 'day': str(obj.get('day') or ''),
               'at': str(obj.get('at') or ''),
               # 这次运行会不会物化。读不到(存量账本行)按会物化算 —— 那一侧
               # 的结论是「不可确认」,比笃定地说 0 条安全。
               'ex': bool(obj.get('ex', 1)), 'items': {}, 'got': False,
               'partial': False}
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


def _flush(objs: list[dict]) -> bool:
    """把这几行连同之前没落下去的一起追加。

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
    if _append(batch):
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


def _rotate(rounds: list[dict], ok: bool, at: datetime) -> None:
    """把早于保留窗口的轮次清出账本(整份重写)。

    **读不全的时候一个字都不动**:那时我们并不知道文件里还有什么,拿认得出来的
    那部分重写回去,等于把读不出来的那几行悄悄删掉。轮换失败也不出声 ——
    最坏只是文件长一点,结论不受影响。

    调用方必须已经持有独占锁,而且那把锁要罩住它前面那次 `_read`:否则另一个
    进程在这中间追加的行会被这份旧快照覆盖掉。
    """
    if not ok:
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
            lines.append({'key': r['key'],
                          'items': [[c, f] for c, f in sorted(r['items'].items())]})
        if r['partial']:
            lines.append({'key': r['key'], 'partial': 1})
    try:
        _write_atomic(_file(), ''.join(
            json.dumps(o, ensure_ascii=False) + '\n' for o in lines).encode('utf-8'))
    except OSError:
        pass


def round_key() -> str:
    """此刻属于哪一次**业务抓取** —— 当日账本的键,也是回执里的抓取身份。

    有批次身份就用它:一轮日报、一次勾了三个来源的同步都是 N 次管道运行、
    **一次**抓取。没有批次的独立运行(单来源同步、重试失败项)用自己的 run id。
    两种身份都跨进程唯一,理由分别在 `pipeline_batch.open_batch` 和 `new_run_id`。

    **口径只有一份,放在这里。** 读它的不只有管道那两个阶段:采集器往自己的库里
    记「这一条是哪一次抓到的」时问的是同一个问题(`feed_items.fetch_round`),
    两处各写一份迟早漂 —— 而漂的后果是药丸把本次新抓到的算成补导的,反过来也会。

    管道没在跑时返回空串(CLI 导出、面板上手点的某个维护动作)。空串是「说不清」,
    照旧落进回执的 `unsure` 那一档,不冒充任何一次抓取。

    **「在跑」要由 `running` 证明,不能只看 `batch`/`run_id` 还在不在。** 那两个
    字段是收尾时**故意留下**的(晚一步来取结果的人还得按 id 认领自己那一轮,见
    `pipeline_state._end_run`),所以空闲时它们躺着的是上一轮的身份 —— 管道外直接
    入库的那一笔会被盖上一个跟它毫无关系的批次号,而 `put_items` 的 `DO NOTHING`
    和 `article_fetch_rounds` 的 `INSERT OR IGNORE` 都是**首次说了算**:盖错了
    救不回来,药丸从此把它算成「这一次新抓到的」。拿不准就留「说不清」。

    置位是安全的那一边:`running` 和 `batch` 在同一把锁里一起写下、工作线程才启动
    (`pipeline.start_pipeline`、`pipeline_retry`),收尾只在 `_end_run` 里放下它,
    所以它严格包着全部阶段,不会出现「阶段在跑而 running 是 False」。
    """
    from kb.pipeline_state import _state

    if not _state.get('running'):
        return ''
    return str(_state.get('batch') or _state.get('run_id') or '')


def open_round(key: str, now: datetime | None = None,
               materializes: bool = True) -> None:
    """抓取阶段开始时报到:今天又开了一次抓取(同一个 `key` 重复调是空操作)。

    多来源同步时每个来源各调一次,但它们带着同一个批次身份,所以只开一轮 ——
    这正是「不许把多来源算成多次抓取」那条落地的地方。

    `materializes`:这次运行后面还跑不跑物化阶段。**只勾了抓取的那种运行确实
    一篇都落不进 vault,它的 0 条是确定的**;而会物化却一条回执都没等到的轮次
    只能说「不可确认」—— 两者在账本上长得一模一样(都是一条开轮行、没有回执),
    所以这个事实要在开轮的时候就写下来,不能事后猜。
    """
    if not key:
        return
    at = _now(now)
    with _locked(True):
        rounds, ok = _read()
        if ok and any(r['key'] == str(key) for r in rounds):
            _flush([])                  # 顺手把攥着的那几行再试一次
            return
        _rotate(rounds, ok, at)
        _flush([{'day': _day(at), 'key': str(key),
                 'at': at.strftime('%Y-%m-%d %H:%M'),
                 'ex': 1 if materializes else 0}])


def note_new(key: str, items) -> None:
    """这一轮抓取往 vault 新落了哪几篇 —— 参数是**生产者交回来的那份清单**。

    参数是一串 `kb.collectors.Fetched`(内容身份 + 抓取身份 + 这次写的路径),
    **不是一个数、也不只是路径**:

    - 不收数,调用方就没法把别的阶段算出来的产出当成抓取成果递进来(前两版正是
      这么错的,先拿日报取材结果、再拿全库首次索引数冒充它)。
    - 收**内容身份**而不是路径,是因为落盘之后折叠按它取并集,**任何重放都幂等**
      —— `write` 成功、`fsync` 报错的那一刀之后重试一遍,2 篇还是 2 篇;而路径
      随标题变,同一篇改过名就会被当成两条。
    - 收**抓取身份**,药丸背后那句分解才答得出来:这几条里哪些是这一次新抓到的、
      哪些是之前抓到、这一次才补导进来的(口径见 docs/decisions/daily.md)。

    空清单要照样记:它是「这一轮确实一篇都没新落」的那条**确定**的回执。不记
    的话,它和「物化中途炸了、回执丢了」在账本上长得一样。

    回执里不在取材范围内的按 `path` 筛掉(`in_scope`)。**范围按路径判、去重按
    内容身份判**,两件事别混:前者问「这棵树算不算」,后者问「是不是同一篇」。

    按 `key` 找轮次、**不按「今天」过滤**:23:59 开的轮次 00:01 才物化完,按今天找
    会找不到它,那一轮永远停在 0 条。读不全时什么都不记(状态本来就是「不可确认」,
    往一份读不懂的账本里追加成果只会多一条对不上轮次的孤行)。

    **清单里有读不懂的行,这一轮就报下界,不许静默少记。** 读不出形状、或者答不
    出路径(范围按路径判,答不出就判不了)的那几条照旧不计数 —— 但同时记一笔
    `partial`,面板据此说「至少 +N 条」。只跳过不记标记的后果和挑着收一份少记的
    清单一样:用户照着一个偏小的、**看着确定**的数决定不重新生成日报。
    """
    if not key:
        return
    with _locked(True):
        rounds, ok = _read()
        if not ok:
            return
        if not _is_open(str(key), rounds):
            _flush([])                  # 没开过这一轮:它不是一次抓取
            return
        scope = _scope_prefixes()
        keep: dict[str, str] = {}
        torn = False
        for one in items:
            cid, fid, path = _identity(one)
            if not cid or not path:
                torn = True             # 读不懂 / 答不出路径:这一轮只是下界
                continue
            if not in_scope(path, scope):
                continue                # 不在取材范围内:这是正常的筛掉,不是缺口
            keep[cid] = fid or keep.get(cid, '')
        lines: list[dict] = [{'key': str(key),
                              'items': [[c, f] for c, f in sorted(keep.items())]}]
        if torn:
            lines.append({'key': str(key), 'partial': 1})
        _flush(lines)


def _identity(one) -> tuple[str, str, str]:
    """一条回执成果的 `(内容身份, 抓取身份, 路径)`。

    只认 `kb.collectors.Fetched` 那个三元形状。**裸路径也照收**(身份退成路径
    自己、抓取身份说不清):一个还按旧契约返回路径集合的采集器(第三方的、
    没跟着升的)交上来的清单,拒掉就是把真落进库的那几篇静默少记成确定值,
    而收下来只损失那句分解 —— 条数照旧确定,`unsure` 那一档如实说「说不清是
    哪一次抓到的」。

    形状读不出来时返回三个空串,**由调用方记成这一轮的缺口**(`note_new` 的
    `torn`),不在这里悄悄吞掉。
    """
    if isinstance(one, str):
        return f'path:{one}', '', one
    try:
        cid, fid, path = one
    except (TypeError, ValueError):
        return '', '', ''
    # **字段不是字符串就算读不懂,不许 `str()` 一下糊过去。** `str(None)` 是
    # `'None'`:那是个**非空**的抓取身份,会被当成"确定不是这一轮抓的"归进
    # `carried`;而 `'None'` 这个路径不在取材范围里,会被范围过滤当成正常筛掉 ——
    # 一个缺口于是伪装成确定的零条。两样都是把"不知道"写成一个确定答案。
    if not all(isinstance(x, str) for x in (cid, fid, path)):
        return '', '', ''
    return cid, fid, path


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
    with _locked(True):
        rounds, ok = _read()
        if not ok:
            return
        if not _is_open(str(key), rounds):
            _flush([])
            return
        _flush([{'key': str(key), 'partial': 1}])


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

    `{'n': 今天第几次, 'added': 那一次新落了多少篇, 'fresh': 其中这一次新抓到的,
    'carried': 其中之前抓到、这一次才补导进来的, 'unsure': 其中说不清是哪一次抓到的,
    'partial': 这个数全不全, 'at': 'HH:MM'}`;`n=0` 是「确定今天还没有抓取过」。
    **整份读不出来、或者有行没落下去时返回 `None`(不可确认)**,由 API 原样交给
    前端。

    **`added` 报的是「这次同步之后日报能多拿到多少」,不是「远端这次新发现多少」**
    —— 仓库主人要的是前者(这个数的用处就是决定要不要再花一次模型钱重新生成
    日报),而补导进来的那几篇确实是日报这一刻才拿得到的新素材。两个数在补导场景
    下必然不等,所以把差额如实拆在 `fresh`/`carried`/`unsure` 里,而不是从 `added`
    里悄悄减掉。口径的来龙去脉在 docs/decisions/daily.md。

    `unsure` 是第三档,不许往 `carried` 里并:抓取身份是空串时我们只知道「没记过」,
    不知道「不是这一次」(存量内容、手工导入的链接、采集器还没来得及记都长这样)。
    并进去就是把"不知道"写成了"确定不是"。

    `partial=True` 的两种来源:某一段成果采集器说不出来(`note_unknown`),或者
    这一轮会物化却一条回执都没等到(进程在写账本之前就没了)。那时 `added` 是
    **下界**,前端据此说「至少 +N 条」而不是「+N 条」。
    """
    with _locked(False):
        rounds, ok = _read()
    if not ok or _unsaved:
        return None
    day = _day(_now(now))
    mine = [r for r in rounds if r['day'] == day]
    if not mine:
        return {'n': 0, 'added': 0, 'fresh': 0, 'carried': 0, 'unsure': 0,
                'partial': False, 'at': ''}
    last = mine[-1]
    owners = list(last['items'].values())
    fresh = sum(1 for f in owners if f == last['key'])
    unsure = sum(1 for f in owners if not f)
    return {'n': len(mine), 'added': len(owners), 'fresh': fresh,
            'carried': len(owners) - fresh - unsure, 'unsure': unsure,
            'partial': bool(last['partial'] or (last['ex'] and not last['got'])),
            'at': last['at'][-5:]}
