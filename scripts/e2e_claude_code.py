#!/usr/bin/env python3
"""End-to-end checks of Claude Code running through kiro-gateway.

Run it on a machine where a Claude Code wrapper (default ``claude-gw``) points at the
gateway. Each scenario gets a scratch project under ``--out``; Claude Code runs headless
(``-p``) or, for the option picker, interactively in a pseudo-terminal rendered with
``pyte``. Afterwards the script checks the outcome, reads Claude Code's own transcript
(API requests, tool calls, embedded system messages), and asks the gateway which Kiro
sessions served the run (``/v1/kiro/sessions`` and their audit records).

    python3 -m pip install --target /tmp/e2e-pylib pyte   # no venv needed
    PYTHONPATH=/tmp/e2e-pylib python3 scripts/e2e_claude_code.py \\
        --gateway http://host:8000 --claude ~/.local/bin/claude-gw

Options: ``--only seq_tools,web_fetch`` runs a subset; ``--claude`` names the wrapper (give
the full path over ssh, where ``~/.local/bin`` may not be on PATH); ``--key-file`` holds
the gateway key (default ``~/.kiro-gw-key``). Scenarios run one at a time; other traffic
on the gateway during a run is counted too. Every scenario runs Claude Code with
``--dangerously-skip-permissions`` inside its own scratch directory.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    name: str
    feature: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)
    interactive: bool = False
    timeout: int = 900
    choose: int = 0  # interactive: which option to pick (1-based)


SCENARIOS = [
    Scenario(
        "seq_tools",
        "tool calls: sequential Bash, working directory changes",
        "This is a tool-sequencing test. Do exactly these steps, one Bash tool call per step, "
        "and wait for each result before the next step. Do not combine steps or use other "
        "tools.\n1. Run `pwd`.\n2. Run `cd alpha && pwd`.\n3. Run `cat one.txt`.\n"
        "4. Run `cd ../beta && pwd`.\n5. Run `cat two.txt`.\n6. Run `cd .. && ls`.\n"
        "Then reply with exactly one line: the contents of one.txt and two.txt separated by "
        "a space.",
        {"alpha/one.txt": "one\n", "beta/two.txt": "two\n"},
    ),
    Scenario(
        "parallel_edit",
        "tool calls: parallel Read, Edit with exact arguments",
        "Read notes/a.txt, notes/b.txt and notes/c.txt with three Read tool calls issued "
        "together in a single response. Then use the Edit tool to change the line "
        "`mode = draft` to `mode = final` in config.ini, keeping every other line unchanged. "
        "Finally reply with only the sum of the three numbers.",
        {
            "notes/a.txt": "17\n",
            "notes/b.txt": "25\n",
            "notes/c.txt": "8\n",
            "config.ini": "[app]\nmode = draft\nretries = 3\n",
        },
    ),
    Scenario(
        "subagent",
        "tool calls: Agent (subagent) with its own tool loop",
        "Use the Agent tool to start a general-purpose subagent with this task: 'Count the "
        "files whose names end in .txt directly inside the data directory, using the Glob "
        "tool, and report the number.' When the subagent answers, reply with only that "
        "number.",
        {**{f"data/n{i}.txt": f"{i}\n" for i in range(1, 8)}, "data/readme.md": "x\n"},
    ),
    Scenario(
        "web_search",
        "web search: WebSearch tool",
        "Use the WebSearch tool to search the web for: IANA reserved example domains. Reply "
        "with the URL of the most relevant result. If the search returned no results, reply "
        "exactly NO_RESULTS.",
    ),
    Scenario(
        "web_fetch",
        "web search: WebFetch tool",
        "Use the WebFetch tool to fetch https://example.com/ and reply with only the main "
        "heading of the page.",
    ),
    Scenario(
        "ask_user",
        "user choice: AskUserQuestion option picker (interactive)",
        "Use the AskUserQuestion tool to ask me which fruit to record, with exactly three "
        "options: Apple, Banana, Cherry. After I answer, use the Write tool to create "
        "fruit.txt containing only the name of the fruit I chose, then reply DONE.",
        interactive=True,
        choose=2,
        timeout=600,
    ),
]

# --------------------------------------------------------------------------- results


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Run:
    scenario: Scenario
    workdir: Path
    started: float = 0.0
    finished: float = 0.0
    exit_code: int | None = None
    result: str = ""
    screens: dict[str, str] = field(default_factory=dict)
    transcript: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    error: str = ""


def parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------- Claude Code


def run_headless(run: Run, claude: str) -> None:
    proc = subprocess.run(
        [
            claude,
            "--dangerously-skip-permissions",
            "-p",
            run.scenario.prompt,
            "--output-format",
            "json",
        ],
        cwd=run.workdir,
        capture_output=True,
        text=True,
        timeout=run.scenario.timeout,
    )
    run.exit_code = proc.returncode
    try:
        data = json.loads(proc.stdout)
        run.result = str(data.get("result") or "")
        if data.get("is_error"):
            run.error = f"Claude Code reported is_error: {run.result[:300]}"
    except json.JSONDecodeError:
        run.error = f"unparseable output: {proc.stdout[:300]!r} stderr: {proc.stderr[:300]!r}"


class Terminal:
    """Claude Code in a pseudo-terminal; the screen is rendered with pyte."""

    def __init__(self, argv: list[str], cwd: Path, rows: int = 50, cols: int = 160) -> None:
        import fcntl
        import pty
        import termios

        import pyte

        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        pid, fd = pty.fork()
        if pid == 0:  # child: size the terminal before Claude Code reads it
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.chdir(cwd)
            env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}
            os.execvpe(argv[0], argv, env)
        self.pid, self.fd, self.alive = pid, fd, True

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while self.alive and time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                self.alive = False
                break
            self.stream.feed(data)

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def wait_for(self, predicate: Callable[[str], bool], timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end and self.alive:
            self.pump(0.5)
            if predicate(self.text()):
                return True
        return predicate(self.text())

    def send(self, data: str, pause: float = 0.3) -> None:
        os.write(self.fd, data.encode())
        self.pump(pause)

    def close(self) -> None:
        for keys in ("\x03", "\x03"):
            if self.alive:
                with contextlib.suppress(OSError):
                    self.send(keys, 0.8)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)
        for _ in range(20):
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return
            time.sleep(0.1)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)


OPTION_RE = re.compile(r"(?m)^\W*(?:❯\s*)?(\d)\.\s+(Apple|Banana|Cherry)\b")


def run_interactive(run: Run, claude: str) -> None:
    term = Terminal([claude, "--dangerously-skip-permissions"], run.workdir)
    idle = re.compile(r"for shortcuts|bypass permissions", re.IGNORECASE)
    try:
        ready = term.wait_for(
            lambda s: "trust this folder" in s.lower() or bool(idle.search(s)), 60
        )
        run.screens["start"] = term.text()
        if "trust this folder" in term.text().lower():
            # The folder safety check defaults to "No, exit"; move to "Yes, I trust this folder".
            term.send("\x1b[B", 0.5)
            term.send("\r", 1)
            ready = term.wait_for(lambda s: bool(idle.search(s)), 60)
        if not ready or "didn't start" in term.text():
            run.screens["not_ready"] = term.text()
            run.error = "Claude Code UI did not come up"
            return
        term.send(run.scenario.prompt, 1)
        if not term.wait_for(lambda s: "AskUserQuestion" in s, 10):
            run.screens["typed"] = term.text()
            run.error = "the prompt did not reach the input box"
            return
        term.send("\r", 1)
        seen = term.wait_for(lambda s: len(set(OPTION_RE.findall(s))) >= 3, run.scenario.timeout)
        run.screens["question"] = term.text()
        if not seen:
            run.error = "the option picker never appeared"
            return
        for _ in range(run.scenario.choose - 1):
            term.send("\x1b[B", 0.5)
        run.screens["choice"] = term.text()
        term.send("\r", 2)
        if re.search(r"(?i)submit answers|review your answers", term.text()):
            run.screens["review"] = term.text()
            term.send("\r", 2)
        target = run.workdir / "fruit.txt"
        term.wait_for(lambda s: target.exists(), run.scenario.timeout)
        term.pump(15)  # let the final reply arrive
        run.screens["end"] = term.text()
    finally:
        term.close()
        run.exit_code = 0


# --------------------------------------------------------------------------- transcript


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude-gw")


def read_transcript(run: Run) -> dict[str, Any]:
    project = re.sub(r"[^A-Za-z0-9-]", "-", str(run.workdir))
    root = config_dir() / "projects" / project
    files = [p for p in root.rglob("*.jsonl") if p.stat().st_mtime >= run.started - 5]
    blobs: dict[str, Any] = {}
    requests, tool_uses, results, errors = [], [], {}, []
    final_text = ""
    for path in sorted(files):
        sub = "subagents" in path.parts
        for raw in path.open(encoding="utf-8"):
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = entry.get("type")
            if kind == "api-request-blob":
                blobs[entry["hash"]] = entry["message"]
            elif kind == "api-request":
                requests.append({**entry, "_sub": sub})
            elif kind in ("assistant", "user"):
                if entry.get("isApiErrorMessage"):
                    errors.append(str(entry.get("message", {}).get("content"))[:300])
                content = (entry.get("message") or {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_uses.append(
                            {
                                "name": block.get("name"),
                                "id": block.get("id"),
                                "input": block.get("input"),
                                "sub": sub,
                                "message": (entry.get("message") or {}).get("id"),
                            }
                        )
                    elif block.get("type") == "tool_result":
                        body = block.get("content")
                        text = (
                            body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        )
                        results[block.get("tool_use_id")] = {
                            "error": bool(block.get("is_error")),
                            "text": text,
                        }
                    elif block.get("type") == "text" and kind == "assistant" and not sub:
                        final_text = block.get("text", "")
    rows = []
    for request in sorted(requests, key=lambda r: r["timestamp"]):
        system = []
        for digest in request.get("tail", []):
            message = blobs.get(digest) or {}
            if message.get("role") == "system":
                body = message.get("content")
                text = (
                    body
                    if isinstance(body, str)
                    else "".join(b.get("text", "") for b in body or [] if isinstance(b, dict))
                )
                system.append(text.strip().splitlines()[0][:80] if text.strip() else "(empty)")
        params = request.get("params") or {}
        rows.append(
            {
                "time": parse_ts(request["timestamp"]),
                "source": request.get("querySource"),
                "model": params.get("model"),
                "effort": (params.get("output_config") or {}).get("effort"),
                "subagent": request["_sub"],
                "system": system,
            }
        )
    batches: dict[str, int] = {}
    for use in tool_uses:
        batches[use["message"]] = batches.get(use["message"], 0) + 1
    for use in tool_uses:
        use["result"] = results.get(use["id"])
    return {
        "files": [str(p) for p in files],
        "requests": rows,
        "tool_uses": tool_uses,
        "max_parallel": max(batches.values(), default=0),
        "api_errors": errors,
        "final_text": final_text,
    }


# --------------------------------------------------------------------------- gateway


class Gateway:
    def __init__(self, url: str, key: str) -> None:
        self.url, self.key = url.rstrip("/"), key

    def get(self, path: str) -> Any:
        request = urllib.request.Request(
            self.url + path, headers={"Authorization": f"Bearer {self.key}"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def activity(self, since: float, until: float) -> dict[str, Any]:
        sessions = []
        for item in self.get("/v1/kiro/sessions")["data"]:
            if (item.get("updated") or 0) < since - 2:
                continue
            records = [
                r
                for r in self.get(f"/v1/kiro/sessions/{item['session_id']}/audit")["records"]
                if since - 2 <= r["time"] <= until + 5
            ]
            turns = [r for r in records if r["kind"] == "turn"]
            if not turns:
                continue
            sessions.append(
                {
                    "id": item["session_id"][:8],
                    "created_in_run": item["created"] >= since - 2,
                    "mode": turns[0].get("mode"),
                    "model": turns[0].get("model"),
                    "turns": len(turns),
                    "reused_turns": sum(1 for t in turns if t.get("reused")),
                    "client_tool_calls": [
                        r.get("name") for r in records if r["kind"] == "harness_tool_call"
                    ],
                    "kiro_tools": [
                        f"{r.get('title') or r.get('tool')}"
                        for r in records
                        if r["kind"] == "tool_call"
                    ],
                    "permissions": [
                        f"{r.get('title') or r.get('tool')}: {r.get('decision')}"
                        for r in records
                        if r["kind"] == "permission"
                    ],
                    "turn_ends": [r.get("stop") for r in records if r["kind"] == "turn_end"],
                }
            )
        turns = sum(s["turns"] for s in sessions)
        reused = sum(s["reused_turns"] for s in sessions)
        return {
            "sessions": sessions,
            "kiro_sessions_started": sum(1 for s in sessions if s["created_in_run"]),
            "turns": turns,
            "reused_turns": reused,
        }


# --------------------------------------------------------------------------- checks


def uses(run: Run, name: str, *, sub: bool | None = None) -> list[dict]:
    return [
        u
        for u in run.transcript.get("tool_uses", [])
        if u["name"] == name and (sub is None or u["sub"] == sub)
    ]


def evaluate(run: Run) -> None:
    s, t, answer = (
        run.scenario,
        run.transcript,
        (run.result or run.transcript.get("final_text", "")).strip(),
    )
    checks = run.checks
    checks.append(
        Check(
            "finished without error",
            not run.error and not t.get("api_errors"),
            run.error or "; ".join(t.get("api_errors", []))[:300],
        )
    )
    failed_tools = [
        f"{u['name']}: {u['result']['text'][:160]}"
        for u in t.get("tool_uses", [])
        if u.get("result") and u["result"]["error"]
    ]
    checks.append(Check("no failed tool calls", not failed_tools, " | ".join(failed_tools)[:400]))
    if s.name == "seq_tools":
        checks.append(
            Check(
                "answer is 'one two'",
                answer.splitlines()[-1:] == ["one two"] if answer else False,
                answer[:120],
            )
        )
        checks.append(
            Check(
                "six Bash calls",
                len(uses(run, "Bash")) == 6,
                f"{len(uses(run, 'Bash'))} Bash calls",
            )
        )
    elif s.name == "parallel_edit":
        config = (run.workdir / "config.ini").read_text()
        checks.append(Check("answer is 50", "50" in answer, answer[:120]))
        checks.append(
            Check(
                "config.ini edited exactly",
                config == "[app]\nmode = final\nretries = 3\n",
                repr(config),
            )
        )
        checks.append(
            Check(
                "Reads issued in parallel",
                t.get("max_parallel", 0) >= 2,
                f"largest batch: {t.get('max_parallel')} tool calls",
            )
        )
        checks.append(Check("Edit tool used", bool(uses(run, "Edit")), ""))
    elif s.name == "subagent":
        checks.append(Check("answer is 7", re.search(r"\b7\b", answer) is not None, answer[:120]))
        checks.append(Check("Agent tool used", bool(uses(run, "Agent") or uses(run, "Task")), ""))
        checks.append(
            Check(
                "subagent ran its own tools",
                bool([u for u in t.get("tool_uses", []) if u["sub"]]),
                ", ".join(u["name"] for u in t.get("tool_uses", []) if u["sub"]),
            )
        )
    elif s.name == "web_search":
        calls = uses(run, "WebSearch")
        result = calls[0]["result"]["text"] if calls and calls[0].get("result") else ""
        links = re.findall(r"https?://[^\s\"')\]]+", result)
        checks.append(Check("WebSearch tool called", bool(calls), ""))
        checks.append(Check("search returned links", bool(links), result[:300].replace("\n", " ")))
        checks.append(Check("answer names iana.org", "iana.org" in answer, answer[:160]))
    elif s.name == "web_fetch":
        calls = uses(run, "WebFetch")
        checks.append(Check("WebFetch tool called", bool(calls), ""))
        checks.append(Check("answer is 'Example Domain'", "Example Domain" in answer, answer[:160]))
    elif s.name == "ask_user":
        calls = uses(run, "AskUserQuestion")
        options = []
        if calls:
            with contextlib.suppress(Exception):
                options = [o.get("label") for o in calls[0]["input"]["questions"][0]["options"]]
        fruit = run.workdir / "fruit.txt"
        content = fruit.read_text().strip() if fruit.exists() else None
        answered = calls[0]["result"]["text"] if calls and calls[0].get("result") else ""
        checks.append(
            Check(
                "AskUserQuestion called with the three options",
                options == ["Apple", "Banana", "Cherry"],
                f"options={options}",
            )
        )
        checks.append(
            Check("option picker shown", "question" in run.screens and not run.error, run.error)
        )
        checks.append(
            Check(
                "answer delivered to the model",
                "Banana" in answered,
                answered[:200].replace("\n", " "),
            )
        )
        checks.append(Check("fruit.txt is 'Banana'", content == "Banana", repr(content)))


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", default=os.environ.get("ANTHROPIC_BASE_URL", ""))
    parser.add_argument("--key-file", default=str(Path.home() / ".kiro-gw-key"))
    parser.add_argument("--claude", default=os.environ.get("CLAUDE_CMD", "claude-gw"))
    parser.add_argument("--only", default="")
    parser.add_argument("--out", default=f"/tmp/kgw-e2e-{time.strftime('%H%M%S')}")
    args = parser.parse_args()
    if not args.gateway:
        parser.error("--gateway is required (or set ANTHROPIC_BASE_URL)")
    claude = shutil.which(args.claude) or args.claude
    gateway = Gateway(args.gateway, Path(args.key_file).expanduser().read_text().strip())
    wanted = {n for n in args.only.split(",") if n}
    out = Path(args.out)
    runs = []
    for scenario in SCENARIOS:
        if wanted and scenario.name not in wanted:
            continue
        workdir = out / scenario.name.replace("_", "-")
        for rel, body in scenario.files.items():
            (workdir / rel).parent.mkdir(parents=True, exist_ok=True)
            (workdir / rel).write_text(body)
        workdir.mkdir(parents=True, exist_ok=True)
        run = Run(scenario, workdir)
        print(f"--- {scenario.name}: {scenario.feature}", flush=True)
        run.started = time.time()
        try:
            (run_interactive if scenario.interactive else run_headless)(run, claude)
        except Exception as error:  # report and carry on with the next scenario
            run.error = f"{type(error).__name__}: {error}"
        run.finished = time.time()
        time.sleep(3)
        try:
            run.transcript = read_transcript(run)
        except Exception as error:
            run.error = run.error or f"transcript: {error}"
        try:
            run.gateway = gateway.activity(run.started, run.finished)
        except Exception as error:
            run.gateway = {"error": str(error)}
        evaluate(run)
        runs.append(run)
        report(run)
    summary = [
        {
            "scenario": r.scenario.name,
            "feature": r.scenario.feature,
            "seconds": round(r.finished - r.started, 1),
            "passed": all(c.ok for c in r.checks),
            "checks": [c.__dict__ for c in r.checks],
            "answer": (r.result or r.transcript.get("final_text", ""))[:300],
            "requests": r.transcript.get("requests"),
            "tool_uses": [
                {k: v for k, v in u.items() if k != "input"}
                for u in r.transcript.get("tool_uses", [])
            ],
            "gateway": r.gateway,
            "screens": r.screens,
            "error": r.error,
        }
        for r in runs
    ]
    (out / "results.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, default=str)
    )
    print(f"\nresults: {out / 'results.json'}")
    return 0 if all(item["passed"] for item in summary) else 1


def report(run: Run) -> None:
    t, g = run.transcript, run.gateway
    requests = t.get("requests", [])
    with_system = sum(1 for r in requests if r["system"])
    print(
        f"    {'PASS' if all(c.ok for c in run.checks) else 'FAIL'} in {run.finished - run.started:.0f}s"
    )
    for check in run.checks:
        print(
            f"      [{'x' if check.ok else ' '}] {check.name}{': ' + check.detail if check.detail and not check.ok else ''}"
        )
    tools = [u["name"] + ("(sub)" if u["sub"] else "") for u in t.get("tool_uses", [])]
    print(
        f"      Claude Code: {len(requests)} API requests, {with_system} carried a new system message; tools: {', '.join(tools) or '-'}"
    )
    if "error" in g:
        print(f"      gateway: {g['error']}")
    else:
        print(
            f"      gateway: {g['kiro_sessions_started']} Kiro sessions started, {g['turns']} turns, {g['reused_turns']} reused"
        )
        for s in g["sessions"]:
            extra = "; ".join(
                x
                for x in (
                    "client tools: " + ",".join(s["client_tool_calls"])
                    if s["client_tool_calls"]
                    else "",
                    "Kiro tools: " + ",".join(s["kiro_tools"]) if s["kiro_tools"] else "",
                    "permissions: " + ",".join(s["permissions"]) if s["permissions"] else "",
                )
                if x
            )
            print(
                f"        {s['id']} {s['mode']:7} {s['model']:18} turns={s['turns']} reused={s['reused_turns']} {extra}"
            )


if __name__ == "__main__":
    sys.exit(main())
