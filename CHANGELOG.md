# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.6] - 2026-11-21

### Fixed

- 0.1.5 missed the progress label seen in a live run, because the request does not end
  with the prompt. Claude Code appends its notices after it (`role: "system"` messages
  with the environment and the deferred-tool list), and a label forked before the
  subagent's first reply carries the prompt as an extra text block of the subagent's
  opening message. The prompt is now looked for in the last message that is not a system
  notice, and only that block is marked as the aside; the rest of the message and the
  notices are sent as they are. On 0.1.5 that label ran as a normal turn (56 s, 1.05
  credits) and its session stayed in the pool.

## [0.1.5] - 2026-11-21

### Fixed

- While a background subagent runs, Claude Code asks for a 3-5 word progress label every
  30 s (`querySource: "agent_summary"`): a fork of the subagent's whole conversation, or a
  short note about it. Each one started a new `kiro-cli`, replayed the subagent's
  conversation, and then stayed in the session pool, where it was never used again. In 26
  minutes of live use with one subagent that was 35 extra sessions. They filled the pool
  (`KIRO_GATEWAY_MAX_SESSIONS`, 8) and pushed out the main conversation's session while it
  sat idle for ten minutes, so its next step started a new `kiro-cli` and replayed about
  1 MB of conversation. The label is now recognized as a side request, and a session
  started just for it is closed after the reply. Each label still costs a `kiro-cli` start
  and a replay (0.4 credits at first, 3 per label once the subagent's conversation had
  grown); `CLAUDE_CODE_DISABLE_AGENT_PROGRESS_SUMMARIES=1` on the client turns them off.
- A side request no longer continues a Kiro turn that is waiting for tool results. A label
  forked just after a subagent's tools returned carries the same results as the subagent's
  next request; arriving first, it would have handed them to the subagent's blocked turn.
- Side-request prompts are also found beside tool results and in any text block of the
  last message, not only as a plain last user message.
- A session closed for idleness or to make room in the pool left its MCP tool bridge
  (with the client's tool list) registered with the broker until the gateway stopped.
  Every way of closing a session now drops it.

### Added

- Audit `turn` records carry `side_request` for side requests.
- A log line when a session is closed to make room in the pool
  (`KIRO_GATEWAY_MAX_SESSIONS`) or because another session took over its conversation;
  both used to be silent.

### Added (tests)

- `scripts/e2e_claude_code.py` scenario `recap`: three prompts, then the terminal is
  reported unfocused until Claude Code asks for its recap (three minutes after the last
  reply), then a fourth prompt. It passes when one Kiro session served every turn; against
  0.1.4 on the live gateway one session served all six turns.

## [0.1.4] - 2026-11-21

### Fixed

- Claude Code's session recap (sent after the user has been away a few minutes, then
  discarded by the client) re-filed the Kiro session under the throwaway exchange, so the
  user's next message started a new `kiro-cli` and replayed the conversation; observed
  twice in 45 minutes of live use. The recap is now recognized
  (`Conversation.side_request`), sent to Kiro marked as an aside, and the session stays
  filed under the conversation it continued. A recap never displaces a live session, and
  one that calls tools is closed instead of leaving a turn open.

## [0.1.3] - 2026-11-21

### Fixed (session reuse with Claude Code)

Found by running Claude Code 2.1.329 against a live gateway (`scripts/e2e_claude_code.py`):
in a tool loop most requests started a new `kiro-cli` and replayed the whole conversation.

- `role: "system"` messages in the middle of `messages` (Claude Code's working-directory
  updates and reminders, OpenAI `system`/`developer` messages) stay in place instead of
  being merged into the system prompt, which changed the session fingerprint on every
  notice. Kiro receives them as `<message role="system">`, or appended to the last tool
  result as `[System notice]` when a tool loop continues.
- The fingerprint counts tool calls by name only: Claude Code echoes Edit calls back with
  `"replace_all": false` added, so every step after an Edit used to miss.
- In MCP tool mode a session whose tool list changed (deferred tools loaded through
  ToolSearch) is updated in place: the bridge sends `notifications/tools/list_changed` and
  Kiro re-reads the list, also while a call is pending (verified on kiro-cli 3.5.0, v2 and
  v3 engines). Emulated-tools sessions still need an identical tool set.
- A client disconnecting mid-stream no longer leaves `kiro-cli` running and the turn
  listed as active: the cleanup is no longer cancelled half-way.
- Agent exit errors carry the exit status (`exit status: None` when stdout closed before
  the process was reaped; this also failed a test on Python 3.11).

### Added (tests)

- `scripts/e2e_claude_code.py`: runs real Claude Code through the gateway (sequential and
  parallel tool calls, Edit, a subagent, WebSearch, WebFetch, and the AskUserQuestion option
  picker driven in a pseudo-terminal), checks the outcomes, and reports how many Kiro
  sessions each scenario used.

Monitoring and logging, with no new dependencies. See the README sections "Metrics" and
"Logging" for the series, the formats, and the measured cost.

### Added

- Request ids: every response carries `X-Request-ID` (the client's own when it is a plain
  token, else a generated `req_…`), and every log line names the request it came from.
- Access log (`KIRO_GATEWAY_ACCESS_LOG`, on by default): one line per request, written
  when the response ends, with status, duration, bytes in and out, client, user agent, and
  `incomplete` / `failed=` markers. It replaces uvicorn's access log; uvicorn's other lines
  use the gateway format. Health checks, scrapes, and dashboard polling are logged at
  `debug` level.
- `KIRO_GATEWAY_LOG_FORMAT=json` / `--log-format json`: one JSON object per line.
- Log records are written by a background thread in batches, so a slow or stuck log
  consumer cannot stall requests. Records beyond 10,000 waiting are dropped and counted
  (`kiro_gateway_log_records_dropped_total`).
- A log line when a `kiro-cli` process starts (pid, engine, session, start time) and when
  a turn ends (finish reason, duration, model, credits, error).
- Metrics: HTTP requests, durations, and in-flight count by route template; queue wait,
  queued requests, free turn slots, and configured concurrency; `kiro-cli` start time;
  running `kiro-cli` processes; event-loop lag; build info; and the standard `process_*`
  series (CPU, resident memory on Linux, open and maximum file descriptors, start time).
- Dashboard: a System panel (uptime, memory, CPU, open files, loop lag, Kiro processes,
  queued and in-flight requests) and an HTTP requests panel. `/v1/kiro/stats` has a new
  `system` section and HTTP, queue-wait, and session-start figures.
- `deploy/docker-compose.yml` caps the container log at 5 × 10 MB
  (`KIRO_GW_LOG_MAX_SIZE`, `KIRO_GW_LOG_MAX_FILE`).

### Fixed

- `kiro_gateway_errors_total` missed every error delivered inside an SSE stream, which is
  most errors from Claude Code and Codex. Streamed errors are now counted with the same
  code and status as the non-streaming response, and 5xx stream errors are logged. 422
  validation errors and unhandled 500s are counted too.
- Turns that ended without a reply were missing from `kiro_gateway_turns_total`. They are
  now recorded as `finish="aborted"` (the client disconnected, or shutdown) or
  `finish="error"` (an exception).
- Log lines from a pooled `kiro-cli` process's reader tasks (permission decisions,
  stderr) no longer carry the id of the request that happened to start the process.
  Permission lines now name the session.
- A client-supplied `X-Request-ID` is no longer written to the log unchecked.

## [0.1.2] - 2026-09-18

Fixes for the six findings of the 2026-09-18 compatibility review
(`docs/COMPATIBILITY_REVIEW_2026-09-18.md`), each with a regression test:

### Fixed

- Session pool: a session displaced by another conversation with the same prefix is now
  closed instead of orphaned, and a session blocked on tool calls is only reused by a
  request carrying its own call ids (finding 1).
- Responses streaming: reasoning, message, and tool-call items keep distinct, stable
  `output_index` values with paired lifecycle events; verified with the OpenAI SDK's
  stream assembler (finding 2).
- MCP tool mode now enforces stop sequences and `max_tokens`, and validates structured
  output (reported, not re-prompted) like the emulated mode (finding 3).
- `tool_choice` is enforced: named and `allowed_tools` restrict the tools Kiro sees,
  `required` / `any` fail with `502 tool_choice_unsatisfied` when the model answers in
  text; both tool modes and all three APIs (finding 4).
- SSE keepalive wrapper awaits its cancelled iterator task before closing the source,
  removing an "asynchronous generator is already running" error on disconnect (finding 5).
- Images in Anthropic `tool_result` blocks are delivered to Kiro as MCP image content, and
  text sent beside tool results is forwarded (finding 6).

### Added

- SDK contract tests: the OpenAI and Anthropic Python SDKs' stream assemblers run against
  the fake agent, and the tool tests cover both tool modes.

## [0.1.1] - 2026-09-18

### Fixed

- Dashboard: clicking a session in "Recent sessions" opened the audit endpoint as a plain
  navigation, which cannot carry the API key and answered `401`. Records now open in a
  side panel inside the dashboard, fetched with the stored key, with a summary and the
  redacted details of every turn, tool call, permission decision, and stall.

### Added

- `kiro-gateway --env-file PATH`, and automatic loading of `~/.config/kiro-gateway/.env`
  when the current directory has no `.env`, so a globally installed gateway can be started
  from any directory with one configuration file.
- README: a numbered global-install procedure (`uv tool install .`, PATH check, config
  file, verification, update, uninstall), a "Permissions in agent mode" section with
  ready-to-copy `.env` configurations and the rule syntax, and an explanation of how the
  gateway's policy layers on Kiro's own `permissions.yaml` in agent and harness mode.

### Changed

- Agent-mode callers (scripts, curl, the SDKs) are now shown naming their own project
  with `X-Kiro-Workspace`, and the gateway examples start with
  `KIRO_GATEWAY_ALLOWED_WORKSPACES` plus a scratch fallback workspace instead of the
  gateway's checkout. All client examples send the header; the CLI's `--cwd` is
  documented. Harness clients are unaffected.
- The README start example and `.env.example` use `KIRO_GATEWAY_PERMISSIONS=allow-always`
  and explain what `deny` actually does.
- The `legacy/` proof-of-concept scripts were removed; they remain in the git history.

## [0.1.0] - 2026-09-17

First release. Verified against Kiro CLI 2.22 on macOS with Claude Code, Codex CLI,
OpenCode, Collomia, the OpenAI and Anthropic Python SDKs, curl, and plain `requests`.

### Library (`kiro_acp.acp`)

- ACP client for `kiro-cli acp`: JSON-RPC over stdio, both Kiro engines (v2 Rust CLI and
  v3 Kiro Agent Server), sessions as event streams, permission policies and rules, client
  file-system and terminal capabilities, session listing and deletion.
- Engine-aware model, mode and effort control (v3 config options; v2 `session/set_model`
  and effort through `_kiro.dev/commands/execute`), cold-start catalogue retry, per-session
  process groups, and an ACP frame recorder with a replay agent for tests.

### CLI (`kiro-acp`)

- `prompt`, `chat`, `models`, `agents`, `sessions` (list, delete, prune), `info`,
  `doctor` (engine checks, orphan detection) and `codex-catalog`.

### Gateway (`kiro-gateway`)

- OpenAI Chat Completions, OpenAI Responses (including freeform `custom` tools), legacy
  Completions and Anthropic Messages APIs, streaming and non-streaming, with images,
  effort, stop sequences, `max_tokens`, structured output with schema validation, model
  aliases and a model catalogue with context windows.
- Two modes chosen per request: harness mode bridges the client's tools to Kiro as native
  MCP tool calls (or an emulated text protocol) and runs Kiro in an empty scratch
  directory; agent mode lets Kiro use its own tools inside the configured workspace.
- Harness support verified for Claude Code (OAuth-token pitfall documented), Codex CLI
  (direct tools and code mode; the Codex model catalogue is served from the gateway),
  OpenCode and any OpenAI-compatible client.
- Session affinity with a bounded pool, per-request workspaces (allow-listed), per-request
  MCP servers from a catalogue or discovered client config files, inline agent
  definitions on the v3 engine, and a `kiro` body extension for all per-request options.
- Reliability: SSE keepalives, queue and rate limits, graceful shutdown, classified Kiro
  errors with `Retry-After`, refusal surfacing, image size guard, stall detection with a
  continue-nudge, and never regressing the model catalogue on an empty answer.
- Operations: Prometheus `/metrics`, a self-contained live dashboard with light and dark
  themes, an audit ledger with secret redaction, health endpoint, Docker/Podman image,
  launchd and systemd installers, GitHub Actions CI, and ready-to-use client examples.

[0.1.2]: https://github.com/robert-mcdermott/kiro-acp-gateway/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/robert-mcdermott/kiro-acp-gateway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/robert-mcdermott/kiro-acp-gateway/releases/tag/v0.1.0
