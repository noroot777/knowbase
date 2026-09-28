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
    'wechat': SourceType('wechat', '微信同步', '关注的公众号文章',
                         ('wechat_oa', 'wechat_local'), manager='wechat', icon='chat'),
    'social': SourceType('social', '大V观点', '关注的人，此刻的想法。', ('social',),
                         icon='chat', interval_minutes=60,
                         url_hint='输入 X / Twitter 账号主页，或其他社交媒体的 RSS 地址',
                         note='X 通过订阅桥接读取公开动态。若连接失败，可修改桥接地址或为账号填写专用订阅链接。', adapter='social'),
    'rss': SourceType('rss', 'RSS', '让值得读的文章，自己来到这里。', ('rss',)),
}


def feed_types():
    return {k: v for k, v in SOURCE_TYPES.items() if v.manager == 'feeds'}


def catalog():
    return [asdict(s) for s in SOURCE_TYPES.values()]
