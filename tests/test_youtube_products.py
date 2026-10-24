import json
from contextlib import closing
from unittest.mock import patch

from kb import config, extract, scanner, store
from kb.chunker import split_chunks
from kb.collectors import FetchScope
from kb.config import Source
from kb.feeds import registry as reg, sync
from kb.youtube import collector, discovery, media, notes, products, storage
from tests.youtube_fixture import YouTubeFixture


OLD_SCHEMA = '''CREATE TABLE youtube_candidates (
    video_id TEXT PRIMARY KEY,source_id INTEGER,source_url TEXT,entry TEXT,
    views INTEGER DEFAULT 0,heat REAL DEFAULT 0,value REAL DEFAULT 0,
    heat_rank INTEGER DEFAULT 0,value_rank INTEGER DEFAULT 0,state TEXT,
    reason TEXT,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,
    duration REAL DEFAULT 0,note TEXT DEFAULT '',committed INTEGER DEFAULT 0,updated_at TEXT)'''


class YouTubeProducts(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()
        self.old = {**self.note({'entry': self.entry_value, 'channel': self.source['name']}),
                    'body': '旧的片尾几句话。\n\n## 完整中文转录\n\n感谢观看。'}

    def legacy(self, committed=False):
        with reg.db() as connection:
            connection.execute(OLD_SCHEMA)
            connection.execute('''INSERT INTO youtube_candidates
                (video_id,source_id,source_url,entry,state,reason,duration,note,committed,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (self.entry_value['video_id'], self.source['id'], self.source['url'], json.dumps(self.entry_value),
                 'ready', '旧版中文成品', 600, json.dumps(self.old), int(committed), reg.now()))
        if committed:
            reg.put_items(self.source, [self.old])

    def run_collect(self, available=True, transcript=None):
        with patch.object(discovery, 'fetch', return_value=[]), \
                patch.object(notes, 'available', return_value=(available, '' if available else '缺模型')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=transcript or self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note) as build:
            result = collector.collect(FetchScope(force=True))
        return result, build.call_count

    def test_uncommitted_legacy_ready_requires_regeneration_before_publication(self):
        self.legacy()
        self.assertEqual([], storage.ready(self.source))
        result, count = self.run_collect(available=False)
        self.assertEqual((0, 0, 0), (result['processed'], result['added'], count))
        self.assertEqual([], reg.items('youtube')['items'])
        result, count = self.run_collect()
        self.assertEqual((1, 1, 1), (result['processed'], result['added'], count))
        self.assertNotIn('旧的片尾', reg.items('youtube')['items'][0]['body'])
        self.assertTrue(products.valid(storage.rows()[0]))
        result, count = self.run_collect()
        self.assertEqual((0, 0), (result['processed'], count))
        with reg.db() as connection:
            self.assertEqual(1, connection.execute('SELECT count(*) FROM youtube_candidates').fetchone()[0])

    def test_committed_legacy_is_quarantined_then_replaced_without_duplicate(self):
        self.legacy(committed=True)
        result, _ = self.run_collect(available=False)
        self.assertNotIn(str(self.source['id']), result['succeeded_ids'])
        item = reg.items('youtube')['items'][0]
        self.assertEqual(products.PENDING, item['body'])
        self.assertNotIn('完整中文转录', item['body'])
        result, count = self.run_collect()
        self.assertEqual((1, 0, 1), (result['processed'], result['added'], count))
        self.assertEqual(1, reg.items('youtube')['total'])
        self.assertIn('具体实现和限制', reg.items('youtube')['items'][0]['body'])

    def test_previous_global_ready_and_bad_transcript_cannot_bypass_new_policy(self):
        storage.discover(self.source, [self.entry_value])
        storage.update(self.entry_value['video_id'], state='ready', note=json.dumps(self.old))
        directory = media.cache_dir(self.entry_value['video_id'])
        media.save_json(directory / 'transcript.json', {'video_id': self.entry_value['video_id'],
            'origin': 'uploaded_captions', 'language': 'en', 'segments': [
                {'start': 590, 'end': 600, 'text': 'Thanks for watching this AI tutorial.'}]})
        with patch.object(discovery, 'fetch', return_value=[]), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'captions', return_value=self.complete_transcript()) as captions, \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual(1, result['processed'])
        captions.assert_called_once()
        self.assertTrue(products.valid(storage.rows()[0]))

    def test_proof_binds_input_policy_and_final_body_at_publication(self):
        storage.discover(self.source, [self.entry_value])
        self.run_collect()
        row = storage.rows()[0]
        self.assertTrue(products.valid(row))
        damaged = json.loads(row['note']) | {'body': '被替换成不完整正文。'}
        storage.update(row['video_id'], note=json.dumps(damaged))
        self.assertEqual([], storage.ready(self.source))
        with self.assertRaises(ValueError):
            collector.commit_notes(self.source, [row], {'added': 0, 'message': ''})
        altered = json.loads(row['validation'])
        altered['input']['duration'] = 3000
        self.assertFalse(products.valid({**row, 'validation': json.dumps(altered)}))
        altered = json.loads(row['validation'])
        altered['version'] = 'obsolete'
        self.assertFalse(products.valid({**row, 'validation': json.dumps(altered)}))

    def test_known_old_published_body_can_repair_outside_new_video_window(self):
        self.entry_value = self.entry(hours=24 * 60)
        self.old.update(url=self.entry_value['url'], published=self.entry_value['published'])
        self.legacy(committed=True)
        result, count = self.run_collect()
        self.assertEqual((1, 1), (result['processed'], count))
        self.assertNotIn('旧的片尾', reg.items('youtube')['items'][0]['body'])

    def test_rebuilt_body_reexports_reindexes_and_retires_old_extraction(self):
        vault = self.root / 'vault'
        for override in (patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                         patch.object(scanner, 'VAULT_ROOT', vault), patch.object(extract, 'VAULT_ROOT', vault)):
            override.start()
            self.addCleanup(override.stop)
        self.legacy(committed=True)
        sync.export('youtube')
        key = reg.item_key('youtube', self.entry_value['url'])
        relative = f'source/youtube/{key}.md'
        source = Source('youtube', vault / 'source/youtube')

        def index():
            document = scanner._doc_from_file(vault / relative, source)
            with closing(store.connect(config.DB_PATH)) as connection:
                store.upsert_document(connection, document, split_chunks(document.body), reg.now())
                connection.commit()

        index()
        with closing(store.connect(config.DB_PATH)) as connection:
            old = {'summary': '旧片尾摘要', 'topics': ['old-video'],
                   'entities': [{'name': 'obsolete', 'kind': 'project'}]}
            stale = extract.save_extraction(connection, relative, old)
            connection.commit()
            extract.writeback_extraction(connection, relative, old, stale)
            self.assertIsNotNone(connection.execute('SELECT extracted_at FROM documents').fetchone()[0])
        result, _ = self.run_collect()
        self.assertEqual(1, result['processed'])
        with reg.db() as connection:
            item = connection.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone()
            self.assertNotEqual(item['content_hash'], item['exported_hash'])
            self.assertEqual(1, item['extract_stale'])
        sync.export('youtube')
        index()
        text = (vault / relative).read_text()
        self.assertIn('具体实现和限制', text)
        self.assertNotIn('旧的片尾', text)
        self.assertNotIn('topic/old-video', text)
        with closing(store.connect(config.DB_PATH)) as connection:
            document = connection.execute('SELECT * FROM documents WHERE path=?', (relative,)).fetchone()
            self.assertIsNone(document['extracted_at'])
            self.assertEqual('', document['summary'])
            self.assertEqual([], extract.pending_docs(connection, 100))
            reg.save_subscription('youtube', {'extract': True}, self.source['id'])
            self.assertEqual([relative], [row['path'] for row in extract.pending_docs(connection, 100)])
            self.assertEqual([], connection.execute('SELECT * FROM entities WHERE path=?', (relative,)).fetchall())
