"""Small text helpers used by the formatter and the archive sender."""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Telegram limits
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def content_hash(*parts: object) -> str:
    """Stable hash used to detect whether an edit actually changed anything."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(repr(part).encode("utf-8", errors="replace"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def safe_filename(name: str, fallback: str = "file", max_length: int = 120) -> str:
    """Turn arbitrary text into something safe to use as a file name."""
    name = unicodedata.normalize("NFC", name or "").strip()
    name = _UNSAFE_FILENAME.sub("_", name)
    name = name.strip(". ")
    if not name:
        name = fallback
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            keep = max_length - len(ext) - 1
            name = f"{stem[:keep]}.{ext}"
        else:
            name = name[:max_length]
    return name


def truncate(text: str, limit: int, suffix: str = "\n… (truncated)") -> str:
    """Cut ``text`` to ``limit`` characters, keeping room for ``suffix``."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split long text into Telegram-sized chunks, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = window.rfind("\n")
        if split_at < limit // 2:
            split_at = window.rfind(" ")
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "0 B"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover
