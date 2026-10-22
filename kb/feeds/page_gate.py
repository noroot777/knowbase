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


# ── 这一次响应自己的身份(状态码 + 关卡响应头)─────────────────────────────────
# **这一档是文字之外的身份证据,它和份额那一档的区别不是「更准」而是「问的不是
# 同一件事」**:份额问「这段文字在说什么」,这一档问「对方这一次有没有把请求拦下
# 来」。页面上的字一个都不算 —— 一篇讲 WAF 的文章把 `cf-mitigated: challenge`
# 原样印在 `<pre>` 里,它的响应头里一样没有这一条(口径同 `ASSETS`:DOM 属性算、
# 页面文字不算)。
#
# **一个「CDN 在场」的证据都不许进这两张表。** `cf-ray`、`server: cloudflare`、
# `x-datadome: protected`、`server: AkamaiGHost` 这些在**正常文章**的响应上也常态
# 出现 —— 被 Cloudflare 代理的站点占了相当大一片互联网,它们进表等于按「这站挂在
# CDN 后面」整批拒稿。判据和 `grab_page._GATE_ROUTES` 那张路由表同一条:只收
# 「对方这次真的拦了才会发」的那几个。
#: 关卡**决策**响应头:出现即意味着这一次请求被拦下或被要求过关。
HEADERS = ('cf-mitigated',        # Cloudflare 只在真 mitigate 了这一次请求时才发
           'cf-chl-bypass',       # Cloudflare 关卡页自己带的
           'x-amzn-waf-action',   # AWS WAF 的 Challenge / CAPTCHA 动作
           'x-sucuri-block')      # Sucuri 只在拦下来时才发
#: 「这个状态码本身就是一句『你过不去』」。**不是「所有非 2xx」**:404 / 500 是
#: 对方坏了或者没这一页,那是抓取失败而不是关卡,说成关卡会把「换个地址」说成
#: 「在浏览器里打开一次」;304 是缓存命中,更不是。
STATUS = (401, 403, 429, 503, 511)
#: 这一句两处用:`grab_page.verdict` 的 ①,以及 `fetching.request` 对 4xx/5xx 的
#: 那句话 —— 后者原先一律说「请检查来源或填写专用订阅链接」,而对着一张
#: `403 + cf-mitigated: challenge` 那是把人指去改订阅地址(话术要指得出下一步)。
#: **口径只有一份**,所以表和这句话都住在这个文件里,两个消费方都 import 它。
SAY_REPLY = '对方这一次把请求拦在了一道验证上（响应自己带着关卡标记）'


def response_reason(status: int | None, headers: dict | None) -> str:
    """这一次**响应自己**说它是一道关卡吗 —— 是就返回那半句话,否则空串。

    判据两样,一条成立就算:响应头里带着某家关卡的**决策**标记(`HEADERS`)、
    或者状态码本身就是一句「你过不去」(`STATUS`)。

    **状态码这一道在当前传输层上到不了 `grab_page.admit`**:`fetching.request`
    对 `>= 400` 一律抛 `FeedError`,于是那一类在补抓那条路上落成「抓取失败」——
    壳留着、`fulltext_at` 不写、计一次次数,本来就是安全的那一侧。留着它有两个
    理由,都不是装饰:①`fetching.request` 自己那句话要用它(见 `SAY_REPLY`);
    ②哪天某条链路把关卡页的 body 原样交进来(浏览器通道、带 body 的 4xx),这一
    道就是那时唯一一道**不靠文字**的判据,而漏一道关卡就是把一道墙永久写成正文。

    headers 的键按 HTTP 规矩**大小写不敏感**地比(`httpx` 交出来的是小写,别的
    地方拼出来的不一定)。值只要求非空 —— 这几个头**只在拦了的时候才发**,再去
    枚举 `challenge` / `captcha` / `block` 那几个值等于给自己留一个「新值没进表
    就漏放」的洞。
    """
    if status is not None and status in STATUS:
        return SAY_REPLY
    low = {str(k).lower(): str(v or '') for k, v in (headers or {}).items()}
    return SAY_REPLY if any(low.get(name, '').strip() for name in HEADERS) else ''
