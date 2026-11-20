"""Gateway logging: request-id correlation, text or JSON lines, and a non-blocking handler.

``configure_logging`` installs one root handler that only queues records; a writer thread
formats them and writes each batch to stderr with one call, every ``FLUSH_INTERVAL``
seconds or at once for warnings and errors. The event loop therefore never waits on a slow
log consumer (a full pipe to the container runtime, journald back-pressure), and there is
no cross-thread wake-up per record. When ``QUEUE_SIZE`` records are waiting, new ones are
dropped and counted (``kiro_gateway_log_records_dropped_total``) instead of blocking or
growing memory.

Every record carries ``request_id``: the id of the HTTP request whose task emitted it
(``-`` outside a request). The id comes from the client's ``X-Request-ID`` header when it
is safe to log and echo, otherwise the gateway generates one.
"""

from __future__ import annotations

import contextvars
import copy
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kiro_gateway_request_id", default=None
)

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"
QUEUE_SIZE = 10_000
FLUSH_INTERVAL = 0.05

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:/=+@-]{1,128}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "request_id",
    "taskName",
}
_EXCEPTION_FORMATTER = logging.Formatter()
_dropped = 0
_dropped_lock = threading.Lock()


def _count_dropped(n: int = 1) -> None:
    global _dropped
    with _dropped_lock:
        _dropped += n


def new_request_id() -> str:
    return f"req_{os.urandom(12).hex()}"


def accept_request_id(value: str | None) -> str | None:
    """A client-supplied request id when it is safe to log and echo back, else ``None``."""
    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return None


def current_request_id() -> str | None:
    return REQUEST_ID.get()


def clean(value: str, limit: int = 200) -> str:
    """Escape control characters (no forged log lines) and cap the length of a client value."""
    if len(value) > limit:
        value = value[:limit] + "..."
    if value.isprintable():  # the usual case, without a regex pass
        return value
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", value)


def dropped_records() -> int:
    """Records lost since the process started: queue full, or the output was unwritable."""
    return _dropped


class RequestIdFilter(logging.Filter):
    """Stamp records with the current request id. Runs in the emitting thread, where the
    context variable is visible (the writer thread has no request context)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = REQUEST_ID.get() or "-"
        return True


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT, defaults={"request_id": "-"})


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``message``, ``request_id``,
    any ``extra=`` fields, and ``exc`` with the traceback when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        created = record.created
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created))
            + f".{int(created % 1 * 1000):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id and request_id != "-":
            entry["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exc"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        return json.dumps(entry, ensure_ascii=False, default=str)


class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """Queue records for the writer thread; never block the caller.

    ``urgent`` is set for warnings and errors so the writer flushes them at once instead
    of at its next interval.
    """

    def __init__(
        self,
        records: queue.SimpleQueue[logging.LogRecord],
        *,
        limit: int = QUEUE_SIZE,
        urgent: threading.Event | None = None,
    ) -> None:
        super().__init__(records)
        self.limit = limit
        self.urgent = urgent

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Merge the arguments and render the traceback here: the record crosses to another
        # thread, and keeping ``exc_text`` separate lets the JSON formatter emit ``exc``.
        record = copy.copy(record)
        record.message = record.getMessage()
        record.msg = record.message
        record.args = None
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = _EXCEPTION_FORMATTER.formatException(record.exc_info)
            record.exc_info = None
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        # emit() runs under the handler lock and the writer only removes records, so the
        # size check cannot race another producer.
        if self.queue.qsize() >= self.limit:
            _count_dropped()
            return
        self.queue.put_nowait(record)
        if self.urgent is not None and record.levelno >= logging.WARNING:
            self.urgent.set()


class _Writer(threading.Thread):
    """Format queued records and write them in one call per batch."""

    def __init__(
        self,
        records: queue.SimpleQueue[logging.LogRecord],
        output: logging.StreamHandler,
        interval: float,
    ) -> None:
        super().__init__(name="kiro-gateway-log-writer", daemon=True)
        self.records = records
        self.output = output
        self.interval = interval
        self.urgent = threading.Event()
        self._closing = False

    def run(self) -> None:
        while True:
            self.urgent.wait(self.interval)
            self.urgent.clear()
            self._drain()
            if self._closing:
                return

    def _drain(self) -> None:
        lines: list[str] = []
        while True:
            try:
                record = self.records.get_nowait()
            except queue.Empty:
                break
            try:
                lines.append(self.output.format(record))
            except Exception:
                self.output.handleError(record)
        if lines:
            try:
                self.output.stream.write("\n".join(lines) + "\n")
                self.output.flush()
            except Exception:  # e.g. stderr closed; nowhere left to report it
                _count_dropped(len(lines))

    def close(self) -> None:
        self._closing = True
        self.urgent.set()
        self.join()


def configure_logging(
    level: str = "info",
    fmt: str = "text",
    *,
    stream: TextIO | None = None,
    queue_size: int = QUEUE_SIZE,
    interval: float = FLUSH_INTERVAL,
) -> Callable[[], None]:
    """Install the gateway's root handler and return a function that flushes and removes it.

    ``fmt`` is ``text`` (``TEXT_FORMAT``) or ``json``. Uvicorn's loggers propagate to the
    root logger when uvicorn is started with ``log_config=None``, so its lines share the
    format. Call it once at startup, before anything else configures logging.
    """
    root = logging.getLogger()
    output = logging.StreamHandler(stream if stream is not None else sys.stderr)
    output.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    writer = _Writer(records, output, interval)
    handler = NonBlockingQueueHandler(records, limit=queue_size, urgent=writer.urgent)
    handler.addFilter(RequestIdFilter())
    writer.start()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    def shutdown() -> None:
        if handler in root.handlers:
            root.removeHandler(handler)  # no new records, then write what is queued
            writer.close()

    return shutdown
