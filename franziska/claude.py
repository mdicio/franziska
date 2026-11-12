"""Claude as a model for Franziska's agents, on the user's Claude subscription (Claude Code's own login).

Claude Code runs its own agent loop; Franziska's agents (pydantic-ai) run theirs, with the guards, proposals and privacy
views around every tool call. This adapter makes Claude Code a plain pydantic-ai model, the way pi-claude-bridge
(github.com/elidickinson/pi-claude-bridge) does for pi:

- one Claude Code process per agent run: no built-in tools (`tools=[]`), no settings files, hooks, CLAUDE.md or MCP
  servers but ours, nothing written to ~/.claude/projects (`--no-session-persistence`);
- the run's tools are served to it as an in-process MCP server. When Claude calls one, the call is held open and goes
  back to pydantic-ai as an ordinary tool call; pydantic-ai runs the tool (guards and all) and its next request carries
  the result, which releases the held call (paired by Claude Code's `claudecode/toolUseId`), and the same process
  goes on. A guard's retry of the final reply is a new message in the same session.
- a request that doesn't continue a live run (the first of a run, or one whose process is gone) starts a new process
  with the earlier conversation as a transcript in the prompt.

Billing: the subscription only. The API variables are blanked for the child, and a process whose init frame reports
an API key source other than 'none' is refused (ModelAPIError, so the route's local fallback answers). Claude Code
prices every response in `total_cost_usd`; on a subscription that is notional, so no cost is logged.

Probed live 2026-11-02 (Claude Code 2.1.316, SDK 0.2.176): the PreToolUse 'defer' route was the alternative, but with
two parallel calls only one came back, and resuming without a new prompt failed (`tool_deferred_unavailable`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, RateLimitEvent, ResultMessage, StreamEvent,
                              SystemMessage, query)
from claude_agent_sdk.types import McpSdkServerConfig
from mcp.server import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic_ai._instrumentation import get_instructions
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import (ModelMessage, ModelRequest, ModelResponse, RetryPromptPart, SystemPromptPart,
                                  TextPart, ToolCallPart, ToolReturnPart, UserPromptPart)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

log = logging.getLogger(__name__)

SERVER = "franziska"
PREFIX = f"mcp__{SERVER}__"
TOOL_USE_ID = "claudecode/toolUseId"  # Claude Code stamps every MCP tools/call with the tool_use id it came from
# The child's environment: only the claude.ai login can authenticate (an exported key or gateway would win over it),
# no claude.ai cloud connectors, no deferred tool loading (every tool is in the request, as with the local model), and
# no MCP timeout of its own: a held call lasts as long as Franziska's tool does.
CHILD_ENV = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_BASE_URL": "",
             "CLAUDE_CODE_USE_BEDROCK": "", "CLAUDE_CODE_USE_VERTEX": "", "CLAUDE_CODE_USE_FOUNDRY": "",
             "ENABLE_CLAUDEAI_MCP_SERVERS": "0", "ENABLE_TOOL_SEARCH": "false", "MCP_TOOL_TIMEOUT": "86400000",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
# How long a run waits for its next request before its process is closed. Held on a tool: as long as the slowest
# tool (a delegation to the local model, run_python); after the final reply: long enough for a guard's retry.
IDLE_ON_TOOL_S = 900.0
IDLE_DONE_S = 30.0
PROFILE = ModelProfile(supports_tools=True, supports_json_schema_output=True, supports_json_object_output=False,
                       default_structured_output_mode="native")


class ClaudeError(ModelAPIError):
    """Claude Code failed, refused, hit the plan's limit or isn't signed in: the route's local fallback answers."""


@dataclass
class _Block:
    kind: str  # text | thinking | tool_use | ...
    text: str = ""
    id: str = ""
    name: str = ""
    args: dict[str, Any] | None = None


@dataclass
class _Step:
    """One assistant message of Claude Code's loop, from the raw API stream."""
    blocks: list[_Block]
    stop_reason: str | None
    usage: RequestUsage


@dataclass
class _Run:
    """One agent run: a Claude Code process, its held tool calls and what it has said so far."""
    key: str
    run_id: str | None
    native: bool
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue)  # follow-up user messages; None closes stdin
    out: asyncio.Queue = field(default_factory=asyncio.Queue)  # ('step'|'result'|'error', item)
    results: dict[str, asyncio.Future] = field(default_factory=dict)  # tool_use id → its CallToolResult
    by_args: dict[str, list[str]] = field(default_factory=dict)  # name + args → ids, when a call has no id in _meta
    returned: list[str] = field(default_factory=list)  # tool_use ids handed to pydantic-ai, awaiting their results
    state: str = "starting"  # starting | waiting (on Claude) | tools (held calls) | done (replied) | closed
    usage: list[RequestUsage] = field(default_factory=list)  # steps not yet reported in a response
    task: asyncio.Task | None = None
    idle: asyncio.TimerHandle | None = None
    model: str = ""

    def result(self, tool_use_id: str) -> asyncio.Future:
        if tool_use_id not in self.results:
            self.results[tool_use_id] = asyncio.get_running_loop().create_future()
        return self.results[tool_use_id]

    def release(self, why: str) -> None:
        """Answer every held call, so no tools/call stays open in a process that is going away."""
        for future in self.results.values():
            if not future.done():
                future.set_result(_text_result(why, error=True))


def _text_result(text: str, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=error)


def _key(name: str, args: dict | None) -> str:
    return name + json.dumps(args or {}, sort_keys=True, ensure_ascii=False)


def _content(content: str | Sequence[Any]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(c if isinstance(c, str) else f"[{type(c).__name__} not shown]" for c in content)


def _tool_text(part: ToolReturnPart | RetryPromptPart) -> tuple[str, bool]:
    if isinstance(part, RetryPromptPart):
        return part.model_response(), True
    return part.model_response_str(), False


def transcript(messages: Sequence[ModelMessage]) -> str:
    """Earlier messages as text, for a run that starts a new process: Claude Code can't take pydantic-ai's history."""
    lines: list[str] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    lines.append(f"User: {_content(p.content)}")
                elif isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name:
                    text, error = _tool_text(p)
                    lines.append(f"Tool {p.tool_name} {'failed' if error else 'returned'}: {text}")
                elif isinstance(p, RetryPromptPart):
                    lines.append(f"(Retry: {p.model_response()})")
        elif isinstance(m, ModelResponse):
            for p in m.parts:
                if isinstance(p, TextPart) and p.content.strip():
                    lines.append(f"Assistant: {p.content}")
                elif isinstance(p, ToolCallPart):
                    lines.append(f"Assistant called {p.tool_name} {p.args_as_json_str()}")
    return "\n".join(lines)


def _system(messages: Sequence[ModelMessage], params: ModelRequestParameters) -> str:
    parts = [p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts
             if isinstance(p, SystemPromptPart)]
    return "\n\n".join(filter(None, [*parts, get_instructions(messages, params)])) or "You are a helpful assistant."


def _first_prompt(messages: Sequence[ModelMessage]) -> str:
    last = messages[-1]
    asked = [_content(p.content) for p in last.parts if isinstance(p, UserPromptPart)] if isinstance(
        last, ModelRequest) else []
    earlier = transcript(messages[:-1] if asked else messages)
    now = "\n\n".join(asked) or "Continue from the tool results above."
    if not earlier:
        return now
    return f"<conversation_so_far>\n{earlier}\n</conversation_so_far>\n\n{now}"


async def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}


class ClaudeCodeModel(Model):
    """Claude through Claude Code on the subscription. `model`: an alias ('sonnet', 'opus', 'haiku') or a model id."""

    def __init__(self, model: str = "sonnet", *, effort: str | None = None, timeout_s: float = 300.0,
                 cwd: Path | None = None, cli_path: str | None = None):
        super().__init__(profile=PROFILE)
        self._model, self.effort, self.timeout_s = model, effort or None, timeout_s
        self.cwd, self.cli_path = cwd, cli_path
        self._runs: dict[str, _Run] = {}

    @property
    def model_name(self) -> str:
        return f"claude-code:{self._model}"

    @property
    def system(self) -> str:
        return "anthropic"

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        model_settings, params = self.prepare_request(model_settings, model_request_parameters)
        timeout = (model_settings or {}).get("timeout") or self.timeout_s
        if not isinstance(timeout, int | float):
            timeout = self.timeout_s
        started = time.monotonic()
        run = self._continuing(messages)
        fresh = run is None
        if run is None:
            run = await self._start(messages, params)
        try:
            response = await self._next(run, params, float(timeout))
            log.debug("claude %s: %s step in %.1f s, %d in / %d out (%d cached)", run.key[:8],
                      "first" if fresh else "next", time.monotonic() - started, response.usage.input_tokens,
                      response.usage.output_tokens, response.usage.cache_read_tokens)
            return response
        except BaseException:
            self._close(run)
            raise

    # ---- which run a request belongs to ----------------------------------------------------------------------------

    def _continuing(self, messages: list[ModelMessage]) -> _Run | None:
        """The live run this request continues: its last response came from that run, in the same agent run."""
        last = messages[-1] if messages else None
        prev = next((m for m in reversed(messages[:-1]) if isinstance(m, ModelResponse)), None)
        key = (prev.provider_details or {}).get("claude_run") if prev else None
        run = self._runs.get(key) if key else None
        if run is None:
            return None
        if (not isinstance(last, ModelRequest) or run.state not in ("tools", "done")
                or (last.run_id and run.run_id and last.run_id != run.run_id)):
            # A later agent run on the same history (its instructions may differ), or a request this run can't
            # take: a new process answers, and this one goes now rather than at its idle timeout.
            self._close(run)
            return None
        if run.idle:
            run.idle.cancel()
        if run.state == "tools":
            answered = {p.tool_call_id: p for p in last.parts if isinstance(p, ToolReturnPart | RetryPromptPart)
                        and p.tool_call_id in run.returned}
            if not answered:
                self._close(run)
                return None
            for tool_use_id in run.returned:
                part = answered.get(tool_use_id)
                text, error = _tool_text(part) if part else ("Not run.", True)
                future = run.result(tool_use_id)
                if not future.done():
                    future.set_result(_text_result(text, error))
            extra = [_content(p.content) for p in last.parts if isinstance(p, UserPromptPart)]
            extra += [p.model_response() for p in last.parts if isinstance(p, RetryPromptPart)
                      and p.tool_call_id not in answered]
            if extra:  # rare: something besides the results (a capability's note); it follows as a message
                run.inbox.put_nowait("\n\n".join(extra))
            run.returned = []
        else:  # done: the final reply was refused by a guard or an output validator, say why in the same session
            follow = [p.model_response() if isinstance(p, RetryPromptPart) else _content(p.content)
                      for p in last.parts if isinstance(p, RetryPromptPart | UserPromptPart)]
            if not follow:
                self._close(run)
                return None
            run.inbox.put_nowait("\n\n".join(follow))
        run.state = "waiting"
        return run

    async def _start(self, messages: list[ModelMessage], params: ModelRequestParameters) -> _Run:
        last = messages[-1] if messages else None
        native = params.output_mode == "native" and params.output_object is not None
        run = _Run(key=uuid.uuid4().hex, run_id=getattr(last, "run_id", None), native=native)
        tools = [*params.function_tools, *params.output_tools]
        options = ClaudeAgentOptions(
            model=self._model,
            tools=[],
            system_prompt=_system(messages, params),
            mcp_servers={SERVER: self._server(run, tools)} if tools else {},
            allowed_tools=[PREFIX + t.name for t in tools],
            strict_mcp_config=True,
            setting_sources=[],
            permission_mode="dontAsk",
            include_partial_messages=True,
            output_format={"type": "json_schema", "schema": params.output_object.json_schema} if native else None,
            effort=self.effort,
            env=CHILD_ENV,
            cwd=self.cwd,
            cli_path=self.cli_path,
            extra_args={"no-session-persistence": None},
            stderr=lambda line: log.debug("claude code: %s", line.rstrip()),
        )
        self._runs[run.key] = run
        run.task = asyncio.create_task(self._own(run, options, _first_prompt(messages)), name=f"claude {run.key[:8]}")
        return run

    # ---- the process --------------------------------------------------------------------------------------------

    def _server(self, run: _Run, tools: list) -> McpSdkServerConfig:
        """The run's tools as an MCP server. Calls are held until pydantic-ai has run them (see _continuing).
        Arguments aren't validated here: pydantic-ai validates them, and a refusal here would leave its own call
        unanswered."""
        wire = [Tool(name=t.name, description=t.description or t.name, inputSchema=t.parameters_json_schema)
                for t in tools]

        async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
            return ListToolsResult(tools=wire)

        async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
            meta = params.meta or {}
            tool_use_id = meta.get(TOOL_USE_ID) if isinstance(meta, dict) else getattr(meta, TOOL_USE_ID, None)
            if not tool_use_id:  # pair by name and arguments instead; the stream registered them first
                for _ in range(50):
                    ids = run.by_args.get(_key(params.name, params.arguments))
                    if ids:
                        tool_use_id = ids.pop(0)
                        break
                    await asyncio.sleep(0.1)
                else:
                    return _text_result("Internal error: this call could not be matched; call it again.", True)
            return await run.result(tool_use_id)

        server = Server(SERVER, version="1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        return McpSdkServerConfig(type="sdk", name=SERVER, instance=server)

    async def _prompts(self, run: _Run, first: str) -> AsyncIterator[dict]:
        yield await _user(first)
        while (text := await run.inbox.get()) is not None:
            yield await _user(text)

    async def _own(self, run: _Run, options: ClaudeAgentOptions, first: str) -> None:
        """The process's lifetime, in one task: read its messages until its input closes."""
        current: dict[int, _Block] = {}
        partial: dict[int, str] = {}
        usage = RequestUsage()
        stop: str | None = None
        try:
            async for msg in query(prompt=self._prompts(run, first), options=options):
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    source = msg.data.get("apiKeySource")
                    run.model = msg.data.get("model") or ""
                    if source not in (None, "none"):
                        raise ClaudeError(self.model_name, f"Claude Code would bill an API key ({source}), not the "
                                                           "subscription: refused. Unset the key for the bot.")
                elif isinstance(msg, StreamEvent) and msg.parent_tool_use_id is None:
                    e = msg.event
                    kind = e.get("type")
                    if kind == "message_start":
                        current, partial, stop = {}, {}, None
                        u = e.get("message", {}).get("usage") or {}
                        usage = RequestUsage(
                            input_tokens=(u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                            + (u.get("cache_creation_input_tokens") or 0),
                            cache_read_tokens=u.get("cache_read_input_tokens") or 0,
                            cache_write_tokens=u.get("cache_creation_input_tokens") or 0)
                    elif kind == "content_block_start":
                        b = e.get("content_block", {})
                        current[e["index"]] = _Block(b.get("type", ""), text=b.get("text", "") or "",
                                                     id=b.get("id", ""), name=b.get("name", ""))
                    elif kind == "content_block_delta" and e["index"] in current:
                        d, block = e.get("delta", {}), current[e["index"]]
                        if d.get("type") == "text_delta":
                            block.text += d.get("text", "")
                        elif d.get("type") == "thinking_delta":
                            block.text += d.get("thinking", "")
                        elif d.get("type") == "input_json_delta":
                            partial[e["index"]] = partial.get(e["index"], "") + d.get("partial_json", "")
                    elif kind == "content_block_stop" and e["index"] in current:
                        block = current[e["index"]]
                        if block.kind == "tool_use":
                            try:
                                block.args = json.loads(partial.get(e["index"]) or "{}")
                            except json.JSONDecodeError:
                                block.args = {}
                            run.by_args.setdefault(_key(block.name.removeprefix(PREFIX), block.args),
                                                   []).append(block.id)
                    elif kind == "message_delta":
                        stop = e.get("delta", {}).get("stop_reason") or stop
                        usage.output_tokens = (e.get("usage") or {}).get("output_tokens") or usage.output_tokens
                    elif kind == "message_stop":
                        blocks = [current[i] for i in sorted(current)]
                        run.out.put_nowait(("step", _Step(blocks, stop, usage)))
                        current, partial = {}, {}
                elif isinstance(msg, AssistantMessage) and msg.error and msg.parent_tool_use_id is None:
                    log.warning("claude code: assistant error %s", msg.error)
                elif isinstance(msg, RateLimitEvent) and msg.rate_limit_info.status != "allowed":
                    log.warning("claude code: plan limit %s (%s)", msg.rate_limit_info.status,
                                msg.rate_limit_info.rate_limit_type)
                elif isinstance(msg, ResultMessage):
                    run.out.put_nowait(("result", msg))
            run.out.put_nowait(("error", ClaudeError(self.model_name, "Claude Code ended unexpectedly")))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # not signed in, a CLI crash, a refused key: the route's fallback answers
            log.warning("claude code run failed: %s", e)
            run.out.put_nowait(("error", e if isinstance(e, ModelAPIError) else ClaudeError(self.model_name, str(e))))
        finally:
            run.state = "closed"
            run.release("The session ended.")
            self._runs.pop(run.key, None)

    # ---- responses ----------------------------------------------------------------------------------------------

    async def _next(self, run: _Run, params: ModelRequestParameters, timeout: float) -> ModelResponse:
        """Wait for the next thing pydantic-ai must act on: tool calls of ours, or the final reply."""
        final: _Step | None = None
        while True:
            try:
                kind, item = await asyncio.wait_for(run.out.get(), timeout)
            except TimeoutError:
                raise ClaudeError(self.model_name, f"no answer from Claude Code in {timeout:.0f} s") from None
            if kind == "error":
                raise item
            if kind == "step":
                run.usage.append(item.usage)
                calls = [b for b in item.blocks if b.kind == "tool_use" and b.name.startswith(PREFIX)]
                if calls:
                    run.returned = [b.id for b in calls]
                    run.state = "tools"
                    self._arm(run, IDLE_ON_TOOL_S)
                    parts = [TextPart(b.text) for b in item.blocks if b.kind == "text" and b.text.strip()]
                    parts += [ToolCallPart(b.name.removeprefix(PREFIX), b.args or {}, tool_call_id=b.id)
                              for b in calls]
                    return self._response(run, parts, "tool_call")
                final = item  # the reply, or a call to Claude Code's own StructuredOutput tool: wait for the result
                continue
            result: ResultMessage = item
            if result.is_error:
                why = "; ".join(result.errors or []) or result.result or result.subtype
                status = f" (HTTP {result.api_error_status})" if result.api_error_status else ""
                raise ClaudeError(self.model_name, f"Claude Code: {why}{status}")
            if run.native and result.structured_output is not None:
                text = json.dumps(result.structured_output, ensure_ascii=False)
            else:
                text = result.result or "".join(b.text for b in (final.blocks if final else []) if b.kind == "text")
            run.state = "done"
            self._arm(run, IDLE_DONE_S)
            return self._response(run, [TextPart(text)], "stop")

    def _response(self, run: _Run, parts: list, finish: str) -> ModelResponse:
        usage = RequestUsage()
        for u in run.usage:
            usage.input_tokens += u.input_tokens
            usage.cache_read_tokens += u.cache_read_tokens
            usage.cache_write_tokens += u.cache_write_tokens
            usage.output_tokens += u.output_tokens
        run.usage = []
        # Thinking stays out: the history goes to the local model too (a fallback, the next turn on another route).
        return ModelResponse(parts=parts, usage=usage, model_name=run.model or self.model_name,
                             provider_name="claude-code", provider_details={"claude_run": run.key},
                             finish_reason=finish)

    def _arm(self, run: _Run, seconds: float) -> None:
        if run.idle:
            run.idle.cancel()
        run.idle = asyncio.get_running_loop().call_later(seconds, self._close, run)

    def _close(self, run: _Run) -> None:
        """Close a run's process: release its held calls, close its input, and stop it if it doesn't go."""
        if run.idle:
            run.idle.cancel()
        run.release("The session was closed.")
        run.inbox.put_nowait(None)
        self._runs.pop(run.key, None)
        task = run.task
        if task and not task.done():
            asyncio.get_running_loop().call_later(10, lambda: task.done() or task.cancel())


def available() -> str | None:
    """Why Claude can't be used here, or None. The SDK bundles its own Claude Code; the login is the user's."""
    creds = Path.home() / ".claude" / ".credentials.json"
    return None if creds.exists() else "Claude Code isn't signed in on this machine: run `claude`, then /login."


