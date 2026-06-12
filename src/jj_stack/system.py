"""Host system helpers used across jj-review."""

from __future__ import annotations

import os
import sys


def pid_is_alive(pid: int) -> bool:
    """Return whether a process with the given PID appears to exist."""

    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
