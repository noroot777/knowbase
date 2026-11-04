"""候选排名那一页按格分组(`storage.shelf` / `storage.listing`)与它带出去的 `rank_limit`。

面板把排上名的全部按发布日期摊开,所以**排上名的一条都不许截** —— 截在第 100 条,名次靠后
的那些就从它们那天的分组里消失,人只会以为那天就这么几条。没排上名的那几格各截最近 100 条,但总数照报。
"""
import json
import time
from datetime import datetime, timezone

from kb.feeds import registry as reg
from kb.youtube import ranking, storage
from kb.youtube.settings import of
from tests.youtube_fixture import YouTubeFixture


class RankingShelves(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()

    def stock(self, first, count, **values):
        entries = [self.entry(number, hours=number) for number in range(first, first + count)]
        storage.discover(self.source, entries)
        for position, entry in enumerate(entries, 1):
            patch = {key: (value(position) if callable(value) else value) for key, value in values.items()}
            if patch:
                storage.update(entry['video_id'], **patch)
        return entries

    def test_every_ranked_video_is_listed_even_past_one_hundred(self):
        self.stock(1, 130, state='candidate', score_rank=lambda position: position, score=1.0)
        listed = storage.listing()
        ranked = [item for item in listed['items'] if item['shelf'] == 'ranked']
        self.assertEqual(list(range(1, 131)), [item['score_rank'] for item in ranked],
                         '第 100 名之后被截掉了,那几天的分组里少了人')
        self.assertEqual(130, listed['shelves']['ranked'])

    def test_unranked_shelves_keep_the_latest_hundred_and_report_the_true_count(self):
        ready = self.stock(1, 120, state='ready', score_rank=0)
        listed = storage.listing()
        shown = [item for item in listed['items'] if item['shelf'] == 'ready']
        self.assertEqual(120, listed['shelves']['ready'])
        self.assertEqual(100, len(shown))
        # `hours=number`:编号越小越新,留下的必须是最新那 100 条。
        self.assertEqual({entry['video_id'] for entry in ready[:100]}, {item['video_id'] for item in shown})

    def test_each_row_lands_on_exactly_one_shelf(self):
        ranked, ready, filtered, waiting = (self.stock(number, 1)[0] for number in range(1, 5))
        storage.update(ranked['video_id'], state='deferred', score_rank=1)
        storage.update(ready['video_id'], state='ready', score_rank=0)
        storage.update(filtered['video_id'], state='filtered', score_rank=0)
        storage.update(waiting['video_id'], state='candidate', score_rank=0)
        listed = storage.listing()
        self.assertEqual({ranked['video_id']: 'ranked', ready['video_id']: 'ready',
                          filtered['video_id']: 'skipped', waiting['video_id']: 'waiting'},
                         {item['video_id']: item['shelf'] for item in listed['items']})
        self.assertEqual({'ranked': 1, 'ready': 1, 'skipped': 1, 'waiting': 1}, listed['shelves'])

    def test_the_endpoint_carries_the_saved_rank_limit(self):
        """页头那行「每轮准入前 N 名」用它,必须是准入时读的那一份;反过来钉一边:改了设置就跟着变。"""
        self.assertEqual(10, self.client.get('/api/intake/youtube/ranking').json()['rank_limit'])
        self.settings(rank_limit=3)
        self.assertEqual(3, self.client.get('/api/intake/youtube/ranking').json()['rank_limit'])

    def test_a_video_judged_unprocessable_leaves_the_ranking_even_with_its_rank_left_over(self):
        """**处理那一步拒掉的(直播回放、Shorts)只改状态,名次要等下一次重排才清。** 这时它不许
        还落在日期榜里、更不许标「当前准入」;退避中的那条留在榜上,但也不标准入。
        「当前准入」要跟 `ranking.select` 真选出来的那几条**一模一样**(比值,不只比有无):
        反过来钉一边,正常候选照旧标着。"""
        rejected, backoff, normal, outside = (self.stock(number, 1)[0] for number in range(1, 5))
        # 照 `collector` 那一步原样写,带上 `gate_code`:没有它的 excluded 会被建库时那道存量迁移
        # 当成旧数据改回 deferred,测的就不是处理那一步拒掉的那一类了。
        storage.update(rejected['video_id'], state='excluded', score_rank=1, gate_code='replay',
                       reason='直播回放不进入自动处理')
        storage.update(backoff['video_id'], state='deferred', score_rank=2, retry_at=time.time() + 3600)
        storage.update(normal['video_id'], state='candidate', score_rank=3)
        storage.update(outside['video_id'], state='candidate', score_rank=11)
        listed = {item['video_id']: item for item in self.client.get('/api/intake/youtube/ranking').json()['items']}
        self.assertEqual('skipped', listed[rejected['video_id']]['shelf'], '不处理的那条还挂在日期榜上')
        self.assertEqual('ranked', listed[backoff['video_id']]['shelf'])
        admitted = {video_id for video_id, item in listed.items() if item['admitted']}
        policy = of(reg.get_settings('youtube'))
        selected = {row['video_id'] for row in ranking.select(
            [row for row in storage.rows() if row['score_rank']], policy)}
        self.assertEqual({normal['video_id']}, admitted)
        self.assertEqual(selected, admitted)

    def test_latest_is_decided_by_the_moment_not_the_string(self):
        """库里的发布时间带着不同偏移:`08:00+08:00` 是 UTC 0 点,比 `01:00+00:00` 早一小时,
        字符串比却排在后面。截到 1 条时留下的必须是真正更新的那一条。"""
        early, late = self.stock(1, 2, state='ready', score_rank=0)
        with storage.db() as connection:
            for entry, published in ((early, '2026-11-04T08:00:00+08:00'), (late, '2026-11-04T01:00:00+00:00')):
                connection.execute('UPDATE youtube_videos SET entry=? WHERE video_id=?',
                                   (json.dumps({**entry, 'published': published}), entry['video_id']))
        listed = storage.listing(limit=1)
        self.assertEqual([late['video_id']], [item['video_id'] for item in listed['items']])
        self.assertEqual(2, listed['shelves']['ready'])
        self.assertEqual(datetime(2026, 11, 4, 1, tzinfo=timezone.utc).timestamp(), listed['items'][0]['published_at'])
