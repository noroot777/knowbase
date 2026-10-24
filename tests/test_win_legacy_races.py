"""旧记录迁移的确定性交错：不能把迁移授权升级为新版 Job 的停机授权。"""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from tests.test_win_entry import REPO
from tests.test_win_legacy import _LegacyProbe
from tests.test_win_web import _WEB_SETUP


_WORKER = '''
import os, signal, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
directory = Path(sys.argv[2])
def interrupted(event, frame):
    (directory / ('interrupted-' + str(os.getpid()))).touch()
    raise SystemExit(0)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        if 'pipeline' in self.path:
            body = b'{"running":true}' if (directory / 'running').exists() else b'{"running":false}'
        else:
            body = b'{"status":"ok"}'
        self.wfile.write(body)
    def log_message(self, *arguments):
        pass
signal.signal(signal.SIGBREAK, interrupted)
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
'''

_REAL_PORTS = r'''
function Get-PortOwners { param([int]$P)
    $owners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $P } | Select-Object -ExpandProperty OwningProcess -Unique)
    return @{ Known = $true; Owners = $owners; Why = '' }
}
'''

_SERVICE_SETUP = _REAL_PORTS + r'''
$script:Force = $false
$script:WebPid = $env:KB_TEST_RECORD
$script:Port = [int]$env:KB_TEST_PORT
$script:Api = "http://127.0.0.1:$script:Port"
Set-Content -LiteralPath $script:WebPid -Value $env:KB_LEGACY_PID -Encoding UTF8
$pipelineFunction = $entryAst.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-PipelineState'
}, $true)
Invoke-Expression $pipelineFunction[0].Extent.Text
function Initialize-WebEnv {}
function Build-WebEnv { return @() }
function Get-Uv { return 'fixture-uv' }
function Invoke-BootPython {
    $result = Invoke-Native $env:KB_TEST_PYTHON @('-X', 'utf8', $script:WinDaemon, 'spawn',
        $script:WebPid, (Join-Path $env:KB_TEST_DIRECTORY 'web.log'), $env:KB_TEST_DIRECTORY,
        '--', $env:KB_TEST_PYTHON, $env:KB_TEST_WORKER, $env:KB_TEST_PORT, $env:KB_TEST_DIRECTORY)
    if ($result.Code -ne 0) { throw $result.Text }
    $global:LASTEXITCODE = $result.Code
}
'''


@unittest.skipUnless(os.name == 'nt', 'Windows legacy migration interleavings')
class LegacyMigrationRaceTest(_LegacyProbe):
    @contextmanager
    def service(self, running):
        from scripts import win_job

        with tempfile.TemporaryDirectory(prefix='kb-legacy-race-') as temporary:
            directory = Path(temporary)
            worker = directory / 'worker.py'
            worker.write_text(_WORKER, encoding='utf-8')
            if running:
                (directory / 'running').touch()
            record = directory / 'instance.pid'
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            try:
                yield {'KB_TEST_RECORD': str(record), 'KB_TEST_PORT': str(port),
                       'KB_TEST_DIRECTORY': str(directory), 'KB_TEST_WORKER': str(worker)}
            finally:
                status = win_job.state(record)
                if 'legacy_token' in status:
                    win_job.migrate_legacy(record, status['legacy_token'], port)
                else:
                    result = subprocess.run([sys.executable, '-X', 'utf8',
                        str(REPO / 'scripts' / 'win_daemon.py'), 'stop', str(record)],
                        capture_output=True, encoding='utf-8', timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', port))

    def run_service(self, body, env):
        return self.run_legacy(_SERVICE_SETUP + 'try {\n' + body + r'''
} finally {
    if (Test-Path -LiteralPath $script:WebPid) {
        $instance = Invoke-Instance 'state' $script:WebPid
        $token = Get-Prop $instance 'legacy_token' ''
        if ($token) {
            $null = Invoke-Instance 'migrate' $script:WebPid @($token, [string]$script:Port)
        } else {
            $record = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
            $manager = Get-Process -Id $record.supervisor -ErrorAction SilentlyContinue
            if ($null -ne $manager) { $null = $manager.Handle }
            $null = Invoke-Instance 'stop' $script:WebPid
            if ($null -ne $manager -and -not $manager.WaitForExit(5000)) {
                throw 'fixture supervisor did not exit'
            }
        }
    }
}
''', self.exited_pid(), extra_env=env)

    def concurrent_start(self, running):
        with self.service(running) as env:
            output = self.run_service(r'''
$script:OriginalGetWebState = (Get-Item Function:Get-WebState).ScriptBlock
$script:Inserted = $false
$script:PeerRecord = $null
$script:PeerPids = @()
function Get-WebState {
    $observed = & $script:OriginalGetWebState
    if (-not $script:Inserted -and $observed.LegacyPid -gt 0) {
        $script:Inserted = $true
        Start-Web -TimeoutSeconds 15
        $script:PeerRecord = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
        $script:PeerPids = @((Invoke-Instance 'state' $script:WebPid).pids)
        [Console]::WriteLine("peer_state=$((& $script:OriginalGetWebState).Name) pipeline=$(Get-PipelineState)")
        if ((Get-PipelineState) -eq 'running') {
            try { Stop-Web; 'UNEXPECTED_STOP' }
            catch { [Console]::WriteLine("normal_stop_refused=$($_.Exception.Message.Contains('同步正在跑'))") }
        }
    }
    return $observed
}
Start-Web -TimeoutSeconds 15
$current = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
"same_job=$($script:PeerRecord.job -eq $current.job)"
"members_survive=$(@($script:PeerPids | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }).Count -eq $script:PeerPids.Count)"
"members_nonempty=$($script:PeerPids.Count -gt 0)"
"state=$((Get-WebState).Name) force=$script:Force"
"interrupted=$(@(Get-ChildItem -LiteralPath $env:KB_TEST_DIRECTORY -Filter 'interrupted-*').Count)"
"backups=$(@(Get-ChildItem -LiteralPath $env:KB_TEST_DIRECTORY -Filter '*.bak').Count)"
''', env)
            self.assertIn('peer_state=ready pipeline=' + ('running' if running else 'idle'), output)
            self.assertIn('same_job=True', output)
            self.assertIn('members_survive=True', output)
            self.assertIn('members_nonempty=True', output)
            self.assertIn('state=ready force=False', output)
            self.assertIn('interrupted=0', output)
            self.assertIn('backups=1', output)
            self.assertNotIn('UNEXPECTED_STOP', output)
            if running:
                self.assertIn('normal_stop_refused=True', output)

    def test_concurrent_starts_preserve_ready_peer(self):
        self.concurrent_start(False)

    def test_concurrent_starts_preserve_running_peer(self):
        self.concurrent_start(True)

    def test_stale_legacy_stop_never_stops_the_replacement_job(self):
        with self.service(True) as env:
            output = self.run_service(r'''
$script:OriginalServiceState = (Get-Item Function:Get-ServiceState).ScriptBlock
$script:Inserted = $false
$script:PeerRecord = $null
function Get-ServiceState { param([string]$PidFile, [int]$Port)
    $observed = & $script:OriginalServiceState $PidFile $Port
    if (-not $script:Inserted -and $observed.LegacyPid -gt 0) {
        $script:Inserted = $true
        Start-Web -TimeoutSeconds 15
        $script:PeerRecord = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
    }
    return $observed
}
try { Stop-Web; 'UNEXPECTED_STOP' } catch { "REFUSED:$($_.Exception.Message)" }
$current = [IO.File]::ReadAllText($script:WebPid) | ConvertFrom-Json
"same_job=$($script:PeerRecord.job -eq $current.job)"
"state=$((Get-WebState).Name) pipeline=$(Get-PipelineState)"
"interrupted=$(@(Get-ChildItem -LiteralPath $env:KB_TEST_DIRECTORY -Filter 'interrupted-*').Count)"
''', env)
            self.assertTrue(any('REFUSED:DIE:实例记录已改变' in line for line in output), output)
            self.assertIn('same_job=True', output)
            self.assertIn('state=ready pipeline=running', output)
            self.assertIn('interrupted=0', output)
            self.assertNotIn('UNEXPECTED_STOP', output)

    def test_port_becomes_busy_after_snapshot_blocks_both_migration_entries(self):
        for action in ('Start-Web -TimeoutSeconds 0', 'Stop-Web'):
            with self.subTest(action=action):
                output = self.run_legacy(r'''
$script:Listener = $null
function Get-PortOwners { param([int]$P)
    $owners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $P } | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 0) { throw 'fixture port unexpectedly busy' }
    $script:Listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $P)
    $script:Listener.Start()
    return @{ Known = $true; Owners = $owners; Why = '' }
}
try {
    try { ''' + action + r'''; 'ACCEPTED' } catch { "REFUSED:$($_.Exception.Message)" }
    "retained=$(Test-Path -LiteralPath $script:WebPid)"
    "backups=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter '*.bak').Count)"
    "listeners=$(@(Get-NetTCPConnection -LocalPort $script:Port -State Listen -ErrorAction Stop).Count)"
} finally {
    if ($null -ne $script:Listener) { $script:Listener.Stop() }
}
''', self.exited_pid())
                self.assertTrue(any('仍有监听 PID' in line for line in output), output)
                self.assertIn('retained=True', output)
                self.assertIn('backups=0', output)
                self.assertIn('listeners=1', output)
                self.assertNotIn('ACCEPTED', output)

    def test_port_conflict_after_archive_prevents_new_spawn(self):
        output = self.run_legacy(_REAL_PORTS + r'''
$script:OriginalMigration = (Get-Item Function:Invoke-LegacyMigration).ScriptBlock
$script:Listener = $null
function Invoke-LegacyMigration { param([string]$PidFile, [int]$Port, [string]$Expected)
    $changed = & $script:OriginalMigration $PidFile $Port $Expected
    $script:Listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
    $script:Listener.Start()
    return $changed
}
try {
    try { Start-Web -TimeoutSeconds 0; 'ACCEPTED' } catch { "REFUSED:$($_.Exception.Message)" }
    "backups=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter '*.bak').Count)"
} finally {
    if ($null -ne $script:Listener) { $script:Listener.Stop() }
}
''', self.exited_pid())
        self.assertTrue(any('未登记的进程占着' in line for line in output), output)
        self.assertIn('backups=1', output)
        self.assertNotIn('ACCEPTED', output)

    def test_locked_record_failure_has_a_clean_error_in_start_and_stop(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        for action in ('Start-Web -TimeoutSeconds 0', 'Stop-Web'):
            with self.subTest(action=action):
                result = self.ps_run(_WEB_SETUP + r'''
$script:BootPyCache = @{ Exe = $env:KB_TEST_PYTHON; Pre = @() }
$script:Force = $true
$script:Port = [int]$env:KB_TEST_PORT
Set-Content -LiteralPath $script:WebPid -Value $env:KB_LEGACY_PID -Encoding UTF8
$stream = [IO.File]::Open($script:WebPid, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
function Die { param([string]$Text)
    "retained=$(Test-Path -LiteralPath $script:WebPid)"
    "backups=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter '*.bak').Count)"
    [Console]::Error.WriteLine("错误:$Text")
    exit 1
}
''' + action, extra_env={'KB_TEST_PYTHON': sys.executable,
                    'KB_LEGACY_PID': str(self.exited_pid()), 'KB_TEST_PORT': str(port)})
                self.assertEqual(result.returncode, 1)
                self.assertIn('retained=True', result.stdout)
                self.assertIn('backups=0', result.stdout)
                self.assertIn('错误:Windows 实例操作失败:', result.stderr)
                self.assertIn('instance.pid', result.stderr)
                self.assertNotIn('CategoryInfo', result.stderr)
                self.assertNotIn('RuntimeException', result.stderr)


if __name__ == '__main__':
    unittest.main()
