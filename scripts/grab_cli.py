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
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

#: 默认服务地址。`kb.sh` 总会显式传 `--api`(它知道 `KB_WEB_PORT`);这个默认值
#: 是给「手里只有这个脚本」的调用方的。
API_DEFAULT = 'http://127.0.0.1:8787'
#: 一次收录请求等多久。服务端自己的墙钟上限是 40 秒(`grab.GRAB_SECONDS`),
#: 这里留出起管道和回包的余量 —— 比它短的话会把一次**正在成功**的抓取说成
#: 超时,而内容其实已经存下了。
GRAB_HTTP_SECONDS = 70
#: 只读那两个接口给得紧:它们一读内存/一读库就有答案。
READ_HTTP_SECONDS = 15
#: 等「写进知识库」那一步的默认上限。done 只要 export+index(不等 extract 调
#: 模型),单条一般几秒。
SETTLE_SECONDS = 120
POLL_SECONDS = 1.0
#: 一条最多替它重投几次恢复。**有上限**:某一条一直导不出去时,无限重投只会把
#: 这件事变成一个不停起管道(那一轮带 extract、要调模型)的循环。
#: **预算在发请求之前就扣**(见 `settle`):用「收到了 200 没有」判断远端那次写
#: 到底发生没有是不可靠的 —— 请求已经生效而回包丢了的那次照样起了一轮同步。
MAX_RENEWS = 2

#: **整行就是一条地址** —— 这一档和显式参数走同一道:一个字符都不动。判据只看
#: 「这一行(去掉两头空白之后)整个就是一条 http(s) 地址、中间没有空白」,不看它
#: 里面有哪些字符 —— 括号、分号、全角都不作数。`pbpaste | kb.sh grab -` 和
#: `kb.sh grab - < links.txt` 喂进来的多半正是这种一行一条。
_WHOLE_LINE = re.compile(r'^https?://\S+$')
#: 从**夹着叙述文字的那一行**里认链接。吃到空白/尖括号/引号/**全角句读**为止。
#: 这两类全角字符要分开看,而且依据不同:
#: - **句读**(`、，。；：！？`)在裸地址里出现**没有来源** —— 路径里要用它必须先
#:   percent-encode,所以裸写只能是行文。排除它才接得住「见 <地址>。后面还有话」
#:   这种紧贴中文、中间没有空白的常见写法。
#: - **括号**(`（）`)**有真实来源**:中文维基的条目名自己就带着一对
#:   (`…/wiki/函数（数学）`)。所以不排除,改按配对算(见 `_unwrap`)—— 排除它等于
#:   在第一个括号处截断,而截断后的 `…/wiki/函数` **也真的存在**:那是收错文章。
_URL_RE = re.compile(r'https?://[^\s<>"\'、，。；：！？]+')
#: 行内抽取时要剥的外围标点,**只剩 ASCII**(全角句读进不了 `_URL_RE`,括号按配对
#: 算)。剥的是「写在地址末尾没有意义、而行文里极常见」的那几个。
#: **ASCII 的 `!` 不剥** —— 它是 URL 合法的子分隔符(`/post!` 真的存在),而
#: 「末尾是叹号就一定是句末」正是那种不能做的推断。
#: **这一层是启发式,只对「行内夹着叙述」那一种生效**:整行一条地址的那一档压根
#: 不进这里,所以可靠的喂法是一行一条。
_TRAIL_PUNCT = '.,;:?'
#: 括号按**对**算,半角全角各一对。判据是**不配对的右括号标志行文从那里开始** ——
#: 配得上对的属于地址(`…/a(b)`、`…/函数（数学）`),而落单的那个只可能是外围写法
#: (`（见 <地址>）以及别的`)。`（` 不在 `_URL_RE` 的排除集里,所以配对判得出来。
_BRACKETS = (('(', ')'), ('（', '）'))
#: 终态词 → 终端上那个前缀。**每一档都有自己的话**,不许几档共用一句
#: (「已存下」和「已写入知识库」混成一句就是谎报,用户搜不到)。
_LABELS = {
    'saved': '已写入知识库',
    'duplicate': '早就在知识库里了',
    'pending': '已存下，还没写进知识库',
    'bad_content': '收下的那一份看不出是正文',
    'failed': '没收下',
    'unknown': '结果不确定',
    'absent': '还没收进来',
    'not_tried': '还没投',
}


class ApiDown(RuntimeError):
    """**打不通 kb-web**(连不上、超时、或者它自己 5xx 了)。

    **和「这条没收下」严格分开**:它说的是通道,不是这条链接 —— 保存排在起管道
    之前,所以这一档里内容**可能已经存下了**。谎称「没收下」会让用户去别处找原因
    (口径同 `grab.js` 那段 catch:回包没送达 ≠ 后端没存下来)。
    """


class ApiRefused(RuntimeError):
    """服务**明确拒绝**了这一条(4xx)。消息就是「为什么没收下 + 下一步」。

    只有这一档说得出「没收下」:后端那六道准入是在写库之前过的,拒了就一个字节
    都没写。5xx 不算 —— 那时写没写进去我们不知道(见 `ApiDown`)。
    """


#: 本机请求**一律不走代理**。环境里设了 `*_proxy` 时(这台机器上常设),urllib 会
#: 把 `127.0.0.1:8787` 也交给代理,拿回一个跟知识库毫无关系的 502 —— 那会被说成
#: 「这条没收下」,而服务其实好好跑着(实测:`--api http://127.0.0.1:9` 回 HTTP 502)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _detail_of(raw: bytes) -> str:
    """错误回包里那句面向用户的话。

    **不许用 `json.loads(...)['detail']` 一步到底**:未捕获异常时 Starlette 回的
    是纯文本 500,那一步会抛,真因被吞成一句无信息的「失败」(口径同
    `frontend-feedback.md` 里 `apiFail` 那条)。
    """
    text = (raw or b'').decode('utf-8', 'replace').strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(data, dict) and isinstance(data.get('detail'), str):
        return data['detail']
    return text[:500]


#: **读 HTTP 会在三个层面上断,这一层要把它们归成同一档。** 网络层是
#: `URLError`/`OSError`/`TimeoutError`,而**正文读到一半对方关连接**抛的是
#: `http.client.IncompleteRead`(`HTTPException` 的子类,**不是** OSError)——
#: 漏掉它的后果实测过:200 回包声明 `Content-Length: 247` 只写了 12 字节,
#: 异常一路穿透 `collect` 和 `main`,终端上是一段 traceback、stdout 一个字没有,
#: **而且一次用于核实的 GET 都没发** —— 服务端那会儿可能已经把这一行存下了。
_READ_BROKEN = (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException)


def _request(api: str, path: str, *, payload: dict | None = None,
             timeout: float = READ_HTTP_SECONDS) -> dict:
    """打一次接口。返回解析好的 JSON。

    4xx → `ApiRefused`(服务明确拒了这一条);5xx、连不上、读到一半断、回的不是
    JSON → `ApiDown`(写没写进去我们不知道)。**归一化只在这一层做**:四个端点
    (`/api/grab`、`/api/grab/state`、`/api/pipeline/status`、`/api/pipeline/run`)
    共用它,在某一个调用点补 catch 只能修那一处。
    """
    req = urllib.request.Request(api.rstrip('/') + path, method='GET')
    if payload is not None:
        req = urllib.request.Request(
            api.rstrip('/') + path, method='POST',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        # **错误回包的正文同样会断在半路**,所以 `e.read()` 也在保护里 ——
        # 不然 5xx 那一档(本该去核实事实)会变成另一个穿透的异常。
        try:
            raw = e.read()
        except _READ_BROKEN:
            raw = b''
        why = _detail_of(raw) or f'HTTP {e.code}'
        # **5xx 不是「这条没收下」**:服务端在保存之后的任何一步抛出,这边看到的
        # 都是 500,而那一行可能已经在库里了。归到通道那一档,由调用方去问事实。
        raise (ApiDown if e.code >= 500 else ApiRefused)(why) from None
    except _READ_BROKEN as e:
        why = str(getattr(e, 'reason', None) or e) or type(e).__name__
        if isinstance(e, http.client.IncompleteRead):
            # 这一句要人读得懂:底层那个 `IncompleteRead(12 bytes read, …)` 留在
            # 括号里给排查用,前面先说清发生了什么。
            why = f'回包读到一半断了({why})'
        raise ApiDown(why) from None
    try:
        return json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        # 回的不是 JSON = 服务那边不正常(反代插了一页、进程半死),同 5xx 一档。
        raise ApiDown('回的不是 JSON:' + body[:200].decode('utf-8', 'replace')) from None


def post_grab(api: str, url: str, refetch: bool, *, restore_only: bool = False,
              timeout: float = GRAB_HTTP_SECONDS) -> dict:
    return _request(api, '/api/grab', timeout=timeout,
                    payload={'url': url, 'refetch': bool(refetch),
                             'restore_only': bool(restore_only)})


def get_state(api: str, url: str, *, timeout: float = READ_HTTP_SECONDS) -> dict:
    return _request(api, '/api/grab/state?url=' + urllib.parse.quote(url, safe=''),
                    timeout=timeout)


def pipeline_idle(api: str, *, timeout: float = READ_HTTP_SECONDS) -> bool | None:
    """管道此刻空着吗。`None` = 问不出来(那时**不许**当成空着去起一轮)。

    `source=false` 的理由同 `kb.sh` 的停机保护:来源探测冷缓存时几十秒,而
    「在不在跑」本来一读内存就有。**它也吃等待预算** —— 轮询阶段的每一次请求都算
    在那个秒数里,漏掉一个就等于上限不成立。
    """
    try:
        return not _request(api, '/api/pipeline/status?source=false',
                            timeout=timeout).get('running')
    except RuntimeError:
        return None


def renew(api: str, it: dict, *, timeout: float) -> None:
    """让**服务端**按这一条自己的归属把后半条链补起来 —— 再 POST 一次 `/api/grab`。

    **不自己打 `/api/pipeline/run`。** 那条路要前端替后端决定「这一条属于哪个
    来源」,而写死 `inbox` 在跨来源的那一刻就错了:一条推文被「大V观点」收过、
    这一轮又没占到管道时,起一轮 `inbox` 的 export **一个字都不会动它**
    (`pipeline_stages._per_channel` 按通道筛;真库 + 真 `sync.export` 实测:
    `export('inbox')` 之后 `exported=false`,换 `export('social')` 才为 true)。
    服务端那条路本来就是**按条目**恢复的:`grab_api._duplicate` 现查一次归属、
    先把欠的那一步落实成 export 认得的形状(`reclaim_export` —— vault 里那一篇
    被移走而两个 hash 还相等时,补跑的 export 压根选不中它),再按 `seen['kind']`
    起那一轮。所以恢复的不变量留在服务端一处,这边不另写一份带固定来源的协议。

    **带 `restore_only`,这是构造上的那道约束。** `/api/grab` 的公开契约是「在就
    恢复、不在就抓一篇新的」,而「我刚才查到它还在」不是写入那一刻它还在的保证 ——
    不带这道约束的后果实测过:等待期间历史清理把那一行删掉之后,这一次重投原地
    **重新抓了一遍**、把一条清理掉的 `rss` 文章重建成 `inbox`、正文换成这次抓到
    的,而命令行照旧退出 0。带上它之后那条路径**压根不存在**(服务端 `known` 为空
    就直接 409),不是靠「再查一次 state」去赌那个窗口。
    **也不带 `refetch`** —— 恢复要补的是后半条链,不是把内容换掉。

    回包只用来看「起跑了没有」,**不覆盖 `it['res']`**:那里面存着本次动作的证据
    (比如一次成功替换的 `replaced`),覆盖掉就等于把它忘了。
    """
    it['renews'] += 1          # **预算在发请求之前扣**(见 MAX_RENEWS)
    it['renewed'] = post_grab(api, it['url'], False, restore_only=True, timeout=timeout)


# ------------------------------------------------------------------ 这一条怎么说

def _cut_at_unpaired(hit: str) -> str:
    """**不配对的右括号标志行文从那里开始** —— 在它那里切断。

    从左往右数深度,第一个让深度变负的右括号就是落单的那个:它前面是地址,它和它
    后面的都是外围写法(`（见 <地址>）以及别的` 的 `）以及别的`)。配得上对的一个
    都不动,所以 `…/a(b)` 和 `…/wiki/函数（数学）` 原样留着。
    """
    depth = dict.fromkeys((pair[0] for pair in _BRACKETS), 0)
    for i, ch in enumerate(hit):
        for open_, close in _BRACKETS:
            if ch == open_:
                depth[open_] += 1
            elif ch == close:
                if not depth[open_]:
                    return hit[:i]
                depth[open_] -= 1
    return hit


def _unwrap(hit: str) -> str:
    """剥掉地址右边那些属于**行文**的字符,保留属于**地址**的。(只在行内抽取时用)

    两件事交替做到不动为止(`…(见 <地址>).` 要先剥句号、再看括号配不配得上对):
    落单的右括号按 `_cut_at_unpaired` 切,外围标点按 `_TRAIL_PUNCT` 剥。
    """
    while True:
        before = hit
        hit = _cut_at_unpaired(hit).rstrip(_TRAIL_PUNCT)
        if hit == before:
            return hit


def links_in(text: str) -> list[str]:
    """**夹着叙述文字的那一行**里的链接,按出现顺序去重。

    只有 `_targets` 判出「这一行不是一条干净地址」时才走这里。**它是启发式** ——
    行内的边界本来就不确定,所以这一层宁可留下一个抓不到的坏地址,也不把地址截短
    成另一个**可能存在**的目标。
    """
    out: list[str] = []
    for hit in _URL_RE.findall(text or ''):
        hit = _unwrap(hit)
        if hit and hit not in out:
            out.append(hit)
    return out


def verdict(res: dict, state: dict | None, *, refetch: bool = False) -> tuple[str, str]:
    """这一条此刻的终态词 + 那句话。**纯函数**,所有输出都过它。

    **两种事实分开,不许互相顶替**:`res` 是**本次动作**的证据(换成了没有),
    `state` 是**库里那一篇此刻**的样子(在不在、看得出是正文吗、搜得到吗)——
    「那一篇目前存在且可检索」证明不了「本次替换完成」,旧那一版的质量也否定不了
    新那一版。档位和界面那一侧(`grab.js` 的 `grabCheckItem`)一一对应:
    - 内容本身不对排**最前面**:后面几步全做完也只是把一份看不出是正文的东西写
      进知识库(同 `owing`)。但**这一档只认两种依据** —— 本次明确没换成,或者
      `state` 说库里当前那一份不是正文;
    - `done` → 真写进去了(按**这一篇自己**的事实,不是按那一轮的阶段结果);
    - 库里有这一行、还没走完 → 如实说欠哪一步、下一步点哪儿;
    - 库里压根没有这一行 → 这才说得出「没存下来」。
    """
    if res.get('replaced') is False:
        # 重抓没换成(撞上关卡 / 还是确认不了 / 这期间别人动过它):不是终态,
        # 再来一次照旧会重抓。
        return 'bad_content', res.get('detail') or '这次重抓没换成'
    if state and state.get('known') and not state.get('content_ok'):
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
    if not state.get('known'):
        return 'failed', '库里没有这一行，这条没存下来；再投一次'
    owes = state.get('owing') or '还没写进知识库'
    return 'pending', owes + (state.get('resume') or '').replace('，', '；', 1)


def _settled(status: str) -> bool:
    """这一档还用等吗。

    等的只有两种:那一轮还没把它写进去(`pending`)、以及刚投出去还没核
    (`writing`)。**`unknown` 是终态** —— 它只由「回包丢了,而只读那一问答出的是
    库里旧那一版」产生,再等下去问到的还是同一个答案。
    """
    return status in ('saved', 'duplicate', 'bad_content', 'failed', 'unknown',
                      'not_tried')


def _left(deadline: float, cap: float) -> float:
    """这一次请求最多还能等多久 —— **剩余预算和单次上限取小的那个**。

    不传预算的话 `--timeout 1` 会在一条慢 state 上等满单次上限(15 秒),一批很多条
    还会累加:说出去的那个秒数就不是上限了(只有这一层能把它变成上限,因为超时是
    逐请求的)。

    **不抬底。** 抬到「至少 1 秒」等于预算耗尽之后每条还能再发一个一秒的请求,
    N 条就是 N 秒 —— 返回 0 或负数,调用方据此**不发**这一次请求。
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
        waiting = [it for it in items if not _settled(it['status'])]
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
            it['status'], it['detail'] = verdict(it['res'], fresh,
                                                 refetch=it['refetch'])
        waiting = [it for it in items if not _settled(it['status'])]
        if not waiting:
            return
        budget = _left(deadline, READ_HTTP_SECONDS)
        if budget <= 0:
            return
        if pipeline_idle(api, timeout=budget) is True:
            for it in waiting:
                if it['renews'] >= MAX_RENEWS:
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
            'refetch': bool(refetch), 'renews': 0}


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
        it['status'], it['detail'] = verdict({}, it['state'], refetch=it['refetch'])
        if it['status'] == 'pending':
            it['detail'] = '连接断在半路：' + it['detail']
        return
    except ApiRefused as e:
        # 4xx 那一句就是「为什么没收下 + 下一步」,原样交给用户。
        it['status'], it['detail'] = 'failed', str(e)
        return
    res = it['res']
    it['title'] = res.get('title') or ''
    # **顺序就是判据的优先级,不许按 `content_ok` 先判。** 重抓**换成了**那一档
    # (`_recover` 的成功分支)只更新 `replaced` / `done` / `title`,`content_ok`
    # 仍是**库里旧那一份**的判断 —— 先看它就会把一次成功的替换说成「看不出是
    # 正文」,而且不去等它写进知识库(用户那一次重抓于是永远停在「没换成」)。
    if res.get('replaced') is False:
        it['status'], it['detail'] = verdict(res, None)      # 明确没换成
    elif res.get('pipeline'):
        # started / busy:两档都要收敛(busy 那一档靠 `settle` 让服务端补一次)。
        # 重抓换成了的那一档也在这里 —— 它起的是同一条后半链。
        it['status'], it['detail'] = 'writing', res.get('detail') or ''
    elif res.get('content_ok') is False:
        # 内容不对、而且没起任何一轮:这一档要人再动一次手。
        it['status'], it['detail'] = verdict(res, None)
    else:
        # 没有 pipeline 这个字段 = 它早就走完整条链了(grab_api 的幂等那一档)。
        it['status'], it['detail'] = 'duplicate', res.get('detail') or ''


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
            'added': res.get('added', 0), 'duplicate': bool(res.get('duplicate')),
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
    ②**只有「这一行夹着叙述文字」才抽取**(`links_in`):剪贴板里常常是「看这篇
      https://… 挺好」。那一层是启发式,宁可留下一个抓不到的坏地址,也不截短成另一个
      可能存在的目标。
    """
    out: list[str] = []
    for raw in args.url:
        found = _from_stdin() if raw == '-' else [raw.strip()]
        for one in found:
            if one and one not in out:
                out.append(one)
    return out


def _from_stdin() -> list[str]:
    """标准输入:**逐行**判模式 —— 整行一条地址的保真,夹着叙述的才抽取。"""
    out: list[str] = []
    for line in sys.stdin.read().splitlines():
        line = line.strip()
        if not line:
            continue
        out.extend([line] if _WHOLE_LINE.match(line) else links_in(line))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog='kb.sh grab', description='手动收录:把链接抓进知识库(网页端那颗按钮的命令行版)')
    p.add_argument('url', nargs='+', metavar='链接',
                   help="要收录的链接,可给多条;`-` = 从标准输入读")
    p.add_argument('--api', default=API_DEFAULT, help=f'kb-web 地址(默认 {API_DEFAULT})')
    p.add_argument('--refetch', action='store_true',
                   help='库里已经有这一条时重抓一次,确认得了就把它换掉')
    p.add_argument('--state', action='store_true',
                   help='只查状态:不抓取、不起管道、不写一个字节')
    p.add_argument('--json', action='store_true', help='一行一条 JSON(给程序读)')
    p.add_argument('--no-wait', action='store_true',
                   help='不等「写进知识库」那一步,投完就返回')
    p.add_argument('--timeout', type=float, default=SETTLE_SECONDS,
                   help=f'等「写进知识库」最多多少秒(默认 {SETTLE_SECONDS:.0f})')
    # `kb.sh` 在起服务**之前**拿它校验一次参数:`--help`、拼错的选项、少给链接都
    # 不该先起一个守护进程(worktree 里那道闸门还会抢在前面,连帮助都拿不到)。
    # 这一档不读标准输入、不发一个请求。
    p.add_argument('--parse-only', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.parse_only:
        return 0

    urls = _targets(args)
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
    _emit(items, args.json)
    if broken:
        print(f'打不通 kb-web({args.api}):{broken}\n'
              '服务没起就 ./kb.sh start;起着的话看一眼 ./kb.sh logs', file=sys.stderr)
    bad = [it for it in items if it['status'] in ('failed', 'unknown', 'not_tried')]
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
