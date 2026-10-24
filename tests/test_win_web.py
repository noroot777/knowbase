"""Windows 服务就绪必须同时满足实例归属与 HTTP 健康检查，不能以 Job 非空代替。

生命周期探针同时覆盖 start/status，真实 Job 夹具保留根退出后的后代用于安全停机。
所有服务使用临时记录、日志和独立端口，不访问用户实例或正式资料。
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_win_proc import REPO, _Probe


_WEB_SETUP = r'''
. (Join-Path $script:Root 'scripts\win_web.ps1')
$apiFunction = $entryAst.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-Api'
}, $true)
Invoke-Expression $apiFunction[0].Extent.Text
$script:Curl = Join-Path $env:SystemRoot 'System32\curl.exe'
$script:CurlArgs = @('-s', '--connect-timeout', '1', '--max-time', '1', '--noproxy', '*')
$script:WebPid = Join-Path $env:TEMP 'instance.pid'
$script:LogDir = $env:TEMP
$script:Api = 'http://127.0.0.1:8787'
function Show-CodeStale {}
function Show-EgressLines {}
function Show-GraphStale {}
function Initialize-WebEnv { throw '不得重复准备环境' }
function Invoke-BootPython { throw '不得另起实例' }
'''

_FACTS = r'''
Set-Content -LiteralPath $script:WebPid -Value 'stub'
function Invoke-Instance {
    return [pscustomobject]@{ managed = $true; pids = @(111); root_alive = $true }
}
function Get-PortOwners { return @{ Known = $true; Owners = @(); Why = '' } }
function Invoke-Api { throw '未监听或不属于实例时不该请求健康检查' }
'''

_WORKER = '''
import signal, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
directory = Path(sys.argv[1])
mode = sys.argv[2]
signal.signal(signal.SIGBREAK, lambda event, frame: sys.exit(0))
if mode == 'descendants':
    subprocess.Popen([sys.executable, __file__, str(directory), 'pending'],
                     creationflags=subprocess.CREATE_NO_WINDOW)
    sys.exit(0)
(directory / 'ready').touch()
if mode == 'delayed':
    while not (directory / 'serve').exists():
        time.sleep(.02)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        def log_message(self, *arguments):
            pass
    HTTPServer(('127.0.0.1', int(sys.argv[3])), Handler).serve_forever()
while True:
    time.sleep(.05)
'''


class WebReadinessTest(_Probe):
    def run_web(self, body, **options):
        return self.ps_lines(_WEB_SETUP + body, **options)

    def test_pending_root_is_not_running_or_successfully_started(self):
        output = self.run_web(_FACTS + '''
Get-WebStatus
try { Start-Web -TimeoutSeconds 0; 'ACCEPTED' } catch { "REFUSED:$($_.Exception.Message)" }
"retained=$(Test-Path -LiteralPath $script:WebPid)"
''')
        self.assertTrue(any('未就绪' in line for line in output))
        self.assertFalse(any('运行中' in line or '[web] 已在' in line for line in output))
        self.assertNotIn('ACCEPTED', output)
        self.assertTrue(any(line.startswith('REFUSED:DIE:') for line in output))
        self.assertIn('retained=True', output)

    def test_unhealthy_owned_listener_is_not_ready(self):
        for code, body in [(0, ''), (503, '{"status":"ok"}'), (200, 'bad json'),
                           (200, '{}'), (200, '{"status":"starting"}')]:
            with self.subTest(code=code, body=body):
                output = self.run_web(_FACTS + r'''
function Get-PortOwners { return @{ Known = $true; Owners = @(111); Why = '' } }
function Invoke-Api { return @{ Code = [int]$env:KB_HEALTH_CODE; Body = $env:KB_HEALTH_BODY } }
Get-WebStatus
try { Start-Web -TimeoutSeconds 0; 'ACCEPTED' } catch { "REFUSED:$($_.Exception.Message)" }
"retained=$(Test-Path -LiteralPath $script:WebPid)"
''', extra_env={'KB_HEALTH_CODE': str(code), 'KB_HEALTH_BODY': body})
                self.assertFalse(any('运行中' in line or '[web] 已在' in line for line in output))
                self.assertNotIn('ACCEPTED', output)
                self.assertTrue(any(line.startswith('REFUSED:DIE:') for line in output))
                self.assertIn('retained=True', output)

    def test_unknown_and_foreign_ports_never_probe_or_start(self):
        for known, owners in [('False', '@()'), ('True', '@(222)')]:
            with self.subTest(known=known):
                output = self.run_web(_FACTS + f'''
function Get-PortOwners {{ return @{{ Known = ${known}; Owners = {owners}; Why = 'fixture' }} }}
Get-WebStatus
try {{ Start-Web -TimeoutSeconds 0; 'ACCEPTED' }} catch {{ "REFUSED:$($_.Exception.Message)" }}
"members=$(@(Get-Prop (Invoke-Instance 'state' $script:WebPid) 'pids' @()).Count)"
''')
                self.assertNotIn('ACCEPTED', output)
                self.assertTrue(any(line.startswith('REFUSED:DIE:') for line in output))
                self.assertIn('members=1', output)
                self.assertFalse(any('运行中' in line for line in output))

    def test_ready_instance_has_idempotent_start_and_running_status(self):
        output = self.run_web(_FACTS + '''
function Get-PortOwners { return @{ Known = $true; Owners = @(111); Why = '' } }
function Invoke-Api { param([string]$Path)
    if ($Path -ne '/api/health') { throw '错误健康检查路径' }
    return @{ Code = 200; Body = '{"status":"ok"}' }
}
Start-Web -TimeoutSeconds 0
Get-WebStatus
''')
        self.assertIn('[web] 已在 http://127.0.0.1:8787', output)
        self.assertIn('kb-web    运行中 (http://127.0.0.1:8787)', output)

    def test_lost_ownership_after_health_does_not_report_ready(self):
        output = self.run_web(_FACTS + '''
$script:healthDone = $false
function Get-PortOwners {
    $owner = if ($script:healthDone) { 222 } else { 111 }
    return @{ Known = $true; Owners = @($owner); Why = '' }
}
function Invoke-Api {
    $script:healthDone = $true
    return @{ Code = 200; Body = '{"status":"ok"}' }
}
$state = Get-WebState
"state=$($state.Name)"
''')
        self.assertIn('state=unknown', output)

    def test_live_healthy_descendant_does_not_require_the_root_to_survive(self):
        output = self.run_web(_FACTS + '''
function Invoke-Instance {
    return [pscustomobject]@{ managed = $true; pids = @(111); root_alive = $false }
}
function Get-PortOwners { return @{ Known = $true; Owners = @(111); Why = '' } }
function Invoke-Api { return @{ Code = 200; Body = '{"status":"ok"}' } }
Start-Web -TimeoutSeconds 0
Get-WebStatus
''')
        self.assertIn('[web] 已在 http://127.0.0.1:8787', output)
        self.assertIn('kb-web    运行中 (http://127.0.0.1:8787)', output)

    def test_fresh_start_waits_for_health_not_just_the_owned_listener(self):
        output = self.run_web(_FACTS + '''
Remove-Item -LiteralPath $script:WebPid
$script:waited = $false
function Initialize-WebEnv {}
function Build-WebEnv { return @() }
function Get-Uv { return 'fixture-uv' }
function Invoke-BootPython {
    Set-Content -LiteralPath $script:WebPid -Value 'stub'
    $global:LASTEXITCODE = 0
}
function Get-PortOwners {
    $owners = if (Test-Path -LiteralPath $script:WebPid) { @(111) } else { @() }
    return @{ Known = $true; Owners = @($owners); Why = '' }
}
function Invoke-Api {
    $code = if ($script:waited) { 200 } else { 503 }
    return @{ Code = $code; Body = '{"status":"ok"}' }
}
function Start-Sleep { $script:waited = $true }
function Write-Note {
    if (-not $script:waited) { throw '过早报告启动成功' }
}
Start-Web -TimeoutSeconds 5
"waited=$script:waited"
Get-WebStatus
''')
        self.assertIn('waited=True', output)
        self.assertIn('kb-web    运行中 (http://127.0.0.1:8787)', output)

    def test_fresh_start_and_repeated_timeout_share_readiness(self):
        output = self.run_web(_FACTS + '''
Remove-Item -LiteralPath $script:WebPid
$script:spawnCount = 0
function Initialize-WebEnv {}
function Build-WebEnv { return @() }
function Get-Uv { return 'fixture-uv' }
function Invoke-BootPython {
    $script:spawnCount++
    Set-Content -LiteralPath $script:WebPid -Value 'stub'
    $global:LASTEXITCODE = 0
}
foreach ($attempt in 1..2) {
    try { Start-Web -TimeoutSeconds 0; 'ACCEPTED' } catch { "REFUSED:$attempt" }
    Get-WebStatus
}
"spawn_count=$script:spawnCount"
"retained=$(Test-Path -LiteralPath $script:WebPid)"
''')
        self.assertIn('REFUSED:1', output)
        self.assertIn('REFUSED:2', output)
        self.assertIn('spawn_count=1', output)
        self.assertIn('retained=True', output)
        self.assertNotIn('ACCEPTED', output)
        self.assertFalse(any('运行中' in line for line in output))

    @contextmanager
    def managed_worker(self, mode):
        from scripts import win_job

        with tempfile.TemporaryDirectory(prefix='kb-readiness-') as temporary:
            directory = Path(temporary)
            worker = directory / 'worker.py'
            worker.write_text(_WORKER, encoding='utf-8')
            record = directory / 'instance.pid'
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            helper = [sys.executable, '-X', 'utf8', str(REPO / 'scripts' / 'win_daemon.py')]
            supervisor = None
            try:
                result = subprocess.run(helper + ['spawn', str(record), str(directory / 'web.log'),
                                        str(directory), '--', sys.executable, str(worker),
                                        str(directory), mode, str(port)],
                                        capture_output=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(record.read_text(encoding='utf-8'))
                supervisor = win_job.open_process(0x00100000, False, manifest['supervisor'])
                deadline = time.monotonic() + 10
                while not (directory / 'ready').exists() or (
                        mode == 'descendants' and win_job.state(record)['root_alive']):
                    if time.monotonic() >= deadline:
                        self.fail('managed worker did not reach fixture state')
                    time.sleep(.02)
                yield directory, record, port
            finally:
                result = subprocess.run(helper + ['stop', str(record)], capture_output=True,
                                        encoding='utf-8', timeout=30)
                try:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(record.exists())
                    self.assertEqual(win_job.state(record)['pids'], [])
                    if supervisor:
                        import _winapi
                        self.assertEqual(_winapi.WaitForSingleObject(supervisor, 5000), 0)
                finally:
                    if supervisor:
                        win_job.close(supervisor)

    def real_setup(self):
        return '''
$script:WebPid = $env:KB_TEST_RECORD
$script:Port = [int]$env:KB_TEST_PORT
$script:Api = "http://127.0.0.1:$script:Port"
'''

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration')
    def test_real_root_exit_with_live_descendants_is_residual_not_success(self):
        from scripts import win_job

        with self.managed_worker('descendants') as (_, record, port):
            before = record.read_text(encoding='utf-8')
            output = self.run_web(self.real_setup() + '''
Get-WebStatus
try { Start-Web -TimeoutSeconds 0; 'ACCEPTED' } catch { "REFUSED:$($_.Exception.Message)" }
"state=$((Get-WebState).Name)"
''', extra_env={'KB_TEST_RECORD': str(record), 'KB_TEST_PORT': str(port)})
            self.assertIn('state=residual', output)
            self.assertNotIn('ACCEPTED', output)
            self.assertTrue(any(line.startswith('REFUSED:DIE:') for line in output))
            self.assertFalse(any('运行中' in line or '[web] 已在' in line for line in output))
            self.assertEqual(record.read_text(encoding='utf-8'), before)
            self.assertTrue(win_job.state(record)['pids'])
            self.assertFalse(win_job.state(record)['root_alive'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration')
    def test_real_pending_timeout_then_readiness_reuses_the_same_job(self):
        with self.managed_worker('delayed') as (directory, record, port):
            before = json.loads(record.read_text(encoding='utf-8'))
            output = self.run_web(self.real_setup() + '''
Get-WebStatus
try { Start-Web -TimeoutSeconds 0; 'EARLY_SUCCESS' } catch { 'PENDING_REFUSED' }
New-Item -ItemType File -Path $env:KB_TEST_SERVE >$null
Start-Web -TimeoutSeconds 15
Get-WebStatus
Start-Web -TimeoutSeconds 0
''', extra_env={'KB_TEST_RECORD': str(record), 'KB_TEST_PORT': str(port),
               'KB_TEST_SERVE': str(directory / 'serve')})
            self.assertIn('PENDING_REFUSED', output)
            self.assertNotIn('EARLY_SUCCESS', output)
            self.assertTrue(any('未就绪' in line for line in output))
            self.assertIn(f'kb-web    运行中 (http://127.0.0.1:{port})', output)
            self.assertIn(f'[web] 已在 http://127.0.0.1:{port}', output)
            self.assertEqual(json.loads(record.read_text(encoding='utf-8')), before)
