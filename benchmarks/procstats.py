

from __future__ import annotations

import ctypes
import os
import sys
import time


def peak_memory_mb() -> float | None:
    """Peak resident memory of this process (Windows: peak working set)."""
    if sys.platform == "win32":
        class _PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = ctypes.c_void_p  # 64-bit pseudo-handle
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return round(pmc.PeakWorkingSetSize / 2**20, 1)
        return None
    try:
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(kb / 1024, 1)
    except Exception:
        return None


def cpu_seconds() -> float:
    """CPU time of this process and its finished children (pool workers)."""
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def wall() -> float:
    return time.perf_counter()
