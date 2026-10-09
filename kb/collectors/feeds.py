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
        from kb.feeds import fulltext, translate
        from kb.feeds.sync import export
        from kb.pipeline_state import _log, _stage

        # 进度每 5 条报一次:_stage 每次都往运行日志里写一行,逐条报会把
        # 只留 80 行的日志刷成一屏"翻译 17/40",别的阶段全被挤没。
        def progress(done, total):
            if done % 5 == 0 or done == total:
                _stage('export', 'running', f'翻译外文内容 {done}/{total}')

        def fetch_progress(done, total):
            if done % 5 == 0 or done == total:
                _stage('export', 'running', f'补抓原文正文 {done}/{total}')

        # **补抓排在翻译前面**:聚合型订阅(HN Buzzing 那类)的 feed 正文只是
        # 一个跳转壳,真正的内容在原文链接后面。反过来写的代价是同一条内容买
        # 两次模型调用 —— 先按壳译一遍,补上正文再译一遍,而第一份译文随后就被
        # 丢掉。还排在补抓队列里的条目既不翻译也不导出(两边各认一次
        # `fulltext.queued_sql`),等正文落下来再一次性走完后面那一段。
        #
        # 没开这个开关的来源在这里是空转:队列判据的第一条就是"属于某条开了
        # 补抓的订阅",一条都选不出来时 `fetch_pending` 当场返回。
        fetched = fulltext.fetch_pending(self.name, progress=fetch_progress)
        if note := fulltext.summarize(fetched):
            _log(f'export: {note}')
        # 摘要一路交给 export,理由同下面翻译那一句:导出是这条链上最后一个报
        # 阶段状态的人,只写进日志的话这个阶段照样以绿色收尾 —— 实测一条新条目
        # 的原文回 403,阶段 ok、failures 空,而那篇文章一个字都没进知识库
        # (2026-10-09 审查实测)。

        # 先把外文条目译成中文再落盘:vault 的 md 是索引/提取/日报共同的事实来源,
        # 导出完再翻等于先写一份英文进去,后面每一层都按英文处理过一遍。这一轮
        # 没轮到的(一轮上限 40 条)同样不导出,留到下一轮译完再落盘;翻译整个被
        # 跳过时(没配 key)照旧按原文导出,否则英文条目永远进不了知识库。
        summary = translate.translate_pending(self.name, progress=progress)
        if note := translate.summarize(summary):
            _log(f'export: {note}')
        # 摘要一路交给 export:阶段状态由它最后落,翻译失败只写日志的话这个
        # 阶段会以绿色收尾,而那些文章一篇都没入库(见 `sync.export`)。
        export(self.name, hold_untranslated=not summary['skipped'], translated=summary,
               fetched=fetched)

    def pending(self):
        with reg.db() as c:
            return c.execute('SELECT count(*) FROM feed_items WHERE kind=? AND exported_hash<>content_hash',
                             (self.name,)).fetchone()[0]


def create_collector(name):
    return FeedCollector(name)
