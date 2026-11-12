"""A rolling summary of the turns that left a route's history, so aging out doesn't mean forgetting.

History keeps the last few whole turns (core.py). What ages out of it (past the budget's turn cap) or is folded out
because the window passed FOLD_AT is summarised by the local model, in the background after the reply, into one short
text per chat and route. The next run reads it as the first message of its history. Nothing else is written: the
summary is working memory for the conversation, never a fact about the user (those go through memory.py and need the
user's tap or corroboration), and the originals stay in the turn log for `past_conversations`.

Local models only: the turns hold money figures and mail text. The same instructions work for a bigger model; set
Budget.summary_tokens for how much it may write.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import (ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart,
                                  UserPromptPart)

RESULT_CHARS = 300  # of each tool result shown to the summariser: what was found, not the dump
KEEP_TURNS = 2  # when the window forces a fold, the newest turns stay verbatim

INSTRUCTIONS = """\
You compress a conversation between a user and their personal assistant, for the assistant's own later use.
Write a short plain-text summary in the user's language (Italian or English). Keep, as bullets:
- what the user asked for and what was done or decided (including changes made: tasks, appointments, payments);
- exact names, dates, amounts, ids and email addresses that a follow-up could refer to ('the second one', 'that bill');
- what is still open or was promised.
Drop greetings, tool noise and anything already superseded. Merge the previous summary with the new turns into one
summary; never write more than asked. Do not invent anything and do not address the user."""


def render(turns: list[list[ModelMessage]]) -> str:
    """The turns as readable lines: what was said, which tools ran and a short head of what they returned."""
    lines = []
    for turn in turns:
        for msg in turn:
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    lines.append(f"User: {part.content}")
                elif isinstance(part, TextPart):
                    lines.append(f"Assistant: {part.content}")
                elif isinstance(part, ToolCallPart):
                    lines.append(f"[tool {part.tool_name} {' '.join(part.args_as_json_str().split())[:150]}]")
                elif isinstance(part, ToolReturnPart):
                    text = " ".join(str(part.content).split())
                    lines.append(f"[result {text[:RESULT_CHARS]}{'…' if len(text) > RESULT_CHARS else ''}]")
    return "\n".join(lines)


async def fold(model, previous: str, turns: list[list[ModelMessage]], max_tokens: int) -> str:
    """`previous` merged with `turns`, in at most about `max_tokens` tokens: a length asked for in the prompt, not a cap
    on the response (a cap cut thinking models off before the summary)."""
    agent = Agent(output_type=str, instructions=INSTRUCTIONS, retries=0, name="summary")
    prompt = (f"Previous summary:\n{previous or '(none)'}\n\nNew turns:\n{render(turns)}\n\n"
              f"Write the merged summary in under {int(max_tokens * 0.7)} words.")
    result = await agent.run(prompt, model=model)
    return result.output.strip()


def as_history(text: str) -> list[ModelMessage]:
    """The summary as the first exchange of a run's history; [] when there is none."""
    if not text:
        return []
    return [ModelRequest(parts=[UserPromptPart(f"(Summary of our earlier conversation, no longer in your window:\n"
                                               f"{text})")]),
            ModelResponse(parts=[TextPart("Understood.")])]
