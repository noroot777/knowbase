# -*- coding: utf-8 -*-
"""「这个域名住在哪」—— 可选地改问一台可信的 DoH 解析器,而不是本机那一台。

**为什么要有这一层**:本机解析可以是假的,而症状会伪装成对方的问题。实测形态、
三个解析器各自给出的假地址、以及同一时刻经代理拿到真地址后整篇正文都抓下来的
对照,全在 `docs/decisions/dns-egress.md`(**动这一族代码之前先读那一份**)。

**它只换「地址从哪来」这一步。** `fetching.public_addresses` 后面那几件事一个字都
没动:`is_global` 过滤、空集拒绝、`dict.fromkeys` 去重与顺序、钉数字 IP + `Host` +
`sni_hostname`。SSRF 防护因此一分不少 —— 实测「经代理 + 钉真实 IP + SNI」同样返回
200,代理和钉 IP 不冲突,今天挡住抓取的只是解析那一步给错了答案。

**默认关**(`KB_FEED_DOH` 空):关着的时候一次 DoH 请求都不发,走原来的
`socket.getaddrinfo`,行为逐字不变。

**不做「哪些域名走 DoH」的名单。** 污染随时变、形态也不只一种(同一轮里实测到
「指向别人家的公网地址」和「指向 127.0.0.1」两种),维护名单正是会静默过期的那类
东西 —— 漏一个域,症状就又变回一句看不出原因的超时。

**这一跳刻意不过 `public_addresses`**:用主机名 + `trust_env=True` 让代理去解析
(CONNECT 带的是主机名),所以端点域名自己被污染也不受影响。放弃的那道 IP 校验在
这里没有威胁模型可言 —— 端点是用户在环境变量里写死的一个常量,不是订阅内容带进来
的地址。**反过来钉一边:订阅 URL 本身绝不许绕过 `public_addresses`。**

**答案当不可信输入解析**:`Status` 只认 0 和 3;`Answer` 只取 type 1/28,**CNAME 忽略
而不是当地址**;每条 `data` 必过一遍 `ipaddress.ip_address`,畸形的那一条**跳过**而
不是带掉整轮。所以下游拿到的全是解析得动的地址串。

**缓存不是优化,是正确性**:`request` 一次最多 6 跳、每跳解析一次,`fulltext` 一轮
20 条跑在 3 个线程里 —— 不缓存就是每跳两次 HTTP 往返(A + AAAA),DoH 端点自己成了
瓶颈。缓存**按域名**(不是按 URL),所以「每跳都重新解析」那条不变量照旧成立:
重定向换了域名就是另一个键。**失败一律不进缓存** —— 一次网络抖动不该粘住十分钟。
并发只锁住查缓存和写缓存两下、不锁 HTTP 等待:同一域名撞上时最多多问一次,
而锁着网络等待会把三个工作线程串成一条。

**预算由调用方给**(`seconds`),不自带一份:只有 `fetching` 那边的 `remaining()` 知道
这一次还剩多少、整轮采集的 `collect_deadline` 有没有更紧。自带一份的后果是单源那
20 秒之外又挂上最多 6 × 2 次 HTTP 往返,而面板上只显示「慢」。

**两种答不上要分开说**(`Unavailable` / `Unknown`):前者是**我们这一侧**没出口(端点
连不上、代理没配、SERVFAIL),后者是**对方明确答了**这个域名没有地址。合成一档就是
把「我这边坏了」说成「你的链接写错了」。面板文案统一由 `fetching` 给,这里只给类型 ——
那几句话每句只有一个出处。
"""
from __future__ import annotations

import ipaddress
import os
import threading
import time

import httpx

#: DoH 端点。空 = 关(走 `socket.getaddrinfo`)。
ENV = 'KB_FEED_DOH'
#: 单次查询最多等多久。整次解析的上限是它和调用方给的 `seconds` 取小。
BUDGET = float(os.environ.get('KB_FEED_DOH_SECONDS', '8'))
#: 缓存按答案自己的 TTL 过期,夹在这两条线之间(对方给的极端值不照抄)。
TTL_MIN, TTL_MAX = 60.0, 600.0
_A, _AAAA = 1, 28

_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
_lock = threading.Lock()


class Unavailable(Exception):
    """**我们这一侧没出口**:端点连不上、超时、代理没配、SERVFAIL、答案不是 DNS JSON。"""


class Unknown(Exception):
    """**对方明确答了**:这个域名没有地址记录(NXDOMAIN,或只有 CNAME 没有 A/AAAA)。"""


def endpoint() -> str:
    """每次读环境变量,不在 import 那一刻固化 —— 测试要能翻开关,运维也才能重启生效。"""
    return os.environ.get(ENV, '').strip()


def configured() -> bool:
    return bool(endpoint())


def forget() -> None:
    """清缓存。给测试,也给「换了端点想立刻生效」留一个出口。"""
    with _lock:
        _cache.clear()


def _ask(url: str, host: str, rtype: int, seconds: float) -> tuple[list[str], list[float]]:
    """问一次(一种记录类型),返回 `(地址串, 各条 TTL)`。答不上一律抛 `Unavailable`。"""
    try:
        with httpx.Client(trust_env=True, follow_redirects=False, timeout=seconds) as client:
            response = client.get(url, params={'name': host, 'type': rtype},
                                  headers={'accept': 'application/dns-json'})
        if response.status_code != 200:
            raise Unavailable(f'HTTP {response.status_code}')
        answer = response.json()
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
    records = answer.get('Answer') or []
    if not isinstance(records, list):
        # **「答案栏目不是个列表」是答得不对,不是「这个域名没有地址」。** 写这一份时
        # 实测到的那一条:`{"Status": 0, "Answer": "nonsense"}` 被 for 当成字符串逐字
        # 迭代、每个字符都不是 dict、于是一条不剩,末态报成「无法解析订阅服务器地址」——
        # 那是在替对方断言一件我们并不知道的事(元规则:判不准一律抛,不许返回「确定没有」)。
        raise Unavailable('Answer 不是记录列表')
    found: list[str] = []
    ttls: list[float] = []
    for record in records:
        if not isinstance(record, dict) or record.get('type') not in (_A, _AAAA):
            continue  # CNAME(5) 等一律忽略,不许当地址
        try:
            address = ipaddress.ip_address(str(record.get('data', '')).strip())
        except ValueError:
            continue  # 畸形的那一条跳过,不带掉整轮
        found.append(str(address))
        ttl = record.get('TTL')
        if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0:
            ttls.append(float(ttl))
    return found, ttls


def addresses(host: str, *, seconds: float) -> list[str]:
    """这个域名的地址串(**未过滤** —— `is_global` 那一道在 `fetching.public_addresses`)。"""
    url = endpoint()
    key = (url, host)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return list(hit[1])
    stop = now + max(0.0, seconds)
    found: list[str] = []
    ttls: list[float] = []
    answered = exhausted = 0
    # A 在前:`request` 是按顺序试、连不上才换下一个,而 IPv4 的可用面更大。
    for rtype in (_A, _AAAA):
        budget = stop - time.monotonic()
        if budget <= 0:
            exhausted += 1
            continue
        try:
            got, ttl = _ask(url, host, rtype, min(BUDGET, budget))
        except Unavailable:
            continue
        answered += 1
        found += got
        ttls += ttl
    if found:
        ttl = min(ttls) if ttls else TTL_MIN
        with _lock:
            _cache[key] = (time.monotonic() + min(max(ttl, TTL_MIN), TTL_MAX), tuple(found))
        return found
    if exhausted or not answered:
        raise Unavailable('域名解析服务没有响应')
    raise Unknown('解析器说这个域名没有地址')
