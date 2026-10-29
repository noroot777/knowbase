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

from kb.config import DATA_DIR
from kb.youtube.diagnosis import (CAPPED, COOKIES, LOCAL, YOUTUBE, excerpt, heard, record, serial, shown,
                                  witnessed)

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


#: Chromium 系浏览器里,**每个扩展还有自己的一份 cookie 分区**,文件名也叫 `Cookies`,
#: 摆在 `<profile>/Storage/ext/<扩展 id>/<序号>/` 下面(Chrome 130+)。
_PARTITION = 'Storage'
#: 现代 Chromium 把主库挪进了 `<profile>/Network/`,旧布局还在 `<profile>/` 下。两种都认。
_STORE = ('Network/Cookies', 'Cookies')


#: 走 Chromium 那套 cookie 布局的浏览器 —— **只有它们有扩展分区这个问题**。
#: Firefox 的 cookie 全在一个 `cookies.sqlite` 里,Safari 是 `Cookies.binarycookies`,
#: 两者都没有「同名文件挑最新那一份」这回事,所以也不需要钉。
_CHROMIUM = ('chrome', 'edge', 'brave', 'chromium', 'vivaldi')


def browser_dir(spec: str) -> Path | None:
    """这个浏览器的用户数据目录。**和 yt-dlp 自己算出来的那一份必须是同一个**,
    因为 Windows 上的解密密钥要从它下面的 `Local State` 里取。"""
    for relative in _ROOTS.get(browser_of(spec), {}).get(platform_key(), []):
        candidate = _HOME / relative
        if candidate.is_dir():
            return candidate
    return None


def profile_of(spec: str) -> str:
    """规格串里点名的 profile;没点名就是空串(由 `cookie_store` 挑最近用过的那个)。"""
    text = str(spec or '').strip()
    return text.split(':', 1)[1].strip() if ':' in text else ''


def cookie_store(spec: str) -> Path | None:
    """这一轮该用哪一份 cookie 库。**只在 profile 一级找,不往下钻。**

    yt-dlp 自己的做法是 `max(os.walk(浏览器目录) 里所有叫 Cookies 的文件, key=mtime)`
    (`yt_dlp/cookies.py` 的 `_find_files` / `_newest`),本意是「多个 profile 就用最近
    用过的那个」。但 Chrome 130+ 把**每个扩展的 cookie 分区**也命名为 `Cookies` 放在
    `<profile>/Storage/ext/<id>/<n>/` 下,于是哪个扩展刚活动过,它就被当成「最近用过的
    profile」—— 实测本机:主库 `Default/Cookies` 983 KB、19:33:24,内置扩展那份
    `Default/Storage/ext/glic/.../Cookies` 32 KB、19:32:09,两者只差 75 秒,同一分钟里
    跑两次 yt-dlp 一次读到 1843 条(`Found YouTube account cookies`)、一次只读到 30 条
    (当场撞 `Sign in to confirm you're not a bot`)。
    **这就是「明明登录着、却时好时坏」的根因**:那一轮根本是以未登录身份出去的,而面板
    上只看得见下游那道墙,于是人被送去反复登录。

    所以这里保留 yt-dlp 的本意(按 mtime 挑最近用过的 profile),只把**扩展分区排除在
    候选之外** —— 判据是「只看 profile 一级的那两个固定落点」,不是去匹配 `ext` 这种
    会变的目录名。点名了 profile 就只认它那一份。
    """
    if _explicit(spec):
        return None
    named = profile_of(spec)
    try:
        root = browser_dir(spec)
        if root is None:
            return None
        profiles = [root / named] if named else \
            [item for item in root.iterdir() if item.is_dir() and item.name != _PARTITION]
        found = [path for profile in profiles for name in _STORE
                 if (path := profile / name).is_file()]
        return max(found, key=lambda path: path.stat().st_mtime, default=None)
    except OSError:
        # 这一层只是「挑哪一份」。读不动就当挑不出来,由调用方降级成只交浏览器名 ——
        # **它挂在每一次 yt-dlp 调用的前面,一个字都不许往上抛。** 守卫从 `browser_dir`
        # 那一步就罩上:`Path.is_dir()` 只替常见的那几个 errno 兜底,别的照旧往外抛。
        return None


def _explicit(spec: str) -> bool:
    """用户自己在设置里给了一条 profile 路径吗。**口径只有一份** —— `cookie_store` 据它
    不替人改,`arguments` 据它不去提醒「没钉住」:那是他的明确选择,不是一次降级。
    """
    named = profile_of(spec)
    return bool(named) and ('/' in named or '\\' in named or named.startswith('~'))


def _link(source: Path, target: Path) -> None:
    """把 `target` 原子地指向 `source`。**先建临时名再 `os.replace`** —— 两轮采集同时
    刷这份视图时,直接 unlink 再 symlink 会留下一个「链接不在」的窗口。
    """
    #: **临时名要逐调用唯一,光有 pid 不够。** 真实并发是同一个进程里的两个线程(面板那颗
    #: 「检查登录态」在 FastAPI 的线程里,同步在后台线程里),它们的 pid 一样:线程 A 刚建好
    #: `Cookies.<pid>.tmp`,线程 B 进来把它 unlink 掉,A 的 `os.replace` 就撞 FileNotFoundError ——
    #: 而 `cookie_view` 把准备失败降级成「只交浏览器名」,于是 yt-dlp 又回去在整个浏览器目录里
    #: 挑最近被写过的那一份,**正是这一轮要修的原问题**。序号走 `diagnosis.serial()`:进程内
    #: 那个计数器只有一份(`media.unique` 给输出文件编号也走它),pid + 序号合起来才唯一。
    scratch = target.with_name(f'{target.name}.{os.getpid()}.{serial()}.tmp')
    scratch.unlink(missing_ok=True)
    try:
        os.symlink(source, scratch)
    except (OSError, NotImplementedError):
        # Windows 上建符号链接要特权(口径同 `docs/decisions/windows-entry.md`);
        # 同卷的硬链接不要,所以退一步试它。两样都不行就由调用方降级。
        os.link(source, scratch)
    os.replace(scratch, target)


def cookie_view(spec: str) -> dict:
    """给 yt-dlp 的 cookie 来源。**我们仍然不碰 cookie 本身** —— 只是把「用哪一份库」
    从「谁最后被写过」改成「哪个 profile 最后被用过」。

    做法是在数据目录下搭一份**只有两个链接**的视图:`Local State` 和
    `<profile>/Cookies` 各指向浏览器里那一份真文件,然后把这个视图的 profile 路径交给
    `--cookies-from-browser`。yt-dlp 在视图里走一遍 `os.walk` 只会看到那一个 `Cookies`,
    扩展分区压根不在里面;`Local State` 之所以要一起链上,是因为 Windows 的解密密钥从
    `视图根/Local State` 读(`yt_dlp/cookies.py` 的 `get_cookie_decryptor`),而视图根
    正是 profile 路径的上一级。**一个字节都不复制,也不读它们的内容。**

    搭不起来(建不了链接、找不到主库、Firefox/Safari 这类另一套布局)就**如实降级**:
    照旧只交浏览器名,`pinned` 报 False、`reason` 说清为什么 —— 面板那一句提醒靠它。
    """
    spec = validate(spec)
    if _explicit(spec):
        # 点名了路径就照他说的走。**这不算降级**,所以 `reason` 空着 —— 它是「要不要提醒」
        # 的唯一判据(见 `arguments`),给了理由就等于每一轮都去提醒一件他自己选的事。
        return {'args': ['--cookies-from-browser', spec], 'pinned': False, 'store': '', 'reason': ''}
    store = cookie_store(spec)
    if store is None:
        return {'args': ['--cookies-from-browser', spec], 'pinned': False, 'store': '',
                'reason': '没在这个浏览器的 profile 一级找到 cookie 主库'}
    # 主库在 `<profile>/Network/Cookies` 还是 `<profile>/Cookies`,profile 目录都往上数一级到它。
    source = store.parent.parent if store.parent.name == 'Network' else store.parent
    view = DATA_DIR / 'youtube' / 'cookie-view' / browser_of(spec)
    profile = view / source.name
    try:
        profile.mkdir(parents=True, exist_ok=True)
        _link(store, profile / 'Cookies')
        state = source.parent / 'Local State'
        if state.is_file():
            _link(state, view / 'Local State')
    except OSError as error:
        return {'args': ['--cookies-from-browser', spec], 'pinned': False, 'store': str(store),
                'reason': f'建不了 cookie 视图（{error}）'}
    return {'args': ['--cookies-from-browser', f'{browser_of(spec)}:{profile}'],
            'pinned': True, 'store': str(store), 'reason': ''}


def arguments(spec: str) -> list[str]:
    """交给 yt-dlp 的那两个参数。校验过才拼,不把用户输入原样塞进命令。

    **这一次到底用了哪一份库,就在这一刻记进账**(`diagnosis.record`,`COOKIES` 那条道)。
    原先是让面板事后再调一次 `cookie_view` 看「现在钉不钉得住」—— 而那是**另一次**准备:
    刚才那一次因为线程撞车或者权限不足降级了、这一次又建成功,面板就一个字都不说,
    人看到的是一次干净的失败,而它恰恰是以未登录身份出去的(形态同 `cookie_store`)。
    **对一次读取做出的结论必须跟那次调用绑在一起,不能由「后来又建成功一次」推出来。**
    同步那条路同样读这本账(`collector.caution`),所以两个入口的口径也只有一份。

    记的是**降级的理由**,不是文案:说法只有一份,在 `CAUTIONS[UNPINNED]`。
    `record` 在轮外是空操作(没人开账),所以直接调它的单元测试不受影响。
    """
    view = cookie_view(spec)
    if not view['pinned'] and view['reason'] and browser_of(spec) in _CHROMIUM:
        record(view['reason'], COOKIES)
    return view['args']


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
#: **JS 挑战没解开单独一档。** YouTube 现在会对一部分请求发一道 JS 挑战,yt-dlp 要用
#: 本地的解题脚本(`yt-dlp-ejs` 这个 Python 包)配一个 JS 运行时去算;两样缺一样,它就只能
#: 拿到预览图那几个格式,接着默认的格式选择器一个都
#: 匹配不上。实测复现(cookie 完全正常、`Found YouTube account cookies` 1843 条):
#: `n challenge solving failed` → `Only images are available for download` →
#: `Requested format is not available`。**这一档和登录态无关**,照 `unknown` 报的那一版
#: 让人去反复检查登录,而要做的是在面板上更新一次 yt-dlp(连解题脚本一起装)。
#: **只认挑战本身那几句话**:光有「格式不可用」不算 —— 那也可能是这条视频真的没有音轨,
#: 那时让人去装 JS 运行时是帮倒忙(口径同「判不准一律落 unknown」)。
#: **要让人装的是 deno,不是「deno / node / bun / quickjs 随便一个」。** yt-dlp 支持这四个,
#: 但 `--js-runtimes` 的默认值就是 `['deno']` 一项(一手源码 `yt_dlp/options.py` 的
#: `default=['deno']`,帮助里也写着 Only "deno" is enabled by default),**没显式启用的那三个
#: 它压根不会用**。本机实测:`node` 和 `deno` 都装着,`yt-dlp -v` 的抬头只印
#: `JS runtimes: deno-2.9.0`;显式加一次 `--js-runtimes node` 才变成 `deno-2.9.0, node-26.4.0`。
#: 所以照「装个 node 就行」说等于让人白装一趟,而面板上照旧提示缺运行时。
#: **我们也不替他把那三个启用上**:未知开关会让 yt-dlp 当场 `no such option` 退出
#: (实测),而自备那份的版本由用户自己点更新决定 —— 为此再探一次「这版认不认这个开关」
#: 要在每一次调用前面加一道缓存,缓存错了就是**每一条路全断**。代价不对等,所以只说实话。
RUNTIME = 'deno'

_CHALLENGE = ('challenge solving failed', 'challenge solver', 'only images are available',
              'jsc:', '[jsc]')
_COOKIE = ('could not find', 'cookies database', 'failed to decrypt', 'unable to decrypt',
           'cannot decrypt', 'no key found', 'unable to open database', 'permission denied',
           'no such file or directory', 'dpapi', 'keyring', 'secretstorage',
           'unsupported browser', 'could not copy')


def limited(text: str) -> bool:
    """这份输出里**出现过**出口限流没有。**和 `classify` 的单一分档是两件事。**

    分档回答的是「这一轮最该跟人说哪一句」,所以 cookie 读不出来排在最前(它是因,
    墙是果);而「同一份输出里出现过 429」是另一件**独立的事实**,熔断要用它。两件事挤在
    一个返回值里的后果:同一次输出既报 cookie 解密失败又报 429 时,分档说 cookie、
    于是限流一笔都没记进本轮,这一轮照旧把剩下的频道查完(审查实测 `checked=2`)。
    特征串只有一份(`_LIMIT`),`classify` 读的也是它。
    """
    lowered = str(text or '').lower()
    return any(mark in lowered for mark in _LIMIT)


#: **分档只有这一张表:左边是档,右边是它认的特征串,从上往下第一个中的说了算。**
#: 顺序就是口径:cookie 读不出来最先判(那一步比登录态更靠前,后面的墙都是它的下游);
#: 接着是出口被限流(429 连网页都没下下来,它下面那些墙同样是果);其次是机器人验证和
#: 「这一条没授权」这两档 —— 它们都**不蕴含**未登录;真正指向登录态的那一组再往后;
#: **挑战排在最末**:上面每一档都是「人要去做一件具体的事」而且都更靠前,429 和机器人
#: 验证甚至会让挑战这一步根本跑不到。
#:
#: **摆成一张表是为了让「有哪几档」这件事只有一份来源**(`VERDICTS`)。原先是一串
#: `if`,而归并层(`merge`)拿按证据强度排的 `_STRENGTH` **兼着当白名单** —— 加
#: `js_challenge` 那一轮没人记得同步第二份:同步时明明判出了「挑战没解开」,一过归并
#: 就变成 `undecided`、完整原文跟着清空,视频失败详情里只剩开头那截普通输出
#: (审查用真实子进程复现)。**谁都会忘记同步第二份名单,所以不许有第二份。**
_MARKS = (
    ('cookie_unreadable', _COOKIE),
    ('rate_limited', _LIMIT),
    ('bot_wall', _BOT),
    ('no_access', _PRIVATE),
    ('not_logged_in', _WALL),
    ('js_challenge', _CHALLENGE),
)

#: `classify` 判得出来的那几档(不含 `unknown`)。**「已经判出来了」的唯一一份名单** ——
#: 归并层用它当白名单,别处要列全部分档也从它来。
VERDICTS = tuple(code for code, _ in _MARKS)


def classify(text: str) -> str:
    """把 yt-dlp 的失败输出归成一类。顺序和名单都在 `_MARKS` 那张表上。

    一个都不中就是 `unknown`,原文带回去给人看。
    """
    lowered = str(text or '').lower()
    if not lowered:
        return 'unknown'
    for code, marks in _MARKS:
        if any(mark in lowered for mark in marks):
            return code
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
    'js_challenge': 'YouTube 这一轮发了一道 JS 挑战，yt-dlp 没能解开，所以只拿到了预览图格式。'
                    '这不是登录态的问题（带着有效登录态也会撞上）：yt-dlp 要用本地解题脚本'
                    '（yt-dlp-ejs）配一个 JS 运行时才能算。在下面更新一次 yt-dlp，'
                    '解题脚本会跟着一起装上；运行时要装的是 deno —— '
                    'yt-dlp 支持好几个，但只有它默认启用，别的装了也不会被用上。'
                    '元数据和字幕不受这一道影响，受影响的是下音频做本地转录那一步。',
    # **本机这一侧的档**(`diagnosis.CAPPED`):原文里一定还混着 yt-dlp 自己的警告,
    # 不在这儿把话说死,人只能照那几句去更新 yt-dlp、装 deno —— 照做了下一轮还是一样。
    CAPPED: 'yt-dlp 其实取到了东西，是 kb 本机给工具输出设的上限把那一行丢掉了：'
            '这不是 YouTube、登录态或 yt-dlp 版本的问题，原文里同时出现的其他警告也不是这次失败的原因。'
            '换一条视频或另一个频道再检查一次；总是同一条的话，把下面的原文附上反馈。',
    'unknown': 'yt-dlp 报了一个没能归类的错误，原文附在下面。',
}


#: 「同一处卡住、会在每个频道上一字不差重复一遍」的那几档。**归并同因失败时按这个集合判,
#: 不是只看 `not_logged_in`** —— 实测最常撞的恰恰是机器人验证和出口限流那两档,只认一个档
#: 等于让最常见的那一类退回逐条重复(48 行同一句话)。
#: `rate_limited` 严格说不是登录态问题,但它和这几档同一个形态:一个出口、一次全中,而且
#: 用户第一反应正是「是不是没登录」—— 归并那一句话恰恰是用来说清它不是的。
#: `js_challenge` 也在这里:它同样是「一个出口、一次全中」—— 48 个频道会一字不差地各报
#: 一遍同一句话,不归并就得逐条读完才明白只有一个原因。
#: **这是 `VERDICTS` 的一个刻意的子集**(今天只少 `no_access` —— 那一档说的是「这一条内容
#: 本身不可访问」,逐条各不相同,归并了反而看不出是哪几条)。**不从 `VERDICTS` 派生**:
#: 新加一档该不该整组归并要人判一次,而不是默默跟上。逼人判的是那条集合级回归
#: (`tests/test_youtube_login.py` 比的是整个集合),加档时它当场红。
#: 这一点和归并层不一样:那边**必须**自动跟上 —— 漏在那儿是静默丢掉分档和原文(见 `merge`),
#: 漏在这儿只是这一档的失败不成组,面板上每一条照旧看得见。
AUTH_CODES = ('not_logged_in', 'bot_wall', 'cookie_unreadable', 'rate_limited', 'js_challenge')


#: 「这一轮确实取到了东西,但工具同时报了这么一条」的说法。**和 `MESSAGES` 分开写,
#: 不许复用**:同一个分档在这两处的结论完全不同 —— 那边是「这一轮没成」,这边是
#: 「成了,但这份成功可能不算数」。拿 `MESSAGES` 顶替等于把一条 cookie 警告升级成
#: 「登录态无效」,而用户明明刚取到了元数据(审查点名:并列显示证据和未确认状态)。
#: 「这一轮没能把 cookie 主库钉在 profile 一级」。**不是 `classify` 算出来的档** ——
#: 它说的是我们自己这一侧准备得怎么样,跟 yt-dlp 说了什么无关,所以永远不会从
#: `classify` 出来,也不进 `AUTH_CODES`(那几档说的是「登录态本身怎么了」)。
UNPINNED = 'cookie_unpinned'

CAUTIONS = {
    # **都不许断言「内容已经到手」** —— 同一句话也用在失败路的摘要上(见
    # `collector.heeded`):警告证明的是「请求发出去了、没带上登录态」,不是产物。
    'cookie_unreadable': '但 yt-dlp 同时报了读不出 {label} 的 cookie：这一轮很可能根本没带上登录态。'
                         'macOS 去钥匙串放行、Windows 先完全退出浏览器，或换一个 profile，'
                         '再检查一次。',
    'rate_limited': '但 yt-dlp 同时报了 429（请求过于频繁）：这一轮算侥幸过的，隔一段时间再检查一次更有把握。',
    'bot_wall': '但 yt-dlp 同时报了机器人验证：这个出口已经被盯上，下一轮可能就过不去了。',
    'not_logged_in': '但 yt-dlp 同时报了登录态失效：真正用得上它的字幕和音频这一轮没验到。',
    # **不许说「只拿到元数据和字幕」** —— 这句话也挂在失败路的摘要上(见 `collector.heeded`),
    # 而那条链很可能一条字幕都没有:审查实测的那一轮是「没有字幕、去下音频、撞上挑战」,
    # 面板却并列说一句「这一轮只拿到元数据和字幕」。只说得住的那半截:**哪几步需要真实
    # 格式**。这是表头那条「都不许断言内容已经到手」在这一档上漏掉的那一处。
    'js_challenge': '但 yt-dlp 同时报了那道 JS 挑战没解开：要真实音视频格式的那几步'
                    '（下音频做本地转录）过不去；元数据和字幕不需要格式，不受这一道影响。'
                    '在下面更新一次 yt-dlp 把解题脚本装上，'
                    '机器上没有 deno 的话也要装一个 —— yt-dlp 只默认启用它这一个运行时。',
    UNPINNED: '另外，这一轮没能把 {label} 的 cookie 主库钉在 profile 一级：yt-dlp 会在整个浏览器'
              '目录里挑「最近被写过的那一份 Cookies」，而每个扩展也各有一份同名的 cookie 分区 —— '
              '挑错时这一轮就是以未登录身份出去的。换一个浏览器，或者在设置里点名 profile'
              '（例如 chrome:Default）可以绕开。',
}


def describe(code: str, spec: str) -> str:
    return MESSAGES.get(code, MESSAGES['unknown']).format(label=LABELS.get(browser_of(spec), spec))


def cautioned(code: str, spec: str) -> str:
    """「这一轮取到了,但工具同时报了这么一条」的那句话。**不认的档返回空串。**

    空串就是「这条警告不值得在面板上说一句」:`unknown` 那一档什么都证明不了,
    而 yt-dlp 平时的退路警告(换 player 客户端、通用提取器)每一轮都有好几条 ——
    一律照搬会把真正要紧的那条 cookie 警告埋掉。口径同 `login.check` 的 `CAUTIONS`。
    """
    return CAUTIONS.get(code, '').format(label=LABELS.get(browser_of(spec), spec))


def caution(code: str, spec: str) -> str:
    """成功那一份结论旁边要并列的那句话。没有对应说法就返回空字符串(面板按空不显示)。"""
    wording = CAUTIONS.get(code, '')
    return wording.format(label=LABELS.get(browser_of(spec), spec)) if wording else ''


def evidence(error, limit: int = 400) -> str:
    """给人看的那份「yt-dlp 原文」,限长按**订阅失败明细**那一处的习惯。

    **实现只有一份,在 `diagnosis.shown`** —— 这儿留个名字是因为调用方都这么叫它。
    原先这儿自己算一遍、`storage.failed` 又自己算一遍(`str(error)[:1000]`),于是
    频道那一侧保住了原文、视频行那一侧丢了(审查实测,见 `shown` 的 docstring)。
    """
    return shown(error, limit)


#: 归并之后的结论:**逐条都判过了,一条可用的 YouTube 分档都没有。**
#: **和 `classify` 算出来的 `unknown` 分开写**:算出来的那种只说明「这一份文本没中任何
#: 特征」,`coded` 照旧该去看完整诊断;这一档是「别再判了」。今天每个生产方的 `code` 都是
#: 在和 `diagnostic` 同一份文本上算出来的,所以两种读法恰好一致 —— **这是冗余带**(代价
#: 就一个常量),别把两个意思合回同一个值:哪天某处的分档改成在另一份文本上算
#: (`_collect_listing` 的 `code` 取自解析错误、`diagnostic` 取自工具输出,就差一步),
#: 合着写就会把「回去看完整诊断」那一道一起关掉。这一档只许 `merge` 写。
UNDECIDED = 'undecided'


def coded(error) -> str:
    """从一个异常里取出分档。**口径只有一份,消费方不许各自 `classify(str(error))`。**

    带着 `code` 的(`diagnosis.ToolFailure` 在**未裁切**的完整诊断上算过了)直接用它;
    没带的才拿消息现算 —— 异常消息为了给人看已经裁过两头,决定性那一行可能正好在
    被省略的中段(审查实测:长诊断里的 429 这样丢掉,整次失败被判成 bot 墙)。

    **原文里出现过 429 就先说它**(`limited`):429 是「网页根本没下下来」,后面那些墙
    都是它的下游,而这一轮正是因为它停的 —— 说成别的档会让面板自相矛盾(审查实测:
    一边说「已经因请求过多停止」,一边说「带上登录态通常能过这一道」,照后一句去重新登录
    没有用)。这一条也顺带盖住「分档被 cookie 那一档占住」的那一半。

    **这里的每一档说的都是 YouTube**,所以只认 YouTube 那个上游报的异常
    (`diagnosis.YOUTUBE`)。模型服务和本机工具同样会说 `429`、`permission denied`,
    按文本一判就成了「YouTube 网页没下下来」「读不出 cookie」—— 状态码相同不能证明请求
    来自同一个服务(审查实测:模型配额那一轮,面板让用户去换出口)。没标上游的按 YouTube
    算,理由在 `diagnosis` 那几个常量上面。
    """
    if getattr(error, 'origin', YOUTUBE) != YOUTUBE:
        # 别的上游只有一档算数:**本机那道输出上限**(它有自己的说法,见 `MESSAGES`)。
        return CAPPED if getattr(error, 'code', '') == CAPPED else 'unknown'
    if getattr(error, 'code', '') == UNDECIDED:
        # **已经判过、判不出来**(见 `merge` 和 `UNDECIDED`):这里再拿**拼起来的那段
        # 文本**判一次,就会把别的上游那句 `permission denied` / `429` 当成 YouTube 的墙
        # —— 丢来源的那条老路换个入口又走回来了。没判过的(`code` 为空、或算出来的
        # `unknown`)照旧往下走,它们还指望下面那两道。
        return 'unknown'
    if limited(getattr(error, 'diagnostic', '') or str(error)):
        return 'rate_limited'
    code = getattr(error, 'code', '')
    return code if code in MESSAGES else classify(str(error))


#: 归并多条失败时的证据强度。**不按到达先后选** —— 先后只是时间,不是证据强度:
#: 逐条解析先报机器人验证、后路 flat 随后报 429,按先后选就对外说成「带上登录态通常能过」,
#: 而这一轮正是因为 429 停的(审查实测:`walls={'bot_wall': …}`,摘要自相矛盾)。
#: **429 在这里排第一**(和 `classify` 里 cookie 优先不同):`classify` 排的是同一份输出里
#: 几个特征的**因果**,这里排的是几次独立失败里「哪一条解释得了这一轮为什么停」。
#: 余下几档照 `classify` 的因果序。**`unknown` 不算结论**,一个都不选。
_STRENGTH = ('rate_limited', 'cookie_unreadable', 'bot_wall', 'not_logged_in', 'no_access')


def strength(code: str) -> int:
    """归并时这一档的证据强度(小的赢)。**这儿只排顺序,不决定「算不算判出来了」** ——
    后者的唯一名单是 `VERDICTS`。

    **没列进 `_STRENGTH` 的已识别档排在最后,照旧穿得过归并。** 两份名单的顺序本来就
    不一样(`classify` 把 cookie 排第一,这儿把 429 排第一:前者问「这段文本说的是什么」,
    后者问「这几条里哪一条最能解释这次失败」),所以不能合成一份;但**漏在这一份里顶多
    是优先级垫底,不许变成「判不出来」**。`js_challenge` 今天正走这条兜底路:它的证据强度
    确实最弱(理由同 `_MARKS` 里它排最末那一段),所以不另外列一行 —— 这也让兜底是生产
    路径而不是只有用例跑过的死代码。
    """
    return _STRENGTH.index(code) if code in _STRENGTH else len(_STRENGTH)


def merge(carrier, errors):
    """把这几条失败里**最有解释力的那一档**抄到 `carrier` 上,再把它交出去。

    合并多条原因的地方(发现的两条路、字幕的几种语言)原先只留文字 —— 分档和完整诊断
    一起丢了,上层只能回去拿裁过的文本重新猜(理由见 `coded`)。各条原因的**原话照旧
    都在** `carrier` 自己的消息里,这里挑的只是「对外说哪一档、附哪一份原文」。
    只认 `YOUTUBE` 那一档:别的上游的结论不该被当成 YouTube 的墙。

    **白名单是 `VERDICTS`(判出来了没有),排序是 `strength()`(哪一条更能解释)** ——
    两件事分开。合着用过一次,后果见 `_MARKS` 上面那段。
    """
    ranked = sorted((error for error in errors
                     if getattr(error, 'origin', YOUTUBE) == YOUTUBE and coded(error) in VERDICTS),
                    key=lambda error: strength(coded(error)))
    if ranked:
        carrier.code = coded(ranked[0])
        carrier.diagnostic = getattr(ranked[0], 'diagnostic', '')
        carrier.origin = YOUTUBE
    elif any(coded(error) == CAPPED for error in errors):
        # 排不上名次、但其中一条是**本机上限丢的**:那就是这一轮的原因,不许被归并成
        # 「判不出来」再落回「没能归类」(那正是让人照着别的警告去更新 yt-dlp 的那一幕)。
        own = next(error for error in errors if coded(error) == CAPPED)
        carrier.code, carrier.origin = CAPPED, LOCAL
        carrier.diagnostic = getattr(own, 'diagnostic', '')
        return carrier
    elif errors:
        # **一条都排不上名次时,结论是「判不出来」,不是「没判」。** 留空的话 `coded`
        # 会拿 `carrier` 那段拼起来的消息重新分类 —— 本机的 `permission denied` 和
        # 模型服务的 `429` 混在里面,于是又被说成「读不出 cookie」「出口被限流」
        # (触发例:详细读取本机写盘失败、flat 后路随后普通超时)。
        # **原话一律不动**:`carrier` 自己的消息里每条原因都在,`evidence` 从那儿取。
        carrier.code = UNDECIDED
    if not ranked and errors and all(getattr(error, 'origin', YOUTUBE) != YOUTUBE for error in errors):
        # **一条 YouTube 的都没有时,输出绝不许默认成 YouTube。** 不标来源的话
        # `coded` 按 YouTube 算,于是本机那一侧的 `permission denied` 经过这道归并
        # 又被判回「读不出 cookie」,面板让人去钥匙串放行,而问题在磁盘上(审查实测:
        # 工具输出目录不可写,直接调用时 origin=local 判得对,过一次归并就错了)。
        # 混着 YouTube 的那种照旧按默认走:里面确实有一条 YouTube 的失败,
        # 标成本机会把 cookie 那一档的线索一起丢掉。
        carrier.origin = getattr(errors[0], 'origin', YOUTUBE)
        carrier.diagnostic = getattr(errors[0], 'diagnostic', '')
    return carrier


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
    """真连一次。两步:先列频道(不需要登录态),再取单条视频元数据(bot 墙在这一步)。

    **整次检查一本诊断账**(`diagnosis.heard`):每一步的原话在 `media.run` 的完成边界
    就记下了,所以**哪一步失败都抹不掉前面那一步说过的话** —— 列频道时明说「读不出
    cookie」、随后 probe 普通超时的那一轮,原先只按后一条异常出结论,面板上只剩
    「yt-dlp 报了一个没能归类的错误」,先前那条铁证一个字都没有(审查实测)。
    """
    import time

    from kb.youtube.settings import of

    spec = of(settings)['cookies_browser']
    with heard() as ledger:
        return _noticed(_check(settings, deadline, spec), ledger, spec)


def _noticed(result: dict, ledger, spec: str) -> dict:
    """把这一次检查里工具说过的话并列挂在结论旁边。**成功和失败同一个出口。**

    分档照旧在**完整文本**上算(账上那几份一个字都没裁过),然后和结论并列显示:
    取到了元数据那一轮 `ready` 仍是 `True`、`code` 仍是 `metadata_ok`,只多一条
    `warning` —— 把它升级成「登录态无效」是另一头的错,人刚刚明明取到了元数据。
    失败那一轮同理只是**并列**:`code` 还是那次失败自己的档,不许被先前那条警告顶掉
    (两件事可以同时成立,而「这一步为什么没成」是人要先知道的那一件)。
    **只认 YouTube 那个上游说的**:本机工具的报错不进这套判据(`diagnosis.witnessed`)。
    (`heard` / `witnessed` 从 `diagnosis` 直接引进来 —— 那一层不认识 `login`,没有环。)
    **`detail` 不许盖掉失败自己的原文**:那一份是人唯一的线索,警告另挂一行。
    """
    said = witnessed(ledger)
    # **失败已经判成本机上限丢的那一行时,不拿同一份输出再判一次**:那几句 yt-dlp 警告正是
    # 当初冒名顶替这次失败的东西,并列挂出来等于把「去更新 yt-dlp」又说一遍(审查实测)。
    flagged = classify(' | '.join(said)) if said and result.get('code') != CAPPED else ''
    wording = caution(flagged, spec)
    # 这一次**实际用的**是哪一份 cookie 库:从账上读(见 `arguments`),不事后再准备一次
    # 看「现在钉不钉得住」。同一段里几次调用说的是同一句话,按原话去重、不堆 N 遍。
    unpinned = list(dict.fromkeys(witnessed(ledger, COOKIES)))
    if unpinned:
        # **并列,不互相顶掉。** 两件事可以同时成立,而且下一步不一样(一个去钥匙串放行,
        # 一个换浏览器或点名 profile)。只有这一条时它就是这次并列警告的档。
        flagged = flagged if wording else UNPINNED
        wording = ' '.join(filter(None, [wording, cautioned(UNPINNED, spec)]))
    if not wording:
        return result
    result['warning_code'] = flagged
    result['warning'] = wording
    result.setdefault('detail', excerpt(' | '.join([*said, *unpinned]), 400))
    return result


def _check(settings, deadline, spec) -> dict:
    """真连那两步本身。**结论由 `check` 统一配上这一次的诊断**,所以这儿只管判这一步。"""
    import time

    from kb.feeds import registry as reg
    from kb.youtube import media
    from kb.youtube.channels import VIDEO_ID

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
        code = coded(error)
        # 本机上限那一档照旧说它自己(说成「连不上」会让人去查网络)。
        code = code if code in ('cookie_unreadable', 'bot_wall', 'rate_limited', CAPPED) else 'unreachable'
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': describe(code, spec), 'detail': evidence(error)}
    video_id = next((item['video_id'] for item in listed if VIDEO_ID.fullmatch(item['video_id'])), '')
    if not video_id:
        return {'ready': False, 'code': 'unreachable', 'browser': spec, 'checked': True,
                'message': describe('unreachable', spec),
                'detail': f"频道 {source['name']} 这一轮没有列出任何视频"}
    row = {'video_id': video_id, 'entry': {'url': 'https://www.youtube.com/watch?v=' + video_id}}
    try:
        media.probe(row, settings, deadline, lambda: False, force=True)
    except Exception as error:
        code = coded(error)
        return {'ready': False, 'code': code, 'browser': spec, 'checked': True,
                'message': describe(code, spec), 'detail': evidence(error)}
    # `confirmed` 和 `ready` 是两件事:ready=「这一轮没撞上任何已知故障」,
    # confirmed=「那份登录态被证实有效」。后者我们现在拿不到证据,就如实报 False ——
    # 面板按它显示中性而不是绿色结论。
    # `confirmed` 和 `ready` 是两件事,见上面那段注释;这一份的警告由 `check` 配上。
    return {'ready': True, 'confirmed': False, 'code': 'metadata_ok', 'browser': spec,
            'checked': True, 'message': describe('metadata_ok', spec)}
