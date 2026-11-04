"""超过单条时长上限的视频不许一直占着准入名次。

事故形态:前 10 名里两条超过 45 分钟的视频只被记成 `deferred`,每轮都排回原名次、每轮再探
一次元数据、再被 `admission.gate` 拒一次 —— 名额被它们连占三天,一轮只处理、入库了 1 条,
后面那些普通视频永远轮不到。判据在 `ranking.rejection`(按**当前**上限),时长由 `process`
在拒掉那一刻记下。

后果断言落在「下一轮谁被处理、库里那一条是什么状态」,不是「duration 写没写」。
"""

import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage
from tests.youtube_fixture import YouTubeFixture


class DurationSlot(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.long = self.entry(1, views=500000)
        self.short = self.entry(2, views=1000)
        self.durations = {self.long['video_id']: 3600, self.short['video_id']: 600}
        self.settings(rank_limit=1, max_video_minutes=45)

    def probe(self, row, *arguments, **keywords):
        return {'duration': self.durations[row['video_id']], 'view_count': row['entry']['views'],
                'timestamp': ranking.stamp(row['entry']['published']).timestamp()}

    def transcript(self, row, metadata, *arguments):
        transcript = self.complete_transcript()
        transcript['segments'][0]['end'] = metadata['duration']
        return transcript

    def collect(self):
        with patch.object(discovery, 'fetch', return_value=[self.long, self.short]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', side_effect=self.probe), \
                patch.object(media, 'obtain', side_effect=self.transcript) as obtain, \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope())
        return result, [call.args[0]['video_id'] for call in obtain.call_args_list]

    def row(self, entry):
        return next(row for row in storage.rows() if row['video_id'] == entry['video_id'])

    def test_over_long_video_yields_its_slot_from_the_next_round(self):
        result, obtained = self.collect()
        self.assertEqual([], obtained)
        self.assertEqual(0, result['added'])
        self.assertEqual('deferred', self.row(self.long)['state'])

        result, obtained = self.collect()
        self.assertEqual([self.short['video_id']], obtained)
        self.assertEqual(1, result['added'])
        long = self.row(self.long)
        self.assertEqual(('filtered', 0), (long['state'], long['score_rank']))
        self.assertIn('60 分钟，上限 45 分钟', long['reason'])
        self.assertEqual('ready', self.row(self.short)['state'])

    def test_raising_the_limit_brings_it_back(self):
        self.collect()
        self.collect()
        self.settings(max_video_minutes=90)
        result, obtained = self.collect()
        self.assertEqual([self.long['video_id']], obtained)
        self.assertEqual('ready', self.row(self.long)['state'])
        self.assertEqual(2, reg.items('youtube')['total'])

    def test_finished_long_video_stays_finished_after_the_limit_is_lowered(self):
        self.settings(max_video_minutes=90)
        self.collect()
        self.assertEqual('ready', self.row(self.long)['state'])
        self.settings(max_video_minutes=45)
        ranking.score(storage.rows(), reg.get_settings('youtube')['youtube'], FetchScope())
        self.assertEqual(('ready', '中文内容笔记已完成'), (self.row(self.long)['state'], self.row(self.long)['reason']))
        self.assertEqual(1, reg.items('youtube')['total'])

    def test_unknown_duration_still_competes(self):
        storage.discover(self.source, [self.long])
        storage.update(self.long['video_id'], state='deferred', reason='超过单条视频时长预算',
                       gate_code='duration_budget', retry_at=time.time() - 1)
        ranked = ranking.score(storage.rows(), reg.get_settings('youtube')['youtube'], FetchScope())
        self.assertEqual([1], [row['score_rank'] for row in ranked])
