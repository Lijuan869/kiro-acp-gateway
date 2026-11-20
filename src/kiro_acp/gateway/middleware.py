"""ASGI middleware: request ids, the access log, and HTTP request metrics.

A plain ASGI callable rather than Starlette's ``BaseHTTPMiddleware``: no extra task per
request, streaming responses pass through untouched, and the request-id context variable
is visible to everything the request runs, including the SSE body generator.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiro_acp.gateway.logs import REQUEST_ID, accept_request_id, clean, new_request_id
from kiro_acp.gateway.metrics import Metrics

ACCESS = logging.getLogger("kiro_acp.gateway.access")
# Polled by health checks, Prometheus, and the dashboard: logged only at debug level
# unless they fail.
QUIET_PATHS = frozenset({"/health", "/metrics", "/v1/kiro/stats"})
UNMATCHED = "<unmatched>"


def route_label(scope: dict[str, Any]) -> str:
    """The matched route's path template, with its router prefix (bounded cardinality).

    FastAPI releases differ in whether ``scope["route"].path`` includes the prefix of an
    included router (``/chat/completions`` vs ``/v1/chat/completions``); the prefix is
    recovered from the request path so the label is the same on every version.
    """
    template = getattr(scope.get("route"), "path", None)
    if not isinstance(template, str) or not template:
        return UNMATCHED
    path = scope.get("path", "")
    head = template.split("{", 1)[0]
    if path.startswith(head):
        return template
    index = path.find(head)
    return path[:index] + template if index > 0 else template


class ObservabilityMiddleware:
    def __init__(self, app: Any, *, metrics: Metrics, access_log: bool = True) -> None:
        self.app = app
        self.metrics = metrics
        self.access_log = access_log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = user_agent = length = None
        for name, value in scope.get("headers") or ():
            if name == b"x-request-id":
                supplied = value.decode("latin-1")
            elif name == b"user-agent":
                user_agent = value.decode("latin-1")
            elif name == b"content-length":
                length = value.decode("latin-1")
        request_id = accept_request_id(supplied) or new_request_id()
        header = (b"x-request-id", request_id.encode("ascii"))
        token = REQUEST_ID.set(request_id)
        status = 0
        sent = 0
        complete = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status, sent, complete
            kind = message["type"]
            if kind == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", ()), header]}
            elif kind == "http.response.body":
                sent += len(message.get("body", b""))
                if not message.get("more_body", False):
                    complete = True
            await send(message)

        started = time.perf_counter()
        failure: str | None = None
        self.metrics.http_started()
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as error:
            failure = type(error).__name__
            if not status:
                status = 500  # ServerErrorMiddleware answers 500 after us
            raise
        finally:
            elapsed = time.perf_counter() - started
            route = route_label(scope)
            self.metrics.http_finished(scope.get("method", ""), route, status, elapsed)
            if self.access_log:
                self._log(
                    scope, route, status, elapsed, sent, complete, failure, length, user_agent
                )
            REQUEST_ID.reset(token)

    @staticmethod
    def _log(
        scope: dict[str, Any],
        route: str,
        status: int,
        elapsed: float,
        sent: int,
        complete: bool,
        failure: str | None,
        length: str | None,
        user_agent: str | None,
    ) -> None:
        path = scope.get("path", "")
        level = logging.DEBUG if path in QUIET_PATHS and status < 400 else logging.INFO
        if not ACCESS.isEnabledFor(level):
            return
        method = clean(scope.get("method", ""), 16)
        path = clean(path)
        client = scope.get("client")
        address = client[0] if client else "-"
        agent = clean(user_agent or "", 120)
        bytes_in = int(length) if length and length.isdigit() else None
        # "incomplete": the body ended early, normally because the client went away mid-stream.
        suffix = f" failed={failure}" if failure else ("" if complete else " incomplete")
        ACCESS.log(
            level,
            '%s %s %d %.3fs in=%s out=%dB client=%s ua="%s"%s',
            method,
            path,
            status,
            elapsed,
            f"{bytes_in}B" if bytes_in is not None else "-",
            sent,
            address,
            agent,
            suffix,
            extra={
                "method": method,
                "path": path,
                "route": route,
                "status": status,
                "duration_ms": round(elapsed * 1000, 1),
                "bytes_in": bytes_in,
                "bytes_out": sent,
                "client": address,
                "user_agent": agent,
                "complete": complete,
                "failure": failure,
            },
        )
