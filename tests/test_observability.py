"""Monitoring and logging: request ids, access log, log formats, HTTP/runtime metrics,
streaming error accounting, turn outcomes, and the loop-lag monitor."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import queue
import re
import time
from pathlib import Path

import httpx
import pytest

from kiro_acp.gateway.app import create_app
from kiro_acp.gateway.backend import TurnOptions
from kiro_acp.gateway.config import Settings
from kiro_acp.gateway.logs import (
    NonBlockingQueueHandler,
    clean,
    configure_logging,
    dropped_records,
)
from kiro_acp.gateway.metrics import _fmt
from kiro_acp.gateway.middleware import ObservabilityMiddleware, route_label
from kiro_acp.gateway.protocols.openai_chat import build_conversation
from kiro_acp.gateway.system import LoopLagMonitor
from tests.test_gateway import READ_TOOL, FakeKiroBackend, make_settings, sse_events


@pytest.fixture
def capture_logs():
    """Run the gateway's real logging setup into a buffer; ``read()`` flushes it."""
    root = logging.getLogger()
    saved_level = root.level
    stops = []

    class Capture:
        def __init__(self, fmt: str, level: str) -> None:
            self.stream = io.StringIO()
            self._stop = configure_logging(level, fmt, stream=self.stream)
            stops.append(self._stop)

        def read(self) -> list[str]:
            self._stop()
            return self.stream.getvalue().splitlines()

    yield lambda fmt="text", level="info": Capture(fmt, level)
    for stop in stops:
        stop()  # removes only the gateway handler; pytest's own handlers stay
    root.setLevel(saved_level)


def make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gw",
        headers={"Authorization": "Bearer secret"},
        timeout=30,
    )


@pytest.fixture
async def client(workspace: Path, engine: str):
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app), make_client(app) as http:
        http.app = app  # type: ignore[attr-defined]
        yield http


CHAT = {"model": "x", "messages": [{"role": "user", "content": "echo: hi"}]}


def metric_value(text: str, line_prefix: str) -> float:
    for line in text.splitlines():
        if line.startswith(line_prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{line_prefix} not found in:\n{text}")


# --------------------------------------------------------------------------- request ids


async def test_request_id_is_echoed_and_validated(client: httpx.AsyncClient) -> None:
    supplied = await client.get("/health", headers={"X-Request-ID": "trace-123:abc"})
    assert supplied.headers["x-request-id"] == "trace-123:abc"
    unsafe = await client.get("/health", headers={"X-Request-ID": "two words"})
    assert unsafe.headers["x-request-id"].startswith("req_")
    too_long = await client.get("/health", headers={"X-Request-ID": "a" * 129})
    assert too_long.headers["x-request-id"].startswith("req_")
    generated = await client.get("/health")
    assert re.fullmatch(r"req_[0-9a-f]{24}", generated.headers["x-request-id"])
    # Error responses carry it too, so a client can quote it in a bug report.
    failed = await client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert failed.status_code == 400 and failed.headers["x-request-id"].startswith("req_")


async def test_every_line_of_a_request_carries_its_id(
    workspace: Path, engine: str, capture_logs
) -> None:
    logs = capture_logs("text")
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app), make_client(app) as http:
        response = await http.post(
            "/v1/chat/completions", json=CHAT, headers={"X-Request-ID": "e2e-1"}
        )
        assert response.status_code == 200
    lines = logs.read()
    tagged = [line for line in lines if "[e2e-1]" in line]
    assert any("Started kiro-cli pid=" in line for line in tagged), lines
    assert any("turn e2e-1: agent" in line for line in tagged), lines
    assert any("turn e2e-1 finished: stop" in line for line in tagged), lines
    access = [line for line in tagged if "kiro_acp.gateway.access" in line]
    assert len(access) == 1 and "POST /v1/chat/completions 200 " in access[0], lines
    assert re.search(r" \d+\.\d{3}s in=\d+B out=\d+B client=127\.0\.0\.1 ", access[0])


async def test_agent_reader_tasks_do_not_inherit_the_spawning_request_id(
    workspace: Path, engine: str, capture_logs
) -> None:
    """A pooled kiro-cli outlives the request that started it; its reader tasks must not
    stamp later lines (permission decisions) with that first request's id."""
    logs = capture_logs("text")
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    body = {"model": "x", "messages": [{"role": "user", "content": "tool"}]}
    async with app.router.lifespan_context(app), make_client(app) as http:
        response = await http.post(
            "/v1/chat/completions", json=body, headers={"X-Request-ID": "spawner"}
        )
        assert response.status_code == 200
    permission = [line for line in logs.read() if "permission call-1 (session " in line]
    assert permission and all("[-]" in line for line in permission), permission


# --------------------------------------------------------------------------- access log and formats


async def test_json_logs_and_access_fields(workspace: Path, engine: str, capture_logs) -> None:
    logs = capture_logs("json")
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app), make_client(app) as http:
        await http.post("/v1/chat/completions", json=CHAT, headers={"X-Request-ID": "json-1"})
        await http.get("/health")  # quiet path: debug level only
        await http.get("/metrics", headers={"Authorization": ""})  # 401: logged despite quiet
    records = [json.loads(line) for line in logs.read()]
    access = [r for r in records if r["logger"] == "kiro_acp.gateway.access"]
    chat = next(r for r in access if r["path"] == "/v1/chat/completions")
    assert chat["level"] == "INFO" and chat["request_id"] == "json-1"
    assert chat["method"] == "POST" and chat["route"] == "/v1/chat/completions"
    assert chat["status"] == 200 and chat["complete"] is True and chat["failure"] is None
    assert chat["duration_ms"] > 0 and chat["bytes_in"] > 0 and chat["bytes_out"] > 0
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", chat["ts"])
    assert not any(r["path"] == "/health" for r in access)
    assert any(r["path"] == "/metrics" and r["status"] == 401 for r in access)
    # Uvicorn-independent gateway lines share the format and the id.
    assert any(r["message"].startswith("turn json-1 finished: stop") for r in records)


def test_json_formatter_keeps_exceptions_and_extra_fields(capture_logs) -> None:
    logs = capture_logs("json")
    log = logging.getLogger("kiro_acp.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed %s", "here", extra={"session": "s-1"})
    (record,) = [json.loads(line) for line in logs.read() if "kiro_acp.test" in line]
    assert record["message"] == "failed here" and record["level"] == "ERROR"
    assert record["session"] == "s-1" and "request_id" not in record
    assert "ValueError: boom" in record["exc"] and "Traceback" in record["exc"]


def test_text_format_and_uvicorn_lines_share_the_handler(capture_logs) -> None:
    logs = capture_logs("text")
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    (line,) = [x for x in logs.read() if "uvicorn.error" in x]
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO uvicorn\.error \[-\]: Application startup complete\.",
        line,
    )


async def test_access_log_cannot_be_used_to_forge_lines(
    client: httpx.AsyncClient, capture_logs
) -> None:
    logs = capture_logs("text")
    await client.get(
        "/v1/x%0A2026-01-01 00:00:00,000 ERROR forged", headers={"User-Agent": "ua\r\nX"}
    )
    access = [line for line in logs.read() if "kiro_acp.gateway.access" in line]
    assert len(access) == 1, access
    assert "/v1/x\\x0a2026-01-01" in access[0] and 'ua="ua\\x0d\\x0aX"' in access[0]
    assert clean("x" * 300, 10) == "xxxxxxxxxx..."


async def test_access_log_can_be_disabled(workspace: Path, engine: str, capture_logs) -> None:
    logs = capture_logs("text")
    settings = make_settings(workspace, engine=engine, access_log=False)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app), make_client(app) as http:
        response = await http.post("/v1/chat/completions", json=CHAT)
        assert response.headers["x-request-id"]  # ids and metrics stay on
        metrics = (await http.get("/metrics")).text
    assert 'route="/v1/chat/completions",status="200"} 1' in metrics
    assert not [line for line in logs.read() if "kiro_acp.gateway.access" in line]


async def test_middleware_marks_truncated_and_failed_responses(capture_logs) -> None:
    from kiro_acp.gateway.metrics import Metrics

    logs = capture_logs("json")

    async def truncated(scope, receive, send):  # body ended early, as on a client disconnect
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": True})

    async def broken_mid_stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream broke")

    async def broken_before_start(scope, receive, send):
        raise KeyError("x")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    metrics = Metrics()
    for app, path in ((truncated, "/a"), (broken_mid_stream, "/b"), (broken_before_start, "/c")):
        scope = {"type": "http", "method": "POST", "path": path, "headers": [], "client": None}
        with contextlib.suppress(RuntimeError, KeyError):
            await ObservabilityMiddleware(app, metrics=metrics)(scope, receive, send)
    assert sent[0]["headers"][-1][0] == b"x-request-id"
    access = {r["path"]: r for r in map(json.loads, logs.read()) if r["logger"].endswith("access")}
    assert access["/a"]["complete"] is False and "incomplete" in access["/a"]["message"]
    assert access["/b"]["status"] == 200 and access["/b"]["failure"] == "RuntimeError"
    assert access["/c"]["status"] == 500 and access["/c"]["failure"] == "KeyError"
    assert metrics.http_in_flight == 0


def test_log_queue_drops_instead_of_blocking() -> None:
    records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = NonBlockingQueueHandler(records, limit=1)
    log = logging.getLogger("kiro_acp.test.drops")
    log.propagate = False
    log.addHandler(handler)
    try:
        before = dropped_records()
        started = time.perf_counter()
        for n in range(3):
            log.warning("record %d", n)
        assert time.perf_counter() - started < 0.5  # never waited for room
        assert records.qsize() == 1 and dropped_records() - before == 2
        assert records.get_nowait().getMessage() == "record 0"
    finally:
        log.removeHandler(handler)
        log.propagate = True


async def test_writer_batches_info_and_flushes_warnings_at_once(capture_logs) -> None:
    stream = io.StringIO()
    stop = configure_logging("info", "text", stream=stream, interval=5.0)
    try:
        log = logging.getLogger("kiro_acp.test.writer")
        log.info("routine")
        await asyncio.sleep(0.2)
        assert stream.getvalue() == ""  # waits for the batch interval
        log.warning("needs attention")
        for _ in range(50):
            if "needs attention" in stream.getvalue():
                break
            await asyncio.sleep(0.01)
        lines = stream.getvalue().splitlines()
        assert [line.rsplit(": ", 1)[1] for line in lines] == ["routine", "needs attention"]
    finally:
        stop()


def test_log_settings_from_env_and_cli(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from kiro_acp.gateway.main import build_parser, settings_from_args

    monkeypatch.setenv("KIRO_GATEWAY_LOG_FORMAT", "json")
    monkeypatch.setenv("KIRO_GATEWAY_ACCESS_LOG", "false")
    settings = Settings(_env_file=None, workspace=str(tmp_path))
    assert settings.log_format == "json" and settings.access_log is False
    monkeypatch.setenv("KIRO_GATEWAY_LOG_FORMAT", "xml")
    with pytest.raises(ValueError):
        Settings(_env_file=None, workspace=str(tmp_path))
    monkeypatch.delenv("KIRO_GATEWAY_LOG_FORMAT")
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["--log-format", "json", "--workspace", str(tmp_path)])
    assert settings_from_args(args).log_format == "json"


# --------------------------------------------------------------------------- HTTP metrics


def test_route_label_recovers_the_router_prefix() -> None:
    class Route:
        def __init__(self, path: str) -> None:
            self.path = path

    def label(template: str | None, path: str) -> str:
        return route_label({"route": Route(template) if template else None, "path": path})

    # FastAPI >= 0.13x reports included routes without their prefix; older ones with it.
    assert (
        label("/chat/completions", "/openai/v1/chat/completions") == "/openai/v1/chat/completions"
    )
    assert label("/v1/chat/completions", "/v1/chat/completions") == "/v1/chat/completions"
    assert label("/models/{model_id:path}", "/v1/models/claude-x") == "/v1/models/{model_id:path}"
    assert (
        label("/v1/kiro/sessions/{session_id}/audit", "/v1/kiro/sessions/abc/audit")
        == "/v1/kiro/sessions/{session_id}/audit"
    )
    assert label(None, "/random/123") == "<unmatched>"


async def test_http_request_metrics(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/chat/completions", json=CHAT)).status_code == 200
    assert (await client.get("/v1/kiro/sessions/sess-xyz/audit")).status_code == 404
    assert (await client.get("/v1/kiro/sessions/sess-abc/audit")).status_code == 404
    assert (await client.get("/no/such/path/1")).status_code == 404
    assert (await client.get("/no/such/path/2")).status_code == 404
    bad = await client.post("/openai/v1/chat/completions", json={"model": "x", "messages": []})
    assert bad.status_code == 400
    await client.request("PROPFIND", "/health")
    text = (await client.get("/metrics")).text
    assert (
        'kiro_gateway_http_requests_total{method="POST",route="/v1/chat/completions",status="200"} 1'
        in text
    )
    # Templates, not raw ids: two session ids, one series.
    assert (
        'kiro_gateway_http_requests_total{method="GET",route="/v1/kiro/sessions/{session_id}/audit",status="404"} 2'
        in text
    )
    assert (
        'kiro_gateway_http_requests_total{method="GET",route="<unmatched>",status="404"} 2' in text
    )
    assert 'route="/openai/v1/chat/completions",status="400"} 1' in text
    assert 'method="OTHER",route="/health"' in text  # unknown methods cannot add label values
    assert (
        metric_value(
            text,
            'kiro_gateway_http_request_duration_seconds_count{method="POST",route="/v1/chat/completions"}',
        )
        == 1
    )
    assert metric_value(text, "kiro_gateway_http_requests_in_flight") == 1  # this scrape


async def test_validation_errors_are_counted(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))

    @app.get("/typed")
    async def typed(n: int):
        return {"n": n}

    async with app.router.lifespan_context(app), make_client(app) as http:
        assert (await http.get("/typed?n=abc")).status_code == 422
        text = (await http.get("/metrics")).text
    assert 'kiro_gateway_errors_total{code="validation_error",status="422"} 1' in text


# --------------------------------------------------------------------------- error accounting


async def test_streaming_errors_are_counted_like_non_streaming_ones(
    client: httpx.AsyncClient,
) -> None:
    """Before, an error inside an SSE stream never reached kiro_gateway_errors_total."""
    failing = {"model": "x", "messages": [{"role": "user", "content": "error"}]}
    plain = await client.post("/v1/chat/completions", json=failing)
    assert plain.status_code == 502
    code = plain.json()["error"]["code"]
    streams = [
        ("/v1/chat/completions", {**failing, "stream": True}),
        ("/v1/completions", {"model": "x", "prompt": "error", "stream": True}),
        ("/v1/responses", {"model": "x", "input": "error", "stream": True}),
        ("/v1/messages", {**failing, "max_tokens": 10, "stream": True}),
    ]
    for path, body in streams:
        response = await client.post(path, json=body)
        assert response.status_code == 200 and "error" in response.text, path
    # A GatewayError raised inside backend.run() after the stream started.
    other = await client.post(
        "/v1/chat/completions",
        json={**CHAT, "stream": True},
        headers={"X-Kiro-Workspace": "/"},
    )
    assert any(isinstance(e[1], dict) and "error" in e[1] for e in sse_events(other.text))
    text = (await client.get("/metrics")).text
    assert f'kiro_gateway_errors_total{{code="{code}",status="502"}} 5' in text, text
    assert 'kiro_gateway_errors_total{code="workspace_not_allowed",status="403"} 1' in text


# --------------------------------------------------------------------------- turns


async def test_turns_ending_without_a_reply_are_recorded(
    workspace: Path, engine: str, caplog
) -> None:
    """Client disconnects and exceptions used to leave no trace in kiro_gateway_turns_total."""
    caplog.set_level(logging.INFO, logger="kiro_acp.gateway.backend")
    settings = make_settings(workspace, engine=engine)
    backend = FakeKiroBackend(settings)
    await backend.start()
    try:
        slow = build_conversation({"messages": [{"role": "user", "content": "slow"}]})
        events = backend.run(slow, TurnOptions(model="claude-opus-4.8", request_id="gone-1"))
        await events.__anext__()
        await events.aclose()  # what Starlette does when the client goes away mid-stream

        must_call = build_conversation(
            {
                "messages": [{"role": "user", "content": "echo: no call"}],
                "tools": [READ_TOOL],
                "tool_choice": "required",
            }
        )
        opts = TurnOptions(model="claude-opus-4.8", emulate_tools=True, request_id="err-1")
        with pytest.raises(Exception, match="tool_choice required"):
            async for _ in backend.run(must_call, opts):
                pass
        finishes = {dict(labels)["finish"]: n for labels, n in backend.metrics.turns.items()}
        assert finishes == {"aborted": 1, "error": 1}, finishes
    finally:
        await backend.stop()
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("turn gone-1 finished: aborted") for m in messages), messages
    assert any(
        m.startswith("turn err-1 finished: error") and "tool_choice required" in m for m in messages
    ), messages


async def test_mcp_turn_abandoned_mid_stream_is_recorded(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, tool_mode="mcp", mcp_batch_window=0.3)
    backend = FakeKiroBackend(settings)
    await backend.start()
    try:
        conversation = build_conversation(
            {"messages": [{"role": "user", "content": "slow"}], "tools": [READ_TOOL]}
        )
        events = backend.run(conversation, TurnOptions(model="claude-opus-4.8", emulate_tools=True))
        await events.__anext__()
        await events.aclose()
        finishes = [dict(labels)["finish"] for labels in backend.metrics.turns]
        assert finishes == ["aborted"]
    finally:
        await backend.stop()


async def test_queue_wait_and_saturation_gauges(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, max_concurrency=1, queue_timeout=20)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    slow = {"model": "x", "messages": [{"role": "user", "content": "sleep:1.5"}]}
    async with app.router.lifespan_context(app), make_client(app) as http:
        first = asyncio.create_task(http.post("/v1/chat/completions", json=slow))
        for _ in range(100):
            if app.state.backend.health()["active_turns"]:
                break
            await asyncio.sleep(0.05)
        second = asyncio.create_task(http.post("/v1/chat/completions", json=CHAT))
        queued = None
        for _ in range(60):
            text = (await http.get("/metrics")).text
            queued = metric_value(text, "kiro_gateway_queued_requests")
            if queued:
                break
            await asyncio.sleep(0.05)
        assert queued == 1
        assert metric_value(text, "kiro_gateway_turn_slots_free") == 0
        assert metric_value(text, "kiro_gateway_max_concurrency") == 1
        assert (await first).status_code == 200 and (await second).status_code == 200
        text = (await http.get("/metrics")).text
    assert metric_value(text, "kiro_gateway_queue_wait_seconds_count") == 2
    assert metric_value(text, "kiro_gateway_queue_wait_seconds_sum") >= 0.5  # the second waited
    assert metric_value(text, "kiro_gateway_queued_requests") == 0


# --------------------------------------------------------------------------- runtime


async def test_process_spawn_and_build_metrics(client: httpx.AsyncClient, engine: str) -> None:
    await client.post("/v1/chat/completions", json=CHAT)
    text = (await client.get("/metrics")).text
    assert metric_value(text, f'kiro_gateway_session_spawn_seconds_count{{engine="{engine}"}}') == 1
    assert metric_value(text, "kiro_gateway_agent_processes") == 1  # the pooled session
    assert metric_value(text, "process_cpu_seconds_total") > 0
    assert metric_value(text, "process_start_time_seconds") > 1.7e9
    assert metric_value(text, "process_open_fds") > 0
    assert metric_value(text, "kiro_gateway_log_records_dropped_total") >= 0
    assert 'kiro_gateway_build_info{version="' in text
    # Every sample line parses as "<name>{labels} <number>".
    sample = re.compile(r'[a-z_]+(\{([a-z_]+="[^"\n]*",?)*\})? -?[0-9.e+]+')
    bad = [
        line
        for line in text.splitlines()
        if not line.startswith("#") and not sample.fullmatch(line)
    ]
    assert not bad, bad


async def test_stats_expose_system_and_http_sections(client: httpx.AsyncClient) -> None:
    await client.post("/v1/chat/completions", json=CHAT)
    backend = client.app.state.backend  # type: ignore[attr-defined]
    backend.metrics.observe_loop_lag(0.003)
    stats = (await client.get("/v1/kiro/stats")).json()
    system = stats["system"]
    for key in ("pid", "uptime_seconds", "cpu_seconds", "open_fds", "max_rss_bytes", "python"):
        assert system[key] is not None, key
    assert system["agent_processes"] == 1 and system["queued_requests"] == 0
    assert system["http_in_flight"] == 1 and system["loop_lag_max_ms"] >= 3.0
    routes = {(r["route"], r["status"]) for r in stats["metrics"]["http"]}
    assert ("/v1/chat/completions", "200") in routes
    assert stats["metrics"]["session_spawn"][0]["count"] == 1
    assert stats["metrics"]["queue_wait"][0]["count"] == 1
    page = (await client.get("/dashboard")).text
    assert 'id="system"' in page and 'id="http"' in page


async def test_loop_lag_monitor_sees_a_blocked_loop() -> None:
    samples: list[float] = []
    monitor = LoopLagMonitor(samples.append, interval=0.02)
    monitor.start()
    await asyncio.sleep(0.1)
    time.sleep(0.15)  # noqa: ASYNC251 - deliberately block the event loop
    await asyncio.sleep(0.1)
    await monitor.stop()
    assert samples and min(samples) < 0.05  # idle samples are near zero
    assert max(samples) >= 0.1


def test_label_values_are_escaped() -> None:
    assert _fmt((("k", 'a\\b\n"c'),)) == '{k="a\\\\b\\n\'c"}'
