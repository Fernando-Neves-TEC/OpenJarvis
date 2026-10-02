"""Abstract base classes and data types for text-to-speech backends."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Matches most emoji / pictographic ranges. Not exhaustive, but covers what
# chat models actually emit.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def clean_text_for_speech(
    text: str, pronunciations: Optional[Dict[str, str]] = None
) -> str:
    """Drop markdown/emoji and fix known mispronunciations before synthesis.

    Single shared cleanup point for every caller that hands text to a TTS
    backend's ``synthesize()`` -- the terminal ``jarvis chat --voice``
    (``cli/_voice_chat.py``) and the HTTP ``/v1/speech/synthesize`` route
    (``server/api_routes.py``) both call this before synthesis, so the fix
    only needs to live here once. Mirrors the regex cleanup already used for
    the morning-digest voice briefing (see ``agents/morning_digest.py``),
    extended to cover links, code fences/backticks, and emoji too.

    ``pronunciations`` is a word -> replacement-spelling map (e.g. from
    ``config.speech.pronunciations``), applied as a case-insensitive
    whole-word substitution -- e.g. mapping "Jarvis" to an accented spelling
    so pt-BR TTS voices stress the right syllable, without touching
    "OpenJarvis" (no word boundary between "Open" and "Jarvis").
    """
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [text](url) -> text
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)  # fenced code
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)  # headings

    def _list_item(match: "re.Match[str]") -> str:
        # Strip the marker and make sure the item ends with punctuation, so
        # the newline-collapse below produces a spoken pause between items
        # instead of running them together ("item um item dois").
        item = match.group(1).rstrip()
        if item and item[-1] not in ".!?;:":
            item += "."
        return item

    cleaned = re.sub(
        r"^\s*(?:[-*+•]|\d+[.)])\s+(.*)$", _list_item, cleaned, flags=re.MULTILINE
    )  # list markers
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", cleaned)  # **bold**/*italic*
    cleaned = re.sub(r"_{2,3}([^_]+)_{2,3}", r"\1", cleaned)  # __bold__
    # Only strip stray * # ` -- a lone "_" is almost always a literal
    # character (snake_case, file names), not markdown emphasis.
    cleaned = re.sub(r"[*#`]", "", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    # Trim horizontal whitespace touching a newline so the pause-insertion
    # below doesn't leave doubled spaces, then collapse any run of blank
    # lines down to one newline before turning it into a pause.
    cleaned = re.sub(r" ?\n ?", "\n", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)

    def _newline_to_pause(match: "re.Match[str]") -> str:
        # Models often answer with one item per line and no "- " marker at
        # all (e.g. "Maçã\nBanana\nLaranja"), which the list-marker step
        # above never sees. Every remaining line break becomes a spoken
        # pause: just a space if the line already ends with punctuation,
        # otherwise a period is inserted so items don't run together.
        prev_char = match.group(1)
        return prev_char + (" " if prev_char in ".!?:;" else ". ")

    cleaned = re.sub(r"([^\n])\n", _newline_to_pause, cleaned)
    cleaned = cleaned.strip()
    for word, replacement in (pronunciations or {}).items():
        if word:
            cleaned = re.sub(
                rf"\b{re.escape(word)}\b", replacement, cleaned, flags=re.IGNORECASE
            )
    return cleaned


@dataclass
class TTSResult:
    """Result of a text-to-speech synthesis."""

    audio: bytes
    format: str = "mp3"
    duration_seconds: float = 0.0
    voice_id: str = ""
    sample_rate: int = 24000
    metadata: Dict[str, Any] = field(default_factory=dict)

    def save(self, path: Path) -> Path:
        """Write audio bytes to a file and return the path."""
        path.write_bytes(self.audio)
        return path


class TTSBackend(ABC):
    """Abstract base class for text-to-speech backends."""

    backend_id: str = ""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "mp3",
    ) -> TTSResult:
        """Synthesize text to audio."""

    @abstractmethod
    def available_voices(self) -> List[str]:
        """Return list of available voice IDs."""

    @abstractmethod
    def health(self) -> bool:
        """Check if the backend is ready."""


__all__ = ["TTSBackend", "TTSResult", "clean_text_for_speech"]
