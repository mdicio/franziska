"""Cloud turns: ChatGPT through the pi SDK (pi/think.ts), used for /think and when the desktop is off.

The cloud agent's only tools are the `life` MCP server (schedule, calendar, notes). It never sees the ledger or mail.
Recent turns of the chat are passed as plain text so a fallback conversation keeps its thread.
"""

from __future__ import annotations

import asyncio
import logging
import os

from franziska.config import ROOT, Settings

log = logging.getLogger(__name__)


class CloudError(RuntimeError):
    """The pi bridge failed or timed out. The message is safe to show to the user."""


def compose(text: str, history: list[tuple[str, str]] = ()) -> str:
    """The prompt for one turn: up to the last few (user, assistant) exchanges, then the new message."""
    if not history:
        return text
    lines = ["Recent conversation (for context only):"]
    for user, assistant in history[-4:]:
        lines += [f"User: {user}", f"Assistant: {assistant}"]
    return "\n".join(lines + ["", f"New message: {text}"])


async def ask(settings: Settings, text: str, history: list[tuple[str, str]] = ()) -> str:
    env = {**os.environ, "FRANZISKA_PI_MODEL": settings.pi_model, "PI_TELEMETRY": "0",
           "FRANZISKA_DATA_DIR": str(settings.data_dir)}
    proc = await asyncio.create_subprocess_exec(
        "node", str(ROOT / "pi" / "think.ts"), cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(compose(text, history).encode()), settings.pi_timeout_s)
    except TimeoutError:
        proc.kill()
        raise CloudError("ChatGPT took too long; try again or ask something narrower.") from None
    if proc.returncode != 0:
        stderr = err.decode()
        log.error("think.ts failed (%s): %s", proc.returncode, stderr[-2000:])
        own = [ln for ln in stderr.splitlines() if ln.startswith("think.ts: ")]
        if own:  # the bridge's own diagnosis (empty answer, unknown model)
            raise CloudError(f"The ChatGPT call failed: {own[-1].removeprefix('think.ts: ')}. Try again.")
        raise CloudError("The ChatGPT call failed (details in the bot log). If it's the first time, sign in once "
                         "on the box: run `pi`, then /login openai.")
    return out.decode().strip()
