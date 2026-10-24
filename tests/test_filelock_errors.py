"""Nonblocking contention is portable; other filesystem failures stay distinct."""

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kb import filelock


class LockErrorTests(unittest.TestCase):
    def test_contended_nonblocking_lock_raises_blocking_io_error_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'job.lock'
            with path.open('a+') as owner, path.open('a+') as contender:
                filelock.flock(owner, filelock.LOCK_EX | filelock.LOCK_NB)
                with self.assertRaises(BlockingIOError) as error:
                    filelock.flock(contender, filelock.LOCK_EX | filelock.LOCK_NB)
                self.assertIn(error.exception.errno, (errno.EAGAIN, errno.EACCES))
                filelock.flock(owner, filelock.LOCK_UN)
                filelock.flock(contender, filelock.LOCK_EX | filelock.LOCK_NB)

    @unittest.skipUnless(os.name == 'nt', 'Windows native error translation')
    def test_access_denied_is_not_mistaken_for_contention(self):
        with tempfile.TemporaryFile() as handle, \
             patch.object(filelock._k32, 'LockFileEx', return_value=0), \
             patch.object(filelock.ctypes, 'get_last_error', return_value=5):
            with self.assertRaises(OSError) as error:
                filelock.flock(handle, filelock.LOCK_EX | filelock.LOCK_NB)
        self.assertNotIsInstance(error.exception, BlockingIOError)
        self.assertEqual(5, error.exception.winerror)

    @unittest.skipUnless(os.name == 'nt', 'Windows native error translation')
    def test_unlock_failure_is_not_mistaken_for_contention(self):
        with tempfile.TemporaryFile() as handle, \
             patch.object(filelock._k32, 'UnlockFileEx', return_value=0), \
             patch.object(filelock.ctypes, 'get_last_error', return_value=33):
            with self.assertRaises(OSError) as error:
                filelock.flock(handle, filelock.LOCK_UN | filelock.LOCK_NB)
        self.assertNotIsInstance(error.exception, BlockingIOError)
        self.assertEqual(33, error.exception.winerror)


if __name__ == '__main__':
    unittest.main()
