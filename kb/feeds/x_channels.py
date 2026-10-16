# -*- coding: utf-8 -*-
"""X 采集通道:同一批账号,三条互斥的取法。选哪条是一个设置。

**硬约束在 `docs/decisions/x-channels.md` 第一节 —— 改这块之前先读那一份。**

| 通道 | 用谁的身份读 X | 要准备什么 | 边界精度 |
|---|---|---|---|
| `feedgrab`| 一份 X cookie    | 装 feedgrab + 跑一次 login | 天 |
| `browser` | 你自己的登录态   | 在独立 profile 里登录一次 | 分钟 |
| `attach`  | 你自己的登录态   | 在日常浏览器里开一次调试开关 | 分钟 |

**`bridge`(第三方 RSS 桥接)这一档已经删掉**:公共实例常年不稳,而三条登录态
通道都已经能无人值守跑。它只以**退役别名**的形式活着(见下面 `_RETIRED`)——
存量设置里写着 `bridge` 的,解析成现在的默认档。

**后两条的差别是「谁的浏览器」**:`browser` 我们自己起一个独立 profile 的无头
Chrome(所以要在那里面单独登录一次,但不依赖用户在不在);`attach` 直连用户
日常那个浏览器(登录态现成,但只在它开着、而且开了远程调试时能用)。对照表和
那条开关的代价写在 `kb/feeds/cdp_attach.py` 的模块注释里。

**通道只管「没填专用订阅链接的那些 X 账号」**。填了 `feed_url` 的订阅照旧走
HTTP、不受这里影响(见 `registry.uses_channel`);非 X 的 social 订阅
(Bluesky 的 RSS 之类)同理。

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from kb.config import DATA_DIR
from kb.source_types import feed_types

FEEDGRAB, BROWSER, ATTACH = 'feedgrab', 'browser', 'attach'
DEFAULT = BROWSER
CHANNELS = (BROWSER, ATTACH, FEEDGRAB)
LABELS = {
    FEEDGRAB: 'feedgrab(cookie)',
    BROWSER: '独立 profile 的登录态',
    ATTACH: '我日常用的浏览器',
}
HINTS = {
    FEEDGRAB: '用一份 X cookie 调 X 自己的接口。建议用小号:拿 cookie 抓取违反 X 服务条款,'
              '账号有被限制的可能。',
    BROWSER: '在一个独立浏览器 profile 里登录一次,之后无头复用这份登录态。'
             '用的是你登录的那个账号,风险同上,而且封的是这个号。',
    ATTACH: '直连你正在用的那个 Chrome/Edge,用它里面现成的登录态,不必再登录一次。'
            '要先在那个浏览器里开一次远程调试开关 —— 开着的时候本机任何程序都能'
            '连上它(连同里面所有站点的登录态)。那一勾是持久的,关掉浏览器只是'
            '停掉这一次监听、不等于撤销:不想留着就回 '
            'chrome://inspect/#remote-debugging 把它取消掉。'
            '只在它开着时能抓,而且每次连接都要你点一下「允许」;账号风险同上。',
}
# 退役名 → 现用名。一个都不许删(见模块注释)。
# `bridge` = 删掉的那条第三方 RSS 桥接:存量设置里写着它的,落到现在的默认档。
_RETIRED: dict[str, str] = {'bridge': BROWSER}

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


# `attach` 和 `browser` **共用一个键**,这和 feedgrab 分开存的理由正好互为反面:
# 分开是因为精度不同(浏览器到分钟、feedgrab 只到天,混用会把一个带时分的值填进
# 只认日期的 `X_USER_TWEETS_SINCE`)。而这两条读的是同一个 profile 时间线、同一个
# `<time datetime>`,精度和语义逐字相同 —— 分开存只会让「从独立 profile 换成日常
# 浏览器」白白重抓一遍历史。
_WATER_KEY = {BROWSER: 'x_watermark_browser', ATTACH: 'x_watermark_browser',
              FEEDGRAB: 'x_watermark_feedgrab'}


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

    **空值 = 什么都不改,所以「这一轮没读完」不能交一个空值过来**:新订阅压根
    没有水位,两条 harvest 因此存回的是**这一轮真正用过的那个下界**,而不是手里
    那个空的 `water` —— 交空值的话下一轮会现算一个「当天往回 FIRST_DAYS 天」,
    没读完的那段日期随时间往前滑走(实测记在 `x_feedgrab.harvest`)。
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


def since_floor(day: str) -> str:
    """用户点名的那一天 → 交给采集器的下界(带时区的 ISO,语义是**排他**的)。

    两件事都得在这一处定下来,交给下游各自解释必然各漂一头:

    ①**时区按项目统一的 Asia/Shanghai,不按部署机器的。** 把 `YYYY-MM-DD`
      原样传下去的话,采集器那头 `fromisoformat` 得到的是个**不带时区**的时间,
      补齐时用的是宿主时区 —— UTC 机器上这一天的起点落在上海时间 08:00,凌晨
      0 点到 7 点 59 的推文一条也翻不到,`errors` 还是空的(2026-10-08 审查
      实测 `TZ=UTC`:同一天四条推文只收进 08:01 那一条)。入库那一侧的
      `published`(`x_browser._as_published`)和 `sync._save_result` 的日期筛选
      固定按上海算,下界必须是同一个口径。

    ②**起点算在区间里。** 下游那条选择规则是严格大于(日常水位要靠它避免重抓
      上一轮最后那条),而用户勾的"从 10-05 开始"包含这一天 00:00:00 发的那条
      —— 直接把日期当水位传下去,零点整那条会被当成"已经收过"丢掉(同轮审查
      在上海时区下实测:`missing_times=['2026-10-05 00:00']`)。所以这里给的是
      **那一天零点前的最后一瞬**(微秒级):闭区间换成等价的排他下界,下游那条
      规则一个字不用改,而推文时间的源精度到毫秒 —— 真实内容落不进这中间。

    **为什么是在这里换算,而不是给下游加一个"包含起点"的开关:** 那只修得了
    浏览器通道。feedgrab 那一侧的下界是 `X_USER_TWEETS_SINCE`,只认日期、而且
    按 UTC 的日算 —— 把 `2026-10-05` 原样交给它,上海时间那天 0 点到 7 点 59
    (UTC 10-04 16:00 起)同样一条都拿不到。换算成前一天的最后一瞬之后它
    `[:10]` 得到 `2026-10-04`,那段窗口才回来;多出来的前一天由
    `sync._save_result` 的日期筛选收口(它按 `published` 的日期比 since/until,
    本来就是闭区间),重叠的条目也早被 `put_items` 按 URL 去重。

    认不出的写法返回空串 —— 那会退回"按日常水位读",而不是把整轮带掉;日期本身
    由 `FetchScope` 那一侧校验。
    """
    try:
        start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    except (TypeError, ValueError):
        return ''
    return (start - timedelta(microseconds=1)).isoformat()


def targets(sources: list[dict], settings: dict) -> list[dict]:
    """这一轮里该由非 HTTP 通道接管的订阅。"""
    from kb.feeds import registry as reg
    return [s for s in sources if reg.uses_channel(s) and _handle(s)]


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
    采集器的仍是 10-07、`added=0`)。那个日期先过 `since_floor` 换算成带时区、
    而且把起点算在内的下界,不原样交下去。
    """
    channel = channel_of(settings)
    picked = targets(sources, settings)
    # 区间补抓的下界要先翻成带时区、且把起点算在内的 ISO(见 `since_floor`):
    # 裸日期交下去的含义由宿主时区决定,而零点整那条会被严格大于的选择规则丢掉。
    floor = since_floor(since) if since else ''
    jobs = [(_handle(s), floor or watermark(s, channel)) for s in picked]
    if not jobs:
        with _lock:
            _harvest.clear()
        return {}
    if channel in (BROWSER, ATTACH):
        from kb.feeds import x_browser
        result = x_browser.harvest(
            jobs, profile=str(BROWSER_PROFILE), budget=budget,
            chrome_path=settings.get('x_chrome_path', ''), on_progress=on_progress,
            should_stop=should_stop, attach=channel == ATTACH)
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
    # 写回空串会把这条订阅之前走 HTTP 时攒下的缓存信息抹掉,等它哪天又填了
    # 专用订阅链接、走回 HTTP 时就要白拉一次全量。
    meta = {k: source.get(k, '') for k in ('etag', 'modified', 'resolved_url')}
    # 水位作为"要并进 config 的补丁"交回去,由存盘那一步和条目同一个事务提交
    # (见 `watermark_patch`)。这里是工作线程,当场写盘就和条目脱钩了。
    if patch := watermark_patch(channel, got.get('watermark', '')):
        meta['config_patch'] = patch
    # 「这一轮没读完」原样带回去:它不是失败(抓到的照旧入库),但也不是一次
    # 完整的同步 —— 面板上要看得见,否则"停在时间线半中间"和"全读完了"长得
    # 一模一样。水位那一侧已经由各自的 harvest 留着旧值兜住:浏览器那边是
    # 「滚不到上次的位置」(`x_browser.harvest`),feedgrab 那边是「翻页中途失败」
    # 或者「这一批有几条没抓下来」(`x_feedgrab._incomplete` —— 这两种它的退出码
    # 照样是 0)。
    if note := got.get('incomplete'):
        meta['note'] = note
    return got['entries'], meta


def reset() -> None:
    with _lock:
        _harvest.clear()


def readiness(settings: dict) -> tuple[bool, str, str]:
    """当前通道能不能开工。返回 (就绪, 说明, 下一步)。

    只做**便宜**的判断(路径在不在、登录过没有、端口活不活),不去真连一次 ——
    这个函数被来源面板每次刷新都调。真实结论以同步结果为准。

    第三项是**下一步怎么办**,有两个消费方:设置面板那一行(`#source-x-next`)和
    面板顶上那条待配置横幅(经 `registry.configuration_error`)。所以凡是"光说
    没配好、不说怎么办"会让人卡住的档都要填它 —— feedgrab 填那条命令(横幅那边
    没有可复制块),attach 填去哪儿开那个开关。独立 profile 那档的下一步是点
    它自己那颗按钮,没有文字可给。
    """
    channel = channel_of(settings)
    if channel == FEEDGRAB:
        from kb.feeds import x_feedgrab
        try:
            x_feedgrab.find_binary(settings.get('x_feedgrab_bin', ''))
        except Exception as e:                       # noqa: BLE001 - 成品文案
            return False, str(e), ''
        sessions = FEEDGRAB_HOME / 'sessions'
        if not any(sessions.glob('*twitter*')) and not any(sessions.glob('*x*')):
            # **这一档的第三项必须带上那条命令。** 它看着和
            # `login_command` 重复(面板上那个可复制的 <code> 块给的是同一条),
            # 但第三项还有**另一个消费方**:`registry.configuration_error` →
            # 面板顶上那条待配置横幅,那里没有可复制块。留空等于让那条横幅只说
            # 「还没有登录态」而不说怎么办,而命令里那个可执行文件路径正是最
            # 容易填错的东西(回归在 `test_x_feedgrab_contract.LoginCommandTests`)。
            # 设置面板上的重复由前端按内容去重(`x_channel.js`:`hint` 和
            # `login_command` 一样时不再显示一遍)—— 那是显示层的事。
            return (False, 'feedgrab 还没有 X 登录态',
                    x_feedgrab.login_command(str(sessions),
                                             settings.get('x_feedgrab_bin', '')))
        return True, '', ''
    if channel == ATTACH:
        # 只问「现在有没有一个开着调试开关的浏览器」—— TCP 探活,不发 WebSocket
        # (那会弹授权弹窗,而这个函数每次刷新面板都调,见 `cdp_attach` 注释)。
        # 里面有没有 X 登录态在这一档看不出来:登录态在用户那个 profile 里,
        # 要真连一次才知道(那是「检查连接」的事)。
        from kb.feeds import cdp_attach
        if cdp_attach.discover() is not None:
            return True, '', ''
        # 填错了 `KB_CDP_ENDPOINT` 时要说的是那件事:显式指定之后 `discover` 压根
        # 不去猜别的,再让用户去勾开关是让他白跑一趟。
        if problem := cdp_attach.explicit_problem():
            return False, problem, cdp_attach.ENDPOINT_HINT
        return False, '没有发现开着远程调试的浏览器', cdp_attach.ENABLE_HINT
    # browser:profile 里有 Cookies 文件就算登录过。真失效要等抓取时才知道
    # (那时 `x_browser` 会认出登录页并给出重新登录的提示)。
    if not (BROWSER_PROFILE / 'Default' / 'Cookies').exists():
        return False, '浏览器通道还没有登录过 X', ''
    return True, '', ''


def login_command(settings: dict) -> str:
    """这条通道要用户在终端里跑的那条命令。没有就是空串。

    feedgrab 的登录是交互式的(它自己会开浏览器、要用户扫码或贴 cookie),
    只能他本人在终端跑;浏览器通道则是点按钮开一个有头窗口(见
    `x_browser.open_login_window`),不需要命令行。

    **收的是整份 settings,不只是通道名**:那条命令要用的可执行文件就存在
    `x_feedgrab_bin` 里(采集和体检都认它),只给通道名的话这里压根拿不到它,
    于是只能写死裸 `feedgrab` —— 见 `x_feedgrab.login_command` 的注释。口径同
    `readiness` / `check`:这个模块里凡是要回答「当前这套设置会怎么跑」的,
    一律收 settings。
    """
    if channel_of(settings) != FEEDGRAB:
        return ''
    from kb.feeds import x_feedgrab
    return x_feedgrab.login_command(str(FEEDGRAB_HOME / 'sessions'),
                                    settings.get('x_feedgrab_bin', ''))


def check(settings: dict) -> tuple[bool, str]:
    """真连一次。返回 (就绪, 成品文案)。

    和 `readiness` 分成两个函数是因为代价差着三个数量级:那个每次刷新面板都
    调,这个是用户点「检查连接」才走的 —— 会真起一个 Chrome、真连一次用户那个
    浏览器(那时他可能还要点一下授权弹窗),或者真跑一次 `feedgrab doctor`。
    """
    channel = channel_of(settings)
    if channel == FEEDGRAB:
        from kb.feeds import x_feedgrab
        return x_feedgrab.doctor(settings.get('x_feedgrab_bin', ''),
                                 str(FEEDGRAB_HOME / 'sessions'))
    from kb.feeds import x_browser
    return x_browser.probe(str(BROWSER_PROFILE), settings.get('x_chrome_path', ''),
                           attach=channel == ATTACH)
