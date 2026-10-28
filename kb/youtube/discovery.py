# -*- coding: utf-8 -*-
"""频道更新的发现:**配了登录态就全程带着它走**,公开订阅只留给「没有登录态」那一档。

**硬约束在 `docs/decisions/youtube-intake.md` 第一节 —— 改这块之前先读那一份。**

**为什么公开订阅不再是主路。** 仓库主人两次点名:先是「那就全程登录态吧」,再是「改成
daily-report 那种,它都运行的好好的」。公开那份 Atom 又便宜又带播放量,但它**不接受
cookie**:那批 47/48 的 404 正是从它来的,而「先试公开、失败再带登录态」等于每一轮都
先付一次那个风险,也先付一次重试的时间。性能代价(单频道从几秒到半分钟一档)已经如实
报过,他没有收回这两句 —— 所以这里就是全程登录态,不留一条悄悄发无登录态请求的路。

**两条路给的东西不一样,所以不是替代关系。** `detailed` 逐条真解析,日期、播放量、简介
都是真的(排名要用的全在里面),代价是每条约 4.3 秒;`approximate` 的 flat 列举**只给
id、标题和时长**(实测连 `youtubetab:approximate_date` 也不给日期),便宜一个数量级。
所以前者是主路,后者只在它也没取到时兜底。

**后路的发布时间是估算的**,条目上打 `approximate` 标记:真实日期要等 `media.probe` 拿到
元数据才算数(`ranking` 对估算日期放行),拿到的那一刻由 `admission.calibrate` 换成事实值、
再过**同一个** `ranking.rejection` 重判资格 —— 估算值不许留到成品里。
**估算数据不许覆盖已有的精确数据** —— 那道判据在 `storage.discover` 的 SQL 里。
"""

import calendar
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser

from kb.feeds.fetching import FeedError, request
from kb.youtube import login, throttle
from kb.youtube.channels import CHANNEL_ID, VIDEO_ID, config
from kb.youtube.settings import of


def local_identity(source):
    """**一次请求都不发**就能说出的频道归属:地址里直接带的,或者上一轮缓存下来的。

    带登录态那两条路要的就是这一份 —— 它们的归属以 yt-dlp 自己报的 `channel_id` 为准
    (`media._detailed` / `_listed` 会带回来),这里只是 handle 订阅第一轮时的兜底。
    **不许在这里回落到 `channel_id()`**:那是一次公开主页请求,而全程登录态这一轮
    的全部意义就是不发那种请求(报出来会是「发现失败」,而工具一个进程都没起)。
    """
    direct = source['url'].rsplit('/', 1)[-1]
    if CHANNEL_ID.fullmatch(direct):
        return direct
    cached = str(config(source).get('channel_id', '') or '')
    return cached if CHANNEL_ID.fullmatch(cached) else ''


def channel_id(source, deadline):
    """解析频道 ID,**解析不出来时会发一次公开主页请求** —— 所以只有公开那条路能用它。"""
    if identity := local_identity(source):
        return identity
    response = request(source['url'], deadline=deadline)
    text = response.body.decode('utf-8', errors='replace')
    patterns = (r'"(?:externalId|channelId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})')
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match[1]
    raise FeedError('无法解析频道 ID，请改填 /channel/UC… 频道主页')


#: 带登录态那两条路各占**单频道总预算**的比例。主路要逐条真解析(实测每条约 4.3 秒),
#: 给它大头;估算那条 flat 列一次实测 1.4~30 秒,剩下那一段够它跑完。两段加起来留一点
#: 余量给异常路径。**不许共用一个 deadline**(理由在 `fetch` 的说明里)。
DETAIL_SHARE = 0.7
#: 公开那条路的重试(照 daily-report 的 `--retry 3 --retry-delay 1 --retry-all-errors`)。
#: **只有「没有登录态可用」那一档会走到它** —— 那时它是唯一的办法,失败多试两次近乎免费。
RSS_ATTEMPTS = 3
RSS_BACKOFF = 1.0


def fetch(source, settings=None, timeout=90, estimates=True, notes=None):
    """取这个频道的近期视频。**配了浏览器就全程带登录态,两条路从贵到便宜依次让位。**

    1. `detailed()`:频道视频页交给 yt-dlp、**带登录态逐条解析**(daily-report 那条路)。
       慢(单频道半分钟一档),但日期、播放量、简介都是真的。
    2. `approximate()`:同一个地址、同一把 cookie,加 `--flat-playlist`。只有 id、标题和
       时长,发布时间按列表顺序**估算**。

    **第 2 条在显式日期范围那一轮被禁**(`estimates=False`):拿估算值去满足用户指定的
    范围等于编造「在范围内」这个结论。第 1 条拿的是真实日期,那一轮照旧走它。

    **`settings=None` 是唯一不带登录态的那一档**:没有设置就没有 `cookies_browser`,
    那时公开订阅是唯一的办法(带重试)。生产里走不到这儿 —— `collect` 开跑前先过
    `login.readiness`,它不就绪整轮都不开始(而 `settings.of` 给 `cookies_browser` 的
    默认值是 `chrome`,拿不到空值)。**这条分支是显式的,不是悄悄的退路。**

    **每条路各有自己的那一段预算,不许共用一个 deadline。** 共用的话,前一条**超时**
    这一档(不是失败,是卡到预算用尽)会把 deadline 耗干,后一条在起子进程之前就被
    `media.run` 的入口条件判成超时 —— 报出来是「都没取到」,而它一个进程都没起。

    `notes` 是出参:某条路「列了几条之后中断」的说明会落在里面(见 `media._collect_listing`),
    由 `collector` 转成面板上那句「这个频道没列完」。

    **工具这一次报了一句什么不走出参**:那是 `media.run` 的完成边界记的账
    (`diagnosis.record`,成功、半截、失败都记),由开账那一段去读(`collector.heeded`)。
    一层层往上传的那一版只在各自的成功分支上交账,换个失败组合就又漏一次。
    **它不是失败**:取到的照旧收下,只是并列说一句这一轮可能没带上登录态。

    **第 1 条撞上 429 之后第 2 条压根起不来**:那道闸在请求入口上(`media.ytdlp` 的
    `throttle.guard`),不在这儿 —— 同一个出口刚说了「请求过于频繁」,换个参数再问一次
    只是把惩罚加深。它会以一条限流失败的形式落进 `reasons`,所以这里的结构不变。
    """
    budget = max(6.0, float(timeout))
    spec = '' if settings is None else of(settings)['cookies_browser']
    if not spec:
        return rss(source, time.monotonic() + budget)
    reasons, failures = [], []
    try:
        return detailed(source, settings, time.monotonic() + budget * DETAIL_SHARE, notes)
    except throttle.NotAttempted:
        # **「没发出去」不许变成「这条路失败了」**:归并成一条 `FeedError` 交出去之后,
        # 上层只看得见一条普通失败,于是这个频道被记「连接失败」、推半小时退避,而它
        # 一个毛病都没有(审查实测)。后路也一样会被同一道闸拒掉,不必再试。
        raise
    except Exception as error:
        reasons.append(f'带登录态逐条解析：{error}')
        failures.append(error)
    if not estimates:
        raise _combined(reasons, failures) from None
    try:
        return approximate(source, settings, time.monotonic() + budget * (1 - DETAIL_SHARE), notes)
    except throttle.NotAttempted:
        # 走到这儿说明**第一条路是真发出去并失败的**(它自己没发出去的话上面那一道就抛了),
        # 那条真实失败说了算 —— 这个频道这一次确实没拿到东西,原文也得留得住。
        raise _combined(reasons, failures) from None
    except Exception as error:
        reasons.append(f'估算列举：{error}')
        failures.append(error)
        raise _combined(reasons, failures) from None


def _combined(reasons, failures):
    """两条路都没成时交出去的那一条。**分档和完整诊断要跟着走**(`login.merge`)。

    只留文字的话,上层只剩一条裁过的 `FeedError` 可读 —— 它只能回去拿文本重新猜是哪一档,
    而那正是 `login.coded` 的说明里记着的那次误判。**挑哪一条不按到达先后**:逐条解析先报
    机器人验证、后路随后报 429 时,按先后挑就对外说成「带上登录态通常能过」,而这一轮正是
    因为 429 停的(审查实测)。各条原因的原话照旧都在这条异常自己的消息里。
    """
    return login.merge(FeedError('；'.join(reasons)), failures)


#: 主路一轮往回看几条。**不是把频道历史翻完**:逐条解析实测每条约 4.3 秒,10 条就是
#: 40 多秒,正好落在它那一段预算里;日更的频道隔一天同步也够。预算在第 7 条上到点时
#: 前面那几条照旧收下(`media._collect_listing`),所以这个数字给大一点也不会颗粒无收。
DETAIL_LIMIT = 10


def detailed(source, settings, deadline, notes=None):
    """第 1 条路:频道页**带登录态逐条解析**,日期和播放量都是真的。

    和第 2 条同一个地址,差别只在不带 `--flat-playlist`(见 `media.detailed_videos`)。
    条目上不打 `approximate`,所以它们走的是正常的窗口与范围判断,不必等
    `admission.calibrate` 事后校准。
    """
    from kb.youtube import media
    listed = media.detailed_videos(source, settings, deadline, lambda: False,
                                   limit=DETAIL_LIMIT, notes=notes)
    return [{'video_id': item['video_id'], 'channel_id': _owner(item, source), 'url': item['url'],
             'title': item['title'], 'published': item['published'],
             'description': item['description'], 'views': item['views'],
             'duration': item['duration']} for item in listed]


def approximate(source, settings, deadline, notes=None):
    """后路:yt-dlp flat 列频道。只有 id、标题和时长,发布时间按列表顺序估算。"""
    from kb.youtube import media
    listed = media.channel_videos(source, settings, deadline, lambda: False, limit=20,
                                  notes=notes)
    now = datetime.now(timezone.utc)
    entries = []
    for position, item in enumerate(listed):
        # 频道页是时间倒序,所以**用列表位置拉开一个递减的估算时间**:顺序信息留住了(同分时
        # 还知道谁更新),又都落在默认窗口里,由 `gate` 按真实 upload_date 收口。
        # 不给所有条目写同一个 now —— 那会把「谁更新」这一位信息丢掉。
        stamp = (now - timedelta(hours=position)).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
        entries.append({'video_id': item['video_id'], 'channel_id': _owner(item, source),
                        'url': item['url'], 'title': item['title'], 'published': stamp,
                        'description': '', 'views': 0, 'approximate': True,
                        'duration': item['duration']})
    return entries


def _owner(item, source):
    """这条视频记在哪个频道名下。**先认 yt-dlp 自己报的那一份**,再退回本地那一份。

    归属留空的话 `storage.discover` 写进库的条目就不知道自己属于谁;而为了补上它去发
    一次公开请求,恰恰是全程登录态这一轮要去掉的那种请求 —— 所以两边都不发请求:
    工具已经在输出里报了 `channel_id`,地址里带 `/channel/UC…` 或缓存过的也认。
    """
    reported = str(item.get('channel_id') or '')
    return reported if CHANNEL_ID.fullmatch(reported) else local_identity(source)


def rss(source, deadline):
    """公开那份 Atom。**只有「没有登录态可用」那一档走到这里**(见 `fetch`)。

    那批 47/48 的 404 是间歇性的(事后同样 4 路并发复测 52 个频道全部 200),所以这一条
    路上第一道防线是**同一条路多试两次**,代价接近零。重试待在给它的那段预算里:
    时间连一次退避加一次请求都放不下就不试了。
    """
    identity = channel_id(source, deadline)
    url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + identity
    for attempt in range(1, max(1, RSS_ATTEMPTS) + 1):
        try:
            return _parse_feed(request(url, deadline=deadline), identity)
        except Exception:
            # 最后一次、或者剩下的时间连一次退避加一次请求都放不下,就把原样的失败抛出去。
            if attempt >= max(1, RSS_ATTEMPTS) or time.monotonic() + RSS_BACKOFF * 2 >= deadline:
                raise
        time.sleep(RSS_BACKOFF)


def _parse_feed(response, identity):
    """把那份 Atom 解成条目。**归属要对**:`identity` 省掉的话 `yt_channelid` 一比就全被
    滤掉,整个频道静默变成「今天没有新内容」。"""
    parsed = feedparser.parse(response.body)
    if not parsed.version or parsed.bozo and not parsed.entries:
        raise FeedError('YouTube 没有返回可解析的频道订阅，可能被限流或访问受限')
    entries = []
    for item in parsed.entries:
        video_id = item.get('yt_videoid') or item.get('id', '').rsplit(':', 1)[-1]
        if not VIDEO_ID.fullmatch(video_id):
            continue
        owner = item.get('yt_channelid', identity)
        if owner != identity:
            continue
        published = item.get('published_parsed')
        stamp = (datetime.fromtimestamp(calendar.timegm(published), timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
                 if published else '')
        statistics = item.get('media_statistics') or {}
        try:
            views = min(2 ** 63 - 1, max(0, int(statistics.get('views', 0))))
        except (ValueError, TypeError):
            views = 0
        entries.append({'video_id': video_id, 'channel_id': identity,
                        'url': 'https://www.youtube.com/watch?v=' + video_id,
                        'title': item.get('title', video_id), 'published': stamp,
                        'description': item.get('summary') or item.get('media_description', ''), 'views': views})
    if parsed.entries and not entries:
        raise FeedError('频道返回了条目，但无法识别视频 ID 或频道归属')
    return entries
