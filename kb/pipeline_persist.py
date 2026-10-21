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
import uuid
from copy import deepcopy
from pathlib import Path


def _loads_json(raw):
    """解析一段 JSON 文本,**读不出来一律 `None`,而且兜住所有异常**。

    **展示用记录的反序列化只有这一个口径**(落盘那一侧是 `_load_json`,逐行那一侧
    是 `fetch_fold._fold`,旁挂凭证那一侧是 `fetch_pending._load`)。按异常类型逐处
    各列一份的后果是实测的:那几处原来写的是 `except (OSError, UnicodeDecodeError,
    ValueError)`,而**深嵌套 JSON 抛的是 `RecursionError`**(`RuntimeError` 的子类,
    一个都不在那张单子上)—— 它从读取那一步逃出来,一路冲到**抓取阶段**的
    `open_round`,于是一份手改坏的计数记录能把整次同步拦在采集器之前,生产者一次
    `fetch` 都没跑(第 4 轮审查实测两处同形)。**纯展示层不许阻断生产者**,所以这里
    只有一条:坏记录读不出来就是「说不清」,交给读侧降级,不往调用方抛。

    `except Exception` 而不是 `except BaseException`:`KeyboardInterrupt` /
    `SystemExit` 不是「这份记录坏了」,吞掉它们等于让 Ctrl-C 失灵。
    """
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 - 任何解析坏法都只是「这一份读不出来」
        return None


def _load_json(path: Path):
    """读一份落盘 JSON,读不出来一律 `None`。**两份落盘(状态、历史)都过它。**

    **口径只有一份,而且兜住所有异常**,不只 `OSError` / `JSONDecodeError`:这两份
    文件是外部状态(手改过、半截写入、别的工具碰过、坏块),而
    `read_text(encoding='utf-8')` 碰上非 UTF-8 字节抛的是 `UnicodeDecodeError`、
    深嵌套 JSON 抛的是 `RecursionError` —— 两个都不属于那两类,从这里逃出去会一路
    冲到 `_end_run()`,把一轮**已经做完**的同步变成「永远进行中」(那一道不变量在
    `_end_run` 自己那里另外钉着,这里是第一道)。解析那一半在 `_loads_json`
    (展示用记录的反序列化共用它,别在调用方那边再按异常类型列一份)。
    **先按字节读、再 `errors='replace'` 解码**,不直接 `read_text`:能读出来的尽量
    读出来,坏字节变成 U+FFFD 而不是整份丢掉(「漏报是静默丢数据」)。
    """
    try:
        raw = path.read_bytes()
    except Exception:  # noqa: BLE001 - 读不出来和解析不出来是同一档:说不清
        return None
    return _loads_json(raw.decode("utf-8", "replace"))


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

    **判「不存在」只认 `FileNotFoundError`,不许用 `Path.exists()`** —— 后者吞掉
    `OSError`(父目录权限不足、路径里有一节不是目录、底层 I/O 错误)返回 False,于是
    「看不见」和「没有」长成一个样,而这两件事在这里导出的结论完全相反:一个是
    「这个库从没跑过」,另一个是「这一刻说不清」。**三份落盘(状态、历史、开工标记)
    都过这一个函数**,「不知道不能算没有」在三处必须是同一句话。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    got = json.loads(raw.decode("utf-8", "replace"))
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
    的话,`*.tmp` 就留在数据目录里慢慢堆起来。

    **临时文件名每次都是新的,不能按 pid 固定。** 同一个进程里两条线程同时写同一
    个 path 时(kb-web 是线程池,两个 POST 并行完全正常),固定名字的那一份会被后
    进来的那条截断重写 —— 先 `replace` 的那个搬走的是**半截**内容,而后一条的
    `finally` 再 `unlink` 掉已经不是自己那份的文件。换成随机名之后两条各写各的,
    两次 `replace` 谁后到谁赢,落盘的始终是某一次的完整内容。
    **这只保证单次写入的完整性,不保证并发更新不丢。** 「读出来 → 改 → 整份写
    回」那种要调用方自己拿互斥(见 `pipeline_failures.remember`)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
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

    **它是「展示版本」,不是事件身份:它保证非空,不保证一轮唯一。** 两条都没有身份
    而收尾时间又相同的旧记录,版本就会撞 —— 拿它证明「这两份副本是同一轮」的后果是
    旧那一轮被当成最新、还报「完整」。要证明同一轮用 `same_round`。
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


def event_id(run) -> str:
    """这一轮的**事件身份:要么能证明唯一,要么就是空串。**

    和 `result_version`(展示版本)**不是一个契约**:那边保证非空,这边保证唯一 ——
    两件事共用一个字段的话,一边的兜底口径就成了另一边的授权。

    唯一的判据还是**凭据自己带出处**(`seq_gen`,同 `_seq_of`):当前这一代 writer
    写的记录,它的 `run_id` 一定出自 `new_run_id()`,带着跨进程唯一的那一截。旧格式
    那些 `run-1` / `run-2` 只有进程内自增、**重启之后会重复**,所以一个都不信。
    """
    if not isinstance(run, dict) or run.get("seq_gen") != _SEQ_GEN:
        return ''
    return str(run.get("run_id") or '')


# 同一轮写出来的两份副本,在这些字段上**逐字相同**:它们都从同一份 `_state` 取。
# 不含身份/序号/世代(旧格式缺这些,而那恰恰是要兼容的那一档),也不含
# `export_filter` / `crawl_retry`(历史那一份本来就没有)、不含 `scopes`
# (反过来:那一格只有历史那一份有,它是补那两份缺掉的输入所能回答的那个问题)。
_ROUND_FIELDS = ("started_at", "finished_at", "run_kind", "stopped",
                 "planned_stages", "stages", "crawl_filter", "log")


def same_round(a, b) -> bool:
    """这两份记录**看起来是不是同一轮** —— 用来认副本,**不用来授权**。

    和 `same_event` 的分工:要据此放行重试的地方(恢复那条捷径)过那一个,只认可信
    身份;这一个多出「内容逐字相同」那一级,够用来**找一份副本**,不够用来证明事件
    唯一。所以它的消费者只剩两处,两处都不授权:
    · **位置锚点**(`_pick_newest`):认错的后果是判出「首条更新」—— 展示较新那一份、
      欠账记 `scope`、重试被拦,方向安全。
    · **锁内起跑核对**(`pipeline_retry`):它问的是「这期间状态变没变」,两边同源时
      必须为真,所以这里要的恰恰是内容那一级;而真又跑完一轮的话,那一轮带着当前
      世代的身份,下面第 1 级当场判出不等。

    两级证明,都不依赖会碰撞的展示版本。**两级之间是互斥的,不是「先试一个再试
    另一个」** —— 高一级的凭据在手时低一级一步都不许走:

    1. **可证明唯一的事件身份**(`event_id`)。任一边拿得出它,就**只看它相等不相等**。
    2. **两份副本的内容逐字相同** —— 只在两边都拿不出身份时(旧格式)才谈。同一轮的
       两份是从同一份 `_state` 写出去的,所以这几个字段一字不差;不同轮的两份在起跑
       时刻、阶段结果和日志上几乎必然不同。

    **让第 2 级盖过第 1 级是漏报**,形态见下面那段注释。反过来,第 2 级自己也只配
    用在上面那两处 —— 旧格式下它证明不了事件唯一(见 `same_event`),所以升级前那最后
    一轮(两份都写成功、确实是同一轮)在恢复那边落到「先后证明不了」:展示仍是它,
    只是重试要先跑一次完整同步。那是误报,顶多多跑一轮。

    少了「不许拿展示版本当证明」这一道同样是漏报:实测两条旧记录都没有身份、收尾
    时间都是 19:30:00,版本于是相等、被当成同一轮,恢复返回状态文件那一份(A 轮的
    翻译失败)并报空欠账 —— 页头显示 A 的失败、重试可点,点下去导出阶段收到的是 A
    那个来源,而历史里唯一那行写着 B 已经成功。
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    ia, ib = event_id(a), event_id(b)
    if ia or ib:
        # **只要有一边拿得出可信身份,就只看身份** —— 内容兜底在这里一步都不许走。
        # 身份不等是**最强的「不是同一轮」证明**,而内容兜底比的那几个字段里没有
        # `export_filter` / `crawl_retry`(重试范围和失败名单),所以两轮「看起来
        # 一模一样」而该抓的来源完全不同:实测两次快速重试落在同一秒、阶段和日志
        # 逐字相同,身份分别是 `...-1` / `...-2`,内容兜底把它们判成同一轮 ——
        # 起跑核对于是放行,导出阶段收到的是上一轮那个订阅。
        # **一边有出处一边没有也是 False**:同一轮的两份副本出自同一次 `_end_run`,
        # 身份和世代必然一致 —— 一新一旧说明它们压根不是同一轮。
        return ia == ib
    return all(a.get(k) == b.get(k) for k in _ROUND_FIELDS)


def _event_round(run) -> bool:
    """这条记录**算不算一次发生过的运行**。判据只有「是字典 + 有收尾时间」。

    **和「渲染得了没有」是两个契约,不许合成一个。** 一轮跑完过这件事由它的身份、
    完成序号和在历史里的位置记着;正文(日志、阶段明细)坏掉只说明**这一份读不出来**,
    不说明那一轮没发生。把两者合成一个判据,后果是**顺序凭据连同正文一起被丢掉**:
    最新那一轮的日志落成字符串时整条记录被过滤,剩下的旧那一份于是成了「最新而且
    完整」—— 实测 B 轮(成功、范围是 B订阅)只有 `log` 坏了,身份、当前世代、递增的
    `run_seq` 全在,恢复却返回 A 轮(翻译失败、范围是 A订阅)并报**空欠账**:页头显示
    A 的 503、按钮可点,点下去导出阶段收到的是 `accounts: ['A订阅']`。
    **可显示性只能决定怎么呈现一条记录,不能决定这次运行有没有发生。**

    候选仍要先验,不能只过滤非字典:历史首条是个空对象时(坏文件、半截写入),原来会
    拿它当「最新」比一轮,把后面那些仍能证明更新的记录整段遮住。
    """
    if not isinstance(run, dict):
        return False
    # **拿得出事件元数据就够了,时间字段坏不坏都不改变这一点。** 可信身份和同代
    # 完成序号都是**这一轮跑过**的直接凭据(出自 `new_run_id()` / `_next_seq()`);
    # 时间是**显示字段**,拿它的类型当「发生过没有」的判据,等于让一处渲染问题去
    # 否决一份完整的事件证据:实测 B 轮的 `started_at` 被改成数字,身份、当前世代、
    # 完成序号 2 和成功阶段全在,整条却被过滤,恢复返回 A 轮并报空欠账 —— 页面上
    # 重试又抓了 A订阅。下一次换个显示字段还是同一出戏,所以判据从根上改掉:
    # **事件元数据优先,时间只是旧格式的兜底迹象。**
    if event_id(run) or _seq_of(run):
        return True
    # 旧格式(升级前那些记录)没有任何事件元数据,只剩「有收尾时间」这一个迹象 ——
    # 那也是「一轮跑完了」的最低限度:没收尾的不算一轮完成的运行。
    got = run.get("finished_at")
    return isinstance(got, str) and bool(got)


def _filter_ok(filt) -> bool:
    """抓取/导出范围那个字典的消费契约 —— **两处过同一份口径**。

    前端 `pipe_channels.js` 拿 `filter.channels.map` 渲染,后端
    `pipeline_stages.py` 还要按每个 scope 取 `accounts` 再迭代:`accounts` 落成
    字符串时那一迭代会把它**拆成一个个字符**当订阅名 —— 不抛,而是静默按一串不存在
    的名字去抓,等于一条都不抓。

    **验到叶子,不只验容器。** 容器对而元素坏的那几档后果一样静默、甚至更糟:
    · `channel` 落成字典 —— 重试派生时 `retry_notes` 那边拿它当**字典键**去查失败
      名单(`notes.get(scope["channel"])`),`TypeError: unhashable type` 从保护块
      外抛出去,整次重试变成一个说不出原因的 500。
    · `accounts` 的元素落成字典 —— 计划原样把它带进抓取阶段,而那边是跟字符串的
      订阅 id / 名字做成员筛选,一个都不匹配:**面板报「已跳过」,一条也没抓**。
    · 所以这两级一起验;验不过就是 `broken`(那一轮**照旧算发生过**,只是渲染不了、
      不许据它派生范围)。
    """
    if filt is None:
        return True
    if not isinstance(filt, dict):
        return False
    scopes = filt.get("channels")
    if not (scopes is None or isinstance(scopes, list)):
        return False
    for one in scopes or []:
        if not isinstance(one, dict):
            return False
        if not _names_ok(one.get("accounts")) or not _channel_ok(one.get("channel")):
            return False
    return _names_ok(filt.get("accounts")) and _channel_ok(filt.get("channel"))


def _channel_ok(name) -> bool:
    """来源名:缺省或字符串。**它是要当字典键用的**(见 `_filter_ok`)。"""
    return name is None or isinstance(name, str)


def _names_ok(names) -> bool:
    """一份订阅名单:缺省,或者一个**元素全是字符串**的列表。

    判据抽出来是因为它有两个落点(每个 scope 自己一份、范围根上还有一份),而
    「元素是什么」这一级原来两处都没验。
    """
    if names is None:
        return True
    return isinstance(names, list) and all(isinstance(x, str) for x in names)


def _valid_round(run) -> bool:
    """这是不是一份**渲染得了、而且说的是实话**的记录。

    **它只决定呈现,不决定「这一轮发生过没有」**(那是 `_event_round`)。两者分开之后,
    最新那一轮渲染不了时的正确行为是:**承认它最新、如实说留档坏了、拦住按旧范围
    重试**,而不是把它当不存在。
    """
    if not _event_round(run):
        return False
    # **判据是整份消费契约** —— 少验一个就是一处整屏抛。时间字段的**类型**也在这边
    # 验:它们一路走到页面上按字符切(`pipe_view.js` 的 `replace` / `slice`),落成
    # 数字时那一下当场抛。它是渲染契约,不是「这一轮发生过没有」的凭据(`_event_round`)。
    for key in ("finished_at", "started_at"):
        got = run.get(key)
        if not (got is None or isinstance(got, str)):
            return False
    if not run.get("finished_at"):
        return False
    # · `log` 落成字符串:恢复进内存之后日志那一屏 `lines.join('\n')` 当场抛
    #   (`pipe_view.js` 的 `renderPipeLog`),而那个异常冒到状态渲染那层,整屏变成
    #   「状态暂时无法更新」—— 不是只跳过这一处。
    # · `stages` 里混进 `null`:`pipeRunLabel` 的 `st.status` 没有可选链,同样当场抛。
    # · `stages` 是空的:一轮运行的定义就是「跑过阶段」,一个阶段都没有的记录对人
    #   没有任何信息,而 `pipeRunSummary` 数不出红也数不出跳过,会把它汇总成
    #   **「已完成」** —— 那是在说假话。
    # 历史行渲染那道 try/catch 只保护它自己那一行,**保护不了它被打开之后的整屏**,
    # 所以判据必须待在读取边界这一处(恢复挑最新、端点标 `invalid` 都过这里)。
    stages = run.get("stages")
    if not isinstance(stages, dict) or not stages:
        return False
    for stage in stages.values():
        if not isinstance(stage, dict):
            return False
        # **嵌套一层也要验**:外层形状对、里面那个失败名单落成字符串时,
        # `pipe_view.js` 的 `failures.every` / `.map` 当场抛 —— 实测打开同步中心就
        # 一直停在「正在读取同步状态」、下面一句「状态暂时无法更新:
        # failures.every is not a function」,整屏不可用。
        got = stage.get("failures")
        if not (got is None or isinstance(got, list)):
            return False
        # **元素也要验到被取用的那一层**:前端逐条读 `f.pending` / `f.title` /
        # `f.reason`,名单里混进一个 `null` 就在 `failures.every` 那一下抛 ——
        # 实测页面一直停在「正在读取同步状态」,下面一句
        # 「Cannot read properties of null (reading 'pending')」,整屏不可用。
        if any(not isinstance(one, dict) for one in got or []):
            return False
    if not isinstance(run.get("log"), list):
        return False
    planned = run.get("planned_stages")
    if not (planned is None or isinstance(planned, list)):
        return False
    # 抓取范围和导出范围**同一份口径**(`_filter_ok`):`export_filter` 原来压根没验,
    # 而重试那边正是从它派生导出范围的(`pipeline_retry` 的 `export_filter`)。
    for key in ("crawl_filter", "export_filter"):
        if not _filter_ok(run.get(key)):
            return False
    # 来源清单**验到元素**:历史那行和主视图逐条读 `s.channel`,混进一个 `null`
    # 就在那一下抛(口径同上面 `failures` 那一段)。老记录没有这一格,照旧有效。
    told = run.get("scopes")
    if told is not None:
        if not isinstance(told, list):
            return False
        for one in told:
            if not isinstance(one, dict):
                return False
            # **逐字段验,和来源范围过同两道**(`_filter_ok` 用的就是它们):
            # `accounts` 落成一个数字时只验到"是个字典"照旧过门,而消费方把它当
            # 可迭代的摊开(`pipeline_stages.run_scopes`),那一下抛 TypeError;
            # 前端那份同口径的则显示成 undefined 条。恢复现在还会把这一格带上
            # 展示副本(`pipeline_state._with_restored_scopes`),它传得比以前远。
            if not (_names_ok(one.get("accounts")) and _channel_ok(one.get("channel"))
                    and _channel_ok(one.get("retry"))):
                return False
    notes = run.get("crawl_retry")
    if not (notes is None or isinstance(notes, dict)):
        return False
    if notes is None:
        return True
    # 失败名单那本账的形状是 `{"run": 身份, "channels": {来源: {...}}}`
    # (`note_retry_targets` 写、`retry_notes` 读)—— **被按字典取用的是里面那层
    # `channels`,不是外层**。外层的 `"run"` 是个字符串,拿「每个值都得是字典」去验
    # 外层会把每一份真实的重试终态都判成渲染不了:恢复于是报 `broken`、那颗按钮
    # 再也按不下去,而那一轮一切正常。
    chans = notes.get("channels")
    if not (chans is None or isinstance(chans, dict)):
        return False
    for one in (chans or {}).values():
        # 每本账里是 `{"targets": [...], "at": 时刻}`:名单要可按列表取(重试那边
        # 拿它收窄范围),时刻要能和采集器自己那套时间字符串比。
        if not isinstance(one, dict):
            return False
        # 名单**验到元素**:重试那边把它直接当 `--targets` 的值传给采集器子进程,
        # 那一步会把每个元素字符串化 —— 混进字典就变成一串 `{'id': 3}` 当订阅名,
        # 不抛,静默把真正该重抓的那几条摘空。
        if not _names_ok(one.get("targets")):
            return False
        if not (one.get("at") is None or isinstance(one.get("at"), str)):
            return False
    return True


# 公开名:历史端点标「这一行有效吗」时过的是**同一个判据** —— 各写一遍的话恢复拒绝
# 的那种记录会在清单上显示成一轮正常运行。
valid_round = _valid_round


def same_event(a, b) -> bool:
    """**能证明是同一次运行事件吗** —— 只认可信身份,一个字的内容都不比。

    和 `same_round` 的分工:那一个还有「两份副本内容逐字相同」那一级,**够用来认一份
    副本、不够用来授权**。旧格式(没有可信身份)下内容相等证明不了事件唯一:
    `_ROUND_FIELDS` 里没有 `export_filter` / `crawl_retry`,因为给人翻的那份历史副本
    本来就不写这两个字段 —— 不是它们无关紧要,恰恰是重试范围和失败名单。实测两次
    不同的重试落在同一秒、步骤和日志相同、各自的导出范围是 A订阅 / B订阅:内容相等
    把它们判成同一轮,于是「状态文件那份完整」这个结论成立、欠账报空,重试端点返回
    200、线程收到 A订阅。
    **所以凡是「要据此放行重试」的地方过这一个**(恢复那条捷径);只用来找副本、
    不授权什么的地方(位置锚点)仍过 `same_round`。
    """
    ia = event_id(a) if isinstance(a, dict) else ''
    return bool(ia) and ia == (event_id(b) if isinstance(b, dict) else '')


def retry_notes(run: dict) -> dict:
    """那一轮各来源留下的失败名单:{channel: {"targets": [...], "at": ts}}。


    `run` 是 `load_last_run()` 或 `get_state()` 给的那份快照。身份对不上一律
    当成"这一轮没有名单"(宁可按原范围重跑,也不拿上一轮的失败去收窄)。
    """
    note = run.get("crawl_retry") or {}
    if note.get("run") != (run.get("run_id") or ""):
        return {}
    return note.get("channels") or {}

def run_record(state: dict) -> dict:
    """这一轮的终态,给人翻的那一份。

    字段取「面板渲染一轮运行要哪些」那一套(`pipe_view.js` 的 `renderPipeState`),
    不含 `export_filter` / `crawl_retry` —— 那两个是重试派生用的,只有最新那一轮
    的才有意义;整轮作用在哪几个来源上改为存**结论**(`scopes`,见下面那段)。
    """
    # 延迟 import:`pipeline_stages` 经 `pipeline_state` 回指本模块
    from kb.pipeline_stages import run_scopes as _run_scopes
    return {
        "run_id": state.get("run_id", ""),
        "run_seq": state.get("run_seq", 0),
        # 发号世代:**凭据自己带出处**,老记录那些号一个都不信(`_seq_of`)
        "seq_gen": _SEQ_GEN,
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "stopped": bool(state.get("stopped", False)),
        "run_kind": state.get("run_kind", ""),
        "planned_stages": state.get("planned_stages"),
        "stages": deepcopy(state["stages"]),
        "crawl_filter": deepcopy(state["crawl_filter"]),
        # **这一轮作用在哪几个来源上,存的是「答案」而不是「输入」**(`run_scopes`
        # 那一份口径)。这一份**故意不存 `export_filter`**(见上面那句),可「只补
        # 导出」那一轮的来源恰恰只写在导出范围里 —— 于是光凭这份记录答不出来,
        # 历史那行写「未记录来源」、翻开它主视图写「选择来源后开始一次同步」
        # (第 1 轮自由审查 R1 实页实测)。**不许改为把 `export_filter` 存进来**:
        # 「这一份没有重试派生字段」正是 `scope` 那档欠账的判据,存进来就等于让
        # 重试从一份只够展示的记录里派生范围(那条路错一个字就是静默不抓)。
        # 存结论没有这个风险:重试那条路一个字都不读它。
        # **空通道那一格不往这儿存**:它的含义是「不挂在任何具名来源上」(整库
        # 同步),而这一格是给人看的来源清单 —— 存着它,那一行会显示成一个空名字
        # (口径同 `pipeline_daybook` 的 `_WHOLE`、`pipeline_merge._merge_scopes`)。
        # **恢复不把它搬进 `_state`**:那边有两份 filter、自己派生得出来,而常驻
        # 内存里多一格就要在每条起跑路径上各清一次(`batch` 那一格的教训)。
        "scopes": [s for s in _run_scopes(state) if s["channel"]],
        # **结果账要带上**(`results`):历史是恢复的另一个入口(状态文件写失败那一
        # 档就靠它),而这份账回答的是「这一轮里哪个来源这一步已经成了」。从这里漏
        # 掉的话,从历史恢复回来的那一轮又退回「整步那张合并卡片」,成功的来源跟着
        # 一起被算成欠着 —— 口径只有一份:已有的来源结论不因存在哪份落盘里、从哪个
        # 入口恢复而丢失(第 4 轮审查 R2)。它跟 `export_filter` / `crawl_retry`
        # 不同,那两份是「只有最新那一轮才有意义」的派生范围,这一份是**事实**。
        "results": deepcopy(state.get("results") or {}),
        "log": list(state["log"]),
    }


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
    失败都不影响下一轮继续递增。**序号缺失(升级前的记录)时只剩位置证据**,它也不
    成立就一律报 `order`,不许默认旧那一份完整。

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
    # **先按「发生过没有」过滤,不按「渲染得了没有」** —— 理由在 `_event_round`。
    saved = saved if _event_round(saved) else None
    runs = [r for r in runs if _event_round(r)]
    pick, gap = _pick_newest(runs, saved)
    if pick is None or _valid_round(pick):
        return pick, gap
    # 挑中的那一轮**确实是最新的,可它的留档渲染不了**。这一档既不能拿它去渲染
    # (整屏会抛),也不能退回更早那一份说「最新而且完整」——**承认它最新、如实说
    # 坏了、拦住按旧范围重试**才是实话。展示退回到读得出来的那一份(历史倒序,
    # 所以越靠前越新;都读不出来就什么也不显示),欠账记 `broken`。
    shown = next((r for r in runs if _valid_round(r)), None)
    if shown is None and _valid_round(saved):
        shown = saved
    # **退回到的那一份要是最新这同一次事件的另一个副本,那就不是「更早一轮」。**
    # 典型形态:状态文件那份的正文坏了,而历史里那份同一轮的副本正常 —— 屏幕上显示
    # 的就是最新那一轮自己,只是少了重试派生字段。那恰恰是 `scope` 的处境(「展示是
    # 对的,但没有导出范围和失败名单」),说成「下面显示的是更早一轮」是假话。
    if same_event(shown, pick):
        return shown, 'scope'
    return shown, 'broken'


def _pick_newest(runs: list[dict], saved: dict | None) -> tuple[dict | None, str]:
    """挑最新那一轮:**只看顺序凭据**,不问正文读不读得出来(那是调用方的事)。"""
    newest = runs[0] if runs else None
    if not newest:
        return saved, ''
    if not saved:
        return newest, 'scope'
    # **「同一轮」要能证明,而且证明得够强到可以据此放行重试** —— 所以这里过的是
    # `same_event`(只认可信身份),不是 `same_round`。旧格式下「内容逐字相同」认得出
    # 一份副本,却证明不了事件唯一:两次不同的重试可以落在同一秒、步骤和日志相同,
    # 而真正不同的那两个字段(导出范围、失败名单)压根不在给人翻的那份副本里。
    # 代价换过来了,**而且换得对**:升级前那最后一轮(两份都写成功、确实是同一轮)
    # 从此落到 `order` —— 展示仍是它,只是重试要先跑一次完整同步。那是**误报**,
    # 顶多多跑一轮;反过来是**漏报**,静默按旧范围抓错来源。
    if same_event(newest, saved):
        return saved, ''                      # 同一轮 —— 用完整那一份
    # ① **结构性凭据**:历史是倒序的,状态文件那一轮出现在首条后面,就证明首条
    #    更新。它是同一份文件内部的位置关系,**不依赖发号这件事做对** —— 而序号是
    #    发得出来才有的,发号那一步读失证就没有(见 `_next_seq`)。
    # 位置锚点**也只认可信身份**(`same_event`)。原来这里过 `same_round`,于是内容
    # 相等也能当成员证明 —— 而那不是唯一的:旧格式 saved 其实晚于首条,历史里还留着
    # 更早的另一轮、它的显示字段恰好和 saved 相同,拿内容去找就「找到」了它,于是
    # 断言首条更新、把更早那一轮摆上屏幕。重试那边是安全的(欠账非空、按钮按不下去),
    # **可顶部那一屏在说假话** —— 而「展示对不对」是这颗判据的另一半职责。
    # 没有身份就证明不了成员关系,那就降到下面的序号档、再不成就 `order`。
    if any(same_event(r, saved) for r in runs[1:]):
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
