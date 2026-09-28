"""Saving is not executing; due jobs survive a busy pipeline and restarts."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from kb import pipeline
from kb.feeds import schedule


class DailyScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'schedule.json'
        for p in (patch.object(pipeline, '_SCHED_FILE', self.path),
                  patch.object(schedule, 'tick'), patch.object(pipeline, '_log')):
            p.start(); self.addCleanup(p.stop)
        self.clock = Mock(wraps=datetime)
        self.clock.now.return_value = datetime(2026,9,28,16,42)
        p = patch.object(pipeline, 'datetime', self.clock)
        p.start(); self.addCleanup(p.stop)
        self.start = Mock(return_value=True)
        p = patch.object(pipeline, 'start_pipeline', self.start)
        p.start(); self.addCleanup(p.stop)

    def test_enable_past_time_waits_until_tomorrow(self):
        saved = pipeline.save_schedule(True, '07:30', 'wechat_local')
        self.assertEqual('2026-09-29T07:30', saved['next_run'])
        self.assertEqual('', saved['last_fired'])
        pipeline._scheduler_tick()
        self.start.assert_not_called()
        self.clock.now.return_value = datetime(2026,9,29,7,30)
        pipeline._scheduler_tick()
        self.start.assert_called_once_with(None, crawl_filter={'channel':'wechat_local'})

    def test_edit_to_earlier_or_current_minute_does_not_catch_up(self):
        pipeline.save_schedule(True, '18:00', 'wechat_local')
        for when in ('08:00','16:42'):
            pipeline.save_schedule(True, when)
            pipeline._scheduler_tick()
        self.start.assert_not_called()

    def test_future_occurrence_fires_once_even_after_restart(self):
        pipeline.save_schedule(True, '18:00', 'wechat_local')
        pipeline._scheduler_tick()
        self.start.assert_not_called()
        self.clock.now.return_value = datetime(2026,9,28,18,5)
        pipeline._scheduler_tick()  # Nothing in memory required for overdue recovery.
        self.assertEqual('2026-09-28', pipeline.load_schedule()['last_fired'])
        self.assertEqual('2026-09-29T18:00', pipeline.load_schedule()['next_run'])
        pipeline._scheduler_tick()
        self.start.assert_called_once()

    def test_busy_due_occurrence_survives_midnight_and_does_not_double_fire(self):
        pipeline.save_schedule(True, '23:59', 'wechat_local')
        self.clock.now.return_value = datetime(2026,9,28,23,59)
        self.start.return_value = False
        pipeline._scheduler_tick()
        saved = pipeline.load_schedule()
        self.assertEqual('', saved['last_fired'])
        self.assertEqual('2026-09-28T23:59', saved['next_run'])
        self.clock.now.return_value = datetime(2026,9,29,0,1)
        self.start.return_value = True
        pipeline._scheduler_tick()
        self.assertEqual('2026-09-29', pipeline.load_schedule()['last_fired'])
        self.assertEqual('2026-09-30T23:59', pipeline.load_schedule()['next_run'])
        pipeline.save_schedule(True, '18:00')
        self.assertEqual('2026-09-30T18:00', pipeline.load_schedule()['next_run'])

    def test_disable_and_reenable_past_time_does_not_run_today(self):
        pipeline.save_schedule(True, '18:00', 'wechat_local')
        pipeline.save_schedule(False, '07:30')
        pipeline._scheduler_tick()
        pipeline.save_schedule(True, '07:30')
        pipeline._scheduler_tick()
        self.start.assert_not_called()

    def test_legacy_config_still_recovers_overdue_run_on_startup(self):
        self.path.write_text(json.dumps({'enabled':True,'time':'07:30','channel':'wechat_local','last_fired':'2026-09-27'}))
        pipeline._scheduler_tick()
        self.start.assert_called_once()
        self.assertEqual('2026-09-29T07:30', pipeline.load_schedule()['next_run'])


if __name__ == '__main__':
    unittest.main()
