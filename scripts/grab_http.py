# -*- coding: utf-8 -*-
"""手动收录那条命令行入口的**通道层**:打那几个接口、把失败分成两档。

从 `grab_cli.py` 拆出来的(单文件要过 700 行的上限),拆的是**纯 IO 这一块** ——
关于收录的判断一个字都不在这里(那些在服务端 `kb/web/grab_api.py`,收敛在
`grab_cli.py`)。

**这一层唯一的职责是把失败分成两档,因为它们的下一步完全不同:**
- `ApiRefused`(4xx):服务明确拒了,**一个字节都没写**(那八道准入排在写库之前)。
- `ApiDown`(5xx / 连不上 / 读到一半断 / 回的不是 JSON):**写没写进去我们不知道**
  —— 保存排在起管道之前,断线可能发生在它之后。

**归一化只在 `_request` 这一层做。** 四条路共用它,在某一个调用点补 catch 只能
修那一处。纯标准库(同 `grab_cli.py`:这条路不该因为 venv 里缺什么而用不了)。
"""
from __future__ import annotations

import http.client
import json
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


class ApiDown(RuntimeError):
    """**打不通 kb-web**(连不上、超时、或者它自己 5xx 了)。

    **和「这条没收下」严格分开**:它说的是通道,不是这条链接 —— 保存排在起管道
    之前,所以这一档里内容**可能已经存下了**。谎称「没收下」会让用户去别处找原因
    (口径同 `grab.js` 那段 catch:回包没送达 ≠ 后端没存下来)。
    """


class ApiRefused(RuntimeError):
    """服务**明确拒绝**了这一条(4xx)。消息就是「为什么没收下 + 下一步」。

    只有这一档说得出「没收下」:后端那八道准入是在写库之前过的,拒了就一个字节
    都没写。5xx 不算 —— 那时写没写进去我们不知道(见 `ApiDown`)。

    **带上状态码**:404(这台服务压根没有这条路 —— 它比代码旧)和 409(有这条路、
    但此刻没有可恢复的条目)要分得开,前者的下一步是重启服务,后者是重投。
    """

    def __init__(self, why: str, code: int = 0):
        super().__init__(why)
        self.code = code


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
    JSON → `ApiDown`(写没写进去我们不知道)。**归一化只在这一层做**:四条路
    (`/api/grab`、`/api/grab/restore`、`/api/grab/state`、`/api/pipeline/status`)
    共用它,在某一个调用点补 catch 只能修那一处。**这条路不打
    `/api/pipeline/run`** —— 起哪一轮由服务端按条目的归属定(见 `grab_cli.renew`)。

    **请求**构造和发送那一步也在保护里。地址本身的毛病该由 `bad_api` 在参数层
    拦掉(那才有「退出 2 + 一句人话」这个正确出口),这一道是**兜底** —— 它挡的
    是那层没想到的组合,挡不住的后果是满屏 traceback、stdout 一个字都没有。
    归到 `ApiDown` 而不是 `ApiRefused`:请求没发出去,但「库里那一行是什么样」
    照旧该去问一次,不许就此断言「这条没收下」。
    """
    try:
        base = normalize_api(api)
        req = urllib.request.Request(base + path, method='GET')
        if payload is not None:
            req = urllib.request.Request(
                base + path, method='POST',
                data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'})
    except (UnicodeError, ValueError, TypeError) as e:
        raise ApiDown(f'这个地址组不成一个请求({e})') from None
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
        if e.code >= 500:
            raise ApiDown(why) from None
        raise ApiRefused(why, e.code) from None
    except UnicodeError as e:
        # **发送那一步也会因为编码炸**(请求行是 ASCII 的)。同上:归通道那一档。
        raise ApiDown(f'这个地址发不出去({e})') from None
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


#: 四个端点各自**承重**的那几个键和它们的类型 —— 少一个、或者类型不对,这份回包就
#: 下不了业务结论(见 `_checked`)。取的是端点**所有分支都给**的那几个:`/api/grab` 的
#: 三个出口(新收录、幂等、受控重抓)和 `/api/grab/state` 的两档(在库里 / 不在)都齐。
_GRAB_NEED = (('url', str), ('added', int), ('duplicate', bool),
              ('done', bool), ('content_ok', bool))
_STATE_NEED = (('url', str), ('known', bool), ('done', bool), ('content_ok', bool))
_RUN_NEED = (('running', bool),)
#: **出现时才校验的那几个** —— 它们只在某些分支带,但**一旦带就必须是那个类型**。
#: 清单按「归并层**实际消费**哪几个键」列,不是只列「下业务结论」的那几个:
#: - `replaced` 要真布尔 —— 归并层对它做三态判断(`is False` / `is not None`),
#:   一个字符串 `"false"` 会被当成「做过替换尝试、只是没换成」,那是另一个结论。
#: - **剩下那几个是拼在人读的那句话里的**,所以同样要校验。漏掉它们的形态实测过:
#:   承重字段齐全、类型也对,只有 `owing` 是个数字(或 `resume` 是个列表),
#:   `--state` 就在**第一条**上抛 `TypeError` / `AttributeError`,整批到不了输出 ——
#:   后面几条连一行结果都没有,而「一条输入一行结果」本来是这条路的承诺。
#:   **不许只在拼接处补 `str()` 兜底**:那是把「这个回包可信吗」这一问散到每个
#:   拼接点上各判一遍(元规则「口径只有一份」),而判不准该归 `ApiDown`。
#:
#: **`pipeline` / `exported` / `stored` / `indexed` 故意不在这儿。** 前者只被当真值
#: 读(类型不对也只是多去问一次事实,那一问会给出真结论);后三个原样进 `--json`、
#: 不参与任何运算,而它们在库里是 0/1,校验成布尔反而会把**正常回包**判成坏的 ——
#: 误判的代价是整批停在「结果不确定」,比漏校验这三个重得多。
_TEXT_MAYBE = (('detail', str), ('title', str), ('kind', str), ('owing', str),
               ('resume', str))
_GRAB_MAYBE = (('replaced', bool),) + _TEXT_MAYBE
_STATE_MAYBE = _TEXT_MAYBE


def _typed(value, kind) -> bool:
    """这个值是不是那个类型。**`isinstance` 在这儿不够**:`True` 是 `int` 的实例,
    而 `added: true` 不是一个计数;反过来 `1` 也不是一个「是/否」。
    """
    if kind is bool:
        return value is True or value is False
    if kind is int:
        return type(value) is int
    return isinstance(value, kind)


def _checked(data, why: str, need=(), maybe=(), *, ok: bool = True) -> dict:
    """回包得是**这个服务对这条路径**的回答,不只是一段合法 JSON。

    **语法有效 ≠ 接口有效,「是个对象且 ok=true」也还不够。** 缺了承重的键时
    `.get()` 一路拿到 `None`,而每一档都读得下去 —— 于是一个不相干的服务替我们
    发布了结论。实测形态(`--api` 指错地方:反代插了一页、另一个应用、异常网关):
    `[]` / `null` 让 CLI 当场抛 `AttributeError`(同一批**第二条压根没被处理**);
    `{"ok": false}`、以及**字段齐不齐都不看**的那一版下 `{"ok": true}` 和
    `{"ok": true, "duplicate": false, "done": false}`,都让两条输入被说成「早就在
    知识库里了」而 `done=false`,一次核实都没发生;`{"ok": true, "known": true}`
    被说成「看不出是正文」,`{"ok": true, "known": null}` 被说成「还没收进来」——
    后者是**「确定没有」**这个结论,照它重投会原地再抓一遍。

    所以**判不准一律归到 `ApiDown`**(通道这一档会去问事实、如实停在 `unknown`),
    不是在各个调用点补 `.get` 兜底 —— 那是同一个问题在四处各判一遍。

    `ok=False` 给 `/api/pipeline/status` 用:那条路的回包是管道状态本体,没有 `ok`
    这个键(`kb/web/pipeline_api.py`),所以它只校验自己要读的那几个键。
    """
    bad = ''
    if not isinstance(data, dict):
        bad = '不是一个对象'
    elif ok and data.get('ok') is not True:
        bad = '没有 ok=true'
    else:
        for key, kind in need:
            if not _typed(data.get(key), kind):
                bad = f'{key} 这个键缺了、或者类型不对'
                break
        else:
            for key, kind in maybe:
                if key in data and not _typed(data[key], kind):
                    bad = f'{key} 这个键的类型不对'
                    break
    if bad:
        raise ApiDown(f'{why}回的不像这个服务的回答({bad}):'
                      + repr(data)[:200].replace('\n', ' '))
    return data


def bad_api(api: str) -> str:
    """`--api` 给的不是一个打得通的地址时,那句话(没问题就空串)。

    **在参数层拦掉,不要等到发请求。** `urllib.request.Request('not-a-url/api/grab')`
    抛的是 `ValueError: unknown url type`,而它发生在 `_request` 那套归一化**之前**
    —— 于是一个打错的参数会变成满屏 traceback、stdout 一个字都没有(实测
    `--api not-a-url --json --state …`:退出 1、stdout 空、stderr 是 traceback)。
    那是**参数错**,不是「投出去了而结果未知」,所以调用方按参数错退出(`return 2`),
    一个请求都不发、也不去核实任何事实。

    只看「打得通吗」这几件事:**整串能编码成 URI**、协议是 http(s)、有主机名、
    端口是数字、**没有查询串和锚点**。**不判它通不通** —— 服务没起是另一回事,
    那一档归 `ApiDown`(那句话会说去 `./kb.sh start`)。

    **查询串和锚点要在这儿明确拒掉,因为再往下这一层拼不出正确的路径。**
    `_request` 是 `base + path`(四条路共用),所以
    `--api http://127.0.0.1:8787?token=abc` 实发的是 `POST /?token=abc/api/grab`、
    带 `#sources` 时实发 `POST /` —— 两种都不是 `/api/grab`,而服务**真的收到了
    一个请求**,只是打在别的路径上(实测)。那不是「投出去了而结果未知」,是参数
    一开始就拼不出我们要打的那条路:**按参数错退出 2、一个请求都不发**。
    (路径前缀照旧支持 —— 反代挂在 `/kb` 下面是正常用法,`normalize_api` 去掉尾
    斜杠之后 `base + '/api/grab'` 拼出来就是对的。)

    **「能编码成 URI」这一条和另外三条一样要在这儿拦。** 请求行是 ASCII 的,
    所以地址里带没编码的非 ASCII 字符(`http://127.0.0.1:8787/知识`、IDN 主机名)
    会在 urllib 组装请求那一步抛 `UnicodeEncodeError` —— 它和 `ValueError` 一样
    发生在 `_request` 那套归一化**之前**(实测:退出 1、stdout 一个字没有、stderr
    是 traceback,本地服务压根没收到请求)。**这里不替用户编码** —— 改写他给的
    地址等于替他决定打到哪儿去,而 `%E7%9F%A5%E8%AF%86` 和 `知识` 在反代后面
    可能是两条路由。
    """
    # **`urlsplit` 自己也会抛**:不完整/非法的 IPv6 写法(`http://[::1`)当场
    # `ValueError: Invalid IPv6 URL` —— 所以它和下面取端口那一步一起包起来,
    # 这一层对「打错的地址」只有一个出口:一句话,不是 traceback。
    try:
        api.encode('ascii')
    except UnicodeEncodeError:
        return ('--api 里有没编码的非 ASCII 字符（请求行只能是 ASCII），'
                f'先按百分号编码写出来:{api!r}')
    try:
        parts = urllib.parse.urlsplit(api)
        host, scheme = parts.hostname, parts.scheme
        _ = parts.port                     # 端口不是数字时这一步才抛
    except ValueError as e:
        return f'--api 不是一个能打的地址({e}):{api!r}'
    if scheme not in ('http', 'https') or not host:
        return ('--api 要写成 http://主机:端口 这种形式(协议名不能省)，'
                f'收到 {api!r}')
    if parts.query or parts.fragment:
        return ('--api 不能带查询串或锚点(接口路径会拼在它后面，拼出来就不是'
                f'我们要打的那一条了):{api!r}')
    return ''


def normalize_api(api: str) -> str:
    """`--api` 收到的那一串 → **真正会被请求的那个地址**。协议名小写、去掉尾斜杠。

    **装配口径只有一份,而且必须是「打出去的那一份」。** `urlsplit` 认得
    `HTTP://…`(协议名按 RFC 3986 不分大小写,urllib 自己也照打),于是
    `--parse-only` 回传原串的那一版下,外壳那道 `case ... in http://*|https://*`
    压根不命中 —— 它于是当成「目标是本机那份服务」,**多起一份用户不需要的守护
    进程**(worktree 里更糟:那道闸门会抢在前面 die,连用户给的地址都没碰过)。
    实测 `HTTP://127.0.0.1:1` 和 `HtTp://127.0.0.1:1` 都真的起了一份 kb-web。
    出路是在这儿规范化一次、`_request` 和 `--parse-only` 都用它,**不是在外壳里
    多加几个大小写分支**。
    """
    parts = urllib.parse.urlsplit(api)
    # **查询串和锚点不带进来**:`_request` 是 `base + path`,带着它们拼出来的是
    # 另一条路径(见 `bad_api` 里那一段)。`bad_api` 已经把这种参数拦在外面,
    # 这里一并落实 —— **这一份是「真正会被请求的那个地址」,它不该有拼不出
    # 正确路径的成分**(两处同一个判据,少了这一处的话某个绕过参数校验的调用方
    # 又会拿到一个拼坏的 base)。
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc, parts.path.rstrip('/'), '', ''))


def post_grab(api: str, url: str, refetch: bool, *,
              timeout: float = GRAB_HTTP_SECONDS) -> dict:
    """投一条:**在就查重/恢复,不在就抓一篇新的** —— `/api/grab` 的公开契约。

    「只恢复、不许创建」那一档**不在这里加字段**,它是 `post_restore` 那条独立
    路径(理由见那边和 `kb.web.grab_api.RestoreRequest`)。
    """
    return _checked(_request(api, '/api/grab', timeout=timeout,
                             payload={'url': url, 'refetch': bool(refetch)}),
                    '收录', _GRAB_NEED, _GRAB_MAYBE)


def post_restore(api: str, url: str, *, timeout: float = GRAB_HTTP_SECONDS) -> dict:
    """求服务端「只把这一条欠的那一步补上」——`POST /api/grab/restore`。

    **这是一条独立路径,不是 `/api/grab` 上的一个字段**,理由在
    `kb.web.grab_api.RestoreRequest` 的说明里:字段会被**更新了文件却没重启**的那个
    kb-web 静默忽略,于是降级成「不在就抓一篇新的」。独立路径在那种服务上回 404,
    这边据此停下来说一句(见 `renew`)。
    """
    return _checked(_request(api, '/api/grab/restore', timeout=timeout,
                             payload={'url': url}), '恢复', _GRAB_NEED, _GRAB_MAYBE)


def get_state(api: str, url: str, *, timeout: float = READ_HTTP_SECONDS) -> dict:
    """库里那一篇此刻的样子。**`known` 这个键是「在不在」的唯一依据**,所以它缺了
    不许当成「不在」—— 那一档说的是「这条没存下来；再投一次」(见 `verdict`),
    照它重投会原地再抓一遍。缺键归 `ApiDown`:那一档只会如实说问不出来。
    """
    return _checked(
        _request(api, '/api/grab/state?url=' + urllib.parse.quote(url, safe=''),
                 timeout=timeout), '查状态', _STATE_NEED, _STATE_MAYBE)


def pipeline_idle(api: str, *, timeout: float = READ_HTTP_SECONDS) -> bool | None:
    """管道此刻空着吗。`None` = 问不出来(那时**不许**当成空着去起一轮)。

    `source=false` 的理由同 `kb.sh` 的停机保护:来源探测冷缓存时几十秒,而
    「在不在跑」本来一读内存就有。**它也吃等待预算** —— 轮询阶段的每一次请求都算
    在那个秒数里,漏掉一个就等于上限不成立。
    """
    try:
        # **`running` 缺了不许当成「空着」** —— `.get('running')` 拿到 `None` 之后
        # `not None` 是真,于是一个不相干的回包会让我们去起一轮同步。
        return not _checked(_request(api, '/api/pipeline/status?source=false',
                                     timeout=timeout),
                            '查管道', _RUN_NEED, ok=False)['running']
    except RuntimeError:
        return None
