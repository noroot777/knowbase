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


def _effective(name: str) -> tuple[str, str]:
    """这一项**最终生效的值**,以及它的出处:`'settings'` / 环境变量名 / `''`(都没有)。

    **这一族「谁说了算」的口径只有这一份。** 取值、来源分类、出处文案、交给 httpx、
    合并密码位,五处全问它 —— 各自再判一遍的后果不是「不一致」这么抽象,实测过两种:
    一是出处判错(`source` 认 `KB_FEED_DOH`、`_where` 把代理也说成它);二是**丢密码**
    —— 代理来自环境变量时 `merge_proxy` 只看设置里那份(空),于是页面原样回传的
    `http://me:***@h:1` 被当成「没有旧值可沿用」,密码位被抹成空串写进 `secrets.json`,
    把本来好用的环境变量那份顶掉,而面板上看着一切正常。
    """
    value = _stored(name)
    if value:
        return value, 'settings'
    for env in ENV_NAMES.get(name, ()):
        got = os.environ.get(env, '').strip()
        if got:
            return got, env
    return '', ''


def endpoint() -> str:
    """DoH 端点。空 = 关(走系统解析)。"""
    return _effective('endpoint')[0]


def configured() -> bool:
    return bool(endpoint())


def seconds() -> float:
    """单次 DoH 查询等多久。**坏值在用到它的时候报**,不在 import 的时候崩。"""
    raw = _effective('seconds')[0]
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
    return _effective('proxy')[0] or None


def client_kwargs() -> dict:
    """给 `httpx.Client(**...)`:**设置里填的**显式传,其余逐字维持原样。

    **只有设置来的那一份显式传,环境变量来的那份照旧交给 `trust_env`。** 这不是漏改:
    显式 `proxy=` 的语义是「所有主机都走它」,而 `trust_env` 会一并读 `NO_PROXY`。把
    环境变量那份改成显式传,等于悄悄作废人家配的 `NO_PROXY` —— 内网订阅会突然开始
    走代理,而面板上只会显示一句超时。设置页上没有 `NO_PROXY` 这一栏,所以从那边来的
    值没有这个包袱。
    """
    url, origin = _effective('proxy')
    if url and origin == 'settings':
        return {'proxy': url}
    return {'trust_env': True}


def source(name: str) -> str:
    """这一项当前由谁说话算数 —— 页面要能说清「你看到的值从哪来」。"""
    origin = _effective(name)[1]
    return 'settings' if origin == 'settings' else ('env' if origin else 'none')


def _where(name: str) -> str:
    """这一项的出处,写给人看。环境变量**要带上到底是哪一个名字** —— 代理那一族有四个
    名字,只说「来自环境变量」的话人得把四个都 grep 一遍。"""
    origin = _effective(name)[1]
    if origin == 'settings':
        return '来自设置页'
    return f'来自环境变量 {origin}' if origin else ''


def _split(url: str):
    """宽松解析:**解析不动就返回 None,一个字都不抛。**

    `urlsplit` 有两处会抛 `ValueError`,而两处都长得不像「会抛」:地址里有没闭合的
    方括号时当场抛(`http://[::1`),端口不是 1..65535 的数字时要等**读 `.port` 那一下**
    才抛(它是个 property)。这两种值都是人能在面板上敲出来的,而这一族函数有一半
    跑在显示路径上 —— 抛出去就是设置页整个打不开,而人正要进去把它改掉。
    所以:**判对错走 `check_url`(抛 `BadValue`,面板就地红字),显示一律走这一份。**
    """
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 —— property,不读它端口就没被校验过
    except ValueError:
        return None
    return parts


def _parsed(url: str):
    """解析得动、**而且真解析出了主机名**的那一份;否则 None。

    **「`urlsplit` 没报错」不等于「这份结果能信」。** 不带协议头的
    `me:s3cret@127.0.0.1:7897` 它一个字都不抱怨:把 `me` 当成 scheme、`netloc` 空、
    整条 `s3cret@127.0.0.1:7897` 落在 `path` 里,于是 `parts.password` 是 `None` ——
    「这条地址里没有密码」这个结论是**假的**,密码就在 path 里。实测后果:`view()`
    和 `./kb.sh status` 两个出口都把 `me:s3cret@127.0.0.1:7897` 原样打出来,贴一张
    截图就泄漏。所以**凡是要按结构取东西的地方都过这一份**,`_split` 只回答
    「会不会抛」。
    """
    parts = _split(url)
    return parts if parts is not None and parts.hostname else None


def _netloc(parts) -> str:
    """从解析结果拼回 `host[:port]`。**这一族只有这一份拼法。**

    两处容易各写各的:**IPv6 要带方括号**(`parts.hostname` 给的是**去过壳**的
    `::1`,直接拼成 `user:***@::1:7897` 不是合法地址 —— 实测页面显示成那样之后,
    人只想改端点都保存不了,`check_url` 当场报「方括号没闭合」);**端口 0 要留着**
    (`if parts.port:` 把 0 当成「没有端口」,于是脱敏之后那个不合法的端口凭空消失,
    面板上看不出这条值哪里不对)。
    """
    host = parts.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    if parts.port is not None:
        host = f'{host}:{parts.port}'
    return host


#: 各项允许的协议头。**端点只认 http(s)** —— DoH 是 HTTP 上的协议,socks5 端点连不出去;
#: 代理那边 socks5 是正经选项。
SCHEMES = {'endpoint': ('http', 'https'), 'proxy': ('http', 'https', 'socks5', 'socks5h')}
LABELS = {'endpoint': 'DoH 端点', 'proxy': '代理地址'}


def check_url(field: str, url: str) -> None:
    """一个地址判完整:解析得动吗、协议对吗、有主机名吗、端口在范围里吗。

    **两个地址共用这一份**(端点、代理),只有协议名单各自不同。分开写过一版,结果是
    端口那一条谁都没判:`http://h:99999` 一路存进 `secrets.json`,下一次抓取在
    `httpx.Client(proxy=...)` 那一行抛 `httpx.InvalidURL` —— 它**不是** `httpx.HTTPError`
    的子类,于是没被 `fetching` 那道 except 接住,整轮同步 500,面板上一个字的原因都
    没有。判在这里、拒在面板上,是这一族唯一拦得住的位置。
    """
    if not url:
        return
    label = LABELS[field]
    parts = _split(url)
    if parts is None:
        raise BadValue(f'{label}不是个能用的地址(方括号没闭合,或者端口不是 1-65535)', field)
    if parts.scheme not in SCHEMES[field]:
        heads = ' '.join(f'{s}://' for s in SCHEMES[field])
        raise BadValue(f'{label}要以 {heads} 开头', field)
    if not parts.hostname:
        raise BadValue(f'{label}里没有主机名', field)
    if parts.port is not None and not 1 <= parts.port <= 65535:
        # **0 要显式拒。** 上面那道只拦得住 `urlsplit` 自己会抛的(`abc`、`99999`),
        # 而 0 是个合法整数,它一路存进 `secrets.json`、要等下一次抓取连接失败才暴露
        # —— 而那时面板上看不出是这条值本身不合法。文案和 docstring 一直写着
        # 「1-65535」,判据得跟上。
        raise BadValue(f'{label}的端口要在 1-65535 之间:{parts.port}', field)


def mask_proxy(url: str) -> str:
    """把 `user:pass@` 里的密码换成 MASK,主机端口照旧看得见。

    **坏值不许抛**:存量里可能有手写进去的垃圾,而页面正要把它显示出来让人改。
    """
    url = (url or '').strip()
    if not url:
        return ''
    parts = _parsed(url)
    if parts is None:
        # 解析不动、**或者解析出来信不过**的值里照样有密码(见 `_parsed`:不带协议头
        # 那种 `password` 是 None,而密码就在 path 里)。整段 userinfo 一起盖掉,
        # 不猜它的结构。
        head, sep, tail = url.rpartition('@')
        if not sep:
            return url
        scheme = head.split('://', 1)[0] + '://' if '://' in head else ''
        return f'{scheme}{MASK}@{tail}'
    if not parts.password:
        return url
    return urlunsplit(parts._replace(netloc=f'{parts.username}:{MASK}@{_netloc(parts)}'))


def merge_proxy(incoming: str) -> str:
    """页面传回来的代理:密码位还是 MASK 就沿用**当前生效**的那一份。

    不这么做的话,人改一次端口就得把密码重输一遍 —— 而「不回传就不会泄露」换来的是
    每次都得重输,那个代价全落在日常使用上。

    **沿用的是 `_effective` 那一份,不是设置里那一份**:代理来自环境变量时设置里是空的,
    照「设置里那份」判就会得出「没有旧值」,把密码抹掉存进去(见 `_effective`)。
    **真的没有旧值可沿用就拒**,不许猜着把 MASK 抹成空串 —— 那存下去就是一条连不上的
    代理,而人要到下一次抓取超时才发现。
    """
    incoming = (incoming or '').strip()
    if MASK not in incoming:
        return incoming
    kept = _effective('proxy')[0]
    if kept and mask_proxy(kept) == incoming:
        # **我们给它什么、它原样交回来,就是「这一位没改」。** 这条先判,因为脱敏值
        # 未必拆得开:存量里的代理可以是不带协议头的、也可以是 IPv6 的,而人进面板
        # 多半只是想改端点。不先认出「没改」的话,那些机器上**只改端点都保存不了**
        # (脱敏值拆不动 → 原样送进 `check_url` → 报一句他根本没动过的代理有问题)。
        return kept
    old, new = _parsed(kept), _parsed(incoming)
    if new is None or new.password != MASK:
        return incoming  # MASK 出现在别处(主机名、路径),不是在替密码占位
    if old is None or not old.password:
        raise BadValue('原来的代理密码已经不在了,请把完整地址重输一遍', 'proxy')
    return urlunsplit(new._replace(netloc=f'{new.username}:{old.password}@{_netloc(new)}'))


def check(endpoint_url: str, seconds_raw: str, proxy_url: str) -> None:
    """保存之前过一遍。**判不准一律拒**,别把坏值写进去等抓取时才炸。"""
    check_url('endpoint', endpoint_url)
    check_url('proxy', proxy_url)
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

    **原样回传的兜底值不落盘。** 页面显示的是**生效值**,人只改了端点、把另外两项原样
    交回来是常态;照单全收的话,环境变量那份就被复制进 `secrets.json` 固化下来 ——
    从此运维改环境变量再也不生效,而面板上显示的值和之前一模一样,看不出发生过什么。
    """
    proxy_url = merge_proxy(proxy_url)
    fields = {'endpoint': endpoint_url.strip(), 'seconds': seconds_raw.strip(),
              'proxy': proxy_url.strip()}
    for name, value in list(fields.items()):
        current, origin = _effective(name)
        if origin not in ('settings', '') and value == current:
            fields[name] = ''  # 维持「设置里没填、环境变量兜底」这个末态
    # **先定下这次到底要写哪几项,再校验那几项。** 顺序反过来实测会卡死一类人:
    # 环境变量里的代理按我们的标准本身就不合格(没写协议头),而人只是想改端点、
    # 把那一格原样交了回来 —— 它压根不会落盘,却因为被一起校验而把整次保存挡掉,
    # 错误还指着一个他根本没动过、也不在设置里的值。**判据只管这次真要写进去的。**
    check(fields['endpoint'], fields['seconds'], fields['proxy'])

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


def presets() -> list[dict]:
    """面板上那几颗一键填入的端点。**名单归协议那一侧**,见 `resolve.JSON_API_PRESETS`。"""
    from kb.feeds import resolve
    return [dict(item) for item in resolve.JSON_API_PRESETS]


def view() -> dict:
    """给页面的那一份:有效值 + 各自来源,代理的密码位脱敏。

    **一个字都不许抛。** 存量里的坏值(端口 99999、方括号没闭合)正是人打开这个面板
    要来改的东西 —— 这儿抛一下,面板就只剩一句「读取失败」,那个值再也改不掉。
    """
    return {
        'endpoint': endpoint(),
        'seconds': _effective('seconds')[0],
        'proxy': mask_proxy(proxy() or ''),
        'sources': {name: source(name) for name in ('endpoint', 'seconds', 'proxy')},
        # 出处**整句**给出去,不只给 'env' 这个分类:代理那一族有四个候选变量名,
        # 前端猜不出是哪个在兜底,而那正是人要去改的那一个。
        'where': {name: _where(name) for name in ('endpoint', 'seconds', 'proxy')},
        'default_seconds': DEFAULT_SECONDS,
        'presets': presets(),
    }


def mask_endpoint(url: str) -> str:
    """端点脱敏到 `scheme://host[:port]` —— **只给要落盘的日志用**。

    有人把 token 写在 path 或 query 里(NextDNS、AdGuard 那类带 profile id 的端点),
    终端输出和日志不该留。页面上**照旧完整回传**:那是人要对照着改的东西。

    **从 hostname 拼,不拿 netloc。** `netloc` 里带着 `user:pass@`,原样打出去就是把
    凭证写进日志 —— 而这个函数存在的全部理由就是别把凭证写进日志。
    **没有协议头的也要剪**:`doh.example/q?token=x` 这种 `urlsplit` 认不出主机名,
    早先那一版直接原样返回,于是最该剪的那一类一个字都没剪。
    """
    url = (url or '').strip()
    if not url:
        return ''
    parts = _parsed(url)
    if parts is None or not parts.scheme:
        # 剪到第一个 `/` 为止,再把 userinfo 去掉。拿不准结构就只保留最保守的那一段。
        return url.split('/', 1)[0].rsplit('@', 1)[-1] or MASK
    return f'{parts.scheme}://{_netloc(parts)}'


def summary() -> list[str]:
    """给 `scripts/web_env.sh` 在起服务/看状态时打的那两三行。

    **口径只有一份**:哪一项生效、来自哪里,由上面那几个函数说,shell 不自己读
    `secrets.json`、也不自己判环境变量 —— 判第二遍的那一版在「设置里填了、环境变量
    没填」时会打「关」,而服务其实是开着的。
    """
    import urllib.request

    url = endpoint()
    if url:
        lines = [f'可信解析:开(端点 {mask_endpoint(url)},{_where("endpoint")})']
    else:
        lines = ['可信解析:关(走系统解析;要开:设置里的「出口与解析」)']
    # **代理这一行和端点开没开无关。** 原先它整段写在「端点开着」那个分支里,于是
    # 「端点关、代理在环境变量里」这种最常见的形态(用户那台机器此刻就是)一行代理
    # 都不打 —— 而这几行存在的理由正是回答「跑着的那个进程在用什么出口」。
    configured_proxy = proxy()
    if configured_proxy:
        lines.append(f'出口代理:{mask_proxy(configured_proxy)}({_where("proxy")})')
    elif url and not urllib.request.getproxies():
        # **这句警告仍然只在端点开着时打**:它说的是「DoH 要出得去」。端点关着时
        # 「没配代理」是默认状态,不是问题,喊一句只是噪声(口径同 code_stale:
        # 提示而不代劳,也不无中生有)。**陈述我们探到什么**,不断言对方连不上 ——
        # macOS 上 getproxies() 读系统代理设置,那里探到了就不打。
        lines.append('出口代理:没配,环境变量和系统代理里也探不到'
                     ' —— DoH 端点在墙外时解析会报「域名解析服务不可用」')
    return lines
