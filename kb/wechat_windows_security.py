"""Protect local access material with an explicit Windows DACL."""

import ctypes
from functools import lru_cache
from pathlib import Path
import re
import subprocess


@lru_cache(maxsize=1)
def current_sid():
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    match = re.search(r'S-1-5-[0-9-]+', result.stdout)
    if result.returncode or not match:
        raise OSError('Cannot identify the current Windows account')
    return match.group(0)


def restrict(path: Path):
    from ctypes import wintypes

    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    apply = security.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    inheritance = 'OICI' if path.is_dir() else ''
    descriptor = 'D:P' + ''.join(f'(A;{inheritance};FA;;;{sid})'
                                for sid in (current_sid(), 'SY', 'BA'))
    pointer = wintypes.LPVOID()
    if not convert(descriptor, 1, ctypes.byref(pointer), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000004, pointer):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(pointer)
