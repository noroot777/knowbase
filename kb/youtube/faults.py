"""本机存储故障不能变成频道或视频的上游失败。"""

import errno
import sqlite3

from kb.maintenance import LockBusy
from kb.youtube.diagnosis import shown


STORAGE_ERRNOS = {errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EIO, errno.EACCES,
                 errno.EPERM, errno.EBUSY, errno.EMFILE, errno.ENFILE, errno.ENOMEM,
                 errno.ENOENT, errno.ENOTDIR}
SQLITE_STORAGE_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR,
                        sqlite3.SQLITE_FULL, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN,
                        sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_PERM}


def local_fault(error):
    """按异常类型和结构化错误码识别资源故障，不把网络超时或业务失败算成本机写盘。"""
    if isinstance(error, LockBusy):
        return True
    if isinstance(error, sqlite3.Error):
        code = getattr(error, 'sqlite_errorcode', 0)
        if isinstance(code, int) and (code & 255) in SQLITE_STORAGE_CODES:
            return True
        if isinstance(error, sqlite3.OperationalError):
            text = str(error).lower()
            if any(marker in text for marker in ('locked', 'busy', 'readonly', 'read-only',
                                                 'disk is full', 'disk i/o', 'unable to open database',
                                                 'out of memory')):
                return True
    return isinstance(error, OSError) and error.errno in STORAGE_ERRNOS


def record_local(summary, error, source_ids=(), phase='读写'):
    """仅留本轮告警和未完成身份，不写频道/视频失败账或冷却。"""
    detail = shown(error) or type(error).__name__
    previous = summary.get('local_error', '')
    if detail not in previous:
        summary['local_error'] = previous + ('；' if previous else '') + detail
        summary['message'] = summary.get('message', '') + ('；' if summary.get('message') else '') + \
            f'本机{phase}失败，成功结果未写入，下一轮重算：' + detail
    identifiers = summary.setdefault('local_ids', [])
    identifiers.extend(str(source_id) for source_id in source_ids if str(source_id) not in identifiers)
