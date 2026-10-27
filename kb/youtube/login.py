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
_COOKIE = ('could not find', 'cookies database', 'failed to decrypt', 'unable to decrypt',
           'cannot decrypt', 'no key found', 'unable to open database', 'permission denied',
           'no such file or directory', 'dpapi', 'keyring', 'secretstorage',
           'unsupported browser', 'could not copy')


def classify(text: str) -> str:
    """把 yt-dlp 的失败输出归成一类。

    **顺序就是口径**:cookie 读不出来最先判(那一步比登录态更靠前,后面的墙都是它的
    下游);其次是机器人验证和「这一条没授权」这两档 —— 它们都**不蕴含**未登录;
    真正指向登录态的只剩最后那一组。一个都不中就是 `unknown`,原文带回去给人看。
    """
    lowered = str(text or '').lower()
    if not lowered:
        return 'unknown'
    if any(mark in lowered for mark in _COOKIE):
        return 'cookie_unreadable'
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
    'bot_wall': 'YouTube 要求验证本机出口不是机器人。带上登录态通常能过这一道；'
                '已经登录还是这样的话，多半是这个出口 IP 被限了 —— 实测换 yt-dlp 版本没用，'
                '换出口或等一段时间才有用。',
    'no_access': '取到的那条视频本身不可访问（私有、会员专属或已下架）。'
                 '这不说明登录态有问题，换一条视频或另一个频道再检查一次。',
    'unknown': 'yt-dlp 报了一个没能归类的错误，原文附在下面。',
}


#: 「这一条失败和登录态那一摊有关」的全部档位。**归并同因失败时按这个集合判,
#: 不是只看 `not_logged_in`** —— 实测最常撞的恰恰是机器人验证那一档,只认一个档
#: 等于让最常见的那一类退回逐条重复(48 行同一句话)。
AUTH_CODES = ('not_logged_in', 'bot_wall', 'cookie_unreadable')


def describe(code: str, spec: str) -> str:
    return MESSAGES.get(code, MESSAGES['unknown']).format(label=LABELS.get(browser_of(spec), spec))


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
    try:
        listed = media.channel_videos(source, settings, deadline, lambda: False, limit=1)
    except Exception as error:
        # 列频道这一步**本来不需要登录态**(实测裸跑可通),所以它失败时有把握的只有两档:
        # 「cookie 读不出来」和「出口撞上机器人验证」—— 两者都和这一步本身对得上。
        # 其余一律算连不上,不拿它冒充「没登录」:那会把人送去反复登录,而问题在另一头。
        code = classify(str(error))
        code = code if code in ('cookie_unreadable', 'bot_wall') else 'unreachable'
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': describe(code, spec), 'detail': str(error)[:400]}
    video_id = next((item['video_id'] for item in listed if VIDEO_ID.fullmatch(item['video_id'])), '')
    if not video_id:
        return {'ready': False, 'code': 'unreachable', 'browser': spec, 'checked': True,
                'message': describe('unreachable', spec),
                'detail': f"频道 {source['name']} 这一轮没有列出任何视频"}
    row = {'video_id': video_id, 'entry': {'url': 'https://www.youtube.com/watch?v=' + video_id}}
    try:
        media.probe(row, settings, deadline, lambda: False, force=True)
    except Exception as error:
        code = classify(str(error))
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': describe(code, spec), 'detail': str(error)[:400]}
    # `confirmed` 和 `ready` 是两件事:ready=「这一轮没撞上任何已知故障」,
    # confirmed=「那份登录态被证实有效」。后者我们现在拿不到证据,就如实报 False ——
    # 面板按它显示中性而不是绿色结论。
    return {'ready': True, 'confirmed': False, 'code': 'metadata_ok', 'browser': spec,
            'checked': True, 'message': describe('metadata_ok', spec)}
