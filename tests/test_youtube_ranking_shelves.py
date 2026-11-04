"""候选排名那一页按格分组(`storage.shelf` / `storage.listing`)与它带出去的 `rank_limit`。

面板把排上名的按 `rank_limit` 切成一期一期,所以**排上名的一条都不许截** —— 截在第 100 条
等于把后几期整期丢掉,人只会以为后面没东西了。没排上名的那几格各截最近 100 条,但总数照报。
"""
from datetime import datetime, timedelta, timezone

from kb.youtube import storage
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
                         '第 11 期之后被截掉了,面板上后几期整期消失')
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
        """面板按它切期,必须是准入时读的那一份;反过来钉一边:改了设置就跟着变。"""
        self.assertEqual(10, self.client.get('/api/intake/youtube/ranking').json()['rank_limit'])
        self.settings(rank_limit=3)
        self.assertEqual(3, self.client.get('/api/intake/youtube/ranking').json()['rank_limit'])
