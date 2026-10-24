"""旧版裸 PID 只作诊断；退出后可备份，存活或无法确认时不接管。"""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from tests.test_win_proc import _Probe
from tests.test_win_web import _WEB_SETUP

if os.name == 'nt':
    from scripts import win_job


@unittest.skipUnless(os.name == 'nt', 'Windows legacy instance records')
class LegacyRecordTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kb-旧记录 ')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'instance.pid'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]

    def migrate(self, expected=None):
        token = expected or hashlib.sha256(self.path.read_bytes()).hexdigest()
        return win_job.migrate_legacy(self.path, token, self.port)

    def exited_pid(self):
        with subprocess.Popen([sys.executable, '-c', 'pass']) as process:
            process.wait(timeout=10)
            return process.pid

    def test_live_pid_is_diagnostic_only_and_never_signalled(self):
        content = str(os.getpid())
        self.path.write_text(content, encoding='utf-8')
        status = win_job.state(self.path)
        self.assertFalse(status['managed'])
        self.assertEqual(status['pids'], [])
        self.assertFalse(status['root_alive'])
        self.assertEqual(status['legacy_pid'], os.getpid())
        self.assertTrue(status['legacy_alive'])
        with mock.patch.object(win_job, 'signal_root') as signal, \
                mock.patch.object(win_job, 'terminate_job') as terminate:
            with self.assertRaisesRegex(win_job.LegacyRecordError, str(os.getpid())):
                win_job.stop(self.path)
            with self.assertRaises(win_job.LegacyRecordError):
                self.migrate()
        signal.assert_not_called()
        terminate.assert_not_called()
        self.assertEqual(self.path.read_text(encoding='utf-8'), content)
        self.assertEqual(list(self.path.parent.glob('*.bak')), [])

    def test_exited_pid_status_is_read_only_and_stop_preserves_original_bytes(self):
        process_id = self.exited_pid()
        content = b'\xef\xbb\xbf' + f'  {process_id}\r\n'.encode('ascii')
        self.path.write_bytes(content)
        status = win_job.state(self.path)
        self.assertEqual(status['legacy_pid'], process_id)
        self.assertFalse(status['legacy_alive'])
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(list(self.path.parent.glob('*.bak')), [])
        with self.assertRaises(win_job.LegacyRecordError):
            win_job.stop(self.path)
        result = self.migrate(status['legacy_token'])
        self.assertFalse(self.path.exists())
        self.assertEqual(Path(result['legacy_backup']).read_bytes(), content)
        self.assertFalse(result['changed'])
        self.assertTrue(self.migrate(status['legacy_token'])['changed'])
        self.assertEqual(win_job.stop(self.path)['pids'], [])

    def test_pid_is_rechecked_under_the_stop_lock(self):
        self.path.write_text(str(os.getpid()), encoding='ascii')
        with mock.patch.object(win_job, 'process_alive', side_effect=[False, True]):
            self.assertFalse(win_job.state(self.path)['legacy_alive'])
            with self.assertRaises(win_job.LegacyRecordError):
                self.migrate()
        self.assertTrue(self.path.exists())

    def test_query_and_backup_failures_retain_the_original_record(self):
        content = str(self.exited_pid())
        self.path.write_text(content, encoding='ascii')
        with mock.patch.object(win_job, 'process_alive', side_effect=OSError('query denied')):
            for operation in (win_job.state, lambda path: self.migrate()):
                with self.assertRaisesRegex(OSError, 'query denied'):
                    operation(self.path)
                self.assertEqual(self.path.read_text(), content)
        with mock.patch.object(Path, 'rename', side_effect=OSError('backup denied')):
            with self.assertRaisesRegex(OSError, 'backup denied'):
                self.migrate()
        self.assertEqual(self.path.read_text(), content)

    def test_only_a_positive_dword_integer_is_a_legacy_record(self):
        for content in (b'0', b'-1', b'true', b'1.0', b'4294967296', b'9' * 40,
                        b'{broken', b'{}', b'\xff'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with mock.patch.object(win_job, 'process_alive') as query:
                    with self.assertRaisesRegex(ValueError, '损坏的实例记录'):
                        win_job.stop(self.path)
                query.assert_not_called()
                self.assertEqual(self.path.read_bytes(), content)

    def test_changed_records_never_enter_job_or_archive_operations(self):
        self.path.write_text(str(self.exited_pid()), encoding='ascii')
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        modern = json.dumps({'version': 1, 'pid': os.getpid(), 'created': '1',
                             'job': 'Local\\Knowbase-' + str(uuid.uuid4())}).encode('utf-8')
        for content in (b'12345\r\n', modern, None):
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(content)
                with mock.patch.object(win_job, 'acquire_job') as job, \
                        mock.patch.object(win_job, 'process_alive', return_value=False) as query, \
                        mock.patch.object(win_job, 'require_free_port') as port, \
                        mock.patch.object(win_job, 'signal_root') as signal, \
                        mock.patch.object(win_job, 'terminate_job') as terminate:
                    self.assertTrue(self.migrate(expected)['changed'])
                for operation in (job, query, port, signal, terminate):
                    operation.assert_not_called()
                self.assertEqual(list(self.path.parent.glob('*.bak')), [])
                if content is not None:
                    self.assertEqual(self.path.read_bytes(), content)

    def test_migration_rechecks_port_and_retains_record_on_any_query_failure(self):
        self.path.write_text(str(self.exited_pid()), encoding='ascii')
        content = self.path.read_bytes()
        for reason in ('port busy', 'port unknown'):
            with self.subTest(reason=reason), \
                    mock.patch.object(win_job, 'require_free_port', side_effect=OSError(reason)) as port:
                with self.assertRaisesRegex(OSError, reason):
                    self.migrate()
                port.assert_called_once_with(self.port)
                self.assertEqual(self.path.read_bytes(), content)
                self.assertEqual(list(self.path.parent.glob('*.bak')), [])

    def test_commit_port_query_only_accepts_known_empty_list_and_hides_its_window(self):
        for output in ('broken', '{}', '[]', '{"Known":true}',
                       '{"Known":false,"Owners":[],"Why":"denied"}',
                       '{"Known":true,"Owners":[123]}'):
            with self.subTest(output=output), mock.patch.object(win_job.subprocess, 'run',
                    return_value=mock.Mock(returncode=0, stdout=output, stderr='')):
                with self.assertRaises(OSError):
                    win_job.require_free_port(self.port)
        with mock.patch.object(win_job.subprocess, 'run',
                return_value=mock.Mock(returncode=0, stdout='{"Known":true,"Owners":[]}', stderr='')) as run:
            win_job.require_free_port(self.port)
        self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        for result in (mock.Mock(returncode=1, stdout='', stderr='denied'),
                       subprocess.TimeoutExpired('powershell', 20)):
            with self.subTest(result=result), mock.patch.object(win_job.subprocess, 'run') as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                with self.assertRaises(OSError):
                    win_job.require_free_port(self.port)

    def test_process_probe_distinguishes_absent_from_denied_and_wait_failure(self):
        with mock.patch.object(win_job, 'open_process', return_value=None), \
                mock.patch.object(win_job.ctypes, 'get_last_error', return_value=87):
            self.assertFalse(win_job.process_alive(123))
        with mock.patch.object(win_job, 'open_process', return_value=None), \
                mock.patch.object(win_job.ctypes, 'get_last_error', return_value=5):
            with self.assertRaises(OSError):
                win_job.process_alive(123)
        with mock.patch.object(win_job, 'open_process', return_value=123), \
                mock.patch.object(win_job, 'wait_process', return_value=0xffffffff), \
                mock.patch.object(win_job.ctypes, 'get_last_error', return_value=5), \
                mock.patch.object(win_job, 'close') as close:
            with self.assertRaises(OSError):
                win_job.process_alive(123)
        close.assert_called_once_with(123)

    def test_bom_prefixed_modern_record_still_uses_job_identity(self):
        record = {'version': 1, 'pid': os.getpid(), 'created': '1',
                  'job': 'Local\\Knowbase-' + str(uuid.uuid4())}
        self.path.write_text(json.dumps(record), encoding='utf-8-sig')
        with mock.patch.object(win_job, 'process_alive') as query:
            self.assertEqual(win_job.state(self.path)['pids'], [])
            self.assertTrue(win_job.stop(self.path)['managed'])
        query.assert_not_called()
        self.assertFalse(self.path.exists())


class _LegacyProbe(_Probe):
    def run_legacy(self, body, process_id, **options):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        env = {'KB_LEGACY_PID': str(process_id), 'KB_TEST_PYTHON': sys.executable,
               'KB_LEGACY_PORT': str(port)}
        env.update(options.pop('extra_env', {}))
        return self.ps_lines(_WEB_SETUP + r'''
$script:BootPyCache = @{ Exe = $env:KB_TEST_PYTHON; Pre = @() }
$script:Force = $true
$script:Port = [int]$env:KB_LEGACY_PORT
Set-Content -LiteralPath $script:WebPid -Value $env:KB_LEGACY_PID -Encoding UTF8
function Get-PortOwners { return @{ Known = $true; Owners = @(); Why = '' } }
''' + body, extra_env=env, **options)

    def exited_pid(self):
        with subprocess.Popen([sys.executable, '-c', 'pass']) as process:
            process.wait(timeout=10)
            return process.pid


class LegacyEntryTest(_LegacyProbe):
    @unittest.skipUnless(os.name == 'nt', 'Windows legacy instance records')
    def test_stop_error_exits_nonzero_without_a_powershell_stack(self):
        result = self.ps_run(_WEB_SETUP + r'''
$script:BootPyCache = @{ Exe = $env:KB_TEST_PYTHON; Pre = @() }
$script:Force = $true
$script:Port = 0
Set-Content -LiteralPath $script:WebPid -Value "$PID" -Encoding ascii
function Die { param([string]$Text) [Console]::Error.WriteLine("错误:$Text"); exit 1 }
Stop-Web
''', extra_env={'KB_TEST_PYTHON': sys.executable})
        self.assertEqual(result.returncode, 1)
        self.assertIn('错误:', result.stderr)
        self.assertIn('旧版 PID 记录', result.stderr)
        self.assertIn('.\\kb.cmd restart', result.stderr)
        self.assertNotIn('RuntimeException', result.stderr)
        self.assertNotIn('CategoryInfo', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows legacy instance records')
    def test_live_legacy_start_and_forced_stop_are_refused_with_migration_steps(self):
        output = self.run_legacy(r'''
function Get-PortOwners { return @{ Known = $true; Owners = @(456); Why = '' } }
Get-WebStatus
try { Start-Web -TimeoutSeconds 0; 'STARTED' } catch { "REFUSED:$($_.Exception.Message)" }
try { Stop-Web; 'STOPPED' } catch { "REFUSED:$($_.Exception.Message)" }
"retained=$(Test-Path -LiteralPath $script:WebPid)"
"backups=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter '*.bak').Count)"
''', os.getpid())
        refused = [line for line in output if line.startswith('REFUSED:')]
        self.assertEqual(len(refused), 2)
        for line in refused:
            self.assertIn(f'PID {os.getpid()}', line)
            self.assertIn('监听 PID: 456', line)
            self.assertIn('.\\kb.cmd restart', line)
            self.assertIn('--force', line)
        self.assertIn('retained=True', output)
        self.assertIn('backups=0', output)
        self.assertNotIn('STARTED', output)
        self.assertNotIn('STOPPED', output)

    @unittest.skipUnless(os.name == 'nt', 'Windows legacy instance records')
    def test_busy_or_unknown_port_never_archives_an_exited_pid_record(self):
        for query in ("@{ Known = $true; Owners = @(456); Why = '' }",
                      "@{ Known = $false; Owners = @(); Why = 'query denied' }"):
            with self.subTest(query=query):
                output = self.run_legacy('''
function Get-PortOwners { return ''' + query + ''' }
try { Stop-Web; 'STOPPED' } catch { "REFUSED:$($_.Exception.Message)" }
"retained=$(Test-Path -LiteralPath $script:WebPid)"
"backups=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter '*.bak').Count)"
''', self.exited_pid())
                refused = [line for line in output if line.startswith('REFUSED:')]
                self.assertEqual(len(refused), 1)
                self.assertIn('query denied' if '$false' in query else '监听 PID: 456', refused[0])
                self.assertIn('retained=True', output)
                self.assertIn('backups=0', output)
                self.assertNotIn('STOPPED', output)

    @unittest.skipUnless(os.name == 'nt', 'Windows legacy instance records')
    def test_status_is_read_only_then_stop_archives_the_exited_pid(self):
        output = self.run_legacy(r'''
Get-WebStatus
"before=$(Test-Path -LiteralPath $script:WebPid)"
Stop-Web
"after=$(Test-Path -LiteralPath $script:WebPid)"
$backups = @(Get-ChildItem -LiteralPath $env:TEMP -Filter '*.bak')
"backups=$($backups.Count)"
"preserved=$(([IO.File]::ReadAllText($backups[0].FullName)).Trim() -eq $env:KB_LEGACY_PID)"
''', self.exited_pid())
        self.assertIn('kb-web    未运行', output)
        self.assertIn('before=True', output)
        self.assertIn('after=False', output)
        self.assertIn('backups=1', output)
        self.assertIn('preserved=True', output)

    @unittest.skipUnless(os.name == 'nt', 'Windows legacy instance records')
    def test_real_legacy_start_then_managed_restart_and_stop(self):
        from tests.test_win_web import REPO

        with tempfile.TemporaryDirectory(prefix='kb-legacy-web-') as temporary:
            directory = Path(temporary)
            worker = directory / 'web.py'
            worker.write_text('''
from http.server import BaseHTTPRequestHandler, HTTPServer
import signal, sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
signal.signal(signal.SIGBREAK, lambda event, frame: sys.exit(0))
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
''', encoding='utf-8')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            record = directory / 'instance.pid'
            try:
                output = self.run_legacy(r'''
$script:WebPid = $env:KB_TEST_RECORD
$script:Port = [int]$env:KB_TEST_PORT
$script:Api = "http://127.0.0.1:$script:Port"
Set-Content -LiteralPath $script:WebPid -Value $env:KB_LEGACY_PID -Encoding UTF8
$original = [IO.File]::ReadAllBytes($script:WebPid)
function Get-PortOwners { param([int]$P)
    $connections = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $P } | Select-Object -ExpandProperty OwningProcess -Unique)
    return @{ Known = $true; Owners = $connections; Why = '' }
}
function Initialize-WebEnv {}
function Build-WebEnv { return @() }
function Get-Uv { return 'fixture-uv' }
function Invoke-BootPython {
    $result = Invoke-Native $env:KB_TEST_PYTHON @('-X', 'utf8', $script:WinDaemon, 'spawn',
        $script:WebPid, (Join-Path $env:KB_TEST_DIRECTORY 'web.log'), $env:KB_TEST_DIRECTORY,
        '--', $env:KB_TEST_PYTHON, $env:KB_TEST_WORKER, $env:KB_TEST_PORT)
    if ($result.Code -ne 0) {
        throw ($result.Text + [IO.File]::ReadAllText((Join-Path $env:KB_TEST_DIRECTORY 'web.log')))
    }
    $global:LASTEXITCODE = $result.Code
}
Start-Web -TimeoutSeconds 15
$first = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
"first=$((Get-WebState).Name) version=$($first.version)"
$backups = @(Get-ChildItem -LiteralPath $env:KB_TEST_DIRECTORY -Filter '*.bak')
"backups=$($backups.Count)"
"preserved=$([Convert]::ToBase64String([IO.File]::ReadAllBytes($backups[0].FullName)) -eq [Convert]::ToBase64String($original))"
Stop-Web
Start-Web -TimeoutSeconds 15
$second = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
"restarted=$($first.job -ne $second.job) state=$((Get-WebState).Name)"
Stop-Web
"stopped=$(-not (Test-Path -LiteralPath $script:WebPid))"
"port_free=$(@((Get-PortOwners $script:Port).Owners).Count -eq 0)"
''', self.exited_pid(), extra_env={'KB_TEST_RECORD': str(record),
                        'KB_TEST_DIRECTORY': str(directory), 'KB_TEST_WORKER': str(worker),
                        'KB_TEST_PORT': str(port)})
                self.assertIn('first=ready version=1', output)
                self.assertIn('backups=1', output)
                self.assertIn('preserved=True', output)
                self.assertIn('restarted=True state=ready', output)
                self.assertIn('stopped=True', output)
                self.assertIn('port_free=True', output)
            finally:
                result = subprocess.run([sys.executable, '-X', 'utf8',
                    str(REPO / 'scripts' / 'win_daemon.py'), 'stop', str(record)],
                    capture_output=True, encoding='utf-8', timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
