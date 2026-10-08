# -*- coding: utf-8 -*-
"""X 采集通道:同一批账号,三条互斥的取法。选哪条是一个设置。

| 通道 | 用谁的身份读 X | 要准备什么 | 边界精度 |
|---|---|---|---|
| `bridge`  | 桥接实例维护者的 | 一个返回 RSS 的 URL 模板 | 由 feed 自己给 |
| `feedgrab`| 一份 X cookie    | 装 feedgrab + 跑一次 login | 天 |
| `browser` | 你自己的登录态   | 在独立 profile 里登录一次 | 分钟 |

**通道只管「走桥接的那些 X 账号」**。填了专用订阅链接(`feed_url`)的订阅照旧走
HTTP、不受这里影响 —— 这条语义是现成的,而且是桥接全挂时唯一还能采的那批
(见 `registry.uses_bridge`);非 X 的 social 订阅(Bluesky 的 RSS 之类)同理。

**后两条通道不是 HTTP feed**,所以它们不走 `fetch_source` 里那套 endpoint /
etag / 公网校验,而是在采集阶段开始时**整批预取**一次、再按订阅各取自己那份:
起一个 Chrome 要两三秒,51 个账号各起一次就撞上 90 秒的采集预算;而同一个
浏览器 profile 同时只能被一个实例打开,并发也不成立。

**退役的通道名要留成别名**(`_RETIRED`):存量 `feed_settings` 里写着的可能正是
旧名字,认不出来就当成"没配"退回默认 —— 那是在用户没动过任何设置的情况下
静默换掉他选的取法。口径只有 `resolve()` 一份,所有读设置的地方都过它。
"""

from __future__ import annotations

import json
import os
import threading

from kb.config import DATA_DIR
from kb.source_types import feed_types

BRIDGE, FEEDGRAB, BROWSER = 'bridge', 'feedgrab', 'browser'
DEFAULT = BRIDGE
CHANNELS = (BRIDGE, FEEDGRAB, BROWSER)
LABELS = {
    BRIDGE: 'RSS 桥接',
    FEEDGRAB: 'feedgrab(cookie)',
    BROWSER: '我的浏览器登录态',
}
HINTS = {
    BRIDGE: '由第三方桥接服务把 X 转成 RSS。不占用你的账号,但公共实例常年不稳。',
    FEEDGRAB: '用一份 X cookie 调 X 自己的接口。建议用小号:拿 cookie 抓取违反 X 服务条款,'
              '账号有被限制的可能。',
    BROWSER: '在一个独立浏览器 profile 里登录一次,之后无头复用这份登录态。'
             '用的是你登录的那个账号,风险同上,而且封的是这个号。',
}
# 退役名 → 现用名。一个都不许删(见模块注释)。
_RETIRED: dict[str, str] = {}

# 浏览器 profile 和 feedgrab 的工作目录都放在 data/ 下:它们装着登录态,
# 跟着数据目录走(KB_DATA_DIR 指到别处时一起搬),不进版本库。
BROWSER_PROFILE = DATA_DIR / 'x-browser-profile'
FEEDGRAB_HOME = DATA_DIR / 'x-feedgrab'

# 这一轮预取的结果。collect 在起线程池**之前**填好,之后只读 —— 所以读侧
# 不用加锁;写侧仍然上锁,因为日报那条路可能和面板上的手动同步前后脚进来。
_harvest: dict[str, dict] = {}
_lock = threading.Lock()


def resolve(name: str) -> str:
    """把设置里存的名字翻成现用通道名。认不出来的退回默认。"""
    value = (name or '').strip()
    value = _RETIRED.get(value, value)
    return value if value in CHANNELS else DEFAULT


def label(name: str) -> str:
    """人话名字。退役名也要翻得出来 —— 历史运行记录里写着的可能正是它。"""
    return LABELS.get(resolve(name), name or DEFAULT)


def channel_of(settings: dict) -> str:
    return resolve(settings.get('x_channel', DEFAULT))


def supports_channels(kind: str) -> bool:
    """只有 social(X)这一类有通道可选。"""
    return feed_types()[kind].adapter == 'social'


def validate(settings: dict) -> None:
    """保存设置时的校验。抛 ValueError,文案直接给用户。"""
    channel = settings.get('x_channel', DEFAULT)
    if (channel or DEFAULT) not in CHANNELS and channel not in _RETIRED:
        raise ValueError(f'不认识的 X 采集通道:{channel}')
    for key in ('x_feedgrab_bin', 'x_chrome_path'):
        value = settings.get(key) or ''
        if value and not isinstance(value, str):
            raise ValueError('可执行文件路径要填成一行文本')
        if value and not os.path.isabs(value):
            raise ValueError('请填写可执行文件的绝对路径')


# ── 水位(按推文时间定边界)──────────────────────────────────────────────────
#
# 存在 `subscriptions.config` 这个 JSON 列里,每条订阅各一份:它是"这个账号
# 抓到哪儿了",不是全局设置。两条通道的精度不同(浏览器到分钟、feedgrab 到
# 天),所以分开两个键存 —— 混用一个的话,从浏览器切到 feedgrab 会拿一个带
# 时分的值去填 `X_USER_TWEETS_SINCE`(它只认日期),那一轮的边界就错了。


_WATER_KEY = {BROWSER: 'x_watermark_browser', FEEDGRAB: 'x_watermark_feedgrab'}


def _config(source: dict) -> dict:
    try:
        value = json.loads(source.get('config') or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def watermark(source: dict, channel: str) -> str:
    return str(_config(source).get(_WATER_KEY.get(channel, ''), '') or '')


def watermark_patch(channel: str, value: str) -> dict:
    """这条通道要写进 `subscriptions.config` 的那一个键。空值 = 什么都不要改。

    **返回补丁而不是直接写盘**:水位必须和条目在**同一个事务**里提交。原来是
    `take()` 当场存一次 —— 那是 fetch 的工作线程,而条目要等主线程的
    `sync._save_result` 才真正写进库,两个独立事务没有共同的成功/失败边界:
    `put_items` 失败那一轮水位已经推进了,这批没入库的推文比旧水位新、又比新
    水位老,从此**任何一轮增量都选不到它们**(审查实测:存盘失败后 `stored_items=0`
    而水位已前进,重试取回 0 条)。
    """
    key = _WATER_KEY.get(channel)
    return {key: value} if key and value else {}


def save_watermark(source_id: int, channel: str, value: str, conn=None) -> None:
    """只改自己那个键,别的键原样留着(另一条通道的水位、将来别人加的字段)。

    `conn` 非空时并进调用方那个事务(存盘那条路要的就是这个),否则自己开一个。
    """
    from kb.feeds import registry as reg
    if patch := watermark_patch(channel, value):
        reg.merge_config(source_id, patch, conn=conn)


# ── 整批预取 ────────────────────────────────────────────────────────────────


def _handle(source: dict) -> str:
    from kb.feeds import registry as reg
    try:
        return reg.x_handle(source['url'])
    except ValueError:
        return ''


def targets(sources: list[dict], settings: dict) -> list[dict]:
    """这一轮里该由非 HTTP 通道接管的订阅。"""
    from kb.feeds import registry as reg
    if channel_of(settings) == BRIDGE:
        return []
    return [s for s in sources if reg.uses_bridge(s) and _handle(s)]


def prefetch(sources: list[dict], settings: dict, *, since: str = '',
             budget: float = 0.0, on_progress=None, should_stop=None) -> dict:
    """抓完整批,把结果放进本轮缓存。返回 {handle: 结果} 以便汇报。

    失败不抛:每个账号的失败落在它自己那一项上,由 `take()` 翻成这条订阅的
    异常 —— 一个改了名的号不该把整轮采集带掉。

    `should_stop` 每个账号之间问一次:面板上那颗停止按钮承诺的是"当前阶段
    结束后停止",而这一批要跑十几分钟,不认它的话按下去要等满整轮预算。

    **`since` 是区间补抓那段日期的起点**(`FetchScope.since`,日常同步为空)。
    它是这条路唯一的逃生口:用户点名要补 10-05 那天,而日常水位已经停在 10-07,
    按水位读的话一条也翻不到那天 —— 下界必须换成用户给的日期(审查实测:传给
    采集器的仍是 10-07、`added=0`)。
    """
    channel = channel_of(settings)
    picked = targets(sources, settings)
    jobs = [(_handle(s), since or watermark(s, channel)) for s in picked]
    if not jobs:
        with _lock:
            _harvest.clear()
        return {}
    if channel == BROWSER:
        from kb.feeds import x_browser
        result = x_browser.harvest(
            jobs, profile=str(BROWSER_PROFILE), budget=budget,
            chrome_path=settings.get('x_chrome_path', ''), on_progress=on_progress,
            should_stop=should_stop)
    else:
        from kb.feeds import x_feedgrab
        result = x_feedgrab.harvest(
            jobs, binary=settings.get('x_feedgrab_bin', ''),
            data_dir=str(FEEDGRAB_HOME / 'sessions'),
            workdir=str(FEEDGRAB_HOME / 'work'), budget=budget,
            on_progress=on_progress, should_stop=should_stop)
    if since:
        # **区间补抓不推进日常增量水位**:它读的下界是用户点的那个日期,跟"上次
        # 读到哪儿了"没有关系。存下去就是拿一段指定的历史冒充增量进度 —— 水位和
        # 真实读取位置之间那一段从此任何一轮都选不到(口径同 `_save_result` 把
        # validators 重建回原值:区间补抓不许改任何缓存状态)。在这里摘掉而不是
        # 在 `take()` 里再判一次,是为了只有一处知道"这一轮是区间"。
        for got in result.values():
            got.pop('watermark', None)
    with _lock:
        _harvest.clear()
        _harvest.update(result)
    return result


def take(source: dict, settings: dict) -> tuple[list[dict], dict]:
    """取这条订阅在本轮预取里的那一份,形状和 `fetch_source` 的返回值一致。

    预取没跑到它(通道刚切、或者它压根不该走这条通道)时抛 LookupError,
    由调用方退回 HTTP —— 那比静默返回空要好:空会被记成"这个号今天没发推"。
    """
    channel = channel_of(settings)
    handle = _handle(source)
    with _lock:
        got = _harvest.get(handle)
    if got is None:
        raise LookupError(f'{label(channel)} 这一轮没有抓到 @{handle}')
    if got.get('error'):
        raise ValueError(got['error'])
    if note := got.get('skipped'):
        from kb.feeds.fetching import CollectionBudgetExceeded
        raise CollectionBudgetExceeded(note)
    # 这条路没有 etag / last-modified 可言,把三个校验字段原样留着:
    # 写回空串会把这条订阅之前走 HTTP 时攒下的缓存信息抹掉,切回 bridge
    # 通道时就要白拉一次全量。
    meta = {k: source.get(k, '') for k in ('etag', 'modified', 'resolved_url')}
    # 水位作为"要并进 config 的补丁"交回去,由存盘那一步和条目同一个事务提交
    # (见 `watermark_patch`)。这里是工作线程,当场写盘就和条目脱钩了。
    if patch := watermark_patch(channel, got.get('watermark', '')):
        meta['config_patch'] = patch
    # 「这一轮没读完」原样带回去:它不是失败(抓到的照旧入库),但也不是一次
    # 完整的同步 —— 面板上要看得见,否则"停在时间线半中间"和"全读完了"长得
    # 一模一样(水位那一侧已经由 harvest 留着旧值兜住,见 `x_browser.harvest`)。
    if note := got.get('incomplete'):
        meta['note'] = note
    return got['entries'], meta


def reset() -> None:
    with _lock:
        _harvest.clear()


def readiness(settings: dict) -> tuple[bool, str, str]:
    """当前通道能不能开工。返回 (就绪, 说明, 下一步)。

    只做**便宜**的判断(路径在不在、登录过没有),不去真连一次 —— 这个函数被
    来源面板每次刷新都调。真实结论以同步结果为准。
    """
    channel = channel_of(settings)
    if channel == BRIDGE:
        return bool(settings.get('bridges')), '', ''
    if channel == FEEDGRAB:
        from kb.feeds import x_feedgrab
        try:
            x_feedgrab.find_binary(settings.get('x_feedgrab_bin', ''))
        except Exception as e:                       # noqa: BLE001 - 成品文案
            return False, str(e), ''
        sessions = FEEDGRAB_HOME / 'sessions'
        if not any(sessions.glob('*twitter*')) and not any(sessions.glob('*x*')):
            return False, 'feedgrab 还没有 X 登录态', x_feedgrab.login_command(str(sessions))
        return True, '', ''
    # browser:profile 里有 Cookies 文件就算登录过。真失效要等抓取时才知道
    # (那时 `x_browser` 会认出登录页并给出重新登录的提示)。
    if not (BROWSER_PROFILE / 'Default' / 'Cookies').exists():
        return False, '浏览器通道还没有登录过 X', ''
    return True, '', ''


def login_command(channel: str) -> str:
    """这条通道要用户在终端里跑的那条命令。没有就是空串。

    feedgrab 的登录是交互式的(它自己会开浏览器、要用户扫码或贴 cookie),
    只能他本人在终端跑;浏览器通道则是点按钮开一个有头窗口(见
    `x_browser.open_login_window`),不需要命令行。
    """
    if resolve(channel) != FEEDGRAB:
        return ''
    from kb.feeds import x_feedgrab
    return x_feedgrab.login_command(str(FEEDGRAB_HOME / 'sessions'))


def check(settings: dict) -> tuple[bool, str]:
    """真连一次。返回 (就绪, 成品文案)。

    和 `readiness` 分成两个函数是因为代价差着三个数量级:那个每次刷新面板都
    调,这个是用户点「检查连接」才走的,会真起一个 Chrome 或真跑一次
    `feedgrab doctor`。桥接通道没有"连一次"可言 —— 它的健康状况只有在真抓
    的时候才知道(桥接实例挂没挂、限流没限流),所以这里直接说清楚。
    """
    channel = channel_of(settings)
    if channel == BRIDGE:
        if not settings.get('bridges'):
            raise ValueError('桥接通道还没有填任何桥接地址')
        return True, f'已配置 {len(settings["bridges"])} 个桥接地址。'\
                     '桥接实例是否可用要等真抓一次才知道'
    if channel == FEEDGRAB:
        from kb.feeds import x_feedgrab
        return x_feedgrab.doctor(settings.get('x_feedgrab_bin', ''),
                                 str(FEEDGRAB_HOME / 'sessions'))
    from kb.feeds import x_browser
    return x_browser.probe(str(BROWSER_PROFILE), settings.get('x_chrome_path', ''))
