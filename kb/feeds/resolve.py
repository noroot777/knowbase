# -*- coding: utf-8 -*-
"""「这个域名住在哪」—— 可选地改问一台可信的 DoH 解析器,而不是本机那一台。

**为什么要有这一层**:本机解析可以是假的,而症状会伪装成对方的问题。实测形态、
三个解析器各自给出的假地址、以及同一时刻经代理拿到真地址后整篇正文都抓下来的
对照,全在 `docs/decisions/dns-egress.md`(**动这一族代码之前先读那一份**)。

**它只换「地址从哪来」这一步。** `fetching.public_addresses` 后面那几件事一个字都
没动:`is_global` 过滤、空集拒绝、`dict.fromkeys` 去重与顺序、钉数字 IP + `Host` +
`sni_hostname`。SSRF 防护因此一分不少 —— 实测「经代理 + 钉真实 IP + SNI」同样返回
200,代理和钉 IP 不冲突,今天挡住抓取的只是解析那一步给错了答案。

**默认关**(端点为空):关着的时候一次 DoH 请求都不发,走原来的
`socket.getaddrinfo`,行为逐字不变。**端点、预算、代理一律问 `kb.feeds.egress`** ——
那一份才知道「设置里填了什么、环境变量兜底是什么」,这里不许自己读环境变量。

**不是 DNS 名字的东西不许送进来问。** 主机名规范化(IP 字面量直接放过、非 ASCII 先
IDNA)在 `fetching.public_addresses` 里**一次做完**,两条路共用那一份 —— 不然开关一翻
行为就不一样:实测过 `https://1.1.1.1/rss` 被 DoH 答成 NXDOMAIN、报「无法解析订阅
服务器地址」,`https://münchen.de/rss` 被端点用 HTTP 400 拒掉、报「解析服务不可用」,
而关着开关时这两条都抓得到。**从「能」变成「不能」是回归,不是新能力。**

**不做「哪些域名走 DoH」的名单。** 污染随时变、形态也不只一种(同一轮里实测到
「指向别人家的公网地址」和「指向 127.0.0.1」两种),维护名单正是会静默过期的那类
东西 —— 漏一个域,症状就又变回一句看不出原因的超时。

**这一跳刻意不过 `public_addresses`**:用主机名 + 和抓取同一份出口配置
(`egress.client_kwargs()`,配了代理就显式走它、没配才交给 `trust_env`)让代理去解析
(CONNECT 带的是主机名),所以端点域名自己被污染也不受影响。放弃的那道 IP 校验在
这里没有威胁模型可言 —— 端点是用户在环境变量里写死的一个常量,不是订阅内容带进来
的地址。**反过来钉一边:订阅 URL 本身绝不许绕过 `public_addresses`。**
**已知的那一层限制**:没有代理时端点域名自己仍走本机解析,本机被污染时它也可能被
带偏(写在决策文档里)。

**答案当不可信输入解析**:`Status` 只认 0 和 3;`Answer` 只取 type 1/28,**CNAME 忽略
而不是当地址**;每条 `data` 必过一遍 `ipaddress.ip_address`。响应体有大小闸
(`MAX_BYTES`)—— 端点行为异常时不该把内存撑爆。

**「答得不对」绝不许说成「确定没有」**(元规则:判不准一律抛)。所以这三种都抛
`Unavailable` 而不是 `Unknown`:`Answer` 不是列表、**有地址记录但每条 data 都解析不动**、
以及任何一个族的预算没跑完。只有「拿到了明确答案、里面确实没有地址记录」才是
`Unknown` —— 把前者说成后者,就是替解析器断言一件我们并不知道的事,而面板上那句
「无法解析订阅服务器地址」会把人指向「改链接」。
反过来钉一边:**只有 CNAME 没有 A/AAAA 是正常答案**(只有 A 记录的域名查 AAAA 时常
如此),那一族返回空集、交给另一族,不算故障。

**缓存不是优化,是正确性**:`request` 一次最多 6 跳、每跳解析一次,`fulltext` 一轮
20 条跑在 3 个线程里 —— 不缓存就是每跳两次 HTTP 往返(A + AAAA),DoH 端点自己成了
瓶颈。缓存**按域名**(不是按 URL),所以「每跳都重新解析」那条不变量照旧成立:
重定向换了域名就是另一个键。**失败一律不进缓存** —— 一次网络抖动不该粘住十分钟。
并发只锁住查缓存和写缓存两下、不锁 HTTP 等待:同一域名撞上时最多多问一次,
而锁着网络等待会把三个工作线程串成一条。

**缓存的是「调用方留下来的那些」,不是端点给的原样**(`keep`)。顺序反过来实测过:
假端点先答 `127.0.0.1`、再改答公网地址,第二次照旧报「未解析到公网地址」——
被 SSRF 正确拒掉的那个答案进了缓存,把这个域名钉死十分钟。所以过滤和缓存的先后
锁在这一份里一处,SSRF 的口径仍然归 `fetching`(它把判据传进来)。

**预算由调用方给**(`seconds`),不自带一份:只有 `fetching` 那边的 `remaining()` 知道
这一次还剩多少、整轮采集的 `collect_deadline` 有没有更紧。自带一份的后果是单源那
20 秒之外又挂上最多 6 × 2 次 HTTP 往返,而面板上只显示「慢」。

**两种答不上要分开说**(`Unavailable` / `Unknown`):前者是**我们这一侧**没出口(端点
连不上、代理没配、SERVFAIL、答得不对),后者是**对方明确答了**这个域名没有地址。
合成一档就是把「我这边坏了」说成「你的链接写错了」。面板文案统一由 `fetching` 给,
这里只给类型和一句具体原因 —— 那几句话每句只有一个出处,而原因要指得到该改什么。
"""
from __future__ import annotations

import ipaddress
import threading
import time
from typing import Callable, Iterable

import httpx

from kb.feeds import egress

#: 响应体上限。DNS JSON 正常是几百字节;给够余量,又不让异常端点撑爆内存。
MAX_BYTES = 256 * 1024
#: 缓存按答案自己的 TTL 过期,夹在这两条线之间(对方给的极端值不照抄)。
TTL_MIN, TTL_MAX = 60.0, 600.0
_A, _AAAA = 1, 28

_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
_lock = threading.Lock()


class Unavailable(Exception):
    """**我们这一侧没出口,或者对方答得不对**:连不上、超时、代理没配、SERVFAIL、
    不是 DNS JSON、地址记录解析不动、预算没跑完。一律**不许**当成「这个域名不存在」。"""


class Unknown(Exception):
    """**对方明确答了**:这个域名没有地址记录(NXDOMAIN,或两个族都只有 CNAME)。"""


def forget() -> None:
    """清缓存。给测试,也给「换了端点想立刻生效」留一个出口。"""
    with _lock:
        _cache.clear()


#: 面板上那几颗一键填入的端点。**只许放「实测能用」的** —— 判据不是「这家有没有
#: DoH」,是「它认不认下面 `_read` 发的这一种请求」:`?name=&type=` 的查询串 +
#: `accept: application/dns-json`(Google 那边叫 JSON API)。DoH 还有另一种协议
#: RFC8484 wireformat(`?dns=<base64 的 DNS 报文>`),**这一层一个字都不会说**。
#: 两者走同一个 `/dns-query` 路径、长得一模一样,所以「看着像个 DoH 地址」不是判据:
#: OpenDNS 的 `https://doh.opendns.com/dns-query` 只认 wireformat,被当成预设发出去
#: 当场 HTTP 400,而面板上只剩一句「域名解析服务不可用」—— 人会以为是自己网络的事。
#:
#: **名单待在这儿、不在前端**:协议归这一份管(`_read` 就在下面),前端抄一份的话,
#: 哪天这里换了请求形式,那边还按老样子推荐。页面通过 `egress.view()['presets']` 拿,
#: 回归 `tests/test_feed_egress.py` 钉着「`egress.js` 里不许再出现写死的端点地址」。
JSON_API_PRESETS = (
    {'name': 'Cloudflare', 'url': 'https://cloudflare-dns.com/dns-query'},
    {'name': 'Google', 'url': 'https://dns.google/resolve'},
    {'name': '关掉', 'url': ''},
)


def _read(client: httpx.Client, url: str, host: str, rtype: int):
    """发一次请求并读回响应体,**读到上限就停**。"""
    with client.stream('GET', url, params={'name': host, 'type': rtype},
                       headers={'accept': 'application/dns-json'}) as response:
        if 300 <= response.status_code < 400:
            # 端点写成 http:// 时常见。单独说一句 —— 「我们没出口」指不到该改什么。
            raise Unavailable(f'HTTP {response.status_code}:端点要写成 https 的直连地址')
        if response.status_code != 200:
            raise Unavailable(f'HTTP {response.status_code}')
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise Unavailable(f'响应超过 {MAX_BYTES} 字节')
            chunks.append(chunk)
    return b''.join(chunks)


def _ask(url: str, host: str, rtype: int, seconds: float) -> tuple[list[str], list[float]]:
    """问一次(一种记录类型),返回 `(地址串, 各条 TTL)`。答不上一律抛 `Unavailable`。"""
    import json
    try:
        with httpx.Client(follow_redirects=False, timeout=seconds,
                          **egress.client_kwargs()) as client:
            raw = _read(client, url, host, rtype)
        answer = json.loads(raw)
    except Unavailable:
        raise
    except Exception as exc:  # 连不上、超时、不是 JSON —— 一律「没答上」,不是「没有这个域名」
        raise Unavailable(str(exc) or type(exc).__name__) from None
    if not isinstance(answer, dict):
        raise Unavailable('响应不是 DNS JSON')
    status = answer.get('Status')
    if status == 3:  # NXDOMAIN:对方明确说没有这个名字
        return [], []
    if status != 0:  # SERVFAIL / REFUSED 这些说的是解析器自己的事
        raise Unavailable(f'DNS Status {status}')
    records = answer.get('Answer')
    if records is None:
        # **只有「这一栏根本没出现」才是合法的空答案。** 不许写成 `or []` ——
        # 那样 `{}`、`""`、`0` 这些**出现了、但不是列表**的值会一起被当成空,
        # 直接绕过下面那道判断(实测:四种畸形值全都报成「无法解析订阅服务器
        # 地址」,人被指去改订阅链接,而坏的是解析端点)。
        records = []
    if not isinstance(records, list):
        # **「答案栏目不是个列表」是答得不对,不是「这个域名没有地址」。** 写这一份时
        # 实测到的那一条:`{"Status": 0, "Answer": "nonsense"}` 被 for 当成字符串逐字
        # 迭代、每个字符都不是 dict、于是一条不剩,末态报成「无法解析订阅服务器地址」。
        raise Unavailable('Answer 不是记录列表')
    found: list[str] = []
    ttls: list[float] = []
    address_records = 0
    for record in records:
        if not isinstance(record, dict):
            # **列表里混着不是记录的东西,同样是答得不对。** 跳过它等于替端点断言
            # 「这里没有地址」,而我们并不知道那一条原本是什么(实测
            # `{"Status": 0, "Answer": ["x"]}` 一路报成「无法解析订阅服务器地址」)。
            raise Unavailable('Answer 里混着不是记录的东西')
        rtype = record.get('type')
        if not isinstance(rtype, int) or isinstance(rtype, bool):
            # **记录自己的 type 认不出来,也是答得不对。** 这一条不能写成 `continue`:
            # 那等于把「这条记录是什么我不知道」说成「这条记录不是地址」,而末态是
            # 「这个域名没有地址」—— 人被指去改一条没毛病的链接(实测:`[{}]` 和
            # `{"type": "1"}` 都报成「无法解析订阅服务器地址」)。
            # **bool 要单独挡掉**:Python 里 `True == 1`、`1.0 == 1`,写成
            # `rtype not in (_A, _AAAA)` 的话 `type: true` / `type: 1.0` 反而**穿过去
            # 当成 A 记录用了**(同一处实测到的另一头)。
            raise Unavailable(f'Answer 记录的 type 不是整数:{rtype!r}')
        if rtype not in (_A, _AAAA):
            continue  # CNAME(5)、SOA 等一律忽略,不许当地址
        address_records += 1
        try:
            address = ipaddress.ip_address(str(record.get('data', '')).strip())
        except ValueError:
            continue  # 畸形的那一条跳过,不带掉整轮
        found.append(str(address))
        ttl = record.get('TTL')
        if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0:
            ttls.append(float(ttl))
    if address_records and not found:
        # **有地址记录、但每条 data 都解析不动** —— 这是答得不对。说成「这个域名没有
        # 地址」就是替它断言我们并不知道的事(实测:`data: nonsense` 一路报成
        # 「无法解析订阅服务器地址」)。注意和「只有 CNAME」分开:那种 address_records
        # 本来就是 0,照旧返回空集交给另一个族。
        raise Unavailable('地址记录都解析不动')
    return found, ttls


def addresses(host: str, *, seconds: float,
              keep: Callable[[Iterable[str]], list[str]]) -> list[str]:
    """这个域名**留得下来**的地址(`keep` 就是调用方那道判据,见模块 docstring)。"""
    url = egress.endpoint()
    key = (url, host)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return list(hit[1])
    stop = now + max(0.0, seconds)
    single = egress.seconds()
    found: list[str] = []
    ttls: list[float] = []
    why: list[str] = []
    answered = 0
    # A 在前:`request` 是按顺序试、连不上才换下一个,而 IPv4 的可用面更大。
    for rtype in (_A, _AAAA):
        budget = stop - time.monotonic()
        if budget <= 0:
            why.append('预算用尽')
            continue
        try:
            got, ttl = _ask(url, host, rtype, min(single, budget))
        except Unavailable as exc:
            # **原因留着带回去。** 吞掉它的后果实测过:HTTP 400(中文域名)、
            # HTTP 301(端点写成了 http://)、响应超限、超时在面板上长**同一句**
            # 「解析服务不可用」,指不到该改什么 —— 而这几种要改的东西完全不同。
            why.append(str(exc))
            continue
        answered += 1
        found += got
        ttls += ttl
    if not found:
        # **先看有没有地址,再谈够不够格说「没有」。** 两件事别合成一条:
        # - 拿到地址了就用,哪怕只问到一个族(另一族连不上是常事,钉在
        #   `test_one_family_failing_does_not_discard_what_the_other_answered`);
        # - **一个地址都没有时,两个族都拿到明确答案才有资格说「这个域名没有地址」**。
        #   少问到一个族就断言没有,是替解析器断言一件我们并不知道的事 —— 没问到的
        #   那一半可能正好有(实测形态:A 族回 NXDOMAIN、AAAA 族被端点 400 拒掉,
        #   原先一路报成「无法解析订阅服务器地址」,把人指向「改一条没毛病的链接」)。
        if answered < 2:
            raise Unavailable('；'.join(why) or '端点一个族都没答上')
        raise Unknown('解析器说这个域名没有地址')
    kept = keep(found)
    if kept:
        ttl = min(ttls) if ttls else TTL_MIN
        with _lock:
            _cache[key] = (time.monotonic() + min(max(ttl, TTL_MIN), TTL_MAX), tuple(kept))
    return kept
