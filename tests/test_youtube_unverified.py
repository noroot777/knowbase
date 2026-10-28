"""「未知不等于不合格」:估算日期和空简介撞上日期范围、窗口下界或 AI 判据时,不许当场判死。

**为什么红必须落在「这一轮还救不救得回来」上,而不是 reason 写的哪句话**:拿估算值判出来的
「不合格」会被 `score()` 写成 `filtered` 并排除在 ranked 之外,而核实(探一次元数据、换成事实值)
只发生在进了 ranked 的那些条目上 —— 两件事一起成立就是这一轮再也救不回来:指定 10-26 补抓时,
真在那天发布的视频报「不在本次指定日期范围」、一条不收;标题没提 AI 而简介明确讲 AI 的那条,
也没有机会再确认(审查冒烟实测两种)。所以这里断言的是**成品出没出来、库里那条是什么**。

反过来那一边同样要钉,少一边就错一头:判得准的拒绝不许跟着放水(观察名单、真的没有简介),
核实完之后该拦的照旧拦住 —— 否则「不许提前拒绝」就变成了「什么都不拦」。

估算数据的写入保护和发现的两条路在 `tests/test_youtube_fallback.py`;那里的
`test_ranged_round_rejects_a_video_whose_real_date_is_another_day` 是本文件 1 号用例的另一半
(事实日期确实落在范围外 → 照旧不产出成品、库里落事实日期)。
"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import admission, collector, media, notes, ranking, storage
from kb.youtube.ranking import TZ
from kb.youtube.settings import of
from tests.youtube_fixture import YouTubeFixture


class UnknownIsNotUnqualified(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.settings = reg.get_settings('youtube')

    def guessed(self, title='AI agent coding tutorial', **values):
        """后路那种条目(`discovery.approximate`):日期按频道页列表顺序估的,简介空着,播放量 0。"""
        entry = {'video_id': '00000000001', 'url': 'https://www.youtube.com/watch?v=00000000001',
                 'title': title, 'description': '', 'views': 0, 'approximate': True,
                 'published': datetime.now(timezone.utc).isoformat(), **values}
        storage.discover(self.source, [entry])
        return entry

    def facts(self, entry, day=None, description=None, **values):
        """元数据替身:一次真实探测拿回来的那几项。"""
        payload = {'id': entry['video_id'], 'duration': 600, 'language': 'en', 'view_count': 4200,
                   'timestamp': int((datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()),
                   **values}
        if day is not None:
            payload.pop('timestamp', None)
            payload['upload_date'] = day.strftime('%Y%m%d')
        if description is not None:
            payload['description'] = description
        return payload

    def stored(self, source=None):
        return storage.rows([(source or self.source)['id']])[0]

    def handle(self, ranked, scope, metadata):
        """跑处理那一段,喂进去的是排过名那一份(生产里 `collect` 就是这个顺序)。"""
        summary = {'added': 0, 'processed': 0, 'errors': [], 'message': '', 'stopped': False}
        with patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=metadata), \
                patch.object(media, 'obtain', return_value=self.complete_transcript()) as obtain, \
                patch.object(notes, 'build', side_effect=self.note):
            # **`products.certify` 不打桩**:成品要真过完整性校验,否则 `quarantine` 会把
            # 它降级回 candidate,「收进来了没有」这条断言就只剩名义上的 processed 计数。
            collector.process(ranked, self.settings, scope, summary, time.monotonic() + 30)
        return summary, obtain

    def test_a_real_date_inside_the_range_survives_long_enough_to_be_checked(self):
        """指定昨天补抓,库里那条估的是今天、事实日期正是昨天 —— 必须收进来。

        旧写法在排名那一头就把它标成 `filtered` 并排除在 ranked 之外,核实永远轮不到它:
        面板上理由写着「不在本次指定日期范围」,而它恰恰在范围里。日期用相对今天算,
        这条用例不会随着日历过期。
        """
        entry = self.guessed()
        day = (datetime.now(TZ) - timedelta(days=1)).date()
        scope = FetchScope(since=day.isoformat(), until=day.isoformat())
        ranked = ranking.score(storage.rows(), of(self.settings), scope)
        # 排名这一头的末态:**不许固化成 filtered** —— 那一刻起这一轮就没有核实入口了。
        self.assertEqual([entry['video_id']], [row['video_id'] for row in ranked],
                         '连榜都没进就等于这一轮救不回来')
        self.assertEqual('candidate', self.stored()['state'])
        self.assertIn('核实', self.stored()['reason'])
        summary, _ = self.handle(ranked, scope, self.facts(entry, day=day))
        self.assertEqual(1, summary['processed'])
        row = self.stored()
        self.assertEqual('ready', row['state'])
        self.assertEqual(day.isoformat(), row['entry']['published'][:10], '事实日期要落库')
        self.assertNotIn('approximate', row['entry'])

    def test_an_unknown_description_gets_the_facts_before_the_ai_filter_decides(self):
        """标题没提 AI、简介还空着 —— 那是「不知道」,不是「与 AI 无关」。

        补回来的简介必须真的参与判定:`calibrate` 只补日期和播放量的话,核实完还是拿空
        简介去判,结论和没核实一模一样 —— 「未知不等于不合格」只做了一半。
        """
        entry = self.guessed(title='The way I work has changed')
        ranked = ranking.score(storage.rows(), of(self.settings), FetchScope())
        self.assertEqual([entry['video_id']], [row['video_id'] for row in ranked])
        summary, _ = self.handle(ranked, FetchScope(),
                                 self.facts(entry, description='An AI agent coding tutorial using Claude'))
        self.assertEqual(1, summary['processed'])
        row = self.stored()
        self.assertEqual('ready', row['state'])
        self.assertIn('AI agent', row['entry']['description'], '简介没补回来,下一轮还要再探一次')

    def test_a_checked_description_without_ai_is_still_filtered(self):
        """反过来钉一边:核实完了确实与 AI 无关,照旧拦住、照旧不下载。

        不钉这一边的话,「不许提前拒绝」会悄悄变成「什么都不拦」—— 用户按下的那个
        开关就不起作用了,而面板上看不出区别。
        """
        entry = self.guessed(title='The way I work has changed')
        ranked = ranking.score(storage.rows(), of(self.settings), FetchScope())
        summary, obtain = self.handle(ranked, FetchScope(),
                                      self.facts(entry, description='A travel vlog about Lisbon food markets'))
        obtain.assert_not_called()
        self.assertEqual(0, summary['processed'])
        row = self.stored()
        self.assertEqual('filtered', row['state'])
        self.assertIn('AI 相关', row['reason'])
        self.assertIn('Lisbon', row['entry']['description'], '核实到的事实要落库,否则下一轮白探一次')

    def test_the_softening_does_not_cover_the_judgements_that_are_certain(self):
        """判得准的那几道不许跟着放水:观察名单上的频道,估算条目照旧不进榜。

        这一条钉的是**范围**:上面要的是「拿未知信息判出来的不合格不算数」,不是
        「估算条目一律放行」。放宽到后者的话,隔离名单这个开关就失效了。
        """
        # 观察名单(`channels.subscription_values` 按分组推出 `ranking_enabled`)。
        quiet = self.channel(2, youtube={'pool': 'creator_watchlist'})
        storage.discover(quiet, [{'video_id': '00000000002', 'title': 'AI agent tutorial',
                                  'url': 'https://www.youtube.com/watch?v=00000000002',
                                  'description': '', 'views': 0, 'approximate': True,
                                  'published': datetime.now(timezone.utc).isoformat()}])
        self.assertEqual([], ranking.score(storage.rows(), of(self.settings), FetchScope()))
        row = self.stored(quiet)
        self.assertEqual('filtered', row['state'])
        self.assertIn('观察', row['reason'])

    def test_a_video_that_really_has_no_description_is_judged_now_not_later(self):
        """反过来钉一边:**没有简介**和**不知道简介**是两件事。

        逐条解析带回来的条目简介空着就是真的空,照旧按标题判。按空字符串放水的话,
        这些条目每一轮都要白探一次元数据,而结论和现在一模一样。
        """
        storage.discover(self.source, [{'video_id': '00000000003', 'title': 'Weekend in Lisbon',
                                        'url': 'https://www.youtube.com/watch?v=00000000003',
                                        'description': '', 'views': 1200,
                                        'published': datetime.now(timezone.utc).isoformat()}])
        self.assertEqual([], ranking.score(storage.rows(), of(self.settings), FetchScope()))
        row = self.stored()
        self.assertEqual('filtered', row['state'])
        self.assertIn('AI 相关', row['reason'])

    def test_calibrate_fills_what_is_unknown_and_leaves_facts_alone(self):
        """校准补的是**未知**那几项;已经有的那几项不许被另一份事实来回换。"""
        metadata = {'upload_date': '20261025', 'view_count': 4200, 'description': 'An AI agent writes code'}
        unknown = {'title': 'T', 'description': '', 'views': 0, 'published': 'x', 'approximate': True}
        filled = admission.calibrate({'entry': unknown}, metadata)
        self.assertEqual(('An AI agent writes code', 4200), (filled['description'], filled['views']))
        self.assertEqual('2026-10-25', filled['published'][:10])
        known = {**unknown, 'description': '精确发现带回来的简介', 'views': 77}
        kept = admission.calibrate({'entry': known}, metadata)
        self.assertEqual(('精确发现带回来的简介', 77), (kept['description'], kept['views']))
