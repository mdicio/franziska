"""Where model turns run: the desktop's GPU (Ollama) when it is up, woken over the LAN when it is not.

The always-on box runs the bot, the stores and every job that needs no model. Model turns go to Ollama on the
desktop; this module answers "is it up?" cheaply, sends the Wake-on-LAN magic packet, and waits for Ollama to answer.
What to do while the desktop is off (ask, use the cloud, or wake and wait) is the bot's policy, `Settings.when_off`.

On the desktop itself (development), `ollama_url` is localhost and the desktop is simply always up.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time

import httpx

log = logging.getLogger(__name__)

DOWN_CACHE_S = 20.0  # a failed check is trusted this long, so a burst of messages doesn't wait on timeouts


def magic_packet(mac: str) -> bytes:
    """Wake-on-LAN payload: 6 × 0xFF, then the MAC 16 times."""
    hexits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(hexits) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(hexits) * 16


class Desktop:
    def __init__(self, ollama_url: str, mac: str = "", broadcast: str = "255.255.255.255", port: int = 9,
                 transport: httpx.AsyncBaseTransport | None = None, health: str = "/api/version"):
        self.ollama_url = ollama_url.rstrip("/")
        self.health = health  # Ollama's; llama-server answers /health once the model is loaded
        self.mac, self.broadcast, self.port = mac, broadcast, port
        self._transport = transport  # tests
        self._down_until = 0.0
        self._waking: asyncio.Task | None = None

    @classmethod
    def from_settings(cls, settings) -> Desktop:
        """The local model server of the settings: llama-server (FRANZISKA_BASE_URL) or Ollama."""
        return cls(settings.server_url, settings.desktop_mac, settings.desktop_broadcast,
                   health="/health" if settings.base_url else "/api/version")

    @property
    def can_wake(self) -> bool:
        return bool(self.mac)

    async def is_up(self, timeout: float = 2.0, fresh: bool = False) -> bool:
        """Ollama answers on the desktop. A recent failure is remembered for DOWN_CACHE_S unless `fresh`."""
        if not fresh and time.monotonic() < self._down_until:
            return False
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                r = await client.get(self.ollama_url + self.health)
            up = r.status_code == 200
        except httpx.HTTPError:
            up = False
        self._down_until = 0.0 if up else time.monotonic() + DOWN_CACHE_S
        return up

    def is_up_now(self, timeout: float = 2.0) -> bool:
        """`is_up` for code with no event loop at hand (the MCP server's tools); no cache."""
        try:
            return httpx.get(self.ollama_url + self.health, timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False

    def wake(self) -> None:
        """Broadcast the magic packet on the LAN. The box must be on the desktop's network segment."""
        if not self.can_wake:
            raise RuntimeError("FRANZISKA_DESKTOP_MAC is not set; cannot wake the desktop")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(magic_packet(self.mac), (self.broadcast, self.port))
        log.info("sent wake-on-LAN packet to %s", self.mac)

    async def wake_and_wait(self, timeout: float = 180.0, every: float = 5.0) -> bool:
        """Wake the desktop and poll until Ollama answers. Concurrent callers share one wait."""
        if await self.is_up(fresh=True):
            return True
        if self._waking is None or self._waking.done():
            self._waking = asyncio.create_task(self._wake_and_poll(timeout, every))
        return await asyncio.shield(self._waking)

    async def _wake_and_poll(self, timeout: float, every: float) -> bool:
        self.wake()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(every)
            if await self.is_up(timeout=min(every, 3.0), fresh=True):
                log.info("desktop is up")
                return True
        log.warning("desktop did not come up within %.0f s", timeout)
        return False
