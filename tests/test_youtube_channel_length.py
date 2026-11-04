"""频道可以单独设单条时长上限,覆盖全局那一个(AI Search 的周报常年 40–55 分钟)。

判据只有一份(`settings.video_minutes`),三处消费:排名时逐归属判(`ranking.rejection`)、
第一次探到时长时的准入(`admission.gate`,取合格归属里最宽的)、卡片上那句说明里的上限数字。
后果断言落在「这一轮处理了谁、库里那条是什么状态、卡片上那行字」。反过来钉住:没设的频道
照旧按全局拦,清空之后回到全局。
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage
from kb.youtube.channels import config
from tests.youtube_fixture import YouTubeFixture


class ChannelLength(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.long = {**self.entry(1, views=500000), 'title': 'AI finally sleeps: AI NEWS'}
        self.durations = {self.long['video_id']: 3234}
        self.published = {}
        self.settings(rank_limit=5, max_video_minutes=45)

    def probe(self, row, *arguments, **keywords):
        return {'duration': self.durations[row['video_id']], 'view_count': row['entry']['views'],
                'timestamp': self.published.get(row['video_id'],
                                                ranking.stamp(row['entry']['published'])).timestamp()}

    def transcript(self, row, metadata, *arguments):
        transcript = self.complete_transcript()
        transcript['segments'][0]['end'] = metadata['duration']
        return transcript

    def collect(self, listing):
        with patch.object(discovery, 'fetch', side_effect=lambda source, *rest: listing.get(source['id'], [])), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', side_effect=self.probe), \
                patch.object(media, 'obtain', side_effect=self.transcript) as obtain, \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope())
        return result, [call.args[0]['video_id'] for call in obtain.call_args_list]

    def state(self, entry):
        return next(row for row in storage.rows() if row['video_id'] == entry['video_id'])['state']

    def notice(self, source):
        return next(item for item in reg.subscriptions('youtube') if item['id'] == source['id'])['last_notice']

    def test_channel_limit_lets_its_long_video_through(self):
        source = self.channel(youtube={'max_video_minutes': 60})
        result, obtained = self.collect({source['id']: [self.long]})
        self.assertEqual([self.long['video_id']], obtained)
        self.assertEqual((1, 'ready'), (result['added'], self.state(self.long)))
        self.assertNotIn('超过单条时长上限', self.notice(source))

    def test_other_channels_keep_the_global_limit(self):
        self.channel(1, youtube={'max_video_minutes': 60})
        plain = self.channel(2)
        for _ in range(2):
            result, obtained = self.collect({plain['id']: [self.long]})
            self.assertEqual([], obtained)
        self.assertEqual('filtered', self.state(self.long))
        self.assertIn('超过单条时长上限 45 分钟未处理：「AI finally sleeps: AI NEWS」54 分钟', self.notice(plain))

    def test_shared_video_follows_the_widest_owner_on_first_probe(self):
        """代表归属是没设上限的那个(同权取较小 ID),准入也得按放行它的那个归属算。"""
        plain = self.channel(1)
        wide = self.channel(2, youtube={'max_video_minutes': 60})
        result, obtained = self.collect({plain['id']: [self.long], wide['id']: [self.long]})
        self.assertEqual([self.long['video_id']], obtained)
        self.assertEqual('ready', self.state(self.long))

    def test_editor_accepts_minutes_and_clears_back_to_global(self):
        source = self.channel(youtube={'max_video_minutes': 60})
        self.assertEqual(60, config(source)['max_video_minutes'])
        for bad in (0, 181, 1.5, '60', True):
            with self.assertRaises(ValueError):
                reg.save_subscription('youtube', {'name': source['name'], 'url': source['url'],
                                                  'youtube': {'max_video_minutes': bad}}, source['id'])
        cleared = reg.save_subscription('youtube', {'name': source['name'], 'url': source['url'],
                                                    'youtube': {'max_video_minutes': None}}, source['id'])
        self.assertNotIn('max_video_minutes', config(cleared))
        result, obtained = self.collect({source['id']: [self.long]})
        self.assertEqual([], obtained)

    def calibrated(self, real_days):
        """估算日期一天前、真实日期 `real_days` 天前;宽的那个订阅历史下界五天,窄的那个三十天。

        代表归属是窄的那个(同权取较小 ID),首次准入按估算日期下两个归属里最宽的 60 分钟放行。
        走 `process` 自己探元数据那一路(元数据预更新预算没轮到的候选就走这里)。
        """
        plain = self.channel(1)
        wide = self.channel(2, youtube={'max_video_minutes': 60})
        now = datetime.now(timezone.utc)
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               ((now - timedelta(days=30)).date().isoformat(), plain['id']))
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               ((now - timedelta(days=5)).date().isoformat(), wide['id']))
        estimated = {**self.long, 'approximate': True, 'published': (now - timedelta(days=1)).isoformat()}
        for source in (plain, wide):
            storage.discover(reg.subscription('youtube', source['id']), [estimated])
        self.published[self.long['video_id']] = now - timedelta(days=real_days)
        settings = reg.get_settings('youtube')
        ranked = ranking.score(storage.rows(), settings['youtube'], FetchScope())
        summary = {'stopped': False, 'message': '', 'processed': 0}
        with patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', side_effect=self.probe), \
                patch.object(media, 'obtain', side_effect=self.transcript) as obtain, \
                patch.object(notes, 'build', side_effect=self.note):
            collector.process(ranked, settings, FetchScope(), summary, time.monotonic() + 30)
        row = next(row for row in storage.rows() if row['video_id'] == self.long['video_id'])
        return [call.args[0]['video_id'] for call in obtain.call_args_list], row

    def test_real_date_dropping_the_wide_owner_rechecks_the_length(self):
        obtained, row = self.calibrated(real_days=6)
        self.assertEqual([], obtained, '只剩 45 分钟那个归属,54 分钟的视频不许被下载')
        self.assertEqual('filtered', row['state'])
        self.assertIn('上限 45 分钟', row['reason'])
        self.assertEqual(3234, row['duration'])

    def test_real_date_keeping_the_wide_owner_still_processes(self):
        obtained, row = self.calibrated(real_days=2)
        self.assertEqual([self.long['video_id']], obtained)
        self.assertEqual('ready', row['state'])

    def test_each_owner_gets_its_own_over_length_notice(self):
        """60 分钟那个放行不等于 45 分钟那个不用说明:后者那边看到的仍是零新增。"""
        plain = self.channel(1)
        wide = self.channel(2, youtube={'max_video_minutes': 60})
        for source in (plain, wide):
            storage.discover(source, [self.long])
        storage.update(self.long['video_id'], state='candidate', duration=3234)
        named = ranking.over_length(storage.rows(), reg.get_settings('youtube')['youtube'], FetchScope())
        self.assertEqual([[plain['id']]], [row['eligible_sources'] for row in named])
