# -*- coding: utf-8 -*-
"""手动收录的**非网页入口**:在终端(或任何能跑命令的地方)把链接收进知识库。

入口是 `./kb.sh grab`,skill `kb-grab` 也打这一条。

**这一层不做任何关于收录的判断。** 抓取、查重、重抓替换、起后半条链那一整套
口径仍然只有 `kb/web/grab_api.py` 那一份(硬约束在
`docs/decisions/link-grab.md` 第一节)。这里只做三件事:把参数变成那两个接口的
请求、**按真实事实收敛那句「写进知识库了没有」**、把结果说成终端上的一行话
(或一行 JSON)。

**为什么不进程内 import `kb.feeds.grab` 自己抓:**
① 查重那几档各自说什么、重抓怎么替换、起哪一轮,全在 grab_api 里 —— 进程内
   再写一遍就是「同一个问题两处各判一遍」(元规则「口径只有一份」),而两处里
   迟早有一处把「暂存下来了」说成「已经在知识库里了」。
② **管道跑在 kb-web 进程内**:进程外抓完只能把内容留在 feeds.db 里,用户检索
   不到,而他以为收进去了。所以这条路和面板上那颗按钮**打同一个接口**,服务
   没起就先起服务(`kb.sh` 的 `cmd_grab` 里那道 `web_start`,口径同
   `./kb.sh sync`)。

**「已存下」和「搜得到」是两件事,终端上也要分开说**(同 grab_api 的模块说明):
`/api/grab` 回的是「那一轮起跑了」,不是「这一篇进去了」。所以收尾去问
`/api/grab/state`(只读)、按**这一篇自己**的事实落终态;整轮 export/index 双 ok
只是旁证 —— vault 里那一篇被删掉而两个 hash 还相等时双 ok,而那一篇搜不到。
这条收敛和界面那一侧(`kb/static/js/grab.js` 的 `grabCheckItem`)是同一条口径。

**回包没送达 ≠ 后端没存下来。** 保存排在起管道之前,断线可能发生在它之后 ——
说「这条没存下来」是在断言一件我们不知道的事。所以那一档去问一次只读的
`/api/grab/state`,问不到就如实说「结果不确定,再投一次是安全的」(重投按 URL
去重,`put_items` 的 `DO NOTHING` 挡着,内容不会变成两份)。

纯标准库:这条路不该因为 venv 里缺什么而用不了(`kb.sh` 用 `$BOOT_PY` 跑它)。
**同目录那两个文件和它是一套**(单文件要过 700 行的上限,按职责拆的):通道层
`grab_http.py`(打接口、把失败分两档)、输入层 `grab_input.py`(把粘进来的一行
文字变成几条地址)。要把这条路拷到别处用,三个文件一起拷。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 这个脚本可能被当文件直接加载(`$BOOT_PY scripts/grab_cli.py`、测试里的
# `spec_from_file_location`),所以把自己那一层的目录显式放进 sys.path —— 只靠
# 「跑脚本时 sys.path[0] 是脚本目录」的那一条,测试那条加载方式就进不来。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from grab_http import (                                              # noqa: E402
    API_DEFAULT, GRAB_HTTP_SECONDS, READ_HTTP_SECONDS, ApiDown, ApiRefused,
    bad_api, get_state, pipeline_idle, post_grab, post_restore)
# **输入层在 `grab_input.py`**:两类边界怎么认、Markdown 怎么按语法取目标,全在
# 那边(这里只管「拿到地址之后做什么」)。`links_in` 在本模块里**照旧可见** ——
# 口径只有一份,引用方(含测试)不该因为它搬了家而各自去找新门。
from grab_input import (                                            # noqa: E402
    TangledInput, dedupe, links_in, whole_line)

#: 等「写进知识库」那一步的默认上限。done 只要 export+index(不等 extract 调
#: 模型),单条一般几秒。
SETTLE_SECONDS = 120
POLL_SECONDS = 1.0
#: 一条最多替它重投几次恢复。**有上限**:某一条一直导不出去时,无限重投只会把
#: 这件事变成一个不停起管道(那一轮带 extract、要调模型)的循环。
#: **预算在发请求之前就扣**(见 `settle`):用「收到了 200 没有」判断远端那次写
#: 到底发生没有是不可靠的 —— 请求已经生效而回包丢了的那次照样起了一轮同步。
MAX_RENEWS = 2

#: 跑着的那个 kb-web 比代码旧时,收尾那句话要补的半句。**只提示,不代劳** ——
#: 重启服务会打断正在跑的管道(口径同 `code_stale`:提示而不代劳,也不拦)。
_STALE_SERVER = ('；补这一步的接口跑着的那个 kb-web 还没有（它比代码旧）——'
                 '`./kb.sh restart` 之后再 `--state` 查一次')

#: 终态词 → 终端上那个前缀。**每一档都有自己的话**,不许几档共用一句
#: (「已存下」和「已写入知识库」混成一句就是谎报,用户搜不到)。
_LABELS = {
    'saved': '已写入知识库',
    'duplicate': '早就在知识库里了',
    'pending': '已存下，还没写进知识库',
    'bad_content': '收下的那一份看不出是正文',
    # **「没换成」和「内容不合格」是两个事实。** 重抓到的内容和库里那一份一样
    # (或者这期间已经有人换过)时,库里那一份**本来是正文**、一个字节也没坏 ——
    # 说成「看不出是正文」是把一次无害的空转报成质量问题。
    'unchanged': '库里那一份没动',
    # **「收下了然后没了」和「压根没收下」是两件事。** 前者再投一次是重新抓一篇,
    # 后者是接着上次没做完的 —— 混成 `failed` 那一句会让人以为一个字节都没写进去。
    'gone': '收下了，现在不在库里',
    'failed': '没收下',
    'unknown': '结果不确定',
    'absent': '还没收进来',
    'not_tried': '还没投',
}


def renew(api: str, it: dict, *, timeout: float) -> None:
    """让**服务端**按这一条自己的归属把后半条链补起来 —— `POST /api/grab/restore`。

    **不自己打 `/api/pipeline/run`。** 那条路要前端替后端决定「这一条属于哪个
    来源」,而写死 `inbox` 在跨来源的那一刻就错了:一条推文被「大V观点」收过、
    这一轮又没占到管道时,起一轮 `inbox` 的 export **一个字都不会动它**
    (`pipeline_stages._per_channel` 按通道筛;真库 + 真 `sync.export` 实测:
    `export('inbox')` 之后 `exported=false`,换 `export('social')` 才为 true)。
    服务端那条路本来就是**按条目**恢复的:`grab_api._duplicate` 现查一次归属、
    先把欠的那一步落实成 export 认得的形状(`reclaim_export` —— vault 里那一篇
    被移走而两个 hash 还相等时,补跑的 export 压根选不中它),再按 `seen['kind']`
    起那一轮。所以恢复的不变量留在服务端一处,这边不另写一份带固定来源的协议。

    **打的是 `/api/grab/restore` 这条独立路径,不是 `/api/grab` 加一个字段。**
    `/api/grab` 的公开契约是「在就恢复、不在就抓一篇新的」,而「我刚才查到它还在」
    不是写入那一刻它还在的保证 —— 不收窄的后果实测过:等待期间历史清理把那一行
    删掉之后,这一次重投原地**重新抓了一遍**、把一条清理掉的 `rss` 文章重建成
    `inbox`、正文换成这次抓到的,而命令行照旧退出 0。

    **收窄这件事必须在协议边界上成立,不能靠一个可选字段。** 布尔字段那一版在
    同版服务上是对的,跨版本就不是:`kb.sh grab` 每次跑的是磁盘上的新 CLI,而
    `web_start` 撞见已经跑着的服务就直接返回(代码比进程新只提示一句 `code_stale`,
    按约定不代劳也不拦)。那个没重启的进程用的是旧 `GrabRequest`,Pydantic 默认
    **忽略**不认识的键 —— 于是这道约束静默消失,它照旧抓一篇新的建进去(实测拿 `git show 5b70036:kb/web/grab_api.py` 的真模块实测:实发 1 次取页、
    把清理掉的 `rss` 条目重建成 `inbox`)。独立路径在那种服务上**回 404**,
    这边据此到此为止(不再试、如实说要重启),而不是被降级成一个会创建内容的动作。
    **一样不带 `refetch`** —— 恢复要补的是后半条链,不是把内容换掉。

    回包只用来看「起跑了没有」,**不覆盖 `it['res']`**:那里面存着本次动作的证据
    (比如一次成功替换的 `replaced`),覆盖掉就等于把它忘了。
    """
    it['renews'] += 1          # **预算在发请求之前扣**(见 MAX_RENEWS)
    try:
        it['renewed'] = post_restore(api, it['url'], timeout=timeout)
    except ApiRefused as e:
        if e.code == 404:
            # 跑着的那个 kb-web 比代码旧,它没有这条路。**到此为止:不换回那条会
            # 创建内容的路。** 库里那一份一个字节没动,所以这不是失败 —— 收尾那句
            # 话里说清要重启(`_STALE_SERVER`)。
            it['renews'], it['no_restore'] = MAX_RENEWS, True
        raise


# ------------------------------------------------------------------ 这一条怎么说

def verdict(res: dict, state: dict | None, *, refetch: bool = False,
            acked: bool = True) -> tuple[str, str]:
    """这一条此刻的终态词 + 那句话。**纯函数**,所有输出都过它。

    **两种事实分开,不许互相顶替**:`res` 是**本次动作**的证据(换成了没有),
    `state` 是**库里那一篇此刻**的样子(在不在、看得出是正文吗、搜得到吗)。
    这条不变量展开就是下面四句,**每一句都对应过一次实测形态**:

    - **`res` 说得出「这一次做了什么」,说不出「现在是什么」。** 回包里的
      `content_ok` 是请求开始那一刻的质量;`replaced=false` 的典型成因正是别人在
      这中间刚换好,那时它带的是人家换之前的旧判断。所以「库里现在是不是正文」
      只认 `state`(没查过就先去查,见 `_settled`)。
    - **「这一次换没换正文」说不出「这一篇交付完了没有」。** `replaced=false` 只
      回答了动作那一问(内容相同 / 别人刚换过 / 重抓撞上关卡而旧正文完好 ——
      `grab_api._recover` 三种结果都回这一个值),而那一篇**可能还欠导出和索引**。
      实测形态:真端点 + 临时库,`content_ok=true`、`done=false`,终端说一句
      「库里那一份没动」就 0 退出 —— 盘上一个 Markdown 文件都没有、恢复请求 0 次,
      不是「等完还没索引」,是压根没人去起那一步。所以这一档只在 `done` 之后才用
      `unchanged` 收口,欠着的照旧报 `pending` 并走恢复那条路。
    - **`state` 说得出「现在是什么」,说不出「这一次结束了没有」。** 没拿到回包
      (`acked=False`)时库里那一份旧快照不许终结这一条 —— 后端可能正停在取页中间,
      随后把它换掉。
    - **`state` 也否认不了已经确认发生过的写入。** 确认 `added`/`replaced`/
      `duplicate` 之后再查到「库里没有这一行」,那是**之后被删了**(`gone`),
      不是「一个字节都没写」(`failed`)。
    - **反过来,「那一篇目前存在且可检索」也证明不了「本次替换完成」** ——
      显式重抓的回包丢了而库里旧那一版本来就 `done`,只能停在不确定。

    档位和界面那一侧(`grab.js` 的 `grabCheckItem`)一一对应:
    - 内容本身不对排**最前面**:后面几步全做完也只是把一份看不出是正文的东西写
      进知识库(同 `owing`)。但**这一档只认两种依据** —— 本次明确没换成**而且**
      库里那一份确实不是正文,或者 `state` 说库里当前那一份不是正文;
    - `done` → 真写进去了(按**这一篇自己**的事实,不是按那一轮的阶段结果);
    - 库里有这一行、还没走完 → 如实说欠哪一步、下一步点哪儿;
    - 库里压根没有这一行 → **要看本次那个动作终结了没有**(`acked`)才答得出。

    `acked=False` 的意思是「这一次投递没拿到明确回答」(回包丢了 / 5xx / 断线)。
    那时**库里没有这一行只说明这一刻还没有**,不说明那个动作以后不会写入 ——
    实测形态:后端已经进入取页、投递连接断掉,此刻 `known=false`,而那次动作随后
    真的保存成功并导出了一篇(实测)。所以这一档只能停在「不确定」。
    """
    # —— **先处理「这一行还在不在」**。库里已经没有它了,那么「这次没换成」
    #    「库里那一份是正文」全都是在描述一篇**不存在**的文章(实测形态:重抓等待
    #    期间同一篇被清理掉,终端照旧说「库里那一份没动」)。所以这一问排在所有
    #    质量/替换的结论之前。
    if state is not None and not state.get('known'):
        if not acked:
            # **空快照证明不了「那次投递不会写入」。** 没拿到明确回答的那一次,
            # 后端可能正停在取页/保存中间 —— 说「没收下」会让人立刻重投,而原动作
            # 随后写进去就变成抓了两次(重抓那一档还会再动库里那一份)。
            return 'unknown', ('投出去了，这一刻库里还没有这一行 —— 那一次的回包'
                               '丢了，它可能还在抓；稍后 --state 再查一次，别急着'
                               '重投')
        # 判据宽一点:`replaced` 这个键**存在**本身就证明后端对这一条做过替换尝试
        # (它当时在库里),哪怕值是 False(没换成)。漏判的代价是把「收下过、现在
        # 没了」说成「一个字节都没写、再投一次」,而反过来顶多多说一句。
        if (res.get('added') or res.get('duplicate')
                or res.get('replaced') is not None):
            # **已经确认发生过的保存,「现在没有了」否认不了。** 等待期间历史清理
            # 或手工删除会把这一行拿走 —— 那是「之后被删了」,不是「一个字节都没
            # 写」。报 `failed` 的代价是 skill 和那句话都在说后者,照它重投会原地
            # 再抓一遍(而用户可能正是刚刚把它删掉的那个人)。
            return 'gone', ('这一条后端确认收下了，但现在库里已经没有这一行'
                            '了 —— 等待期间被清理或删掉了；还要的话再投一次')
        return 'failed', '库里没有这一行，这条没存下来；再投一次'
    if res.get('replaced') is False:
        if state is None:
            # **缺当前证据时不发布质量结论。** 回包里那个 `content_ok` 是请求开始
            # 那一刻的,而 CAS 没换成的典型成因正是**别人在这中间刚换好** —— 那时
            # 它带的是人家换之前的判断。停在不确定,由 `_settled` 去查一次只读。
            return 'unknown', '这次重抓没换成；库里那一份现在是什么样还没核到'
        # 重抓没换成。**两个事实要分开**:库里那一份本身不是正文 → 还是
        # `bad_content`(再 --refetch 一次有意义);内容相同或被别人换过 →
        # `unchanged`,库里那一份完好,不许说成质量问题。
        #
        # **而「库里那一份现在是不是正文」只认 `state`。** 回包里的 `content_ok`
        # 说的是**请求开始那一刻**的质量:CAS 没换成那一档的典型成因正是**别人在
        # 这中间刚换好**,那时回包里带的是人家换之前的旧判断 —— 拿它报「看不出是
        # 正文、加 --refetch 再来一次」就是让人去重抓一份**已经好了**的正文。
        # 没查过 `state` 时这一档不许收口:`_settled` 会让它先去查一次。
        if not state.get('content_ok'):
            return 'bad_content', (res.get('detail')
                                   or '这次重抓没换成，库里那一份还是看不出是正文')
        # 库里现在是正文。**不许转述回包里那句按旧质量写的话。**
        #
        # **而「这次没换上」和「这一篇交付完了」是两个维度**(见上面那条不变量):
        # 只有走完整条链之后 `unchanged` 才是终态;还欠导出/索引的往下走到
        # `pending`,由 `settle` 照它的归属走**只恢复、不创建**那条路补上。
        if state.get('done'):
            return 'unchanged', ('这次重抓没换成（抓到的内容相同，或者这期间已经'
                                 '有人换过）；库里那一份是正文，全文检索里搜得到')
    if state and state.get('known') and not state.get('content_ok'):
        if not acked:
            # **这一份坏内容是动作开始之前的事实。** 没拿到回包的那一次可能正停在
            # 取页中间、随后把它换掉 —— 凭它说「加 --refetch 再来一次」会让人在原
            # 动作成功之前又投一次(实测:真后端随后 `replaced=true`、
            # 新正文 `content_ok` 为真、真导出一篇,而 CLI 已经按旧快照 0 退出了)。
            # 所以停在不确定,由 `_settled` 继续在预算内核实。
            return 'unknown', ('投出去了，回包丢了；库里那一份还是动作之前那一版'
                               '（看不出是正文），这一次换没换成核不出来 —— 稍后'
                               ' --state 再查一次，别急着重投')
        return 'bad_content', '加 --refetch 会重抓一次，确认得了就把它换掉'
    if res.get('content_ok') is False and not state and not res.get('replaced'):
        # **换成了的那一档不许走这里。** 后端那一档(`_recover` 的成功分支)只更新
        # `replaced` / `done` / `title`,回包里的 `content_ok` 仍是**库里旧那一份**
        # 的判断 —— 拿它下结论就是用旧内容的质量否定新内容。
        return 'bad_content', res.get('detail') or '库里那一份看不出是正文'
    if state and state.get('done'):
        if refetch and not res:
            # **「库里那一篇走完整条链了」证明不了这次替换成功。** 显式重抓的回包
            # 丢了(5xx / 断线),而库里旧那一版本来就 `done` —— 照 `done` 报
            # 「已写入知识库」就是把一件不知道的事说成已完成,而用户要的是**新**
            # 那一篇。只读那一问答不出这件事,所以如实停在不确定。
            return 'unknown', ('库里那一份是走完整条链的，但这次重抓换没换成核不'
                               '出来；再 --refetch 一次是安全的（换不换得成由后端'
                               '那六道准入和 CAS 定）')
        label = '已经按这次重抓到的内容换掉了；' if res.get('replaced') else ''
        return 'saved', label + '全文检索里搜得到了'
    if state is None:
        # 没能问到这一篇的事实。**不许替它下结论**。
        return 'unknown', res.get('detail') or '没核到这一篇的结果'
    owes = state.get('owing') or '还没写进知识库'
    # 重抓没换成那一条也会落到这儿(好正文、还欠后半条链)。**动作那个事实要留着**
    # —— 不写这一句的话,一次明确要求的重抓会只剩一句「还没写成 vault 里那一篇」,
    # 看不出「我要的那次替换到底做了没有」。
    head = ('这次重抓没换成（库里那一份本来就是正文）；'
            if res.get('replaced') is False else '')
    return 'pending', head + owes + (state.get('resume') or '').replace('，', '；', 1)


def _settled(it: dict) -> bool:
    """这一条还用等吗。**判据是「本次那个动作终结了没有」,不只是档位那个词。**

    等的有四种:那一轮还没把它写进去(`pending`)、刚投出去还没核(`writing`)、
    **没拿到明确回答**那两档(此刻库里还没有这一行、或者库里是动作之前那一份
    坏内容)—— 后端可能正停在取页中间,预算内再问几次问得出来(实测:断线之后
    那次保存/替换随后都成功了)、以及**还没拿 `state` 作证的 `bad_content`**。

    **拿到了明确回答的 `unknown` 是终态**:它由「回包丢了,而只读那一问答出的是
    库里旧那一版」产生,再等下去问到的还是同一个答案。预算用完时留在 `unknown`
    照旧是如实的 —— 不许因为等不到就改口说「确定没存下」。
    """
    status = it['status']
    if status == 'bad_content':
        # **这一档要 `state` 作证。** 回包里的 `content_ok` 是请求开始那一刻的
        # 质量,CAS 没换成那一档(别人刚换好)带的是旧判断 —— 凭它停下来就会让人
        # 去重抓一份已经好了的正文。只读查一次很便宜,查完 `verdict` 自会重算。
        return it.get('state') is not None
    if status == 'unchanged':
        # **「这次没换正文」不是「这一篇交付完了」**(同 `bad_content` 那条:终态
        # 要有当前事实作证)。归并层已经只在 `done` 之后才给这个档,这儿再钉一道
        # —— 「交付没完就不许停止等待和补写」这件事不该押在上游某一个分支上。
        return bool((it.get('state') or {}).get('done'))
    if status in ('saved', 'duplicate', 'failed', 'gone', 'not_tried'):
        return True
    if status == 'unknown':
        # 两种都还问得出来:**没拿到明确回答**(后端可能正停在取页中间)、以及
        # **还没拿 `state` 核过库里此刻的样子**(CAS 没换成那一档就落在这儿)。
        # 预算用完时留在 `unknown` 照旧是如实的 —— 不许因为等不到就改口。
        return bool(it.get('acked', True)) and it.get('state') is not None
    return False


def _left(deadline: float, cap: float) -> float:
    """这一次请求最多还能等多久 —— **剩余预算和单次上限取小的那个**。

    不传预算的话 `--timeout 1` 会在一条慢 state 上等满单次上限(15 秒),一批很多条
    还会累加:说出去的那个秒数就不是上限了(只有这一层能把它变成上限,因为超时是
    逐请求的)。

    **不抬底。** 抬到「至少 1 秒」等于预算耗尽之后每条还能再发一个一秒的请求,
    N 条就是 N 秒 —— 返回 0 或负数,调用方据此**不发**这一次请求。

    **它管得到的是「还发不发下一个请求」,管不到「一个已经发出的请求读多久」。**
    传下去的那个数变成 socket 超时,而 socket 超时是**两次收包之间**的空闲上限:
    对方每次都在超时之前喂一点数据的话,那一次 `resp.read()` 可以跨过预算的终点
    才返回,随后主循环才重新看预算(实测,按代码推断未实测)。所以对外
    那句话只能说「轮询的预算」,不能说成「整段等待的硬上限」——真要硬上限得让读取
    本身也盯同一个绝对终点,那是另一件事(而且读到一半掐掉有自己的代价)。
    """
    return min(cap, deadline - time.monotonic())


def settle(api: str, items: list[dict], *, deadline: float) -> None:
    """轮询到每条都有终态,或者等满。**原地改 items**。

    **问不出来的那一轮不重算档位。** `get_state` 失败时保持原档继续等 —— 拿
    `state=None` 去重算,会让「本次已经换成、只是这一次没问到」被旧 `content_ok`
    判成 `bad_content` 并当场停止等待(新正文明明已经换好了)。

    **管道空着而还有没写进去的 → 让服务端按那一条的归属补一次**(`renew`):批量
    投递时第二条起必然撞上第一条那一轮,它们的内容在库里却没人替它们跑 export。
    一轮只补一条:那一轮是该来源的全量待办,同来源的其余几条跟着一起带上;**跨来源
    的那几条**在后面的轮次里各自补(所以不许把它们合成一次写死来源的补跑)。

    超时不谎报:最后一次问到的事实就是那句话(`pending` 自带「欠哪一步 + 去哪儿
    点一次」)。
    """
    while True:
        waiting = [it for it in items if not _settled(it)]
        if not waiting:
            return
        for it in waiting:
            # **每一次请求之前都看一眼预算**,不是整批查完才看:一批 N 条慢查询
            # 否则会各自等满自己那一份,说出去的秒数就不是上限了。
            budget = _left(deadline, READ_HTTP_SECONDS)
            if budget <= 0:
                return
            try:
                fresh = get_state(api, it['url'], timeout=budget)
            except RuntimeError:
                continue              # 这一轮问不出来:保持原档,别拿 None 重算
            it['state'] = fresh
            # 回包丢了的那一条,标题只有只读这一问答得出来(N2):别让一条确认
            # 存下的结果带着空标题交给调用方。
            it['title'] = it['title'] or fresh.get('title') or ''
            it['status'], it['detail'] = verdict(it['res'], fresh,
                                                 refetch=it['refetch'],
                                                 acked=it['acked'])
        waiting = [it for it in items if not _settled(it)]
        if not waiting:
            return
        budget = _left(deadline, READ_HTTP_SECONDS)
        if budget <= 0:
            return
        if pipeline_idle(api, timeout=budget) is True:
            for it in waiting:
                # **只给「库里有这一行、就差后半条链」的那几条补** —— 包括**重抓
                # 没换成、而库里那一份是好正文还欠导出/索引**的那一条(归并层把它
                # 判成 `pending` 正是为了走到这儿)。别的那两档(回包丢了还在核实、
                # 内容不对)压根没有可补的那一步,去求一次恢复只会白占预算,而服务端
                # 那条路本来就会拒。
                if it['status'] != 'pending' or it['renews'] >= MAX_RENEWS:
                    continue
                budget = _left(deadline, GRAB_HTTP_SECONDS)
                if budget <= 0:
                    return
                try:
                    renew(api, it, timeout=budget)
                except RuntimeError:
                    pass    # 预算已经扣了(那次请求可能已经生效),下一轮按事实再看
                break
        if time.monotonic() >= deadline:
            return
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def new_item(url: str, *, refetch: bool = False) -> dict:
    """一条输入的结果对象。

    **先把它建好、放进列表,再去打接口**:通道断在半路时它已经在列表里了,所以
    「一条输入对一条结果」这个契约在失败路径上也成立 —— 少了它的后果是 stdout
    一个字都没有(单条)、或者出问题那一条连同后面几条**一声不响地消失**(批量),
    而 skill 被告知「只按 status 汇报」时手里压根没有 status。
    """
    return {'url': url, 'res': {}, 'state': None, 'status': 'not_tried',
            'detail': '还没投 —— 前面那一条就打不通服务了', 'title': '',
            'refetch': bool(refetch), 'renews': 0,
            # **本次那个动作终结了没有**(拿到 200 回包、或者被 4xx 明确拒了)。
            # 没终结的时候,「库里此刻没有这一行」说明不了它以后不会写入 ——
            # 见 `verdict` 的 `acked` 和 `_settled`。
            'acked': False}


def collect(api: str, it: dict) -> None:
    """投一条,**原地填充** `it`。回包落地即为初始档位(还要不要等由 `settle` 定)。"""
    url = it['url']
    try:
        it['res'] = post_grab(api, url, it['refetch'])
    except ApiDown as down:
        # **回包没送达 ≠ 后端没存下来**(保存排在起管道之前,断线可能发生在它
        # 之后)。去问一次只读那一问;问得到就照事实说,问不到才把通道那句话抛
        # 上去 —— 说「这条没存下来」是在断言一件我们不知道的事。
        try:
            it['state'] = get_state(api, url)
        except RuntimeError:
            it['status'] = 'unknown'
            it['detail'] = f'投出去了，但结果核不到:{down}'
            raise ApiDown(str(down)) from None
        it['title'] = it['title'] or it['state'].get('title') or ''
        it['status'], it['detail'] = verdict({}, it['state'], refetch=it['refetch'],
                                             acked=False)
        if it['status'] == 'pending':
            it['detail'] = '连接断在半路：' + it['detail']
        return
    except ApiRefused as e:
        # 4xx 那一句就是「为什么没收下 + 下一步」,原样交给用户。**这一档动作终结
        # 了**:那六道准入排在写库之前,拒了就一个字节都没写。
        it['status'], it['detail'] = 'failed', str(e)
        it['acked'] = True
        return
    it['acked'] = True          # 拿到明确回答了:下面几档都按它说的算
    res = it['res']
    it['title'] = res.get('title') or ''
    # **顺序就是判据的优先级,不许按 `content_ok` 先判。** 重抓**换成了**那一档
    # (`_recover` 的成功分支)只更新 `replaced` / `done` / `title`,`content_ok`
    # 仍是**库里旧那一份**的判断 —— 先看它就会把一次成功的替换说成「看不出是
    # 正文」,而且不去等它写进知识库(用户那一次重抓于是永远停在「没换成」)。
    if res.get('replaced') is False:
        # 明确没换成。**这一档此刻还不算终态**:库里那一份现在是不是正文只有
        # `state` 说得准(回包里那个 `content_ok` 是请求开始时的),所以先落档、
        # 由 `settle` 查一次只读再重算(`_settled` 的 `bad_content` 那一条)。
        it['status'], it['detail'] = verdict(res, None)
    elif res.get('pipeline'):
        # started / busy:两档都要收敛(busy 那一档靠 `settle` 让服务端补一次)。
        # 重抓换成了的那一档也在这里 —— 它起的是同一条后半链。
        it['status'], it['detail'] = 'writing', res.get('detail') or ''
    elif res.get('content_ok') is False:
        # 内容不对、而且没起任何一轮:这一档要人再动一次手。
        it['status'], it['detail'] = verdict(res, None)
    elif res.get('duplicate') is True and res.get('done') is True:
        # 幂等那一档(`grab_api._duplicate` 的 `done` 分支):库里那一行走完整条链
        # 了,这一次不占管道。**这句话要正面证据**,不能按「少了 pipeline 这个字段」
        # 推 —— 那不是证据,一个不相干的回包(或者端点将来多一个分支)会被说成
        # 「早就在知识库里了」而 `done=false`,一次核实都没发生。
        it['status'], it['detail'] = 'duplicate', res.get('detail') or ''
    else:
        # 既没起管道、也拿不出「它已经走完整条链」的证据。**不替它下结论**:
        # `verdict` 在没有 `state` 时给 `unknown`,`_settled` 会去只读问一次事实
        # 再重算(通道层挡得住缺字段的回包,这一档兜的是语义上自相矛盾的那种)。
        it['status'], it['detail'] = verdict(res, None)


def recheck(api: str, it: dict) -> None:
    """`--state`:**只读**问一句这一条此刻在知识库里是什么状态,原地填充 `it`。

    不发抓取、不起管道、不写一个字节 —— 「这篇我存过吗」是随时问得出的事实。
    """
    try:
        it['state'] = state = get_state(api, it['url'])
    except ApiRefused as e:
        # 4xx:多半是「这一串不是一条链接」(`grab.normalize` 那句话)。原样转述,
        # 而不是抛一个 traceback 给人看 —— 一批地址里混进一行脏数据是常事。
        it['status'], it['detail'] = 'failed', str(e)
        return
    except ApiDown as down:
        # **「现在查不到」不是「还没投」。** 只读这一问压根没有投递这个动作 ——
        # 留着 `not_tried` 那句默认文案(「前面那一条就打不通服务了」)时,第一条
        # 查询失败会被说成一件不存在的事,而用户问的是「这篇我存过吗」。
        it['status'] = 'unknown'
        it['detail'] = f'现在查不到，不能确定这篇存过没有:{down}'
        raise
    it['url'] = state.get('url') or it['url']
    it['title'] = state.get('title') or ''
    if not state.get('known'):
        it['status'], it['detail'] = 'absent', '还没收进来'
    elif not state.get('content_ok'):
        it['status'], it['detail'] = 'bad_content', '加 --refetch 会重抓一次'
    elif state.get('done'):
        it['status'], it['detail'] = 'saved', '全文检索里搜得到了'
    else:
        it['status'] = 'pending'
        it['detail'] = (state.get('owing') or '还没写进知识库') + (state.get('resume') or '')


# ---------------------------------------------------------------------- 说出来

def line_of(it: dict) -> str:
    """一条的人读形态:状态词 + 标题,下一行地址,再一行那句话。

    **状态词按 `_LABELS` 逐档取**,不许几档共用一句:「已存下」和「已写入知识
    库」混成一句话,用户就会去检索里找一篇还没写进去的文章。
    """
    label = _LABELS.get(it['status'], it['status'])
    head = f"{label}  {it.get('title') or ''}".rstrip()
    rows = [head, f"  {it['url']}"]
    if it.get('detail'):
        rows.append(f"  {it['detail']}")
    return '\n'.join(rows)


def record_of(it: dict) -> dict:
    """一条的机读形态(JSONL 一行)。skill `kb-grab` 读的就是这个。

    **`status` 是收敛之后的终态**,另把后端那几个事实原样透出 —— 调用方要能
    自己复核,而不是只拿到我们归纳的一个词。
    """
    res, state = it.get('res') or {}, it.get('state') or {}
    return {'url': it['url'], 'status': it['status'], 'title': it.get('title') or '',
            'detail': it.get('detail') or '', 'label': _LABELS.get(it['status'], it['status']),
            # **缺证据不是 0。** 回包丢了的那一次可能真的新建了一条,把它编码成
            # `added: 0` 是在断言一件我们不知道的事(N2):那时给 `null`。
            'added': res.get('added', 0) if res else None,
            'duplicate': bool(res.get('duplicate')),
            'replaced': res.get('replaced'), 'kind': state.get('kind') or res.get('kind') or '',
            # **没查过不许编码成「确认未完成」。** 幂等那一档不查 state(它早就
            # 走完了),而后端回包里就有 `done=true` —— 只看 state 会输出
            # `status=duplicate, done=false` 这种自相矛盾的一行。
            'done': bool(state.get('done', res.get('done'))),
            'content_ok': state.get('content_ok', res.get('content_ok')),
            'owing': state.get('owing', ''), 'exported': state.get('exported'),
            'stored': state.get('stored'), 'indexed': state.get('indexed'),
            'refetch': bool(it.get('refetch')), 'renews': it.get('renews', 0)}


def _targets(args: argparse.Namespace) -> list[str]:
    """命令行上/标准输入里要收的那几条。`-` = 从标准输入读。

    **两种模式,判据是「这一行是不是整个就是一条地址」:**
    ①**显式参数、以及标准输入里整行就是一条地址的那些 → 原样交给后端,一个字符
      都不动。** 地址合不合法只有 `grab.normalize` 那一份口径说得准,而在这儿做
      有损的抽取,代价是把一个**合法**地址改坏:`…/wiki/函数（数学）`(中文维基的
      条目名真的带着一对全角括号)会被截成 `…/wiki/函数`,而**那一页也真的存在**
      —— 收进来的于是是另一篇文章;末尾带 `;` `,` `:` 的地址同理。`--refetch` 和
      `--state` 走同一条解析,所以那还会连带认错「要替换哪一篇」「要查哪一篇」。
      把一句话当参数给进来的人,会拿到后端那句明确的「这不是一条链接」,改一下再来
      即可 —— 比静默换一篇文章好。
    ②**只有「这一行夹着叙述文字」才抽取**(`grab_input.links_in`):剪贴板里常常是「看这篇
      https://… 挺好」。那一层是启发式,宁可留下一个抓不到的坏地址,也不截短成另一个
      可能存在的目标。
    """
    out: list[str] = []
    for raw in args.url:
        out.extend(_from_stdin() if raw == '-' else [raw.strip()])
    # **去重那一份口径在输入层**(`grab_input.dedupe`):按出现顺序、集合判在不在。
    # 这边再写一遍 `not in out` 就是同一个判据两处各一份,而那种写法在成百上千条
    # 互不相同的地址上是平方级的比较。
    return dedupe(out)


def _from_stdin() -> list[str]:
    """标准输入:**逐行**判模式 —— 整行一条地址的保真,夹着叙述的才抽取。"""
    out: list[str] = []
    for line in sys.stdin.read().splitlines():
        line = line.strip()
        if not line:
            continue
        out.extend([line] if whole_line(line) else links_in(line))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog='kb.sh grab', description='手动收录:把链接抓进知识库(网页端那颗按钮的命令行版)')
    p.add_argument('url', nargs='+', metavar='链接',
                   help='要收录的链接,可给多条;`-` = 从标准输入读(整行一条、'
                        '或者把地址圈进 <…> / "…" / Markdown 的那些 —— 边界给定的'
                        '原样收,Markdown 按语法取目标;裸写在行文里的按空白断、'
                        '一个标点都不剥)')
    p.add_argument('--api', default=API_DEFAULT, help=f'kb-web 地址(默认 {API_DEFAULT})')
    p.add_argument('--refetch', action='store_true',
                   help='库里已经有这一条时重抓一次,确认得了就把它换掉')
    p.add_argument('--state', action='store_true',
                   help='只查状态:不抓取、不起管道、不写一个字节')
    p.add_argument('--json', action='store_true', help='一行一条 JSON(给程序读)')
    p.add_argument('--no-wait', action='store_true',
                   help='不等「写进知识库」那一步,投完就返回')
    p.add_argument('--timeout', type=float, default=SETTLE_SECONDS,
                   help=f'等「写进知识库」的轮询预算,最多多少秒'
                        f'(默认 {SETTLE_SECONDS:.0f})')
    # `kb.sh` 在起服务**之前**拿它校验一次参数:`--help`、拼错的选项、少给链接都
    # 不该先起一个守护进程(worktree 里那道闸门还会抢在前面,连帮助都拿不到)。
    # 这一档不读标准输入、不发一个请求。
    p.add_argument('--parse-only', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    # **打错的 `--api` 是参数错,不是「投出去了而结果未知」**:它会在 Request 构造
    # 那一步抛 ValueError,穿过通道层那套归一化(实测满屏 traceback、stdout 空)。
    # 排在 `--parse-only` **之前** —— `kb.sh` 拿那一档预检参数,所以这一句也能让
    # 它别为一个打错的地址先起一个守护进程。
    if why := bad_api(args.api):
        print(why, file=sys.stderr)
        return 2
    if args.parse_only:
        # **把最终生效的那个地址报回去。** `kb.sh` 据它决定要不要起本机那份服务:
        # `--api` 指到别处时,它那道 worktree 闸门和 `web_start` 管的是**另一个**
        # 服务(实测形态:worktree 里会在连上用户给的地址之前就 die,主检出
        # 则多起一份用户压根不需要的守护进程)。**装配口径只有一份** —— 默认值由
        # `kb.sh` 用 `--api` 传进来,用户自己写的那个照 argparse 的规矩覆盖它,所以
        # 打出来的就是真正会被请求的地址(`rstrip('/')` 同 `_request`)。
        print(args.api.rstrip('/'))
        return 0

    try:
        urls = _targets(args)
    except TangledInput as e:
        # **「这一行我认不全」是参数错,不是「这一行没有链接」。** 后者是「确定
        # 没有」这个结论,而我们只知道没认全 —— 退出 2、把那一行的开头回显出来,
        # 用户看得见、改一下再来(见 `grab_input._Budget`)。
        print(f'{e}', file=sys.stderr)
        return 2
    if not urls:
        print('没给出要收录的链接', file=sys.stderr)
        return 2
    # **一条输入一个结果对象,先全建好。** 下面任何一步断在半路,输出里都还有
    # 它那一行(`not_tried` / `unknown`),不会静默漏条。
    items = [new_item(url, refetch=args.refetch) for url in urls]
    broken = ''
    for it in items:
        try:
            (recheck if args.state else collect)(args.api, it)
        except ApiDown as e:
            # 通道都打不通了:后面几条不投(它们留在 `not_tried`,如实说没试过)。
            broken = str(e)
            break
    if not broken and not args.state and not args.no_wait:
        settle(args.api, items, deadline=time.monotonic() + max(0.0, args.timeout))
    # `--no-wait` 下 `writing` 就是终态:那一轮起跑了,但我们没等它。
    for it in items:
        if it['status'] == 'writing':
            it['status'] = 'pending'
            it['detail'] = it['detail'] or '正在写入知识库，稍后 --state 查一次'
        if it.get('no_restore'):
            # 补后半条链那条路在跑着的那个服务上还不存在(它比代码旧):内容在库里,
            # 所以档位照旧,但要说清下一步在哪儿 —— 不说的话这几条只是「还没写进
            # 知识库」,看不出是因为服务没重启。
            it['detail'] = (it['detail'] + _STALE_SERVER).lstrip('；')
    _emit(items, args.json)
    if broken:
        print(f'打不通 kb-web({args.api}):{broken}\n'
              '服务没起就 ./kb.sh start;起着的话看一眼 ./kb.sh logs', file=sys.stderr)
    # **非 0 的判据是「这一条此刻没在知识库里躺好」**,不是「出错了」:`gone`
    # (确认收下过、现在被删了)照样要人看一眼,所以也算。
    bad = [it for it in items
           if it['status'] in ('failed', 'unknown', 'not_tried', 'gone')]
    return 1 if bad else 0


def _emit(items: list[dict], as_json: bool) -> None:
    for it in items:
        print(json.dumps(record_of(it), ensure_ascii=False) if as_json else line_of(it))
    if not as_json and len(items) > 1:
        tally: dict[str, int] = {}
        for it in items:
            tally[it['status']] = tally.get(it['status'], 0) + 1
        parts = [f'{n} 条{_LABELS.get(s, s)}' for s, n in tally.items()]
        print(f"共 {len(items)} 条:" + '、'.join(parts))


if __name__ == '__main__':
    sys.exit(main())
