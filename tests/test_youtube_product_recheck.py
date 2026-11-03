"""成品完整性复核只复核变过的那几条(`products.quarantine`)。

`quarantine` 跑在 `registry.db()` 的初始化事务里,每开一个写连接跑一次。它从前每次都把
全部成品的字幕重新校验一遍,一个要开几十次连接的接口(「问题与待办」的总览)因此要
10–20 秒、整个服务跟着卡住。这里钉两边:

- **不该复核的不复核**:没人动过的成品,再开多少次连接 `valid` 一次都不调;
- **该复核的一条不漏**:写入哪怕绕过本进程的代码(另一个进程的裸连接),下一次开连接
  就撤下来;进程重启后照旧全量复核一遍,盖住校验口径的变化。

判据落在后果上:库里那一条正文是什么、`repair_pending` 是几,而不是「标志打上没有」。
"""
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

from kb.feeds import registry as reg
from kb.youtube import collector, products, storage, transcripts
from tests.youtube_fixture import YouTubeFixture


class YouTubeProductRecheck(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.sources = [self.channel(number) for number in (2, 3)]
        self.entries = [self.entry(number) for number in (2, 3)]
        self.notes = [self.publish(source, entry) for source, entry in zip(self.sources, self.entries)]
        # 发布那一下自己写了 `feed_items`,下一次打开会把它复核一遍;先让它过去,
        # 各条用例数的就只是自己那一笔写入带出来的复核。
        with reg.db():
            pass

    def publish(self, source, entry):
        storage.discover(source, [entry])
        row = storage.rows([source['id']])[0]
        note = self.note(row)
        storage.update(entry['video_id'], state='ready', note=json.dumps(note),
                       validation=products.certify(row, {'duration': 600}, self.complete_transcript(), note))
        collector.commit_notes(source, storage.ready(source), {'added': 0, 'message': ''})
        return note

    def body(self, entry):
        with reg.db() as connection:
            return connection.execute("SELECT body FROM feed_items WHERE kind='youtube' AND url=?",
                                      (entry['url'],)).fetchone()['body']

    def video(self, entry):
        with reg.db() as connection:
            return connection.execute('SELECT * FROM youtube_videos WHERE video_id=?',
                                      (entry['video_id'],)).fetchone()

    def elsewhere(self, sql, *params):
        """另一个进程的写:不经过 `registry.db()`,本进程的 Python 一行都没参与。"""
        with closing(sqlite3.connect(reg.DB_PATH)) as connection:
            connection.execute(sql, params)
            connection.commit()

    def counted(self):
        return patch.object(products, 'valid', side_effect=products.valid)

    def test_untouched_products_are_not_rechecked_on_every_connection(self):
        self.elsewhere('UPDATE youtube_videos SET views=views+1, score_rank=7')
        with self.counted() as valid:
            for _ in range(5):
                with reg.db():
                    pass
        self.assertEqual(0, valid.call_count)
        for entry, note in zip(self.entries, self.notes):
            self.assertEqual(note['body'], self.body(entry))
            self.assertEqual('ready', self.video(entry)['state'])

    def test_a_write_from_another_connection_is_withdrawn_on_the_next_open(self):
        damaged, kept = self.entries
        self.elsewhere("UPDATE youtube_videos SET validation='' WHERE video_id=?", damaged['video_id'])
        with self.counted() as valid:
            self.assertEqual(products.PENDING, self.body(damaged))
        self.assertEqual(1, valid.call_count)
        row = self.video(damaged)
        self.assertEqual(('candidate', 1), (row['state'], row['repair_pending']))
        self.assertEqual(self.notes[1]['body'], self.body(kept))
        self.assertEqual('ready', self.video(kept)['state'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute(f'SELECT count(*) FROM {products.DIRTY}').fetchone()[0])

    def test_a_replaced_body_and_a_deleted_video_are_both_withdrawn(self):
        replaced, orphaned = self.entries
        self.elsewhere("UPDATE feed_items SET body='被替换成不完整正文。' WHERE kind='youtube' AND url=?",
                       replaced['url'])
        self.elsewhere('DELETE FROM youtube_videos WHERE video_id=?', orphaned['video_id'])
        self.assertEqual(products.PENDING, self.body(replaced))
        self.assertEqual(products.PENDING, self.body(orphaned))
        # 成品本身还合格,修复走「重新提交」那条路:归属的已提交标记被放掉。
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT committed FROM youtube_video_sources'
                                                   ' WHERE video_id=?', (replaced['video_id'],)).fetchone()[0])

    def test_a_new_process_rechecks_everything_once(self):
        """校验口径变了只在重启后生效:重启后的第一次打开要把旧成品全部按新口径过一遍。"""
        with patch.object(transcripts, 'validate', side_effect=ValueError('口径收紧')):
            with reg.db():
                pass
            self.assertEqual(self.notes[0]['body'], self.body(self.entries[0]))
            with patch.object(products, '_SWEPT', set()):
                for entry in self.entries:
                    self.assertEqual(products.PENDING, self.body(entry))
        for entry in self.entries:
            self.assertEqual('candidate', self.video(entry)['state'])
