"""Voice notes to text, on this machine: faster-whisper (CTranslate2) on the CPU, int8.

The CPU on purpose: llama-server keeps the 26B resident in the 3060's 12 GB, so a second GPU model would fail or push
it out (gpu.py). large-v3-turbo handles Italian and English and picks the language itself: a 5 s Italian note came
back word for word in ~17 s on 12 busy cores (2026-10-28). FRANZISKA_VOICE_MODEL=small is several times faster and
less exact. The model downloads once from Hugging Face on first use, then works offline. Audio never leaves the
machine.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from franziska import obs

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model: str = "large-v3-turbo", device: str = "cpu"):
        self.model_name, self.device = model, device
        self._model = None
        self._lock = threading.Lock()  # one transcription at a time: each uses every core

    @classmethod
    def from_settings(cls, settings) -> Transcriber | None:
        return cls(settings.voice_model, settings.voice_device) if settings.voice_model else None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading whisper %s on %s", self.model_name, self.device)
            self._model = WhisperModel(self.model_name, device=self.device,
                                       compute_type="int8" if self.device == "cpu" else "int8_float16")
        return self._model

    def transcribe(self, path: Path) -> str:
        """The note's text ('' if nobody speaks). Blocking: call it in a thread."""
        with self._lock, obs.span("transcribe", model=self.model_name) as span:
            segments, info = self._load().transcribe(str(path), beam_size=5, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
            span.set_attributes(obs.clean(seconds=info.duration, language=info.language, text=text[:500]))
        log.info("voice note: %.1f s, %s (p=%.2f), %d chars", info.duration, info.language,
                 info.language_probability, len(text))
        return text
