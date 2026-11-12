"""The model's window as one budget: every size limit is a share of it, so a bigger model needs no code change.

`Budget(window)` takes the server's `n_ctx` (core.context_window) and hands out the limits that used to be constants
sized for 32k: how much of one attachment or one Gmail answer, how many turns of history, how much tool output a run
may keep. At 32768 they equal the old values; at 131072 they are four times as large.

`ProcessHistory` (a pydantic-ai capability) runs before every model request of a run and writes its result back into
the run's messages, so the same trim applies mid-run (a mail sweep that piles up search results) and in the history
kept for the next turn. Only tool *results* are shortened, and only the oldest ones once the run's results exceed
their share; the model's own words, the questions and the tool calls stay, and it can call a tool again.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

REFERENCE = 32768  # the window the original constants were sized for
CHARS_PER_TOKEN = 3.2  # Italian and English with JSON tool output; the web UI's counter shows the real figure
STUB_MIN = 300  # chars: a shorter result costs less than its stub


@dataclass(frozen=True)
class Budget:
    window: int = REFERENCE

    # Shares of the window; the rest is the system prompt, the answer and a margin.
    TOOLS = 0.45

    def chars(self, share: float) -> int:
        return int(self.window * share * CHARS_PER_TOKEN)

    @property
    def scale(self) -> float:
        return self.window / REFERENCE

    @property
    def history_turns(self) -> int:
        """Whole turns kept per chat and route: 6 at 32k, more as the window grows."""
        return max(4, min(24, round(6 * self.scale)))

    @property
    def attachment_chars(self) -> int:
        """Of one attachment's text: 12000 at 32k."""
        return int(12000 * self.scale)

    @property
    def gmail_bytes(self) -> int:
        """Of one Gmail tool answer: 48 KiB at 32k."""
        return int(48 * 1024 * self.scale)

    @property
    def fold_at(self) -> int:
        """Tokens in the window after a turn from which older turns are folded into the summary."""
        return int(self.window * 0.6)

    @property
    def summary_tokens(self) -> int:
        """What the rolling summary may take: 1300 at 32k."""
        return max(600, int(self.window * 0.04))

    @property
    def tool_chars(self) -> int:
        """What the tool results of a run may add up to before the oldest are stubbed."""
        return self.chars(self.TOOLS)

    def trim(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """The messages with the oldest tool results beyond `tool_chars` replaced by a one-line stub."""
        keep, out = self.tool_chars, list(messages)
        for i in range(len(out) - 1, -1, -1):  # newest first: the latest results are the ones still being used
            msg = out[i]
            if not isinstance(msg, ModelRequest):
                continue
            parts = list(msg.parts)
            for j in range(len(parts) - 1, -1, -1):
                part = parts[j]
                if not isinstance(part, ToolReturnPart) or part.outcome == "failed":
                    continue
                size = len(part.content) if isinstance(part.content, str) else len(str(part.content))
                keep -= size
                if keep < 0 and size > STUB_MIN:
                    parts[j] = dataclasses.replace(
                        part, content=f"[{part.tool_name} result of {size} characters dropped to save room; "
                                      "call the tool again if you need it]")
            out[i] = dataclasses.replace(msg, parts=parts) if parts != list(msg.parts) else msg
        return out

    def capability(self):
        from pydantic_ai.capabilities import ProcessHistory

        return ProcessHistory(self.trim)
