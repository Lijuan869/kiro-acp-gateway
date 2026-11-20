"""Process and event-loop health for ``/metrics`` and the dashboard.

Process figures are read when they are requested (a scrape or a dashboard refresh), so
they cost nothing per request. The loop-lag monitor wakes once per interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

START_TIME = time.time()
_STATM = "/proc/self/statm"
_HAVE_STATM = os.path.exists(_STATM)
_FD_DIR = next((d for d in ("/proc/self/fd", "/dev/fd") if os.path.isdir(d)), None)
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _rss_bytes() -> int | None:
    """Current resident set size (Linux; ``None`` where /proc is unavailable)."""
    if not _HAVE_STATM:
        return None
    try:
        with open(_STATM, "rb") as statm:
            return int(statm.read().split()[1]) * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return None


def _max_rss_bytes() -> int | None:
    """Peak resident set size; ru_maxrss is bytes on macOS and KiB on Linux."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _open_fds() -> int | None:
    if _FD_DIR is None:
        return None
    try:
        return len(os.listdir(_FD_DIR))
    except OSError:
        return None


def _max_fds() -> int | None:
    if resource is None:
        return None
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return None if soft == resource.RLIM_INFINITY else int(soft)


def process_stats() -> dict[str, float | int | None]:
    """The gateway process's own resources (not its kiro-cli children)."""
    return {
        "pid": os.getpid(),
        "start_time": START_TIME,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "cpu_seconds": round(time.process_time(), 3),
        "rss_bytes": _rss_bytes(),
        "max_rss_bytes": _max_rss_bytes(),
        "open_fds": _open_fds(),
        "max_fds": _max_fds(),
        "threads": threading.active_count(),
    }


class LoopLagMonitor:
    """Measure how late the event loop runs a timer.

    Every ``interval`` seconds the monitor sleeps and records how much later than asked it
    woke up. A healthy loop answers within a millisecond or two; a larger value means
    something ran on the loop thread without yielding (blocking I/O, heavy CPU work) and
    every in-flight request waited that long.
    """

    def __init__(self, observe: Callable[[float], None], interval: float = 1.0) -> None:
        self.observe = observe
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="kiro-loop-lag")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(self.interval)
            self.observe(max(0.0, loop.time() - before - self.interval))
