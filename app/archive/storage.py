"""Filesystem layout for downloaded media.

Layout::

    data/media/<kind>/<chat_id>/<message_id>_<index>_<name>

Downloads always go to a ``.part`` file first and are renamed into place only
after the transfer finishes. A crashed or interrupted download therefore never
leaves a truncated file that looks complete — stale ``.part`` files are cleaned
up on startup.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from ..utils.text import safe_filename

log = logging.getLogger(__name__)

# Sub-directories per media kind, as requested in the project layout.
KIND_DIRS = {
    "photo": "photos",
    "video": "videos",
    "video_note": "videos",
    "gif": "videos",
    "animation": "videos",
    "document": "documents",
    "audio": "audio",
    "voice": "audio",
    "sticker": "stickers",
    "other": "other",
}


class MediaStorage:
    """Decides where a file lives and moves it there atomically."""

    def __init__(self, media_root: Path) -> None:
        self.root = Path(media_root)

    # ------------------------------------------------------------------ paths
    def directory_for(self, kind: str, chat_id: int) -> Path:
        sub = KIND_DIRS.get(kind, "other")
        # Negative chat ids would create odd directory names like "-100123".
        chat_dir = f"chat_{str(chat_id).replace('-', 'n')}"
        path = self.root / sub / chat_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def target_path(
        self,
        kind: str,
        chat_id: int,
        message_id: int,
        index: int,
        file_name: str | None,
    ) -> Path:
        name = safe_filename(file_name or "", fallback=f"{kind}")
        stem = f"{message_id}_{index}_{name}" if name else f"{message_id}_{index}"
        return self.directory_for(kind, chat_id) / safe_filename(stem, fallback=str(message_id))

    @staticmethod
    def part_path(final_path: Path) -> Path:
        return final_path.with_name(final_path.name + ".part")

    # ------------------------------------------------------------------ moves
    @staticmethod
    def finalise(part_path: Path, final_path: Path) -> Path:
        """Atomically move a finished ``.part`` file into its final place."""
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            final_path.unlink()
        os.replace(part_path, final_path)
        return final_path

    def ensure_ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def cleanup_partials(self) -> int:
        """Delete leftover ``.part`` files from a previous crashed run."""
        if not self.root.exists():
            return 0
        removed = 0
        for part in self.root.rglob("*.part"):
            try:
                part.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("Could not remove stale partial %s: %s", part, exc)
        if removed:
            log.info("Cleaned up %s partial download(s) from a previous run", removed)
        return removed

    # ------------------------------------------------------------------ misc
    @staticmethod
    def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str | None:
        """Hash a downloaded file so identical media can be recognised later."""
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(chunk_size):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Could not hash %s: %s", path, exc)
            return None

    def relative(self, path: Path) -> str:
        """Store paths relative to the data root so the archive stays portable."""
        try:
            return str(path.relative_to(self.root.parent))
        except ValueError:
            return str(path)
