"""Session reuse with what Claude Code really sends (found against a live gateway):

* ``role: "system"`` notices in the middle of ``messages`` (working-directory updates,
  reminders), which used to be merged into the system prompt and change the fingerprint;
* tool inputs echoed back with defaults filled in (Edit gains ``"replace_all": false``);
* the tool list growing mid-conversation (deferred tools loaded through ToolSearch);
* a client that disconnects mid-stream, whose session cleanup used to be cancelled.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from kiro_acp.gateway.app import create_app
from kiro_acp.gateway.conversation import Message, TextPart, ToolCallPart, canonical_message
from kiro_acp.gateway.prompting import render_prompt
from kiro_acp.gateway.protocols import anthropic, openai_chat, openai_responses
from tests.test_gateway import ANTHROPIC_HEADERS, FakeKiroBackend, make_settings, sse_events

NOTICE = "# Environment update\n - Primary working directory: /p/alpha (was /p)"
READ = {
    "name": "Read",
    "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
}
BASH = {
    "name": "Bash",
    "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
}


@pytest.fixture
async def mcp_app(workspace: Path, engine: str):
    settings = make_settings(workspace, engine=engine, tool_mode="mcp", mcp_batch_window=0.3)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=60
        ) as http,
    ):
        http.app = app  # type: ignore[attr-defined]
        yield http


def spawns(http: httpx.AsyncClient) -> int:
    backend = http.app.state.backend  # type: ignore[attr-defined]
    return sum(entry[2] for entry in backend.metrics.spawn.series.values())


def assistant_from_stream(text: str) -> dict:
    """Rebuild the assistant message the way an SDK client assembles it from SSE."""
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    for name, data in sse_events(text):
        if name == "content_block_start":
            blocks[data["index"]] = dict(data["content_block"])
        elif name == "content_block_delta":
            delta = data["delta"]
            if delta["type"] == "text_delta":
                block = blocks[data["index"]]
                block["text"] = block.get("text", "") + delta["text"]
            elif delta["type"] == "input_json_delta":
                partial[data["index"]] = partial.get(data["index"], "") + delta["partial_json"]
    content = []
    for index in sorted(blocks):
        block = blocks[index]
        if block["type"] == "tool_use":
            block["input"] = json.loads(partial.get(index) or "{}")
        if block["type"] in ("text", "tool_use"):
            content.append(block)
    return {"role": "assistant", "content": content}


async def first_tool_turn(http: httpx.AsyncClient, tools: list, prompt: str = "mcp2"):
    response = await http.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 100,
            "stream": True,
            "tools": tools,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    assistant = assistant_from_stream(response.text)
    calls = [b for b in assistant["content"] if b["type"] == "tool_use"]
    results = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": b["id"], "content": f"out{i}"}
            for i, b in enumerate(calls)
        ],
    }
    return assistant, results


# --------------------------------------------------------------------------- parsing


def test_system_messages_mid_conversation_stay_in_place() -> None:
    body = {
        "system": "top-level prompt",
        "messages": [
            {"role": "system", "content": "leading system message"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": NOTICE},
            {"role": "user", "content": "next"},
        ],
    }
    conversation = anthropic.build_conversation(body)
    assert conversation.system == "top-level prompt\n\nleading system message"
    assert [m.role for m in conversation.messages] == ["user", "assistant", "system", "user"]
    assert conversation.messages[2].text() == NOTICE

    chat = openai_chat.build_conversation(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "developer", "content": "now be verbose"},
                {"role": "user", "content": "next"},
            ]
        }
    )
    assert chat.system == "be brief"
    assert [m.role for m in chat.messages] == ["user", "assistant", "system", "user"]

    responses = openai_responses.build_conversation(
        {
            "input": [
                {"role": "developer", "content": "be brief"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "developer", "content": "now be verbose"},
                {"role": "user", "content": "next"},
            ]
        }
    )
    assert responses.system == "be brief"
    assert [m.role for m in responses.messages] == ["user", "assistant", "system", "user"]


def test_notices_render_in_place_and_keep_the_prefix_fingerprint() -> None:
    before = anthropic.build_conversation(
        {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]}
    )
    after = anthropic.build_conversation(
        {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok"},
                {"role": "system", "content": NOTICE},
                {"role": "user", "content": "next"},
            ]
        }
    )
    start = after.prefix_length_for_affinity()
    assert after.fingerprint(start) == before.fingerprint()
    blocks = render_prompt(after, start=start, include_system=False, emulate_tools=False)
    text = blocks[0]["text"]
    assert f'<message role="system">\n{NOTICE}\n</message>' in text
    assert text.index('role="system"') < text.index('<message role="user">\nnext')


def test_fingerprint_ignores_tool_arguments() -> None:
    sent = Message("assistant", [TextPart("editing"), ToolCallPart("a", "Edit", {"x": 1})])
    echoed = Message(
        "assistant",
        [TextPart("editing"), ToolCallPart("b", "Edit", {"x": 1, "replace_all": False})],
    )
    assert canonical_message(sent) == canonical_message(echoed)
    other = Message("assistant", [TextPart("editing"), ToolCallPart("c", "Write", {"x": 1})])
    assert canonical_message(sent) != canonical_message(other)


# --------------------------------------------------------------------------- tool loops


async def test_tool_loop_survives_a_system_notice(mcp_app: httpx.AsyncClient) -> None:
    assistant, results = await first_tool_turn(mcp_app, [READ, BASH])
    second = await mcp_app.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 100,
            "tools": [READ, BASH],
            "messages": [
                {"role": "user", "content": "mcp2"},
                assistant,
                results,
                {"role": "system", "content": NOTICE},
            ],
        },
    )
    body = second.json()
    assert body["kiro"]["reused_session"] is True, body
    assert spawns(mcp_app) == 1
    reply = body["content"][-1]["text"]
    # The blocked Kiro turn got the results and, with the last one, the notice.
    assert (
        reply.startswith("results: out0 | out1")
        and "[System notice]: # Environment update" in reply
    )


async def test_tool_loop_survives_normalized_tool_inputs(mcp_app: httpx.AsyncClient) -> None:
    assistant, results = await first_tool_turn(mcp_app, [READ, BASH])
    for block in assistant["content"]:
        if block["type"] == "tool_use":
            block["input"] = {**block["input"], "replace_all": False}  # Claude Code's echo
    second = await mcp_app.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 100,
            "tools": [READ, BASH],
            "messages": [{"role": "user", "content": "mcp2"}, assistant, results],
        },
    )
    assert second.json()["kiro"]["reused_session"] is True
    assert spawns(mcp_app) == 1


async def test_tool_loaded_mid_turn_is_added_in_place(mcp_app: httpx.AsyncClient) -> None:
    assistant, results = await first_tool_turn(mcp_app, [READ], 'mcp:Read:{"n": 1}')
    messages = [{"role": "user", "content": 'mcp:Read:{"n": 1}'}, assistant, results]
    second = await mcp_app.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "max_tokens": 100, "tools": [READ, BASH], "messages": messages},
    )
    body = second.json()
    assert body["kiro"]["reused_session"] is True and body["content"][-1]["text"] == "result: out0"
    messages += [
        {"role": "assistant", "content": body["content"]},
        {"role": "user", "content": "tools?"},
    ]
    third = await mcp_app.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "max_tokens": 100, "tools": [READ, BASH], "messages": messages},
    )
    body = third.json()
    assert body["kiro"]["reused_session"] is True
    assert body["content"][-1]["text"] == "tools: Read, Bash"  # Kiro re-read the list
    assert spawns(mcp_app) == 1


async def test_emulated_tools_still_need_the_same_tool_set(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine)  # tool_mode=emulate
    app = create_app(settings, backend=FakeKiroBackend(settings))
    read = {"type": "function", "function": {"name": "Read", "parameters": {"type": "object"}}}
    bash = {"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http,
    ):
        first = await http.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "tools": [read],
                "messages": [{"role": "user", "content": "echo: a"}],
            },
        )
        history = [
            {"role": "user", "content": "echo: a"},
            {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
            {"role": "user", "content": "echo: b"},
        ]
        same = await http.post(
            "/v1/chat/completions", json={"model": "x", "tools": [read], "messages": history}
        )
        assert same.json()["kiro"]["reused_session"] is True
        history += [
            {"role": "assistant", "content": same.json()["choices"][0]["message"]["content"]},
            {"role": "user", "content": "echo: c"},
        ]
        grown = await http.post(
            "/v1/chat/completions",
            json={"model": "x", "tools": [read, bash], "messages": history},
        )
        # The schemas are baked into the emulated session's prompt: start a new one.
        assert grown.json()["kiro"]["reused_session"] is False


async def test_agent_mode_reuse_delivers_the_notice(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=30
        ) as http,
    ):
        messages = [{"role": "user", "content": "echo: one"}]
        for content in ("echo: two", "history?"):
            reply = await http.post(
                "/v1/messages",
                headers=ANTHROPIC_HEADERS,
                json={"model": "x", "max_tokens": 50, "messages": messages},
            )
            body = reply.json()
            messages += [
                {"role": "assistant", "content": body["content"]},
                {"role": "system", "content": NOTICE},
                {"role": "user", "content": content},
            ]
        final = await http.post(
            "/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json={"model": "x", "max_tokens": 50, "messages": messages},
        )
    body = final.json()
    assert body["kiro"]["reused_session"] is True
    prompts = json.loads(body["content"][-1]["text"])
    # The fake agent lists the prompts before the current one: one session served every turn.
    assert len(prompts) == 2
    assert f'<message role="system">\n{NOTICE}\n</message>' in prompts[1]
    assert "<operator_instructions>" not in prompts[1]  # only the new messages were sent


# --------------------------------------------------------------------------- side requests

RECAP = (
    "The user stepped away and is coming back. Recap in under 40 words, 1-2 plain sentences, "
    "no markdown. Lead with the overall goal and current task, then the one next action."
)


def test_recap_is_recognized_only_as_the_last_user_message() -> None:
    def conversation(*messages: dict):
        return anthropic.build_conversation({"messages": list(messages)})

    base = ({"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"})
    assert conversation(*base, {"role": "user", "content": RECAP}).side_request() == "recap"
    assert (
        conversation(*base, {"role": "user", "content": "please " + RECAP}).side_request() is None
    )
    assert (
        conversation(
            *base, {"role": "user", "content": RECAP}, {"role": "assistant", "content": "x"}
        ).side_request()
        is None
    )
    assert conversation(*base, {"role": "user", "content": "next"}).side_request() is None


async def test_recap_leaves_the_session_with_the_conversation(mcp_app: httpx.AsyncClient) -> None:
    """Claude Code's recap is served on the conversation's session and then discarded by
    the client; the next real message must still find that session."""

    async def ask(messages: list) -> dict:
        response = await mcp_app.post(
            "/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json={"model": "x", "max_tokens": 100, "tools": [READ], "messages": messages},
        )
        assert response.status_code == 200, response.text
        return response.json()

    history = [{"role": "user", "content": "echo: one"}]
    first = await ask(history)
    history.append({"role": "assistant", "content": first["content"]})
    recap = await ask([*history, {"role": "user", "content": RECAP}])
    assert recap["kiro"]["reused_session"] is True
    assert recap["kiro"]["side_request"] == "recap"
    after = await ask([*history, {"role": "user", "content": "history?"}])
    assert after["kiro"]["reused_session"] is True
    assert spawns(mcp_app) == 1
    prompts = json.loads(after["content"][-1]["text"])
    assert "<side_request>" in prompts[-1] and RECAP in prompts[-1]  # sent to Kiro as an aside


async def test_recap_on_a_cold_conversation_warms_its_session(mcp_app: httpx.AsyncClient) -> None:
    """With no live session (expired, gateway restarted) the recap starts one; the user's
    next message then reuses it instead of starting another."""
    history = [
        {"role": "user", "content": "echo: one"},
        {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
    ]
    body = {"model": "x", "max_tokens": 100, "tools": [READ]}
    recap = await mcp_app.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={**body, "messages": [*history, {"role": "user", "content": RECAP}]},
    )
    assert recap.json()["kiro"]["reused_session"] is False
    after = await mcp_app.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={**body, "messages": [*history, {"role": "user", "content": "echo: two"}]},
    )
    assert after.json()["kiro"]["reused_session"] is True
    assert spawns(mcp_app) == 1


async def test_side_request_never_displaces_a_session(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine)
    backend = FakeKiroBackend(settings)
    await backend.start()
    try:
        from kiro_acp.gateway.backend import TurnOptions

        opts = TurnOptions(model="claude-opus-4.8")
        conversation_holder = await backend._spawn(opts, "allow-once")
        side_session = await backend._spawn(opts, "allow-once")
        await backend._release(conversation_holder, "key", keep=True)
        await backend._release(side_session, "key", keep=True, displace=False)
        assert backend._pool["key"] is conversation_holder
        assert not side_session.agent.client.is_running
        assert conversation_holder.agent.client.is_running
    finally:
        await backend.stop()


# --------------------------------------------------------------------------- disconnects


async def test_client_disconnect_mid_stream_closes_the_session(engine: str) -> None:
    """Starlette cancels the response on http.disconnect; cleanup must still finish."""
    settings = make_settings(Path(tempfile.mkdtemp()), engine=engine)
    backend = FakeKiroBackend(settings)
    app = create_app(settings, backend=backend)
    body = json.dumps(
        {"model": "x", "stream": True, "messages": [{"role": "user", "content": "slow"}]}
    ).encode()
    sent = asyncio.Event()
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": body, "more_body": False}
        await sent.wait()
        return {"type": "http.disconnect"}

    chunks = 0

    async def send(message):
        nonlocal chunks
        if message["type"] == "http.response.body" and message.get("body"):
            chunks += 1
            if chunks >= 2:
                sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "server": ("gw", 80),
        "client": ("127.0.0.1", 1),
        "headers": [(b"authorization", b"Bearer secret"), (b"content-type", b"application/json")],
    }
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)
        for _ in range(100):
            if not backend._active and backend.agent_processes() == 0:
                break
            await asyncio.sleep(0.05)
        assert not backend._active, "the abandoned turn is still listed as running"
        assert backend.agent_processes() == 0, "the kiro-cli process was left running"
        finishes = [dict(labels)["finish"] for labels in backend.metrics.turns]
        assert finishes == ["aborted"]
