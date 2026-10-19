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

    def status(self, targets: tuple[str, ...] = ()):
        """见 `kb.collectors.SourceCollector.status`。

        **就绪在这条来源上是逐个订阅的**(X 账号各自要么走通道、要么填专用链接),
        所以给了范围就只按范围内那几条算 —— 整条通道里别人配好了,证明不了这几条
        配好了。范围匹配走 `reg.in_scope`(唯一一份口径,`sync.collect` 同款)。
        """
        sources = reg.subscriptions(self.name)
        active = [s for s in sources
                  if s['enabled'] and reg.in_scope(s, targets)]
        settings = reg.get_settings(self.name)
        unconfigured = sum(bool(reg.configuration_error(s, settings)) for s in active)
        errors = sum(bool(s['last_error']) for s in active)
        scoped = '这一轮要抓的' if targets else '已启用'
        return SourceStatus(online=True, ready=len(active) > unconfigured,
                            message=f'{scoped} {len(active)} 个来源 · {unconfigured} 个待配置 · {errors} 个来源上次连接失败',
                            hint=('请在来源设置里配好 X 的取法，或给账号填专用订阅链接' if unconfigured else '连接结果以实际同步为准'),
                            targets=tuple(s['name'] for s in active))

    def fetch(self, scope):
        from kb.feeds.sync import collect
        collect(self.name, scope)

    def retry_targets(self, note):
        """「重试失败项」要重抓的订阅(见 `kb.collectors.RetryNarrowing`)。"""
        from kb.feeds.sync import still_failing
        return still_failing(self.name, note)

    def _manual_retry(self):
        """这一轮是不是用户点的「重新翻译」,以及他点的是哪一条订阅的范围。

        **意图跟着管道状态走,而不是由端点先写进库。** 端点那一侧必须做到
        「忙碌拒绝不写库」:原来是先 `requeue_failed` 再 `start_pipeline`,于是
        已有同步在跑时返回 409、而额度和点名标记已经落了盘 —— 页面再读一次,
        失败说明和按钮一起消失,用户按拒绝文案所说的「结束后重试」已经没有入口
        可点(2026-10-13 第 1 轮审查实测:`translate_tries` 3→0、
        `translate_manual` 0→1、`stuck.failed` 1→0)。在端点开头补一次
        `running` 检查不够 —— 那和 `start_pipeline` 之间仍然能被调度线程抢占。

        所以意图只放在 `start_pipeline` 原子写进去的那份 `crawl_filter` 里:
        占不到管道 = 这份状态压根没写 = 库里一个字节都没动。而真正改库的那一下
        挪到这里 —— 它和紧跟着的取数在同一个执行体里,中间没有别的写入方。
        """
        from kb.pipeline_state import get_state
        f = get_state().get('crawl_filter') or {}
        want = f.get('translate_retry') if isinstance(f, dict) else None
        if not isinstance(want, dict) or want.get('channel') != self.name:
            return None
        return want

    def materialize(self):
        """落 md,返回这一轮真写下去的那几篇(见 `kb.collectors` 的契约)。"""
        from kb.feeds import fulltext, translate
        from kb.feeds.sync import export
        from kb.pipeline_state import _log, _stage

        # 用户点名的那一轮:先把他点的那些放回队列队首,再**只**译这些。
        # 两件事必须挨在一起 —— 点名是射程唯一跟得到下面那次取数的东西
        # (见 `translate.requeue_failed` 的说明)。
        retry = self._manual_retry()
        picked = 0
        if retry is not None:
            try:
                picked = translate.requeue_failed(
                    self.name, retry.get('source_id'), retry.get('scope_sig') or '')
                _log(f'export: 已点名 {picked} 条等待翻译的内容，这一轮只译它们')
            except translate.ScopeChanged as e:
                # **这一轮一条都不译,而且射程不许放开。** `only_manual` 照旧是
                # True(判据是「这一轮是不是点名的」),而点名标记已经在那次清场
                # 里全清掉了,于是取数取到 0 条。端点那一侧还有一道同样的校验,
                # 它在用户点下去的那一刻就能把话说清楚;这一道是兜底 —— 端点
                # 放行到这里开跑之间还隔着整个 crawl 阶段。
                _log(f'export: {e}')

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
        #
        # **`finally` 从点名之后就开始,把补抓一起圈进来。** 只圈翻译那一句的话,
        # 补抓阶段抛异常(磁盘写失败、原文站点把连接掐了)会直接越过清理,标记
        # 留在库里 —— 下一次筛着另一条订阅点重试,上一轮的残留会被一起译掉
        # (2026-10-14 第 2 轮审查实测:在正文保存处注入 `OSError`,随后筛另一条
        # 订阅重试,确认 1 条、实际调了 2 次模型)。
        # 这一道只是清理。**进程被 kill、机器断电时它同样跑不到**,那一档由
        # `requeue_failed` 开工前那次清场兜住 —— 按本次 `source_id` 收窄只挡得住
        # **跨订阅**的残留,同一条订阅下的旧标记和本次写下的长得一模一样
        # (2026-10-14 第 3 轮审查实测)。三道各有自己的射程,见
        # `translate.requeue_failed` 与 `translate.translate_pending`。
        try:
            fetched = fulltext.fetch_pending(self.name, progress=fetch_progress)
            if note := fulltext.summarize(fetched):
                _log(f'export: {note}')
            # 摘要一路交给 export,理由同下面翻译那一句:导出是这条链上最后一个报
            # 阶段状态的人,只写进日志的话这个阶段照样以绿色收尾 —— 实测一条新条目
            # 的原文回 403,阶段 ok、failures 空,而那篇文章一个字都没进知识库
            # (2026-10-09 审查实测)。

            # 先把外文条目译成中文再落盘:vault 的 md 是索引/提取/日报共同的事实
            # 来源,导出完再翻等于先写一份英文进去,后面每一层都按英文处理过一遍。
            # 这一轮没轮到的(一轮上限 40 条)同样不导出,留到下一轮译完再落盘;
            # 翻译整个被跳过时(没配 key)照旧按原文导出,否则英文条目永远进不了
            # 知识库。
            # 点名的那一轮只译带标记、而且属于本次那个范围的条目(付费边界,见
            # `translate_pending`)。**判据是「这一轮是不是点名的」,不是「点到了
            # 几条」** —— 按后者写的话,端点放行之后、这里开跑之前那个范围恰好
            # 被别人译空时(`picked == 0`),射程会当场放开成整个来源,于是用户
            # 筛着订阅 A 点下去、库里动的是 B 的积压。点名范围空了就这一轮译
            # 0 条 —— 那是如实的,而 export/index/extract 照旧有自己的活要干。
            summary = translate.translate_pending(
                self.name, progress=progress, only_manual=retry is not None,
                source_id=retry.get('source_id') if retry else None)
        finally:
            # **标记是一次性的,无论这一轮成败都要收掉。** 留着的话下一次常规
            # 同步仍然只盯这批(排序上压过所有新内容)。译成的那条 `_save` 自己
            # 清过了,这里收的是没译成的。
            if retry is not None:
                translate.clear_manual(self.name)
        if note := translate.summarize(summary):
            _log(f'export: {note}')
        # 摘要一路交给 export:阶段状态由它最后落,翻译失败只写日志的话这个
        # 阶段会以绿色收尾,而那些文章一篇都没入库(见 `sync.export`)。
        # 回执原样交上去:那几条身份(内容身份 + 发现它的那一次抓取 + 这次写的
        # 路径)是 `sync.export` 自己写文件那一下攒的,这里不再数一遍、也不许拿
        # `done` 之类的计数替它 —— 上层要的是身份,不是数目(理由在
        # `kb.fetch_rounds` 开头那条「任何重放都幂等」)。
        return export(self.name, hold_untranslated=not summary['skipped'],
                      translated=summary, fetched=fetched)

    def pending(self):
        with reg.db() as c:
            return c.execute('SELECT count(*) FROM feed_items WHERE kind=? AND exported_hash<>content_hash',
                             (self.name,)).fetchone()[0]


def create_collector(name):
    return FeedCollector(name)
