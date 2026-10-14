# -*- coding: utf-8 -*-
"""两份落盘的读写原语,和「哪一轮更新」那套判据。

从 `pipeline_state` 里分出来的:那边是**运行状态**(阶段、日志、锁、收尾),这边是
**落盘与候选判据**(序列化、原子换文件、历史形状、完成版本、谁更新)。两边的判据
性质完全不同 —— 那边错一下是「这一轮的进度不对」,这边错一下是「恢复之后拿错了
一轮,面板显示上一轮的结论、重试去抓上一轮的来源」。

**这里的函数一律不碰 `_state`**,要比哪一份传哪一份进来。`pipeline_state` 那边
包了一层默认 `_state` 的 `result_version`,给「现在这一屏是哪个版本」用。

硬约束见 `docs/decisions/pipeline-history.md` 的第一节 —— 这里几个判据写错之后多半
**不可恢复**:恢复时挑错一轮,面板显示上一轮的结论、重试去抓上一轮的来源。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _load_json(path: Path):
    """读一份落盘 JSON,读不出来一律 `None`。**两份落盘(状态、历史)都过它。**

    **口径只有一份,而且兜住所有异常**,不只 `OSError` / `JSONDecodeError`:这两份
    文件是外部状态(手改过、半截写入、别的工具碰过、坏块),而
    `read_text(encoding='utf-8')` 碰上非 UTF-8 字节抛的是 `UnicodeDecodeError`、
    深嵌套 JSON 抛的是 `RecursionError` —— 两个都不属于那两类,从这里逃出去会一路
    冲到 `_end_run()`,把一轮**已经做完**的同步变成「永远进行中」(那一道不变量在
    `_end_run` 自己那里另外钉着,这里是第一道)。
    **先按字节读、再 `errors='replace'` 解码**,不直接 `read_text`:能读出来的尽量
    读出来,坏字节变成 U+FFFD 而不是整份丢掉(「漏报是静默丢数据」)。
    """
    try:
        return json.loads(path.read_bytes().decode("utf-8", "replace"))
    except Exception:
        return None


def _load_json_strict(path: Path):
    """读一份落盘 JSON。**文件不存在返回 `None`,其余读不出来一律抛。**

    恢复那一步要分得开「这个库从没跑过」和「这一次没读到」—— 两者都当成 `None`
    的话,一次瞬时读失败(权限、磁盘抖动)会被永久固化成「从没跑过」:内存里那份
    终态一直是空的,而展示那条路照旧从磁盘读得到真实的那一轮,于是起跑核对拿一个
    空版本去跟真实那一轮比,**每一次重试都报「这期间又跑完了一轮」,而其实什么都
    没跑过**(实测:第一次读状态文件注入 `PermissionError`、之后全部正常,页面照旧
    显示 503 和重试按钮,连点两次都被 409 顶回来)。
    内容坏了(解码、JSON、嵌套过深)也抛:那一档重读多半还是坏的,但**「拿不准就
    别固化终态」** —— 代价只是每次多读一次小文件。
    展示那条路照旧用宽口径的 `_load_json`(读不出来少显示几行、不出声)。
    """
    if not path.exists():
        return None
    got = json.loads(path.read_bytes().decode("utf-8", "replace"))
    if got is None:
        # **「文件里是 `null`」不是「文件不存在」。** 两者都返回 `None` 的话,上面
        # 那些「`None` = 确定没有」的判据就把一份坏文件当成了空库:恢复静默认可
        # 另一份(可能正是旧的那一轮),追加拿空集合整份替换。
        raise ValueError(f'{path.name} 存在,但内容是 null')
    return got


def _dump_bytes(obj) -> bytes:
    """序列化成要落盘的字节。**不可编码的字符就地替换,这一步不许抛。**

    `ensure_ascii=False` 是为了文件能给人读,代价是**编码**那一步会炸:某条记录里
    混进一个孤立代理字符(`\ud800` 一类 —— JSON 里合法、UTF-8 里不合法,旧文件里
    转义存着、读回来就是它),`UnicodeEncodeError` 就从写盘抛出来。按
    `errors='replace'` 落盘:坏掉的是那一个字符,其余内容和其他几轮一个字节不差。
    """
    return json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8", "replace")


def _write_atomic(path: Path, data: bytes) -> None:
    """同目录临时文件 + rename,**替换没成功一律把临时文件收掉**。

    面板那两条读的请求随时在跑,截断重写的窗口里读到的是半截 JSON(同
    `docs/decisions/llm-stages.md` 那条)。而 `replace()` 失败(权限、跨设备)时不清
    的话,`*.<pid>.tmp` 就留在数据目录里 —— 同一个进程里重名覆盖,每次重启换一个
    pid 就慢慢堆起来。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)



def _history_runs(raw) -> list[dict]:
    """把读出来的那份历史文件解析成记录列表。**解析口径只有一份** —— 恢复和追加
    那两步各有自己的严格版(下面两个),形状检查都过这里,不在那边另写一遍。

    这一份是**宽口径**,给只读展示用:形状不对就少显示几行、不出声。
    """
    runs = raw.get("runs") if isinstance(raw, dict) else None
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict)]


def _state_shape_ok(raw) -> bool:
    """状态文件的根形状:要么文件不存在(`None`),要么是个字典。

    和历史那边(下面)**一个口径** —— 发号、恢复都过它,不在各自那边另写一遍。
    各写一遍的话两处会在某一档上岔开:发号层把坏根当成「没有记录」照旧发号、恢复
    层却拒绝,而「这一份算不算有」在两处必须是同一个答案。
    """
    return raw is None or isinstance(raw, dict)


def _history_shape_ok(raw) -> bool:
    """这份读出来的历史**形状对不对**:要么文件不存在(`None`),要么是
    `{"runs": [...]}`。

    「可解析但形状不对」和「读不出来」是**同一档**,都不能当成「这份副本没有」:
    恢复那一步会就此静默认可另一份旧结果,追加那一步会拿一个空集合整份替换掉存量。
    `runs` 里个别元素不是字典不算形状坏(那一条过滤掉就是了,它只坏自己那一行)。
    """
    return raw is None or (isinstance(raw, dict) and isinstance(raw.get("runs"), list))



def result_version(run: dict) -> str:
    """一份终态的「完成版本」:**任何一份快照都有值,包括从没跑过和旧格式记录。**

    `run_id` 是后来才加的字段,所以凡是拿「有没有 run_id」当判据的地方都得另开一
    条兜底分支,而兜底写成「拿不准一律放行」就把不变量放掉了:实测里升级后重启、
    点一次「重试失败项」,期间另一轮刚好成功结束 —— 旧快照没有身份,起跑那道核对
    整条跳过,上一轮失败的那几个订阅又被抓了一遍,而失败其实发生在另一个来源上。
    所以口径降一级:身份没有就用收尾时间(旧记录有),再没有就是「从没跑过」——
    **那也是一个确定的版本,不是「未知」**。前端拿它当应用凭据,就不必把空串
    同时当成「还不知道」和「已经追平」两件事。
    """
    return str(run.get("run_id") or run.get("finished_at") or "") or "none"



# 发号世代。**只有同一个世代的号才比得**:单调性是发号那一步的性质,不是数值
# 本身的性质 —— 换一套发号逻辑就换一个世代,跨世代一律不比(退到位置证据,没有
# 就报「证明不了」)。老记录没有这个字段,所以它们的号一个都不信。
_SEQ_GEN = 2


def _seq_of(run) -> int:
    """这一轮**可信的**完成序号;不可信或者没有就是 0。

    可信的判据是**它出自当前这一代发号逻辑**(`seq_gen`),不是「是个正整数」。
    老代码在发号读失证时从 1 重新开始,那些**回退号已经落在盘上了**,修了发号也
    追不回来 —— 单看一个正整数分不出它是哪一代发的,而拿回退号当顺序凭据的后果是
    「旧那一轮被判成最新」:页头显示它的失败、点「重试失败项」去抓它那个来源。
    所以**凭据要自己带出处**,没出处的一律当没有。
    """
    if not isinstance(run, dict) or run.get("seq_gen") != _SEQ_GEN:
        return 0
    got = run.get("run_seq")
    return got if isinstance(got, int) and got > 0 else 0


def _valid_round(run) -> bool:
    """这是不是一份**能当终态用**的记录。

    候选要先验,不能只过滤非字典:历史首条是个空对象时(坏文件、半截写入),原来会
    拿它当「最新」比一轮,把后面那些仍能证明更新的记录整段遮住 —— 恢复于是退回
    状态文件那一份,而它可能是旧的。
    """
    return isinstance(run, dict) and bool(run.get("finished_at"))


# 公开名:历史端点标「这一行有效吗」时过的是**同一个判据** —— 各写一遍的话恢复拒绝
# 的那种记录会在清单上显示成一轮正常运行。
valid_round = _valid_round


def _newest_round(runs: list[dict], saved: dict | None) -> tuple[dict | None, str]:
    """两份落盘里挑出最新那一轮,并说明这一份**可信到什么程度**。

    返回 `(那一轮, 欠账)`,欠账三档:

    - `''`      两份对得上、或者顺序凭据证明了谁更新,而且挑中的那份是完整的
    - `'scope'` 挑中的是历史那一份 —— 它**没有重试派生字段**
    - `'order'` 谁更新**证明不了** —— 两样凭据(同一份历史里的位置、同一代发出来的
                完成序号)都不成立。**这一档不许用收尾时间凑**,理由在下面

    **两份独立可失败的副本加墙上时钟构不成可靠总序。** 实测形态:A 轮的历史没写
    上、紧接着 B 轮的状态没写上,磁盘上状态记着 A、历史只有 B,而两轮**落在同一
    秒**(自然时钟实测两个收尾时间都是 16:08:29)—— 位置证据不存在(A 不在历史里)、
    时间也分不开,于是默认旧那一份完整:页头显示 A 的 503,点「重试失败项」导出
    阶段收到的是 A 那个来源。
    所以每一轮落盘时带一个跨进程单调的完成序号(`run_seq`),按它比 —— 哪一份写
    失败都不影响下一轮继续递增。序号缺失(升级前的记录)时退回历史位置和时间,
    **而那两样都分不出先后时一律报 `order`,不许默认旧那一份完整**。

    **凭据只认两样:同一份历史里的位置、同一代发出来的完成序号。** 墙上时间不在
    其内 —— 它不是顺序凭据,时钟回拨(手动改、NTP 校正、夏令时)之后后跑完的那一轮
    时间更早。拿它凑一个「先后」出来,判据就反过来指向旧那一轮,**而且报「完整」**:
    页头显示旧那一轮的失败、重试可点,点下去抓的是它那个来源。

    **位置排在序号前面**:它只用同一份文件内部的先后次序,这份文件读出来了就成立;
    序号的单调性要靠发号那一步做对,而发号要读磁盘、读失证时根本发不出来
    (`_next_seq` 那时返回 0)。反过来的败法实测过:发号读失证的那一轮拿到小号落进
    历史首条,状态文件记着的上一轮号更大 —— 序号这一档当场判出「旧那一轮更新」,
    而历史里明明还留着它、位置已经证明了它是过去的一轮。
    """
    saved = saved if _valid_round(saved) else None
    runs = [r for r in runs if _valid_round(r)]
    newest = runs[0] if runs else None
    if not newest:
        return saved, ''
    if not saved:
        return newest, 'scope'
    if result_version(newest) == result_version(saved):
        return saved, ''                       # 同一轮 —— 用完整那一份
    # ① **结构性凭据**:历史是倒序的,状态文件那一轮出现在首条后面,就证明首条
    #    更新。它是同一份文件内部的位置关系,**不依赖发号这件事做对** —— 而序号是
    #    发得出来才有的,发号那一步读失证就没有(见 `_next_seq`)。
    want = result_version(saved)
    if any(result_version(r) == want for r in runs[1:]):
        return newest, 'scope'
    # ② 同一代发出来的完成序号。**不等就是先后**,不拿收尾时间去反驳它 ——
    #    时钟回拨的时候序号才是对的那一个。
    seq_new, seq_old = _seq_of(newest), _seq_of(saved)
    if seq_new and seq_old and seq_new != seq_old:
        return (newest, 'scope') if seq_new > seq_old else (saved, '')
    # ③ **两样都没有就是证明不了,不许拿墙上时间凑一个。** 这里原来比收尾时间,
    #    而那不是顺序凭据:时钟回拨(手动改、NTP 校正、夏令时)之后,后跑完的那一轮
    #    时间更早 —— 判据于是反过来指向旧那一轮,还报「完整」。实测形态:A 轮失败
    #    (状态写上了、历史没写上)、B 轮成功(历史写上了、状态没写上),两轮的收尾
    #    时间因为回拨成了 18:30:05 和 18:30:00 —— 重启之后页头显示 A 的失败、重试
    #    可点,点下去导出阶段收到的是 A 那个来源,而历史里唯一那行写着 B 已完成。
    return saved, 'order'


def row_key(run: dict, taken: set) -> str:
    """这一行的**选择键:逐行唯一**。面板拿它认「点的是哪一行」、再次定位、高亮。

    新记录用完成序号(跨进程单调,天然唯一);升级前那些没有序号的退回完成版本,
    而**两条都缺身份、收尾时间又相同时那个版本会撞** —— 撞上就加一截序数,否则
    点较早那行打开的是较新那行。
    **这跟新鲜度那个 `version` 不是一个契约**:那个要回答「最新是哪一轮」(所以
    空清单也得有值),这个要回答「点的是哪一行」(所以必须逐行不同)。两件事共用
    一个字段的话,一边的降级口径就会变成另一边的 bug。
    """
    seq = _seq_of(run)
    base = f'seq-{seq}' if seq else result_version(run)
    key, n = base, 1
    while key in taken:
        n += 1
        key = f'{base}#{n}'
    taken.add(key)
    return key
