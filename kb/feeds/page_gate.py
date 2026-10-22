"""Live gate components shared by HTML ingestion and browser page-state readers."""
import json

# grab_page._gate_assets and x_page_state.STATE_JS share these markers and liveness rules.
# Only orchestration components count; generic captcha widgets and jsd/main.js do not.
ASSETS = ('/cdn-cgi/challenge-platform/h/', '_cf_chl_opt', 'captcha-delivery.com/captcha')
ATTRS = (('script', 'src'), ('iframe', 'src'), ('link', 'href'),
         ('form', 'action'), ('img', 'src'))
EXEC_TYPES = ('', 'text/javascript', 'application/javascript', 'module',
              'text/ecmascript', 'application/ecmascript', 'text/jscript')


def live(node) -> bool:
    return node.find_parent('template') is None and (
        node.name != 'script' or str(node.get('type') or '').strip().lower() in EXEC_TYPES)


def has_assets(soup) -> bool:
    for tag, attr in ATTRS:
        for node in soup.find_all(tag):
            value = str(node.get(attr) or '').lower()
            if live(node) and any(marker in value for marker in ASSETS):
                return True
    return any(not node.has_attr('src') and live(node)
               and any(marker in (node.string or '').lower() for marker in ASSETS)
               for node in soup.find_all('script'))


ASSETS_JS = r'''(() => {
  const live = node => !node.closest('template') && (node.localName !== 'script'
    || __EXEC_TYPES__.includes((node.getAttribute('type') || '').trim().toLowerCase()));
  const matches = value => __ASSETS__.some(marker => (value || '').toLowerCase().includes(marker));
  return __ATTRS__.some(([tag, attr]) =>
    [...document.querySelectorAll(`${tag}[${attr}]`)].some(node =>
      live(node) && matches(node.getAttribute(attr))))
    || [...document.querySelectorAll('script')].some(node =>
      !node.hasAttribute('src') && live(node) && matches(node.textContent));
})()'''.replace('__EXEC_TYPES__', json.dumps(EXEC_TYPES)).replace(
    '__ASSETS__', json.dumps(ASSETS)).replace('__ATTRS__', json.dumps(ATTRS))


# ── 这一次响应自己的身份(关卡响应头)─────────────────────────────────────────
# **这一档是文字之外的身份证据,它和份额那一档的区别不是「更准」而是「问的不是
# 同一件事」**:份额问「这段文字在说什么」,这一档问「对方这一次有没有把请求拦下
# 来」。
#
# **它住在传输层**(`fetching.request` 一拿到响应就问,认出来抛 `Gated`),不是
# 住在某个格式分派的下游 —— 响应身份与正文是什么格式无关,放在分派之后就会被
# 「这是 Markdown / 这是专用原文」绕过去(审查第 1 轮实测:带 `cf-mitigated` 的
# `text/markdown` 响应,两个收录入口都把 492 字拒绝说明收成了正文)。
#: 关卡**决策**响应头:出现即意味着这一次请求被拦下或被要求过关。
HEADERS = ('cf-mitigated',        # Cloudflare 只在真 mitigate 了这一次请求时才发
           'cf-chl-bypass',       # Cloudflare 关卡页自己带的
           'x-amzn-waf-action',   # AWS WAF 的 Challenge / CAPTCHA 动作
           'x-sucuri-block')      # Sucuri 只在拦下来时才发
#: 这一句两处用:`grab_page.verdict` 的 ①,以及 `fetching.request` 认出关卡时那
#: 句话 —— 后者原先对 4xx/5xx 一律说「请检查来源或填写专用订阅链接」,而对着一张
#: `403 + cf-mitigated: challenge` 那是把人指去改订阅地址(话术要指得出下一步)。
#: **口径只有一份**,所以表和这句话都住在这个文件里,两个消费方都 import 它。
SAY_REPLY = '对方这一次把请求拦在了一道验证上（响应自己带着关卡标记）'


def response_reason(headers: dict | None) -> str:
    """这一次**响应自己**说它是一道关卡吗 —— 是就返回那半句话,否则空串。

    **判据只有响应头这一样,状态码一个都不算。** 曾经把
    `401/403/429/503/511` 也当成「这个状态码本身就是一句你过不去」,审查当场
    指出那是另一类判据混进来了:没有关卡头的 429 是限流、503 是对方不可用,
    它们**说得出下一步**(稍后重试),而说成「要过一道验证」会把人指向一件
    他做不了也不需要做的事 —— 顺带还让 `fetching.request` 原来那句准确的
    「服务限流,请稍后重试」变成不可达。状态码的分类留在
    `fetching.request` 自己那段话术里,这里只回答「对方拦了没有」。

    **一个「CDN 在场」的证据都不许进这张表。** `cf-ray`、`server: cloudflare`、
    `x-datadome: protected`、`server: AkamaiGHost` 这些在**正常文章**的响应上也
    常态出现 —— 被 Cloudflare 代理的站点占了相当大一片互联网,它们进表等于按
    「这站挂在 CDN 后面」整批拒稿。判据和 `grab_page._GATE_ROUTES` 那张路由表
    同一条:只收「对方这次真的拦了才会发」的那几个。

    页面上的字一个都不算:一篇讲 WAF 的文章把 `cf-mitigated: challenge` 原样
    印在 `<pre>` 里,它的响应头里一样没有这一条(口径同 `ASSETS`:DOM 属性算、
    页面文字不算)。

    headers 的键按 HTTP 规矩**大小写不敏感**地比(`httpx` 交出来的是小写,别的
    地方拼出来的不一定)。值只要求非空 —— 这几个头**只在拦了的时候才发**,再去
    枚举 `challenge` / `captcha` / `block` 那几个值等于给自己留一个「新值没进表
    就漏放」的洞。
    """
    low = {str(k).lower(): str(v or '') for k, v in (headers or {}).items()}
    return SAY_REPLY if any(low.get(name, '').strip() for name in HEADERS) else ''
