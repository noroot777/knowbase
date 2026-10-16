"""Source modules are a UI/catalog concept; channels are replaceable collectors.

To add a feed-compatible category, register one SourceType and a collector using
FeedCollector. A source with different transport/storage (e.g. WeChat) implements
SourceCollector independently. Pipeline/search code does not dispatch on modules.
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SourceType:
    id: str
    label: str
    description: str
    channels: tuple[str, ...]
    manager: str = 'feeds'
    icon: str = 'rss'
    interval_minutes: int = 360
    url_hint: str = '输入 RSS / Atom 订阅地址'
    note: str = '读取订阅中当前提供的内容；不能保证补齐订阅之前的全部历史。'
    adapter: str = 'rss'
    # 这一类来源**有没有可以轮询的源**。False = 内容由人逐条投进来(手动收录:
    # 抓取发生在用户给出链接的那一刻,见 `kb/feeds/grab.py`),于是 crawl 阶段
    # 对它空转(`kb.feeds.sync.collect` 开头那一道)—— 它参与的是后面那半条链:
    # 导出 → 索引 → 提取。**不是"这一类来源不进管道"**:积压的条目正是靠那
    # 半条链进知识库的,所以它照旧出现在同步中心的通道里。
    pollable: bool = True


SOURCE_TYPES = {
    'wechat': SourceType('wechat', '公众号', '关注的公众号文章',
                         ('wechat_oa', 'wechat_local'), manager='wechat', icon='chat'),
    'social': SourceType('social', '大V观点', '关注的人，此刻的想法。', ('social',),
                         icon='chat', interval_minutes=1440,
                         url_hint='输入 X / Twitter 账号主页，或其他社交媒体的 RSS 地址',
                         note='X 的取法在下面的「取法」那一块里选，每一档要准备的东西不同。'
                              '也可以为单个账号填写专用订阅链接，它优先于任何通道。',
                         adapter='social'),
    'rss': SourceType('rss', 'RSS', '让值得读的文章，自己来到这里。', ('rss',)),
    # 没有 feed 的站点走这条路:解析列表页。独立成一类而不是塞进 rss,是因为
    # 它的失败形态不同 —— 读 feed 坏在对方服务,解析页面还会坏在**对方改版**,
    # 而那需要人去适配。混在一起的话 rss 那条线的状态和定时会被它带着抖。
    # 邮件简报:RSS 只给期号,内容在每期的汇总页上并列排布。独立成一类是因为
    # 它**只能两跳**(期号从 RSS 拿、正文从落地页拿),而 rss/web 两条线都只发
    # 一次 HTTP —— 它走的是预取,见 `kb.feeds.digest`。
    'digest': SourceType('digest', '邮件简报', '每期一个汇总页，里面是几十条带外链的简讯。',
                         ('digest',), icon='paper', interval_minutes=720,
                         url_hint='输入简报的 RSS 地址（它只给期号，正文由我们去每期页面取）',
                         note='给 TLDR 这类"一期一封"的简报用：RSS 本身不带正文，我们按它给的'
                              '期号去每期页面把每条简讯拆出来，标着「赞助」的那些不收。'
                              '正文就是简报自己写的摘要，**不要再开「补抓原文」**——那个'
                              '外链指向的是别人家的文章，抓回来就不是这条简讯了。',
                         adapter='digest'),
    'web': SourceType('web', '网页专栏', '没有订阅源的站点，靠解析列表页跟踪。',
                      ('web',), icon='paper', interval_minutes=720,
                      url_hint='输入站点的文章列表页地址（例如 news / blog 首页）',
                      note='给没有 RSS / Atom 的站点用：按「每条带发布时间的链接」认出'
                           '列表，正文建议打开「补抓原文」去文章页取。站点改版时这条'
                           '订阅会报错，而不是静静地变成「今天没有新文章」。',
                      adapter='page'),
    # 手动收录:看到一篇就存下来。**它没有可轮询的源** —— 内容在用户给出链接的
    # 那一刻就抓好入库了(`kb/feeds/grab.py`),所以 `pollable=False`、
    # `adapter='manual'`(**刻意不在 `fetching.ADAPTERS` 里**:哪条路漏了跳过
    # crawl 这一道,报出来的是一个说得清的 KeyError,而不是拿一个假地址去发 HTTP)。
    # 除此之外它和别的 feed 类来源逐字一样 —— vault 树、索引扫描、检索的来源
    # 筛选、提取开关、日报素材范围、同步中心的通道全部由 `feed_types()` 自动
    # 派生(见 `kb/config.py`、`kb/collectors/__init__.py`)。
    'inbox': SourceType('inbox', '手动收录', '看到就存：给一个链接，内容抓进知识库。',
                        ('inbox',), icon='box', interval_minutes=1440,
                        url_hint='粘贴文章或推文链接，回车存入',
                        note='这一类不轮询：你给一个链接，正文当场抓好写进知识库。'
                             'X 的单条推文走公开接口（长文连正文一起取），其他站点'
                             '按文章正文提取。按站点自动归到一个收件桶里，'
                             '抓不到正文的不会入库——重试一次的代价只是再抓一遍。',
                        adapter='manual', pollable=False),
}


def feed_types():
    return {k: v for k, v in SOURCE_TYPES.items() if v.manager == 'feeds'}


def catalog():
    return [asdict(s) for s in SOURCE_TYPES.values()]
