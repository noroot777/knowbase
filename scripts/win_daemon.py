"""Windows 后台启动与实例级停机：隐藏控制台 + 命名 Job Object。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def host(argv: list[str], ready: str) -> int:
    import _winapi
    import msvcrt
    from win_job import (assign_job, check, close, create_job, created, load_record,
                         members, acquire_job, resume, terminate_job, write_record, contain,
                         instance_lock)

    if len(argv) < 4:
        raise ValueError('spawn 要有 pidfile / logfile / workdir / 命令')
    pidfile, logfile, workdir, *rest = argv
    env = None
    while rest and rest[0] == '--env':
        if len(rest) < 2 or '=' not in rest[1]:
            raise ValueError('--env 后面要跟 NAME=VALUE')
        name, _, value = rest[1].partition('=')
        if env is None:
            env = {}
        env[name] = value
        rest = rest[2:]
    if rest and rest[0] == '--':
        rest = rest[1:]
    if not rest:
        raise ValueError('spawn 要有命令')
    Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    with instance_lock(pidfile):
        previous = acquire_job(load_record(pidfile))
        if previous:
            try:
                if members(previous):
                    raise OSError('已有受管理实例，拒绝重复启动')
            finally:
                close(previous)
        job_name = 'Local\\Knowbase-' + str(uuid.uuid4())
        job = check(create_job(None, job_name))
        process = thread = None
        try:
            contain(job)
            with open(logfile, 'ab', buffering=0) as log, open(os.devnull, 'rb') as null:
                output = msvcrt.get_osfhandle(log.fileno())
                input_handle = msvcrt.get_osfhandle(null.fileno())
                os.set_handle_inheritable(output, True)
                os.set_handle_inheritable(input_handle, True)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESHOWWINDOW | subprocess.STARTF_USESTDHANDLES
                startup.wShowWindow = subprocess.SW_HIDE
                startup.hStdInput = input_handle
                startup.hStdOutput = startup.hStdError = output
                startup.lpAttributeList = {'handle_list': [output, input_handle]}
                process, thread, process_id, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(rest), None, None, True,
                    subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
                    | 0x00000004, env, workdir, startup)
            check(assign_job(job, process))
            record = {'version': 1, 'pid': process_id, 'created': created(process),
                      'job': job_name, 'supervisor': os.getpid()}
            write_record(pidfile, record)
            if resume(thread) == 0xffffffff:
                raise OSError('ResumeThread 失败')
            write_record(ready, {'ready': True})
        except BaseException:
            if process is not None:
                _winapi.TerminateProcess(process, 1)
                terminate_job(job, 1)
                _winapi.WaitForSingleObject(process, 5000)
            raise
        finally:
            if thread is not None:
                close(thread)
            if process is not None:
                close(process)
            if sys.exc_info()[0] is not None:
                close(job)
    try:
        while members(job):
            time.sleep(0.05)
    finally:
        close(job)
    return 0


def spawn(argv: list[str]) -> int:
    if len(argv) < 4:
        raise ValueError('spawn 要有 pidfile / logfile / workdir / 命令')
    pidfile, logfile = argv[:2]
    Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    ready = str(Path(pidfile).with_name(Path(pidfile).name + '.' + uuid.uuid4().hex + '.ready'))
    with open(logfile, 'ab', buffering=0) as log:
        process = subprocess.Popen([sys._base_executable, '-X', 'utf8', str(Path(__file__).resolve()), 'host', ready, *argv],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 30
        while not Path(ready).exists():
            if process.poll() is not None:
                raise OSError(f'实例管理进程启动失败，日志:{logfile}')
            if time.monotonic() >= deadline:
                raise OSError(f'实例管理进程启动超时，日志:{logfile}')
            time.sleep(0.05)
        return 0
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise
    finally:
        try:
            Path(ready).unlink(missing_ok=True)
        except OSError as error:
            print(f'启动握手已完成，但临时文件清理失败: {ready}: {error}', file=sys.stderr)


def main() -> int:
    if os.name != 'nt':
        print('这个脚本只支持 Windows，POSIX 上走 kb.sh', file=sys.stderr)
        return 2
    from win_job import state, stop

    try:
        action, *rest = sys.argv[1:]
        if action == 'spawn':
            return spawn(rest)
        if action == 'host' and rest:
            return host(rest[1:], rest[0])
        if action in ('state', 'stop') and len(rest) == 1:
            result = state(rest[0]) if action == 'state' else stop(rest[0])
            print(json.dumps(result))
            return 0
        raise ValueError('用法: win_daemon.py spawn ... | state pidfile | stop pidfile')
    except (OSError, ValueError) as error:
        print(f'Windows 实例操作失败: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
