"""Claude on the subscription as a bench model (evals/bench.py): the same `ask` as bench's Chat, over
franziska.claude.ClaudeCodeModel, so a task's tool calls go through the same held-call mechanism the bot uses.

Bench is synchronous; the model is async and keeps one Claude Code process per agent run alive between `ask` calls
(a tool call is held open until bench sends its result). So the model lives on an event loop in a thread of its own,
and each `ask` is a coroutine sent to it. A run is found again from its tool-call ids (bench rebuilds the assistant
messages, so nothing else of ours survives the round trip). No GPU: a cloud-only bench run doesn't hold it.
"""

from __future__ import annotations

import asyncio
import threading
import time

from pydantic_ai.messages import (ModelRequest, ModelResponse, SystemPromptPart, TextPart, ToolCallPart,
                                  ToolReturnPart, UserPromptPart)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from franziska.claude import ClaudeCodeModel


class ClaudeChat:
    def __init__(self, model: str, effort: str = "", timeout_s: float = 600.0):
        self.model = ClaudeCodeModel(model, effort=effort or None, timeout_s=timeout_s)
        self.label = f"claude-code:{model}"
        self.log: list[dict] = []
        self.shot_tokens: int | None = None
        self._runs: dict[str, str] = {}  # tool_call id → the Claude run that made it
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="claude-chat").start()

    def ask(self, messages: list[dict], meter, deadline: float, tools: list[dict] | None = None,
            max_tokens: int | None = None) -> dict:
        from evals.bench import Timeout

        left = deadline - time.time()
        if left < 5:
            raise Timeout
        history, names = [], {}
        request: list = []
        for m in messages:
            role = m["role"]
            if role in ("system", "user", "tool"):
                if role == "system":
                    request.append(SystemPromptPart(m["content"]))
                elif role == "user":
                    request.append(UserPromptPart(m["content"]))
                else:
                    request.append(ToolReturnPart(names.get(m.get("tool_call_id", ""), "tool"), m["content"],
                                                  tool_call_id=m.get("tool_call_id", "")))
                continue
            if request:
                history.append(ModelRequest(request))
                request = []
            parts: list = [TextPart(m["content"])] if m.get("content") else []
            run = None
            for c in m.get("tool_calls") or []:
                names[c["id"]] = c["function"]["name"]
                parts.append(ToolCallPart(c["function"]["name"], c["function"].get("arguments") or "{}",
                                          tool_call_id=c["id"]))
                run = run or self._runs.get(c["id"])
            if run is None and not m.get("tool_calls"):  # a final reply: the run it ended, for a follow-up turn
                run = self._runs.get(f"final:{len(history)}")
            history.append(ModelResponse(parts or [TextPart("")], provider_details={"claude_run": run} if run else None))
        if request:
            history.append(ModelRequest(request))
        params = ModelRequestParameters(
            function_tools=[ToolDefinition(name=t["function"]["name"], description=t["function"].get("description"),
                                           parameters_json_schema=t["function"].get("parameters") or {"type": "object"})
                            for t in tools or []],
            allow_text_output=True)
        future = asyncio.run_coroutine_threadsafe(
            self.model.request(history, {"timeout": left}, params), self._loop)
        try:
            response = future.result(timeout=left + 10)
        except TimeoutError as e:
            future.cancel()
            raise Timeout from e
        except Exception as e:
            if "no answer from Claude Code" in str(e):
                raise Timeout from e
            raise
        run = (response.provider_details or {}).get("claude_run")
        calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
        for p in calls:
            self._runs[p.tool_call_id] = run
        if not calls:
            self._runs[f"final:{len(history)}"] = run
        u = response.usage
        meter.requests += 1
        meter.prompt_n += u.input_tokens
        meter.gen_n += u.output_tokens
        text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
        msg = {"role": "assistant", "content": text}
        if calls:
            msg["tool_calls"] = [{"id": p.tool_call_id, "type": "function",
                                  "function": {"name": p.tool_name, "arguments": p.args_as_json_str()}} for p in calls]
        self.log.append({"request": messages[-1], "reply": msg, "finish": response.finish_reason})
        return msg


__all__ = ["ClaudeChat"]
