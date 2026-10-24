from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

if os.name == 'nt':
    from scripts import win_job


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / 'scripts' / 'win_daemon.py'
WORKER = '''
import os, signal, subprocess, sys, time
from pathlib import Path
directory = Path(sys.argv[1])
mode = sys.argv[2]
signal.signal(signal.SIGBREAK, signal.SIG_IGN)
if mode == 'root':
    child = subprocess.Popen([sys.executable, __file__, str(directory), 'child'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    (directory / 'child.pid').write_text(str(child.pid))
    def finish(event, frame):
        (directory / 'late.request').touch()
        sys.exit(0)
    signal.signal(signal.SIGBREAK, finish)
    (directory / 'ready').touch()
elif mode == 'child':
    (directory / 'child.ready').touch()
    while not (directory / 'late.request').exists():
        time.sleep(.02)
    child = subprocess.Popen([sys.executable, __file__, str(directory), 'leaf'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    (directory / 'late.pid').write_text(str(child.pid))
else:
    (directory / 'ready').touch()
while True:
    time.sleep(.05)
'''


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration')
class WindowsJobTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='knowbase-实例 ')
        self.directory = Path(self.temporary.name)
        self.records = {}
        self.worker = self.directory / 'worker.py'
        self.worker.write_text(WORKER, encoding='utf-8')

    def tearDown(self):
        for path, content in self.records.items():
            path.write_text(content, encoding='utf-8')
            result = self.helper('stop', str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def helper(self, *arguments):
        return subprocess.run([sys.executable, '-X', 'utf8', str(HELPER), *arguments],
                              capture_output=True, encoding='utf-8', timeout=40)

    def wait_for(self, path):
        deadline = time.monotonic() + 10
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail(f'worker did not create {path}')
            time.sleep(.02)

    def start(self, name='service', mode='leaf'):
        directory = self.directory / name
        directory.mkdir()
        path = directory / 'instance.pid'
        result = self.helper('spawn', str(path), str(directory / 'web.log'),
                             str(directory), '--', sys.executable, str(self.worker),
                             str(directory), mode)
        if path.exists():
            self.records[path] = path.read_text(encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr + (directory / 'web.log').read_text())
        self.wait_for(directory / 'ready')
        return path, directory

    def assert_gone(self, process_id):
        process = win_job.open_process(0x00100000, False, process_id)
        if not process:
            return
        try:
            import _winapi
            self.assertEqual(_winapi.WaitForSingleObject(process, 1000), 0)
        finally:
            win_job.close(process)

    def test_managed_members_survive_launcher_and_stop_cleans_everything(self):
        path, _ = self.start()
        record = json.loads(self.records[path])
        self.assertIn(record['pid'], win_job.state(path)['pids'])
        result = self.helper('stop', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assert_gone(record['pid'])

    def test_late_descendant_after_root_exit_is_still_killed(self):
        path, directory = self.start(mode='root')
        self.wait_for(directory / 'child.ready')
        root_pid = json.loads(self.records[path])['pid']
        child_pid = int((directory / 'child.pid').read_text())
        result = self.helper('stop', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((directory / 'late.pid').exists(), 'late child was never spawned')
        late_pid = int((directory / 'late.pid').read_text())
        for process_id in (root_pid, child_pid, late_pid):
            self.assert_gone(process_id)

    def test_stopping_one_instance_leaves_another_running(self):
        first, _ = self.start('first')
        second, _ = self.start('second')
        second_pid = json.loads(self.records[second])['pid']
        result = self.helper('stop', str(first))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(second_pid, win_job.state(second)['pids'])

    def test_different_creation_stamp_never_receives_a_console_event(self):
        path, _ = self.start()
        record = json.loads(self.records[path])
        handle = win_job.acquire_job(record)
        try:
            record['created'] = str(int(record['created']) - 1)
            with mock.patch.object(win_job, 'attach_console', return_value=False) as attach:
                win_job.signal_root(handle, record)
                attach.assert_not_called()
        finally:
            win_job.close(handle)

    def test_job_membership_is_required_even_with_exact_creation_stamp(self):
        path, _ = self.start()
        record = json.loads(self.records[path])
        process = win_job.open_process(0x1000, False, os.getpid())
        try:
            record.update(pid=os.getpid(), created=win_job.created(process))
        finally:
            win_job.close(process)
        handle = win_job.acquire_job(record)
        try:
            with mock.patch.object(win_job, 'attach_console', return_value=False) as attach:
                win_job.signal_root(handle, record)
                attach.assert_not_called()
        finally:
            win_job.close(handle)

    def test_failed_termination_keeps_instance_record(self):
        path, _ = self.start()
        with mock.patch.object(win_job, 'signal_root'), mock.patch.object(win_job, 'terminate_job', return_value=0):
            with self.assertRaises(OSError):
                win_job.stop(path, grace=0)
        self.assertEqual(path.read_text(encoding='utf-8'), self.records[path])
        self.assertTrue(win_job.state(path)['pids'])

    def test_unconfirmed_cleanup_is_not_success(self):
        path, _ = self.start()
        with mock.patch.object(win_job, 'signal_root'), mock.patch.object(win_job, 'terminate_job', return_value=1):
            with self.assertRaisesRegex(OSError, '仍未退出'):
                win_job.stop(path, grace=0, timeout=0)
        self.assertTrue(path.exists())

    def test_missing_job_is_not_reclaimed_from_a_recycled_pid(self):
        path, _ = self.start()
        record = json.loads(self.records[path])
        result = self.helper('stop', str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        record['pid'] = os.getpid()
        process = win_job.open_process(0x1000, False, os.getpid())
        try:
            record['created'] = win_job.created(process)
        finally:
            win_job.close(process)
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(win_job.state(path)['pids'], [])
        self.assertEqual(self.helper('stop', str(path)).returncode, 0)

    def test_legacy_and_corrupt_records_are_refused_and_retained(self):
        path = self.directory / 'legacy.pid'
        for content in (str(os.getpid()), '9' * 40, '{broken', '{}'):
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                result = self.helper('stop', str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_duplicate_start_does_not_replace_the_instance_record(self):
        path, directory = self.start()
        result = self.helper('spawn', str(path), str(directory / 'web.log'),
                             str(directory), '--', sys.executable, str(self.worker),
                             str(directory), 'leaf')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(encoding='utf-8'), self.records[path])

    def test_concurrent_first_starts_register_only_one_instance(self):
        directory = self.directory / 'concurrent'
        directory.mkdir()
        path = directory / 'instance.pid'
        arguments = ('spawn', str(path), str(directory / 'web.log'), str(directory),
                     '--', sys.executable, str(self.worker), str(directory), 'leaf')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda unused: self.helper(*arguments), range(2)))
        self.records[path] = path.read_text(encoding='utf-8')
        self.assertEqual(sorted(result.returncode for result in results), [0, 1])
        self.wait_for(directory / 'ready')
        record = json.loads(self.records[path])
        self.assertIn(record['pid'], win_job.state(path)['pids'])

    def test_supervisor_crash_does_not_leave_background_work(self):
        import _winapi

        path, _ = self.start()
        record = json.loads(self.records[path])
        process = _winapi.OpenProcess(0x00100001, False, record['supervisor'])
        try:
            _winapi.TerminateProcess(process, 1)
            self.assertEqual(_winapi.WaitForSingleObject(process, 5000), 0)
        finally:
            _winapi.CloseHandle(process)
        self.assert_gone(record['pid'])
        self.assertTrue(path.exists())

    def test_registration_failure_never_runs_the_suspended_worker(self):
        import _winapi
        from scripts import win_daemon

        path = self.directory / 'unregistered.pid'
        original = _winapi.CreateProcess
        processes = []

        def capture(*arguments):
            result = original(*arguments)
            processes.append(result[2])
            return result

        with mock.patch.dict(sys.modules, {'win_job': win_job}), \
                mock.patch.object(_winapi, 'CreateProcess', side_effect=capture), \
                mock.patch.object(win_job, 'write_record', side_effect=OSError('disk denied')):
            with self.assertRaisesRegex(OSError, 'disk denied'):
                win_daemon.host([str(path), str(self.directory / 'failure.log'),
                                 str(self.directory), '--', sys.executable,
                                 str(self.worker), str(self.directory), 'leaf'],
                                str(self.directory / 'unregistered.ready'))
        self.assertEqual(len(processes), 1)
        self.assert_gone(processes[0])
        self.assertFalse((self.directory / 'ready').exists())

    def test_query_failure_retains_the_instance_record(self):
        path, _ = self.start()
        with mock.patch.object(win_job, 'members', side_effect=OSError('query denied')):
            with self.assertRaisesRegex(OSError, 'query denied'):
                win_job.stop(path, grace=0)
        self.assertEqual(path.read_text(encoding='utf-8'), self.records[path])

    def test_ready_cleanup_error_does_not_report_a_live_start_as_failed(self):
        from scripts import win_daemon

        process = mock.Mock()
        original_unlink = Path.unlink

        def start_manager(command, **options):
            self.assertEqual(command[0], sys._base_executable)
            Path(command[5]).write_text('ready', encoding='ascii')
            return process

        def deny_ready_unlink(path, *arguments, **options):
            if path.suffix == '.ready':
                raise PermissionError('temporary file is being scanned')
            return original_unlink(path, *arguments, **options)

        output = io.StringIO()
        with mock.patch.object(win_daemon.subprocess, 'Popen', side_effect=start_manager), \
                mock.patch.object(Path, 'unlink', new=deny_ready_unlink), \
                mock.patch.object(sys, 'stderr', output):
            result = win_daemon.spawn([str(self.directory / 'instance.pid'),
                                       str(self.directory / 'web.log'),
                                       str(self.directory), '--', sys.executable, '-c', 'pass'])
        self.assertEqual(result, 0)
        process.terminate.assert_not_called()
        self.assertIn('临时文件清理失败', output.getvalue())


if __name__ == '__main__':
    unittest.main()
