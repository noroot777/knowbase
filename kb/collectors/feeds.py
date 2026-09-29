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
        settings = reg.get_settings(self.name)
        unconfigured = sum(bool(reg.configuration_error(s, settings)) for s in active)
        errors = sum(bool(s['last_error']) for s in active)
        return SourceStatus(online=True, ready=len(active) > unconfigured,
                            message=f'已启用 {len(active)} 个来源 · {unconfigured} 个待配置 · {errors} 个来源上次连接失败',
                            hint=('请配置 X 订阅桥接或账号的专用订阅链接' if unconfigured else '连接结果以实际同步为准'),
                            targets=tuple(s['name'] for s in active))

    def fetch(self, scope):
        from kb.feeds.sync import collect
        collect(self.name, scope)

    def materialize(self):
        from kb.feeds import translate
        from kb.feeds.sync import export
        from kb.pipeline_state import _log, _stage

        # 进度每 5 条报一次:_stage 每次都往运行日志里写一行,逐条报会把
        # 只留 80 行的日志刷成一屏"翻译 17/40",别的阶段全被挤没。
        def progress(done, total):
            if done % 5 == 0 or done == total:
                _stage('export', 'running', f'翻译外文内容 {done}/{total}')

        # 先把外文条目译成中文再落盘:vault 的 md 是索引/提取/日报共同的事实来源,
        # 导出完再翻等于先写一份英文进去,后面每一层都按英文处理过一遍。这一轮
        # 没轮到的(一轮上限 40 条)同样不导出,留到下一轮译完再落盘;翻译整个被
        # 跳过时(没配 key)照旧按原文导出,否则英文条目永远进不了知识库。
        summary = translate.translate_pending(self.name, progress=progress)
        if note := translate.summarize(summary):
            _log(f'export: {note}')
        export(self.name, hold_untranslated=not summary['skipped'])

    def pending(self):
        with reg.db() as c:
            return c.execute('SELECT count(*) FROM feed_items WHERE kind=? AND exported_hash<>content_hash',
                             (self.name,)).fetchone()[0]


def create_collector(name):
    return FeedCollector(name)
