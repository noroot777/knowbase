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


SOURCE_TYPES = {
    'wechat': SourceType('wechat', '公众号', '关注的公众号文章',
                         ('wechat_oa', 'wechat_local'), manager='wechat', icon='chat'),
    'social': SourceType('social', '大V观点', '关注的人，此刻的想法。', ('social',),
                         icon='chat', interval_minutes=1440,
                         url_hint='输入 X / Twitter 账号主页，或其他社交媒体的 RSS 地址',
                         note='X 的取法在下面的「取法」里选：桥接、cookie、浏览器登录态几条路，'
                              '各自要准备的东西不同。也可以为单个账号填写专用订阅链接，它优先于任何通道。',
                         adapter='social'),
    'rss': SourceType('rss', 'RSS', '让值得读的文章，自己来到这里。', ('rss',)),
    # 没有 feed 的站点走这条路:解析列表页。独立成一类而不是塞进 rss,是因为
    # 它的失败形态不同 —— 读 feed 坏在对方服务,解析页面还会坏在**对方改版**,
    # 而那需要人去适配。混在一起的话 rss 那条线的状态和定时会被它带着抖。
    'web': SourceType('web', '网页专栏', '没有订阅源的站点，靠解析列表页跟踪。',
                      ('web',), icon='paper', interval_minutes=720,
                      url_hint='输入站点的文章列表页地址（例如 news / blog 首页）',
                      note='给没有 RSS / Atom 的站点用：按「每条带发布时间的链接」认出'
                           '列表，正文建议打开「补抓原文」去文章页取。站点改版时这条'
                           '订阅会报错，而不是静静地变成「今天没有新文章」。',
                      adapter='page'),
}


def feed_types():
    return {k: v for k, v in SOURCE_TYPES.items() if v.manager == 'feeds'}


def catalog():
    return [asdict(s) for s in SOURCE_TYPES.values()]
