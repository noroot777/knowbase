# -*- coding: utf-8 -*-
"""YouTube 的登录态:从哪个浏览器读 cookie、读得出来没有、那份登录态还有效没有。

**硬约束在 `docs/decisions/youtube-intake.md` 第一节 —— 改这块之前先读那一份。**

**我们自己不碰加密 cookie**:只把浏览器名交给 yt-dlp 的 `--cookies-from-browser`,由它去读、
去解密。我们不读 cookie 文件、不解析、不落盘、不记日志,错误原文里也只取分类用的那几个特征串。

**「读不出 cookie」和「没登录」必须分开报。** 两者都让采集失败,但**让用户做的事完全不同**:
前者要去授权钥匙串 / 关掉浏览器 / 换 profile,后者要去浏览器里登录一次。混成一句「登录失败」
会让人反复去登录,而问题在另一头。分不清的时候**既不说读不出、也不说没登录**,原样把 yt-dlp
那句话交出去(`unknown`)—— 猜一个具体原因比承认不知道更坏。

**两步检查把「连不上」和「没登录」拆开**(`check`):第一步 `--flat-playlist` 列频道,它**不需要
登录态也能过**(实测裸跑可通),所以它失败只说明连不上 YouTube;第二步才拿单条视频的元数据,
那一步是 bot 墙真正拦人的地方。只有第二步报墙,才是登录态的问题。

**便宜的判断和真连一次是两件事**(同 `x_channels.readiness` 的口径):`readiness` 只看目录在不在、
yt-dlp 有没有,来源面板每次刷新都调它;`check` 会真起两次 yt-dlp,只在用户点「检查登录态」时走。
**cookie 过期在文件上看不出来**,所以两个都要有。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from kb.youtube.diagnosis import excerpt

DEFAULT = 'chrome'
# 顺序决定面板上那张清单的顺序。**每一档的说明都要能单独读懂,不许写「同上」。**
BROWSERS = ('chrome', 'edge', 'brave', 'chromium', 'vivaldi', 'firefox', 'safari')
LABELS = {
    'chrome': 'Google Chrome',
    'edge': 'Microsoft Edge',
    'brave': 'Brave',
    'chromium': 'Chromium',
    'vivaldi': 'Vivaldi',
    'firefox': 'Firefox',
    'safari': 'Safari（仅 macOS）',
}
_SPEC = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,48}(:[A-Za-z0-9 _.+-]{1,48})?$')
_HOME = Path.home()
# 各浏览器的用户数据目录。**只用来回答「这台机器上有没有这个浏览器」**,不进去读任何文件。
_ROOTS = {
    'chrome': {'darwin': ['Library/Application Support/Google/Chrome'],
               'win32': ['AppData/Local/Google/Chrome/User Data'],
               'linux': ['.config/google-chrome', '.var/app/com.google.Chrome/config/google-chrome']},
    'edge': {'darwin': ['Library/Application Support/Microsoft Edge'],
             'win32': ['AppData/Local/Microsoft/Edge/User Data'],
             'linux': ['.config/microsoft-edge']},
    'brave': {'darwin': ['Library/Application Support/BraveSoftware/Brave-Browser'],
              'win32': ['AppData/Local/BraveSoftware/Brave-Browser/User Data'],
              'linux': ['.config/BraveSoftware/Brave-Browser']},
    'chromium': {'darwin': ['Library/Application Support/Chromium'],
                 'win32': ['AppData/Local/Chromium/User Data'],
                 'linux': ['.config/chromium', 'snap/chromium/common/chromium']},
    'vivaldi': {'darwin': ['Library/Application Support/Vivaldi'],
                'win32': ['AppData/Local/Vivaldi/User Data'],
                'linux': ['.config/vivaldi']},
    'firefox': {'darwin': ['Library/Application Support/Firefox'],
                'win32': ['AppData/Roaming/Mozilla/Firefox'],
                'linux': ['.mozilla/firefox', 'snap/firefox/common/.mozilla/firefox']},
    'safari': {'darwin': ['Library/Cookies', 'Library/Containers/com.apple.Safari/Data/Library/Cookies']},
}


def platform_key() -> str:
    if sys.platform == 'darwin':
        return 'darwin'
    return 'win32' if os.name == 'nt' else 'linux'


def validate(spec) -> str:
    """校验浏览器规格串。接受 `chrome` 和 `chrome:Profile 1` 两种写法。"""
    text = str(spec or '').strip()
    if not text:
        return DEFAULT
    if not _SPEC.fullmatch(text):
        raise ValueError('浏览器名称格式不正确，可写 chrome 或 chrome:Profile 1')
    if text.split(':', 1)[0].lower() not in BROWSERS:
        raise ValueError('不支持这个浏览器，可选：' + '、'.join(BROWSERS))
    return text


def browser_of(spec: str) -> str:
    return str(spec or DEFAULT).split(':', 1)[0].lower()


def installed(spec: str) -> bool:
    """这台机器上有没有这个浏览器的用户数据目录。**找不到不等于没装**,只是我们没在常见位置看到。"""
    roots = _ROOTS.get(browser_of(spec), {}).get(platform_key(), [])
    return any((_HOME / relative).is_dir() for relative in roots)


def arguments(spec: str) -> list[str]:
    """交给 yt-dlp 的那两个参数。校验过才拼,不把用户输入原样塞进命令。"""
    return ['--cookies-from-browser', validate(spec)]


# yt-dlp 失败输出里的分类特征。**宁可归到 unknown,也不要猜一个具体原因。**
#
# **机器人验证单独一档,不归「没登录」。** 那道墙是出口 IP 级的:实测同一分钟里
# 两个 yt-dlp 版本撞的是同一道,换版本无效、带 cookie 才过;已经登录的人照样可能撞上。
# 归成「没登录」会把他送去反复登录,而问题在出口那一头。
_BOT = ('sign in to confirm', 'confirm you', 'not a bot')
# **「这个视频是私有的」也不归「没登录」。** 已经登录、只是没拿到这一条的授权时
# yt-dlp 报的是同一句 —— 而第一个启用频道的最新一条恰好受限,就会被当成整个账号的状态。
_PRIVATE = ('this video is private', 'private video', 'members-only', 'join this channel')
_WALL = ('sign in to view', 'login required', 'account cookies are no longer valid',
         'cookies are no longer valid', 'please sign in')
# **出口被限流单独一档,而且排在机器人验证前面。** 429 是「网页根本没下下来」,后面那些墙
# 都是它的下游:实测这一轮里 YouTube 先回 429,yt-dlp 换别的 player 客户端再试,最后报出
# 「Sign in to confirm you're not a bot」—— 两句话一起出现时,照 bot 那一档说「带上登录态
# 通常能过」是错的,他本来就带着 cookie。**只认这两个精确特征**,不要把「rate」「limit」
# 这类词放进来:别处的正常句子也会撞上,而误判成限流会让人白等一个小时。
_LIMIT = ('http error 429', 'too many requests')
_COOKIE = ('could not find', 'cookies database', 'failed to decrypt', 'unable to decrypt',
           'cannot decrypt', 'no key found', 'unable to open database', 'permission denied',
           'no such file or directory', 'dpapi', 'keyring', 'secretstorage',
           'unsupported browser', 'could not copy')


def classify(text: str) -> str:
    """把 yt-dlp 的失败输出归成一类。

    **顺序就是口径**:cookie 读不出来最先判(那一步比登录态更靠前,后面的墙都是它的
    下游);接着是出口被限流(429 连网页都没下下来,它下面那些墙同样是果);其次是机器人
    验证和「这一条没授权」这两档 —— 它们都**不蕴含**未登录;真正指向登录态的只剩最后
    那一组。一个都不中就是 `unknown`,原文带回去给人看。
    """
    lowered = str(text or '').lower()
    if not lowered:
        return 'unknown'
    if any(mark in lowered for mark in _COOKIE):
        return 'cookie_unreadable'
    if any(mark in lowered for mark in _LIMIT):
        return 'rate_limited'
    if any(mark in lowered for mark in _BOT):
        return 'bot_wall'
    if any(mark in lowered for mark in _PRIVATE):
        return 'no_access'
    if any(mark in lowered for mark in _WALL):
        return 'not_logged_in'
    return 'unknown'


MESSAGES = {
    # **这一档只说「读到了」,不说「登录态有效」。** 取元数据这一步公开视频也过得去,
    # 所以它证明的是工具链和出口通,不是那份 cookie 还有效 —— 真正用到登录态的是
    # 字幕和音频那两步,那要等同步时才知道。说成「有效」是拿一个更弱的证据冒充结论。
    'metadata_ok': '能从 {label} 带着登录态取到视频元数据。这一步公开视频也能过，'
                   '所以它说明工具链和网络出口通，还不能证明那份登录态有效；'
                   '字幕和音频才真正用得上它，同步时会报出来。',
    'yt_dlp_missing': '没有找到 yt-dlp。先在下面更新一次，或自行安装后重启服务。',
    'no_browser': '在常见位置没找到 {label} 的用户数据目录。确认装的是这个浏览器，或改选另一个。',
    'no_channel': '还没有启用的 YouTube 频道订阅，没有可以用来验证登录态的频道。先添加一个频道。',
    'unreachable': '连不上 YouTube（这一步不需要登录态，所以先看网络和出口设置，不是登录的问题）。',
    'cookie_unreadable': '读不出 {label} 的 cookie。macOS 可能要在钥匙串里放行；'
                         'Windows 上新版浏览器需要先完全退出浏览器再试；也可能该换一个 profile。',
    'not_logged_in': '{label} 里没有登录 YouTube，或那份登录态已经失效。'
                     '在 {label} 里打开 youtube.com 登录一次，再回来检查。',
    'rate_limited': 'YouTube 对本机出口返回了 429（请求过于频繁），网页根本没下下来，'
                    '所以这一轮看不出登录态。这不是登录的问题：一轮里逐条解析很多视频最容易'
                    '撞上它。等一段时间再检查，或把一轮的频道数和处理条数调小；换出口也有用，'
                    '换 yt-dlp 版本没用。',
    'bot_wall': 'YouTube 要求验证本机出口不是机器人。带上登录态通常能过这一道；'
                '已经登录还是这样的话，多半是这个出口 IP 被限了 —— 实测换 yt-dlp 版本没用，'
                '换出口或等一段时间才有用。',
    'no_access': '取到的那条视频本身不可访问（私有、会员专属或已下架）。'
                 '这不说明登录态有问题，换一条视频或另一个频道再检查一次。',
    'unknown': 'yt-dlp 报了一个没能归类的错误，原文附在下面。',
}


#: 「同一处卡住、会在每个频道上一字不差重复一遍」的那几档。**归并同因失败时按这个集合判,
#: 不是只看 `not_logged_in`** —— 实测最常撞的恰恰是机器人验证和出口限流那两档,只认一个档
#: 等于让最常见的那一类退回逐条重复(48 行同一句话)。
#: `rate_limited` 严格说不是登录态问题,但它和这几档同一个形态:一个出口、一次全中,而且
#: 用户第一反应正是「是不是没登录」—— 归并那一句话恰恰是用来说清它不是的。
AUTH_CODES = ('not_logged_in', 'bot_wall', 'cookie_unreadable', 'rate_limited')


#: 「这一轮确实取到了东西,但工具同时报了这么一条」的说法。**和 `MESSAGES` 分开写,
#: 不许复用**:同一个分档在这两处的结论完全不同 —— 那边是「这一轮没成」,这边是
#: 「成了,但这份成功可能不算数」。拿 `MESSAGES` 顶替等于把一条 cookie 警告升级成
#: 「登录态无效」,而用户明明刚取到了元数据(审查点名:并列显示证据和未确认状态)。
CAUTIONS = {
    'cookie_unreadable': '但 yt-dlp 同时报了读不出 {label} 的 cookie：这一轮很可能是没带登录态'
                         '取到的公开内容。macOS 去钥匙串放行、Windows 先完全退出浏览器，或换一个 profile，'
                         '再检查一次。',
    'rate_limited': '但 yt-dlp 同时报了 429（请求过于频繁）：这一轮算侥幸过的，隔一段时间再检查一次更有把握。',
    'bot_wall': '但 yt-dlp 同时报了机器人验证：这个出口已经被盯上，下一轮可能就过不去了。',
    'not_logged_in': '但 yt-dlp 同时报了登录态失效：真正用得上它的字幕和音频这一轮没验到。',
}


def describe(code: str, spec: str) -> str:
    return MESSAGES.get(code, MESSAGES['unknown']).format(label=LABELS.get(browser_of(spec), spec))


def caution(code: str, spec: str) -> str:
    """成功那一份结论旁边要并列的那句话。没有对应说法就返回空字符串(面板按空不显示)。"""
    wording = CAUTIONS.get(code, '')
    return wording.format(label=LABELS.get(browser_of(spec), spec)) if wording else ''


def evidence(error, limit: int = 400) -> str:
    """给人看的那份「yt-dlp 原文」。**两头都留**(见 `diagnosis.excerpt`)。

    优先用异常自带的完整诊断,没有才退到消息本身 —— 原先取 `str(error)[:400]`,
    而最终那句错误在尾部,从开头一裁就看不见了(审查实测:429 在头、机器人验证在尾,
    面板上两条都没显示)。
    """
    return excerpt(getattr(error, 'diagnostic', '') or str(error), limit)


def coded(error) -> str:
    """从一个异常里取出分档。**口径只有一份,消费方不许各自 `classify(str(error))`。**

    带着 `code` 的(`diagnosis.ToolFailure` 在**未裁切**的完整诊断上算过了)直接用它;
    没带的才拿消息现算 —— 异常消息为了给人看已经裁过两头,决定性那一行可能正好在
    被省略的中段(审查实测:长诊断里的 429 这样丢掉,整次失败被判成 bot 墙)。
    """
    code = getattr(error, 'code', '')
    return code if code in MESSAGES else classify(str(error))


def readiness(settings) -> dict:
    """便宜的那一道:只看 yt-dlp 在不在、浏览器目录在不在。**不代表登录态有效。**"""
    from kb.youtube import toolchain
    from kb.youtube.settings import of
    spec = of(settings)['cookies_browser']
    if not toolchain.command():
        return {'ready': False, 'code': 'yt_dlp_missing', 'message': describe('yt_dlp_missing', spec),
                'browser': spec}
    if not installed(spec):
        return {'ready': False, 'code': 'no_browser', 'message': describe('no_browser', spec),
                'browser': spec}
    return {'ready': True, 'confirmed': False, 'code': 'unverified', 'browser': spec,
            'message': f'已配置从 {LABELS.get(browser_of(spec), spec)} 读取登录态。'
                       '文件上看不出 cookie 是否过期，点「检查登录态」才会真连一次。'}


def check(settings, deadline=None) -> dict:
    """真连一次。两步:先列频道(不需要登录态),再取单条视频元数据(bot 墙在这一步)。"""
    import time

    from kb.feeds import registry as reg
    from kb.youtube import media
    from kb.youtube.channels import VIDEO_ID
    from kb.youtube.settings import of

    spec = of(settings)['cookies_browser']
    base = readiness(settings)
    if not base['ready']:
        return {**base, 'checked': True}
    deadline = deadline or (time.monotonic() + 120)
    source = next((item for item in reg.subscriptions('youtube') if item['enabled']), None)
    if not source:
        return {'ready': False, 'code': 'no_channel', 'message': describe('no_channel', spec),
                'browser': spec, 'checked': True}
    # **成功那一份也要把工具的警告收上来**(出参,口径见 `media._collect_listing`):
    # cookie 解密失败只是一条 warning,进程退 0、公开视频照旧列得出来 —— 不收的话这条
    # 铁证跟着临时输出一起被删,面板上只剩「取到了元数据」(审查实测)。
    noise = []
    try:
        listed = media.channel_videos(source, settings, deadline, lambda: False, limit=1, warnings=noise)
    except Exception as error:
        # 列频道这一步**本来不需要登录态**(实测裸跑可通),所以它失败时有把握的只有两档:
        # 「cookie 读不出来」和「出口撞上机器人验证」—— 两者都和这一步本身对得上。
        # 其余一律算连不上,不拿它冒充「没登录」:那会把人送去反复登录,而问题在另一头。
        code = coded(error)
        code = code if code in ('cookie_unreadable', 'bot_wall', 'rate_limited') else 'unreachable'
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': describe(code, spec), 'detail': evidence(error)}
    video_id = next((item['video_id'] for item in listed if VIDEO_ID.fullmatch(item['video_id'])), '')
    if not video_id:
        return {'ready': False, 'code': 'unreachable', 'browser': spec, 'checked': True,
                'message': describe('unreachable', spec),
                'detail': f"频道 {source['name']} 这一轮没有列出任何视频"}
    row = {'video_id': video_id, 'entry': {'url': 'https://www.youtube.com/watch?v=' + video_id}}
    try:
        media.probe(row, settings, deadline, lambda: False, force=True, warnings=noise)
    except Exception as error:
        code = coded(error)
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': describe(code, spec), 'detail': evidence(error)}
    # `confirmed` 和 `ready` 是两件事:ready=「这一轮没撞上任何已知故障」,
    # confirmed=「那份登录态被证实有效」。后者我们现在拿不到证据,就如实报 False ——
    # 面板按它显示中性而不是绿色结论。
    result = {'ready': True, 'confirmed': False, 'code': 'metadata_ok', 'browser': spec,
              'checked': True, 'message': describe('metadata_ok', spec)}
    # **取到了东西不等于可以把警告扔掉,但也不等于这一轮失败。** 分档照旧在完整文本上算
    # (这里的 `noise` 一个字都没裁过),然后和「未被证实」那句话**并列**显示:ready 仍是
    # True、`code` 仍是 `metadata_ok`,只多一条 `warning`。把它升级成「登录态无效」是另一
    # 头的错 —— 人刚刚明明取到了元数据。
    flagged = classify(' | '.join(noise)) if noise else ''
    if caution(flagged, spec):
        result['warning_code'] = flagged
        result['warning'] = caution(flagged, spec)
        result['detail'] = excerpt(' | '.join(noise), 400)
    return result
