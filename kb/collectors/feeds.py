"""Feed-backed sources implement the same pipeline contract as WeChat."""
from kb.collectors import SourceStatus
from kb.feeds import registry as reg
from kb.source_types import SOURCE_TYPES


class FeedCollector:
    def __init__(self, name):
        spec = SOURCE_TYPES[name]
        self.name = name
        self.label = self.channel_label = spec.label
        self.channel_hint = spec.note
        self.range_hint = '按订阅条目的发布时间筛选；来源只提供近期内容，选择历史日期不能补齐未提供的历史。无日期条目仅在不限时间时收录。'

    def status(self):
        sources = reg.subscriptions(self.name)
        active = [s for s in sources if s['enabled']]
        errors = sum(bool(s['last_error']) for s in active)
        return SourceStatus(online=True, ready=bool(active),
                            message=f'已启用 {len(active)} 个来源 · {errors} 个来源上次连接失败',
                            hint='连接结果以实际同步为准', targets=tuple(s['name'] for s in active))

    def fetch(self, scope):
        from kb.feeds.sync import collect
        collect(self.name, scope)

    def materialize(self):
        from kb.feeds.sync import export
        export(self.name)

    def pending(self):
        with reg.db() as c:
            return c.execute('SELECT count(*) FROM feed_items WHERE kind=? AND exported_hash<>content_hash',
                             (self.name,)).fetchone()[0]


def create_collector(name):
    return FeedCollector(name)
