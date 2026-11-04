"""窗口里的视频因为超过单条时长上限没做,同步结果和频道卡片要点名说出来。

事故形态:只同步 AI Search 一个频道,它三天前那期 54 分钟的周报被 45 分钟上限挡掉,而这一轮
报的是「排名 0 条、处理 0 条、入库 0 条」,卡片上只有一句与它无关的积压提醒 —— 人只会以为
频道没更新、或者同步坏了。判据在 `ranking.over_length`(拿掉上限再过一次 `qualify`),文案在
`source_status.over_length`。

后果断言落在「这一轮的结果那行字」和「库里那条订阅的 last_notice」上。反过来钉住:窗口外的
超长视频、调高上限之后、没有超长视频的频道,都不许冒出这句话。
"""

from datetime import datetime, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage
from tests.youtube_fixture import YouTubeFixture


class OverLengthNotice(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.long = {**self.entry(1, views=500000), 'title': 'AI finally sleeps: AI NEWS'}
        self.short = self.entry(2, views=1000)
        self.durations = {self.long['video_id']: 3234, self.short['video_id']: 600}
        self.settings(rank_limit=1, max_video_minutes=45)

    def probe(self, row, *arguments, **keywords):
        return {'duration': self.durations[row['video_id']], 'view_count': row['entry']['views'],
                'timestamp': ranking.stamp(row['entry']['published']).timestamp()}

    def transcript(self, row, metadata, *arguments):
        transcript = self.complete_transcript()
        transcript['segments'][0]['end'] = metadata['duration']
        return transcript

    def collect(self, entries=None):
        with patch.object(discovery, 'fetch', return_value=entries or [self.long, self.short]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', side_effect=self.probe), \
                patch.object(media, 'obtain', side_effect=self.transcript), \
                patch.object(notes, 'build', side_effect=self.note):
            return collector.collect(FetchScope())

    def notice(self):
        return next(source for source in reg.subscriptions('youtube')
                    if source['id'] == self.source['id'])['last_notice']

    def test_first_probe_names_the_over_long_video_in_the_same_round(self):
        result = self.collect()
        self.assertEqual(0, result['added'])
        expected = '超过单条时长上限 45 分钟未处理：「AI finally sleeps: AI NEWS」54 分钟'
        self.assertIn(expected, result['message'])
        self.assertIn(expected, self.notice())

    def test_still_named_after_it_is_filtered_out_of_the_ranking(self):
        self.collect()
        result = self.collect()
        self.assertEqual(1, result['added'])
        self.assertIn('「AI finally sleeps: AI NEWS」54 分钟', result['message'])
        self.assertIn('「AI finally sleeps: AI NEWS」54 分钟', self.notice())
        self.assertNotIn(self.short['title'], self.notice())

    def test_raising_the_limit_drops_the_notice(self):
        self.collect()
        self.settings(max_video_minutes=90, rank_limit=5)
        result = self.collect()
        self.assertEqual(2, result['added'])
        self.assertNotIn('超过单条时长上限', result['message'])
        self.assertNotIn('超过单条时长上限', self.notice())

    def test_channel_without_long_videos_says_nothing(self):
        result = self.collect([self.short])
        self.assertEqual(1, result['added'])
        self.assertNotIn('超过单条时长上限', result['message'])
        self.assertNotIn('超过单条时长上限', self.notice())

    def test_over_long_outside_the_window_is_not_named(self):
        old = {**self.entry(3, hours=24 * 20), 'title': 'Old AI NEWS'}
        storage.discover(self.source, [old])
        storage.update(old['video_id'], state='filtered', duration=3600)
        policy = reg.get_settings('youtube')['youtube']
        self.assertEqual([], ranking.over_length(storage.rows(), policy, FetchScope()))
        storage.discover(self.source, [self.long])
        storage.update(self.long['video_id'], state='filtered', duration=3234)
        named = ranking.over_length(storage.rows(), policy, FetchScope(), datetime.now(timezone.utc))
        self.assertEqual([self.long['video_id']], [row['video_id'] for row in named])
        self.assertEqual([self.source['id']], named[0]['eligible_sources'])
