from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


kernel = ctypes.WinDLL('kernel32', use_last_error=True)
HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
SIZE_T = ctypes.c_size_t


def _api(name, restype, *argtypes):
    function = getattr(kernel, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


close = _api('CloseHandle', wintypes.BOOL, HANDLE)
create_job = _api('CreateJobObjectW', HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
open_job = _api('OpenJobObjectW', HANDLE, DWORD, wintypes.BOOL, wintypes.LPCWSTR)
assign_job = _api('AssignProcessToJobObject', wintypes.BOOL, HANDLE, HANDLE)
terminate_job = _api('TerminateJobObject', wintypes.BOOL, HANDLE, wintypes.UINT)
query_job = _api('QueryInformationJobObject', wintypes.BOOL, HANDLE, ctypes.c_int,
                 ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD))
set_job = _api('SetInformationJobObject', wintypes.BOOL, HANDLE, ctypes.c_int,
               ctypes.c_void_p, DWORD)
open_process = _api('OpenProcess', HANDLE, DWORD, wintypes.BOOL, DWORD)
wait_process = _api('WaitForSingleObject', DWORD, HANDLE, DWORD)
process_times = _api('GetProcessTimes', wintypes.BOOL, HANDLE,
                     ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                     ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
in_job = _api('IsProcessInJob', wintypes.BOOL, HANDLE, HANDLE, ctypes.POINTER(wintypes.BOOL))
resume = _api('ResumeThread', DWORD, HANDLE)
free_console = _api('FreeConsole', wintypes.BOOL)
attach_console = _api('AttachConsole', wintypes.BOOL, DWORD)
ignore_ctrl = _api('SetConsoleCtrlHandler', wintypes.BOOL, ctypes.c_void_p, wintypes.BOOL)
send_ctrl = _api('GenerateConsoleCtrlEvent', wintypes.BOOL, DWORD, DWORD)


def check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def created(handle) -> str:
    stamps = [wintypes.FILETIME() for _ in range(4)]
    check(process_times(handle, *[ctypes.byref(stamp) for stamp in stamps]))
    return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)


def contain(handle):
    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', DWORD), ('min_working', SIZE_T), ('max_working', SIZE_T),
                    ('active_limit', DWORD), ('affinity', SIZE_T),
                    ('priority', DWORD), ('scheduling', DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io', ctypes.c_uint64 * 6),
                    ('process_memory', SIZE_T), ('job_memory', SIZE_T),
                    ('peak_process', SIZE_T), ('peak_job', SIZE_T)]

    limits = ExtendedLimits()
    limits.basic.flags = 0x00002000
    check(set_job(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))


def members(handle) -> list[int]:
    capacity = 32
    while capacity <= 1048576:
        class ProcessList(ctypes.Structure):
            _fields_ = [('assigned', DWORD), ('count', DWORD), ('ids', SIZE_T * capacity)]

        listing = ProcessList()
        if query_job(handle, 3, ctypes.byref(listing), ctypes.sizeof(listing), None):
            if listing.assigned <= capacity:
                return list(listing.ids[:listing.count])
        elif ctypes.get_last_error() != 234:
            raise ctypes.WinError(ctypes.get_last_error())
        capacity *= 2
    raise OSError('Job Object 的进程集合过大，无法确认停机')


class LegacyRecordError(ValueError):
    def __init__(self, path, process_id, token):
        self.process_id = process_id
        self.token = token
        super().__init__(f'{path}: 旧版 PID 记录 (PID {process_id})，不能凭裸 PID 认领；'
                         '请先关闭旧服务（原窗口按 Ctrl+C；无窗口时可重启 Windows），再运行 .\\kb.cmd restart；'
                         '确认旧 PID 已退出且端口空闲后，入口会备份旧记录，无需手动删除')


def process_alive(process_id) -> bool:
    process = open_process(0x00100000, False, process_id)
    if not process:
        if ctypes.get_last_error() == 87:
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = wait_process(process, 0)
        if result == 0:
            return False
        if result == 258:
            return True
        if result == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        raise OSError(f'无法确认旧 PID {process_id} 是否已退出: {result}')
    finally:
        close(process)


def load_record(path) -> dict | None:
    try:
        content = Path(path).read_bytes()
        record = json.loads(content.decode('utf-8-sig'))
        if type(record) is int and 0 < record <= 0xffffffff:
            raise LegacyRecordError(path, record, hashlib.sha256(content).hexdigest())
        if (not isinstance(record, dict) or record.get('version') != 1
                or type(record.get('pid')) is not int or record['pid'] <= 0
                or not isinstance(record.get('created'), str) or not record['created'].isdigit()
                or not isinstance(record.get('job'), str)
                or not record['job'].startswith('Local\\Knowbase-')
                or str(uuid.UUID(record['job'][15:])) != record['job'][15:]):
            raise ValueError('invalid instance record')
        return record
    except FileNotFoundError:
        return None
    except LegacyRecordError:
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f'{path}: 损坏的实例记录，无法确认实例身份；'
                         '请先确认并关闭旧服务，再备份移走这份记录后重新启动') from error


def acquire_job(record):
    if record is None:
        return None
    handle = open_job(0x0004 | 0x0008, False, record['job'])
    if not handle and ctypes.get_last_error() != 2:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def state(path) -> dict:
    try:
        record = load_record(path)
    except LegacyRecordError as error:
        return {'managed': False, 'pids': [], 'root_alive': False,
                'legacy_pid': error.process_id, 'legacy_alive': process_alive(error.process_id),
                'legacy_token': error.token}
    handle = acquire_job(record)
    try:
        process_ids = members(handle) if handle else []
        return {'managed': record is not None, 'pids': process_ids,
                'root_alive': bool(record and record['pid'] in process_ids)}
    finally:
        if handle:
            close(handle)


@contextmanager
def instance_lock(path):
    import msvcrt

    with open(str(path) + '.lock', 'a+b') as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        yield


def signal_root(handle, record):
    process = open_process(0x1000 | 0x00100000, False, record['pid'])
    if not process:
        if ctypes.get_last_error() == 87:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        belongs = wintypes.BOOL()
        check(in_job(process, handle, ctypes.byref(belongs)))
        if created(process) != record['created'] or not belongs.value:
            return
        free_console()
        if attach_console(record['pid']):
            handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, DWORD)
            handler = handler_type(lambda event: True)
            try:
                check(ignore_ctrl(ctypes.cast(handler, ctypes.c_void_p), True))
                check(send_ctrl(1, 0))
                time.sleep(0.05)
            finally:
                free_console()
    finally:
        close(process)


def require_free_port(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('迁移旧记录需要有效端口 (1-65535)')
    script = str(Path(__file__).with_name('win_proc.ps1')).replace("'", "''")
    command = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
               "$ErrorActionPreference = 'Stop'; "
               f". '{script}'; Get-PortOwners {port} | ConvertTo-Json -Compress")
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive',
                                 '-ExecutionPolicy', 'Bypass', '-Command', command],
                                capture_output=True, encoding='utf-8', errors='replace',
                                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise OSError(f'端口 {port} 查询超时，旧记录已保留') from error
    if result.returncode:
        raise OSError(f'端口 {port} 查询失败，旧记录已保留: {result.stderr.strip()}')
    try:
        query = json.loads(result.stdout)
    except ValueError as error:
        raise OSError(f'端口 {port} 查询结果无效，旧记录已保留') from error
    if (not isinstance(query, dict) or query.get('Known') is not True
            or not isinstance(query.get('Owners'), list)):
        reason = query.get('Why', '') if isinstance(query, dict) else ''
        raise OSError(f'无法确认端口 {port} 空闲，旧记录已保留: {reason}')
    if query['Owners']:
        raise OSError(f'端口 {port} 仍有监听 PID: {query["Owners"]}，旧记录已保留')


def migrate_legacy(path, expected, port) -> dict:
    if (not isinstance(expected, str) or len(expected) != 64
            or any(character not in '0123456789abcdef' for character in expected)):
        raise ValueError('迁移旧记录需要有效的预期内容标识')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('迁移旧记录需要有效端口 (1-65535)')
    if not Path(path).parent.exists():
        return {'changed': True}
    with instance_lock(path):
        try:
            load_record(path)
        except LegacyRecordError as error:
            if error.token != expected:
                return {'changed': True}
            if process_alive(error.process_id):
                raise
            require_free_port(port)
            backup = Path(str(path) + '.legacy-' + uuid.uuid4().hex + '.bak')
            Path(path).rename(backup)
            return {'changed': False, 'legacy_backup': str(backup)}
        return {'changed': True}


def _stop_locked(path, grace, timeout) -> dict:
    record = load_record(path)
    handle = acquire_job(record)
    try:
        if handle:
            if members(handle):
                signal_root(handle, record)
            deadline = time.monotonic() + grace
            while members(handle) and time.monotonic() < deadline:
                time.sleep(0.05)
            if members(handle):
                check(terminate_job(handle, 1))
            deadline = time.monotonic() + timeout
            while members(handle):
                if time.monotonic() >= deadline:
                    raise OSError('受管理进程仍未退出，实例记录已保留')
                time.sleep(0.05)
        if record is not None:
            Path(path).unlink()
        return {'managed': record is not None, 'pids': []}
    finally:
        if handle:
            close(handle)


def stop(path, grace=5.0, timeout=5.0) -> dict:
    if not Path(path).parent.exists():
        return {'managed': False, 'pids': []}
    with instance_lock(path):
        return _stop_locked(path, grace, timeout)


def write_record(path, record):
    temporary = Path(str(path) + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
