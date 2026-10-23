# -*- coding: utf-8 -*-
"""出口配置:抓取走哪个代理、域名改问谁。**这一族的口径只有这一份。**

为什么要这一块,见 `docs/decisions/dns-egress.md`(**动这一族之前先读那一份**)。

**设置优先,环境变量兜底。** 这是仓库主人定的方向(demo 里的 B):页面是正经入口,
「看得见的就是生效的」;环境变量只在设置里没填时起作用,给容器和 CI 留一条路
(那边没人去点页面)。**注意这和 `kb.llm` 的 key / base_url 是反的** —— 那边是环境
变量压过文件。两处方向不同是知情的:key 那条路上环境变量常由运维注入、不该被一份
文件悄悄改掉;而出口这几项是人在页面上按网络状况随手调的东西,页面说了不算就等于
这个入口是假的。

**每次取值都读一遍盘,不在进程里固化。** 这是「保存即生效」(demo 里的 C)的全部
实现:改完下一条抓取就用新端点,不用重启。代价是一轮抓取会多出几十次几 KB 的文件
读 —— 和一次 DNS 往返比可以忽略,而且 `kb.llm.get_key` 一直是这么做的。
**保存那一侧要顺手清解析缓存**(`invalidate()`):不清的话旧答案最多还粘 10 分钟,
人会以为新端点没生效。

**代理配了就显式传给 httpx,不再看环境**(`client_kwargs()`):httpx 里显式 `proxy`
压过 `trust_env`,所以两者不会打架。没配就逐字维持原样(`trust_env=True`)——
那条路上 macOS 读系统代理设置、Linux / 容器读环境变量,都不受这一块影响。

**凭证只脱敏「密码位」,别把整条地址藏起来。** 代理地址里可能有 `user:pass@`,那是
凭证;但主机和端口不是 —— 整条藏掉的话,人改一次端口就得把密码重输一遍。所以读接口
回传 `http://user:***@host:port`,写回来时带着 `***` 就按「这一位没改」处理
(`merge_proxy`)。端点则**完整回传**:它是个要让人看清、随时对照着改的地址。
"""
from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

from kb import llm

#: secrets.json 里的那一段。
SECTION = 'egress'
#: 设置里没填时兜底的环境变量名(和 `scripts/web_env.sh` 透传的是同一族)。
ENV_ENDPOINT = 'KB_FEED_DOH'
ENV_SECONDS = 'KB_FEED_DOH_SECONDS'
#: 代理的兜底不止一个名字,按 httpx / curl 的习惯顺序看。
ENV_PROXY = ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy')
#: 各项的兜底变量名 —— **只有这一份**,`source()` 和 `_where()` 都问它。
ENV_NAMES = {'endpoint': (ENV_ENDPOINT,), 'seconds': (ENV_SECONDS,), 'proxy': ENV_PROXY}
DEFAULT_SECONDS = 8.0
#: 回传给页面时占住密码那一位的字样。写回来还是它 = 这一位没改。
MASK = '***'


class BadValue(Exception):
    """配置本身写错了(端点不是 http(s)、秒数不是正数)。面板上就地红字。

    **带上是哪一项**(`field`):面板要把这句话写在对应那个输入框下面,只给一句话的话
    只能飘一个 toast —— 而「错在哪个框」正是人这时唯一想知道的事。
    """

    def __init__(self, message: str, field: str = ''):
        super().__init__(message)
        self.field = field


def _section() -> dict:
    value = llm.read_settings().get(SECTION)
    return value if isinstance(value, dict) else {}


#: 「测一下」用的临时覆盖。ContextVar 而不是模块级变量 —— 见 `using()`。
_override: ContextVar[dict | None] = ContextVar('egress_override', default=None)


def _stored(name: str) -> str:
    over = _override.get()
    if over is not None:
        value = over.get(name)
        return value.strip() if isinstance(value, str) else ''
    value = _section().get(name)
    return value.strip() if isinstance(value, str) else ''


@contextlib.contextmanager
def using(**fields: str):
    """临时按这几项取值。**只给面板上的「测一下」** —— 人想在保存之前先试一把。

    **用 ContextVar 不用模块级变量**:作用域是当前线程/任务。FastAPI 的同步端点跑在
    自己的工作线程里、上下文是拷出来的,所以这一下换不走正在跑的那一轮采集 ——
    换走了就是「点一次测试把线上抓取改了」,而面板上看不出来。

    **进出各清一次解析缓存**:缓存键里只有端点和域名,代理不在里面。不清的话「用代理
    测出来的地址」会被随后不走代理的抓取拿去用,反过来也一样。代价是极少数情况下多
    解析几次 —— 「测一下」是人手点的,不在热路径上。
    """
    invalidate()
    token = _override.set({k: (v or '') for k, v in fields.items()})
    try:
        yield
    finally:
        _override.reset(token)
        invalidate()


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, '').strip()
        if value:
            return value
    return ''


def endpoint() -> str:
    """DoH 端点。空 = 关(走系统解析)。"""
    return _stored('endpoint') or _env(ENV_ENDPOINT)


def configured() -> bool:
    return bool(endpoint())


def seconds() -> float:
    """单次 DoH 查询等多久。**坏值在用到它的时候报**,不在 import 的时候崩。"""
    raw = _stored('seconds') or _env(ENV_SECONDS)
    if not raw:
        return DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise BadValue(f'单次上限不是数字:{raw!r}', 'seconds') from None
    if not value > 0:
        raise BadValue(f'单次上限要是正数:{raw!r}', 'seconds')
    return value


def proxy() -> str | None:
    """抓取走的代理。None = 不显式指定(httpx 的 trust_env 照旧看环境和系统设置)。"""
    return _stored('proxy') or _env(*ENV_PROXY) or None


def client_kwargs() -> dict:
    """给 `httpx.Client(**...)`:配了代理就显式传,没配就逐字维持原样。"""
    url = _stored('proxy')
    if url:
        return {'proxy': url}
    return {'trust_env': True}


def source(name: str) -> str:
    """这一项当前由谁说话算数 —— 页面要能说清「你看到的值从哪来」。"""
    if _stored(name):
        return 'settings'
    return 'env' if _env(*ENV_NAMES.get(name, ())) else 'none'


def _where(name: str) -> str:
    """这一项的出处,写给人看。环境变量**要带上到底是哪一个名字** —— 代理那一族有四个
    名字,只说「来自环境变量」的话人得把四个都 grep 一遍。"""
    if _stored(name):
        return '来自设置页'
    for env in ENV_NAMES.get(name, ()):
        if os.environ.get(env, '').strip():
            return f'来自环境变量 {env}'
    return ''


def mask_proxy(url: str) -> str:
    """把 `user:pass@` 里的密码换成 MASK,主机端口照旧看得见。"""
    if not url:
        return ''
    parts = urlsplit(url)
    if not parts.password:
        return url
    host = parts.hostname or ''
    if parts.port:
        host = f'{host}:{parts.port}'
    return urlunsplit(parts._replace(netloc=f'{parts.username}:{MASK}@{host}'))


def merge_proxy(incoming: str) -> str:
    """页面传回来的代理:密码位还是 MASK 就沿用存着的那一份。

    不这么做的话,人改一次端口就得把密码重输一遍 —— 而「不回传就不会泄露」换来的是
    每次都得重输,那个代价全落在日常使用上。
    """
    incoming = (incoming or '').strip()
    if MASK not in incoming:
        return incoming
    kept = _stored('proxy')
    if not kept:
        return incoming.replace(MASK, '')
    old, new = urlsplit(kept), urlsplit(incoming)
    if new.password != MASK or not old.password:
        return incoming
    host = new.hostname or ''
    if new.port:
        host = f'{host}:{new.port}'
    return urlunsplit(new._replace(netloc=f'{new.username}:{old.password}@{host}'))


def check(endpoint_url: str, seconds_raw: str, proxy_url: str) -> None:
    """保存之前过一遍。**判不准一律拒**,别把坏值写进去等抓取时才炸。"""
    for field, label, url in (('endpoint', 'DoH 端点', endpoint_url),
                              ('proxy', '代理地址', proxy_url)):
        if url and urlsplit(url).scheme not in ('http', 'https', 'socks5', 'socks5h'):
            raise BadValue(f'{label}要以 http:// https:// 或 socks5:// 开头', field)
        if url and not urlsplit(url).hostname:
            raise BadValue(f'{label}里没有主机名', field)
    if seconds_raw:
        try:
            value = float(seconds_raw)
        except ValueError:
            raise BadValue('单次上限不是数字', 'seconds') from None
        if not 0 < value <= 60:
            raise BadValue('单次上限要在 0 到 60 秒之间', 'seconds')


def save(endpoint_url: str, seconds_raw: str, proxy_url: str) -> None:
    """写盘并让它立刻生效。空串 = 清掉这一项(于是环境变量重新成为兜底)。

    **代理先过 `merge_proxy` 再 `check`**:页面回传的是脱敏过的那一份,直接存下去
    的话密码就真的变成 `***` 了(人下次抓取才发现,而那时原密码已经没了)。
    """
    proxy_url = merge_proxy(proxy_url)
    check(endpoint_url, seconds_raw, proxy_url)
    fields = {'endpoint': endpoint_url.strip(), 'seconds': seconds_raw.strip(),
              'proxy': proxy_url.strip()}

    def mutate(data: dict) -> None:
        section = data.get(SECTION)
        if not isinstance(section, dict):
            section = {}
        for name, value in fields.items():
            if value:
                section[name] = value
            else:
                section.pop(name, None)
        if section:
            data[SECTION] = section
        else:
            data.pop(SECTION, None)

    llm.update_settings(mutate)
    invalidate()


def invalidate() -> None:
    """清掉解析缓存 —— 「保存即生效」的另一半(不清旧答案最多还粘 10 分钟)。

    延迟 import:`resolve` 要取这里的配置,顶层互相 import 会成环。
    """
    from kb.feeds import resolve
    resolve.forget()


def view() -> dict:
    """给页面的那一份:有效值 + 各自来源,代理的密码位脱敏。"""
    return {
        'endpoint': endpoint(),
        'seconds': _stored('seconds') or _env(ENV_SECONDS) or '',
        'proxy': mask_proxy(proxy() or ''),
        'sources': {name: source(name) for name in ('endpoint', 'seconds', 'proxy')},
        # 出处**整句**给出去,不只给 'env' 这个分类:代理那一族有四个候选变量名,
        # 前端猜不出是哪个在兜底,而那正是人要去改的那一个。
        'where': {name: _where(name) for name in ('endpoint', 'seconds', 'proxy')},
        'default_seconds': DEFAULT_SECONDS,
    }


def mask_endpoint(url: str) -> str:
    """端点脱敏到 `scheme://host` —— **只给要落盘的日志用**。

    有人把 token 写在 path 或 query 里(NextDNS、AdGuard 那类带 profile id 的端点),
    终端输出和日志不该留。页面上**照旧完整回传**:那是人要对照着改的东西。
    """
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme else url


def summary() -> list[str]:
    """给 `scripts/web_env.sh` 在起服务/看状态时打的那两三行。

    **口径只有一份**:哪一项生效、来自哪里,由上面那几个函数说,shell 不自己读
    `secrets.json`、也不自己判环境变量 —— 判第二遍的那一版在「设置里填了、环境变量
    没填」时会打「关」,而服务其实是开着的。
    """
    import urllib.request

    url = endpoint()
    if not url:
        return ['可信解析:关(走系统解析;要开:设置里的「出口与解析」)']
    lines = [f'可信解析:开(端点 {mask_endpoint(url)},{_where("endpoint")})']
    configured_proxy = proxy()
    if configured_proxy:
        lines.append(f'出口代理:{mask_proxy(configured_proxy)}({_where("proxy")})')
    elif not urllib.request.getproxies():
        # 提示而不代劳(口径同 code_stale / graph_stale)。**陈述我们探到什么**,
        # 不断言对方连不上 —— macOS 上 getproxies() 读系统代理设置,那里探到了就不打。
        lines.append('出口代理:没配,环境变量和系统代理里也探不到'
                     ' —— DoH 端点在墙外时解析会报「域名解析服务不可用」')
    return lines
