"""Prometheus text-format metrics without external dependencies.

Recording is a dictionary update under an uncontended lock (well under a microsecond);
process figures are read only when ``/metrics`` or ``/v1/kiro/stats`` is requested.
"""

from __future__ import annotations

import platform
import threading
from bisect import bisect_left
from collections import defaultdict, deque
from typing import Any

from kiro_acp import __version__

_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 900)
HTTP_BUCKETS = (0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900)
QUEUE_BUCKETS = (0.005, 0.05, 0.25, 1, 2.5, 5, 10, 30, 60, 120, 300)
SPAWN_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60, 120)
LAG_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
# Anything else is reported as OTHER so arbitrary method tokens cannot add label values.
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
Labels = tuple[tuple[str, str], ...]


def _escape(value: object) -> str:
    # A backslash or newline would break the exposition format; double quotes become
    # apostrophes as they always have here.
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', "'")


def _fmt(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


class _Histogram:
    """Bucket counts, sum, and count per label set. Callers hold the ``Metrics`` lock."""

    __slots__ = ("bounds", "series")

    def __init__(self, bounds: tuple[float, ...]) -> None:
        self.bounds = bounds
        # labels -> [per-bucket counts (last slot is +Inf), sum, count]
        self.series: dict[Labels, list[Any]] = {}

    def observe(self, labels: Labels, value: float) -> None:
        entry = self.series.get(labels)
        if entry is None:
            entry = self.series[labels] = [[0] * (len(self.bounds) + 1), 0.0, 0]
        entry[0][bisect_left(self.bounds, value)] += 1
        entry[1] += value
        entry[2] += 1

    @staticmethod
    def _cumulative(counts: list[int]) -> list[int]:
        out, running = [], 0
        for n in counts[:-1]:
            running += n
            out.append(running)
        return out

    def snapshot(self) -> list[dict[str, Any]]:
        rows = []
        for labels, (counts, total, count) in sorted(self.series.items()):
            rows.append(
                {
                    **dict(labels),
                    "count": count,
                    "sum": round(total, 3),
                    "mean": round(total / count, 3) if count else 0.0,
                    "buckets": [
                        {"le": bound, "count": n}
                        for bound, n in zip(self.bounds, self._cumulative(counts), strict=True)
                    ],
                }
            )
        return rows

    def render(self, name: str, lines: list[str]) -> None:
        for labels, (counts, total, count) in sorted(self.series.items()):
            for bound, n in zip(self.bounds, self._cumulative(counts), strict=True):
                lines.append(f"{name}_bucket{_fmt(labels + (('le', str(bound)),))} {n}")
            lines.append(f"{name}_bucket{_fmt(labels + (('le', '+Inf'),))} {count}")
            lines.append(f"{name}_sum{_fmt(labels)} {total:.3f}")
            lines.append(f"{name}_count{_fmt(labels)} {count}")


class Metrics:
    """Counters, gauges, and histograms for turns, HTTP requests, and the runtime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.turns: dict[Labels, int] = defaultdict(int)
        self.credits: dict[Labels, float] = defaultdict(float)
        self.reuse: dict[Labels, int] = defaultdict(int)
        self.errors: dict[Labels, int] = defaultdict(int)
        self.latency = _Histogram(_BUCKETS)
        self.http_requests: dict[Labels, int] = defaultdict(int)
        self.http_latency = _Histogram(HTTP_BUCKETS)
        self.http_in_flight = 0
        self.queue_wait = _Histogram(QUEUE_BUCKETS)
        self.spawn = _Histogram(SPAWN_BUCKETS)
        self.loop_lag = _Histogram(LAG_BUCKETS)
        self.loop_lag_recent: deque[float] = deque(maxlen=60)

    def record_turn(
        self,
        *,
        mode: str,
        engine: str,
        model: str,
        finish: str,
        seconds: float,
        credits: float,
        reused: bool,
    ) -> None:
        base: Labels = (("mode", mode), ("engine", engine), ("model", model))
        with self._lock:
            self.turns[base + (("finish", finish),)] += 1
            if credits:
                self.credits[(("model", model),)] += credits
            self.reuse[(("reused", "true" if reused else "false"),)] += 1
            self.latency.observe(base, seconds)

    def record_error(self, code: str, status: int) -> None:
        with self._lock:
            self.errors[(("code", code), ("status", str(status)))] += 1

    def http_started(self) -> None:
        with self._lock:
            self.http_in_flight += 1

    def http_finished(self, method: str, route: str, status: int, seconds: float) -> None:
        method = method if method in HTTP_METHODS else "OTHER"
        with self._lock:
            self.http_in_flight -= 1
            self.http_requests[(("method", method), ("route", route), ("status", str(status)))] += 1
            self.http_latency.observe((("method", method), ("route", route)), seconds)

    def observe_queue_wait(self, seconds: float) -> None:
        with self._lock:
            self.queue_wait.observe((), seconds)

    def observe_spawn(self, engine: str, seconds: float) -> None:
        with self._lock:
            self.spawn.observe((("engine", engine),), seconds)

    def observe_loop_lag(self, seconds: float) -> None:
        with self._lock:
            self.loop_lag.observe((), seconds)
            self.loop_lag_recent.append(seconds)

    def loop_lag_summary(self) -> tuple[float, float]:
        """Latest loop-lag sample and the largest of the last minute, in seconds."""
        with self._lock:
            if not self.loop_lag_recent:
                return 0.0, 0.0
            return self.loop_lag_recent[-1], max(self.loop_lag_recent)

    def snapshot(self) -> dict:
        """Structured form of the counters for the dashboard (``/v1/kiro/stats``)."""

        def rows(table: dict) -> list[dict]:
            return [{**dict(labels), "value": value} for labels, value in sorted(table.items())]

        with self._lock:
            return {
                "turns": rows(self.turns),
                "credits": rows(self.credits),
                "session_reuse": rows(self.reuse),
                "errors": rows(self.errors),
                "latency": self.latency.snapshot(),
                "http": rows(self.http_requests),
                "http_latency": self.http_latency.snapshot(),
                "queue_wait": self.queue_wait.snapshot(),
                "session_spawn": self.spawn.snapshot(),
            }

    def render(
        self,
        *,
        active_turns: int,
        live_sessions: int,
        models_cached: int,
        queued_requests: int = 0,
        turn_slots_free: int | None = None,
        max_concurrency: int | None = None,
        agent_processes: int | None = None,
        process: dict[str, Any] | None = None,
        log_records_dropped: int = 0,
    ) -> str:
        lines: list[str] = []

        def header(name: str, kind: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")

        def gauge(name: str, help_text: str, value: float | None, kind: str = "gauge") -> None:
            if value is None:
                return
            header(name, kind, help_text)
            lines.append(f"{name} {value}")

        with self._lock:
            header(
                "kiro_gateway_turns_total",
                "counter",
                "Completed turns by mode, engine, model, finish reason.",
            )
            for labels, value in sorted(self.turns.items()):
                lines.append(f"kiro_gateway_turns_total{_fmt(labels)} {value}")
            header("kiro_gateway_credits_total", "counter", "Kiro credits consumed, by model.")
            for labels, value in sorted(self.credits.items()):
                lines.append(f"kiro_gateway_credits_total{_fmt(labels)} {value:.6f}")
            header(
                "kiro_gateway_session_reuse_total",
                "counter",
                "Turns that reused a live Kiro session vs started one.",
            )
            for labels, value in sorted(self.reuse.items()):
                lines.append(f"kiro_gateway_session_reuse_total{_fmt(labels)} {value}")
            header(
                "kiro_gateway_errors_total",
                "counter",
                "API errors by gateway error code and HTTP status.",
            )
            for labels, value in sorted(self.errors.items()):
                lines.append(f"kiro_gateway_errors_total{_fmt(labels)} {value}")
            header("kiro_gateway_turn_seconds", "histogram", "Turn latency in seconds.")
            self.latency.render("kiro_gateway_turn_seconds", lines)
        header("kiro_gateway_active_turns", "gauge", "Turns currently running.")
        lines.append(f"kiro_gateway_active_turns {active_turns}")
        header("kiro_gateway_live_sessions", "gauge", "Idle Kiro sessions kept for reuse.")
        lines.append(f"kiro_gateway_live_sessions {live_sessions}")
        header("kiro_gateway_models_cached", "gauge", "Models in the cached catalogue.")
        lines.append(f"kiro_gateway_models_cached {models_cached}")

        gauge(
            "kiro_gateway_queued_requests",
            "Requests waiting for a free turn slot.",
            queued_requests,
        )
        gauge("kiro_gateway_turn_slots_free", "Turn slots not in use.", turn_slots_free)
        gauge(
            "kiro_gateway_max_concurrency",
            "Configured turn slots (KIRO_GATEWAY_MAX_CONCURRENCY).",
            max_concurrency,
        )
        gauge(
            "kiro_gateway_agent_processes",
            "Running kiro-cli processes started by the gateway.",
            agent_processes,
        )
        with self._lock:
            header(
                "kiro_gateway_queue_wait_seconds",
                "histogram",
                "Time a turn waited for a free slot.",
            )
            self.queue_wait.render("kiro_gateway_queue_wait_seconds", lines)
            header(
                "kiro_gateway_session_spawn_seconds",
                "histogram",
                "Time to start kiro-cli and create a session, by engine.",
            )
            self.spawn.render("kiro_gateway_session_spawn_seconds", lines)
            header(
                "kiro_gateway_http_requests_total",
                "counter",
                "HTTP requests by method, route template, and status.",
            )
            for labels, value in sorted(self.http_requests.items()):
                lines.append(f"kiro_gateway_http_requests_total{_fmt(labels)} {value}")
            header(
                "kiro_gateway_http_request_duration_seconds",
                "histogram",
                "HTTP request duration (streams: until the last byte), by method and route.",
            )
            self.http_latency.render("kiro_gateway_http_request_duration_seconds", lines)
            header("kiro_gateway_http_requests_in_flight", "gauge", "HTTP requests being served.")
            lines.append(f"kiro_gateway_http_requests_in_flight {self.http_in_flight}")
            header(
                "kiro_gateway_event_loop_lag_seconds",
                "histogram",
                "How late the event loop ran a timer, sampled once a second.",
            )
            self.loop_lag.render("kiro_gateway_event_loop_lag_seconds", lines)
        gauge(
            "kiro_gateway_log_records_dropped_total",
            "Log records lost because the log queue was full or the output was unwritable.",
            log_records_dropped,
            kind="counter",
        )
        header("kiro_gateway_build_info", "gauge", "Gateway version and Python version.")
        build: Labels = (("version", __version__), ("python", platform.python_version()))
        lines.append(f"kiro_gateway_build_info{_fmt(build)} 1")
        if process:
            gauge(
                "process_cpu_seconds_total",
                "Total user and system CPU time spent in seconds.",
                process.get("cpu_seconds"),
                kind="counter",
            )
            gauge(
                "process_resident_memory_bytes",
                "Resident memory size in bytes.",
                process.get("rss_bytes"),
            )
            gauge("process_open_fds", "Number of open file descriptors.", process.get("open_fds"))
            gauge(
                "process_max_fds",
                "Maximum number of open file descriptors.",
                process.get("max_fds"),
            )
            gauge(
                "process_start_time_seconds",
                "Start time of the process since unix epoch in seconds.",
                process.get("start_time"),
            )
        return "\n".join(lines) + "\n"
