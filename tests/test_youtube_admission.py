import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage
from kb.youtube.settings import of
from tests.youtube_fixture import YouTubeFixture


class YouTubeAdmission(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()

    def collect(self, metadata, force=False):
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=metadata) as probe, \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope(force=force))
        return result, probe

    def test_all_subscriptions_admit_before_video_dedup_and_commit_separately(self):
        alias = self.channel(2)
        self.entry_value = self.entry(hours=24 * 6)
        now = datetime.now(timezone.utc)
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               ((now - timedelta(days=5)).isoformat(), self.source['id']))
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               ((now - timedelta(days=30)).isoformat(), alias['id']))
        result, probe = self.collect({'duration': 600})
        self.assertEqual((1, 1, 1), (result['ranked'], result['processed'], result['added']))
        probe.assert_called_once()
        self.assertEqual(1, reg.items('youtube', source_id=alias['id'])['total'])
        self.assertEqual(0, reg.items('youtube', source_id=self.source['id'])['total'])

    def test_highest_eligible_weight_wins_not_insertion_order(self):
        alias = self.channel(2, youtube={'score_multiplier': 3})
        reg.save_subscription('youtube', {'youtube': {'score_multiplier': .1}}, self.source['id'])
        storage.discover(self.source, [self.entry_value])
        storage.discover(alias, [self.entry_value])
        policy = self.settings()['youtube']
        global_row = ranking.score(storage.rows(), policy, FetchScope())[0]
        alias_row = ranking.score(storage.rows([alias['id']]), policy, FetchScope())[0]
        self.assertEqual(alias['id'], global_row['source_id'])
        self.assertEqual(alias_row['score'], global_row['score'])
        reg.save_subscription('youtube', {'youtube': {'pool': 'creator_watchlist'}}, alias['id'])
        global_row = ranking.score(storage.rows(), policy, FetchScope())[0]
        self.assertEqual(self.source['id'], global_row['source_id'])

    def test_missing_duration_waits_then_force_rechecks_and_recovers(self):
        result, _ = self.collect({'duration': None})
        row = storage.rows()[0]
        self.assertEqual(('deferred', 'duration'), (row['state'], row['gate_code']))
        self.assertGreater(row['retry_at'], time.time())
        self.assertNotIn(str(self.source['id']), result['succeeded_ids'])
        result, probe = self.collect({'duration': 600})
        self.assertEqual(0, result['processed'])
        probe.assert_not_called()
        result, probe = self.collect({'duration': 600}, force=True)
        self.assertEqual(1, result['processed'])
        self.assertTrue(probe.call_args.args[-1])

    def test_premiere_and_live_reprobe_after_wait_without_force(self):
        for status in ('is_upcoming', 'is_live'):
            with self.subTest(status=status):
                result, _ = self.collect({'duration': 600, 'live_status': status})
                self.assertEqual(0, result['processed'])
                row = storage.rows()[0]
                self.assertEqual('deferred', row['state'])
                storage.update(row['video_id'], retry_at=0)
                result, probe = self.collect({'duration': 600, 'live_status': 'not_live'})
                self.assertEqual(1, result['processed'])
                self.assertTrue(probe.call_args.args[-1])
                storage.update(row['video_id'], state='candidate', note='', validation='')

    def test_permanent_exclusion_does_not_rank_but_explicit_retry_rechecks(self):
        self.collect({'duration': 60, 'media_type': 'short'})
        self.assertEqual(('excluded', 'shorts'), tuple(storage.rows()[0][field] for field in ('state', 'gate_code')))
        result, probe = self.collect({'duration': 600})
        self.assertEqual(0, result['processed'])
        probe.assert_not_called()
        result, probe = self.collect({'duration': 600}, force=True)
        self.assertEqual(1, result['processed'])
        probe.assert_called_once()

    def test_legacy_untyped_exclusion_is_reopened_on_upgrade(self):
        storage.discover(self.source, [self.entry_value])
        storage.update(self.entry_value['video_id'], state='excluded', reason='没有可靠时长')
        self.assertEqual('legacy', storage.rows()[0]['gate_code'])
        result, probe = self.collect({'duration': 600})
        self.assertEqual(1, result['processed'])
        self.assertTrue(probe.call_args.args[-1])

    def test_real_short_metadata_blocks_watch_and_shorts_urls_before_media(self):
        for number, path in enumerate(('watch?v=', 'shorts/'), 2):
            with self.subTest(path=path):
                entry = self.entry(number)
                metadata = {'id': entry['video_id'], 'duration': 60, 'media_type': 'short',
                            'webpage_url': 'https://www.youtube.com/' + path + entry['video_id']}
                with patch.object(discovery, 'fetch', return_value=[entry]), \
                        patch.object(notes, 'available', return_value=(True, '')), \
                        patch.object(media, 'probe', return_value=metadata), \
                        patch.object(media, 'obtain') as obtain, patch.object(notes, 'build') as build:
                    result = collector.collect(FetchScope(force=True))
                obtain.assert_not_called()
                build.assert_not_called()
                self.assertEqual((0, 0), (result['processed'], result['admitted']))
                row = next(row for row in storage.rows() if row['video_id'] == entry['video_id'])
                self.assertEqual(('excluded', 'shorts'), (row['state'], row['gate_code']))

    def test_short_type_normalization_does_not_exclude_ordinary_short_videos(self):
        policy = self.settings()['youtube']
        for media_type in ('short', 'shorts', ' SHORT '):
            self.assertEqual('shorts', collector.gate({'duration': 60, 'media_type': media_type}, policy).code)
        for media_type in ('video', None, ''):
            self.assertIsNone(collector.gate({'duration': 60, 'media_type': media_type,
                                            'webpage_url': self.entry_value['url']}, policy))


class SingleRanking(YouTubeFixture):
    """热度榜和价值榜合成一张榜之后的准入。

    合之前准入写成「热度名次 ≤ A **或** 价值名次 ≤ B」:实际准入面是两个上限之和,
    面板上那两个数字谁都说不清一轮会处理几条。所以这里钉的是**一个分、一个名次、
    一个上限**,以及合并没有把两组信号中的任何一组扔掉。
    """

    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()

    def ranked(self, entries, **values):
        storage.discover(self.source, entries)
        policy = self.settings(**values)['youtube']
        return ranking.score(storage.rows(), policy, FetchScope()), policy

    def test_one_score_one_rank_and_the_old_columns_stop_moving(self):
        rows, _ = self.ranked([self.entry(1, views=500000),
                               self.entry(2, views=100), self.entry(3, views=9000)])
        self.assertEqual([1, 2, 3], sorted(row['score_rank'] for row in rows))
        self.assertEqual(3, len({row['score_rank'] for row in rows}), '名次不许并列')
        best = min(rows, key=lambda row: row['score_rank'])
        self.assertEqual(f'{1:011d}', best['video_id'], '播放量最高的那条该排第一')
        stored = {row['video_id']: row for row in storage.rows()}
        self.assertTrue(all(row['score'] > 0 and row['score_rank'] for row in stored.values()))
        # **旧的四列停止读写但没 DROP**(主干迁移只增不减)。它们不再被写,就该一直是 0;
        # 哪天又有人往里写,说明两张榜回来了。
        with reg.db() as connection:
            legacy = connection.execute('SELECT heat,value,heat_rank,value_rank '
                                        'FROM youtube_videos').fetchall()
        self.assertTrue(all(tuple(item) == (0, 0, 0, 0) for item in legacy), list(map(tuple, legacy)))

    def test_rank_limit_alone_decides_how_many_get_processed(self):
        """**一个上限说得清一轮处理几条。** 合之前「关键词多但播放量低」那条会从另一张
        榜挤进来,准入面是两个上限之和;现在上限就是上限。"""
        rows, policy = self.ranked([self.entry(1, views=500000),
                                    {**self.entry(2, views=120), 'title': 'open source paper tutorial benchmark'},
                                    self.entry(3, views=9000)], rank_limit=1)
        chosen = ranking.select(rows, policy)
        self.assertEqual(1, len(chosen))
        self.assertEqual(f'{1:011d}', chosen[0]['video_id'])
        # 反过来钉一边:放宽上限就该让后面那几条照旧进来,这道闸不是把来源关掉。
        wider = ranking.select(rows, {**policy, 'rank_limit': 3})
        self.assertEqual([1, 2, 3], [row['score_rank'] for row in wider])

    def test_merging_keeps_both_signals(self):
        """**合并不是「只按播放量排」。** 两组信号各自都要还能改变名次,所以这三条播放量、
        发布时间、权重全一样,只有标题不同:干货加分、标题党扣分。

        **三条的编号故意按「期望名次的反序」排**:并列时 `score` 相同就按 video_id 定序,
        编号顺着期望排的话,任一组信号被扔掉这条用例都还是绿的(第一次就这么写,注入
        「只按播放量」没红)。
        """
        rows, _ = self.ranked([
            {**self.entry(1, views=10000), 'title': 'AI agent SHOCKING secret'},
            {**self.entry(2, views=10000), 'title': 'AI agent update'},
            {**self.entry(3, views=10000), 'title': 'AI agent benchmark tutorial explained'}])
        order = {row['video_id']: row['score_rank'] for row in rows}
        self.assertEqual([3, 2, 1], [int(video_id) for video_id, _ in
                                     sorted(order.items(), key=lambda item: item[1])],
                         f'干货第一、普通第二、标题党最后;实得 {order}')

    def test_settings_stored_before_the_merge_still_open(self):
        """**旧键一个都不许不认。** `validate` 对不认识的键是整个抛,而它同时管读和写 ——
        只认新键的话,库里存着 `heat_limit` 的人一打开这一块,连预算和登录态配置一起读不出来。
        换算取宽的那一个:老口径是两个名次取并集,取小的会悄悄缩掉他原来的准入面。
        """
        # 直接按「升级前那一版写下的设置」落库,再走面板读设置的那条路。
        with reg.db() as connection:
            connection.execute("INSERT INTO feed_settings(kind,value) VALUES('youtube',?) "
                               "ON CONFLICT(kind) DO UPDATE SET value=excluded.value",
                               (json.dumps({'youtube': {'heat_limit': 12, 'value_limit': 25,
                                                        'max_videos': 4}}),))
        policy = of(reg.get_settings('youtube'))
        self.assertEqual(25, policy['rank_limit'])
        self.assertEqual(4, policy['max_videos'], '同一份设置里别的字段不许被顺带丢掉')
        self.assertNotIn('heat_limit', policy)
        # 显式给了新键就以新键为准;越界的旧值夹进范围,不悄悄换成默认的 10。
        self.assertEqual(7, of({'youtube': {'heat_limit': 12, 'rank_limit': 7}})['rank_limit'])
        self.assertEqual(1, of({'youtube': {'heat_limit': 0}})['rank_limit'])
        self.assertEqual(50, of({'youtube': {'value_limit': 999}})['rank_limit'])
