import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event, Lock, get_ident
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import fulltext, llm_request, media, notes, throttle, transcripts, translation_pool
from kb.youtube.diagnosis import MODEL
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response


class ParallelTranslation(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.row['entry']['title'] = '智能体代码核验'
        self.transcript = self.complete_transcript()
        self.blocks = [f'Original section {position} verifies generated changes.' for position in range(6)]
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        split = transcripts.blocks

        def sections(segments, limit=3000):
            return list(self.blocks) if segments is self.transcript['segments'] else split(segments, limit)

        for override in (patch.object(notes, 'model', return_value=self.selected),
                         patch.object(transcripts, 'blocks', side_effect=sections)):
            override.start()
            self.addCleanup(override.stop)

    def build(self, stopped=lambda: False, deadline=None, row=None):
        self.transcript['segments'][0]['text'] = ' '.join(self.blocks)
        return notes.build(row or self.row, self.transcript, deadline or time.monotonic() + 5, stopped)

    def position(self, prompt):
        source = prompt.split(fulltext.SOURCE_START, 1)[1].rsplit('\n</source>', 1)[0]
        return self.blocks.index(source)

    def test_three_blocks_run_together_but_output_and_cache_remain_ordered(self):
        barrier = Barrier(3, timeout=3)
        second_finished = Event()
        writer = get_ident()
        save = media.save_json
        finishes = []

        def reply(prompt, *arguments):
            position = self.position(prompt)
            if position < 3:
                barrier.wait()
            if position == 0:
                self.assertTrue(second_finished.wait(3))
            finishes.append(position)
            if position == 1:
                second_finished.set()
            return response(prompt, f'第 {position} 节：逐项核验生成的修改。')

        def persist(path, payload):
            self.assertEqual(writer, get_ident())
            save(path, payload)

        with patch.object(notes, 'call', side_effect=reply), patch.object(media, 'save_json', side_effect=persist):
            entry = self.build()
        self.assertLess(finishes.index(1), finishes.index(0))
        positions = [entry['body'].index(f'第 {position} 节：') for position in range(6)]
        self.assertEqual(sorted(positions), positions)
        cache = media.read_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json')
        self.assertEqual(6, len(cache))
        with patch.object(notes, 'call') as call:
            self.assertEqual(entry, self.build())
        call.assert_not_called()

    def test_request_limit_is_shared_by_two_simultaneous_videos(self):
        full = Event()
        release = Event()
        lock = Lock()
        active = 0
        maximum = 0

        def reply(prompt, *arguments):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == 3:
                    full.set()
            try:
                self.assertTrue(release.wait(3))
                return response(prompt, '逐项核验生成的修改并记录限制。')
            finally:
                with lock:
                    active -= 1

        other = deepcopy(self.row)
        other['video_id'] = self.entry(2)['video_id']
        with patch.object(notes, 'call', side_effect=reply), ThreadPoolExecutor(max_workers=2) as builders:
            first = builders.submit(self.build)
            second = builders.submit(self.build, row=other)
            try:
                self.assertTrue(full.wait(3))
                with lock:
                    self.assertEqual(3, active)
            finally:
                release.set()
            self.assertIn('中文译文', first.result(timeout=3)['body'])
            self.assertIn('中文译文', second.result(timeout=3)['body'])
        self.assertEqual(3, maximum)

    def test_identical_blocks_share_one_job_and_are_not_dropped_from_output(self):
        self.blocks = [self.blocks[0], self.blocks[0], self.blocks[1], self.blocks[0]]
        with patch.object(notes, 'call', side_effect=lambda prompt, *arguments:
                          response(prompt, '保留重复段落的完整中文译文。')) as call:
            entry = self.build()
        self.assertEqual(2, call.call_count)
        chinese, _ = entry['body'].split('## 原文对照', 1)
        self.assertEqual(4, chinese.count('保留重复段落的完整中文译文。'))

    def test_service_failure_keeps_received_cache_and_cancels_other_work(self):
        self.blocks = self.blocks[:3]
        saved = Event()
        save = media.save_json
        first_key = notes.signature(fulltext.STAGE, self.selected, self.blocks[0])

        def persist(path, payload):
            save(path, payload)
            if path.name == 'notes-cache.json' and first_key in payload:
                saved.set()

        def reply(prompt, selected, deadline, stopped):
            position = self.position(prompt)
            if position == 0:
                return response(prompt, '已经完成的译文应当保留。')
            if position == 1:
                self.assertTrue(saved.wait(3))
                raise media.ToolFailure('model service unavailable', origin=MODEL)
            while not stopped() and time.monotonic() < deadline:
                time.sleep(.005)
            raise media.Interrupted('cancelled')

        with patch.object(notes, 'call', side_effect=reply), patch.object(media, 'save_json', side_effect=persist), \
                self.assertRaises(media.ToolFailure):
            self.build()
        directory = media.cache_dir(self.row['video_id'])
        self.assertEqual({first_key}, set(media.read_json(directory / 'notes-cache.json')))
        self.assertFalse((directory / 'notes-language-failures.json').exists())

    def test_model_429_stops_dispatch_without_starting_a_fourth_request(self):
        barrier = Barrier(3, timeout=3)
        started = []

        def reply(prompt, selected, deadline, stopped):
            position = self.position(prompt)
            started.append(position)
            barrier.wait()
            if position == 0:
                raise llm_request.failed('Error code: 429 too many requests')
            while not stopped() and time.monotonic() < deadline:
                time.sleep(.005)
            raise media.Interrupted('cancelled')

        with throttle.opened(), patch.object(notes, 'call', side_effect=reply), \
                self.assertRaises(media.ToolFailure) as failure:
            self.build()
        self.assertNotIsInstance(failure.exception, throttle.NotAttempted)
        self.assertEqual(MODEL, failure.exception.origin)
        self.assertEqual([0, 1, 2], sorted(started))

    def test_user_cancel_rejects_late_replies_and_does_not_write_cache(self):
        full = Barrier(4, timeout=3)
        cancelled = Event()
        release = Event()
        started = []

        def reply(prompt, *arguments):
            started.append(self.position(prompt))
            full.wait()
            self.assertTrue(release.wait(3))
            return response(prompt, '取消之后到达的译文不能写入。')

        with patch.object(notes, 'call', side_effect=reply), ThreadPoolExecutor(max_workers=1) as builder:
            future = builder.submit(self.build, cancelled.is_set)
            try:
                full.wait()
                cancelled.set()
            finally:
                release.set()
            with self.assertRaises(media.Interrupted):
                future.result(timeout=3)
        self.assertEqual(3, len(started))
        directory = media.cache_dir(self.row['video_id'])
        self.assertFalse((directory / 'notes-cache.json').exists())
        self.assertFalse((directory / 'notes-language-failures.json').exists())

    def test_deadline_rejects_late_responses_without_publishing_or_caching(self):
        def reply(prompt, selected, deadline, stopped):
            while not stopped() and time.monotonic() < deadline:
                time.sleep(.005)
            return response(prompt, '超过截止时间的译文不能使用。')

        with patch.object(notes, 'call', side_effect=reply), self.assertRaises(media.Interrupted):
            self.build(deadline=time.monotonic() + .1)
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())

    def test_expired_queued_work_never_calls_the_model_when_a_slot_opens(self):
        release = Event()
        full = Barrier(4, timeout=3)

        def occupy():
            full.wait()
            release.wait(3)

        occupied = [translation_pool._POOL.submit(occupy) for _ in range(3)]
        try:
            full.wait()
            with patch.object(notes, 'call') as call, self.assertRaises(media.Interrupted):
                self.build(deadline=time.monotonic() + .1)
            call.assert_not_called()
        finally:
            release.set()
            for future in occupied:
                future.result(timeout=3)
