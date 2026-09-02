"""Downloading media attachments.

Reliability rules applied here:

* every download lands in a ``.part`` file and is renamed into place only when
  it completes, so an interrupted transfer never masquerades as a good file;
* oversized files are skipped by policy (metadata is still archived);
* FloodWait and transient network errors are retried by
  :func:`app.utils.ratelimit.call_with_retry`;
* self-destructing media is never downloaded — see the module docstring of
  :mod:`app.telegram.handlers` and the README for the reasoning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from telethon import errors

from ..archive.storage import MediaStorage
from ..config import Settings
from ..utils.ratelimit import call_with_retry
from ..utils.text import human_size
from .extract import MediaInfo

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadResult:
    status: str
    local_path: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "downloaded"


class MediaDownloader:
    def __init__(self, client: Any, storage: MediaStorage, settings: Settings) -> None:
        self.client = client
        self.storage = storage
        self.settings = settings

    def _precheck(self, info: MediaInfo, self_destructing: bool) -> Optional[DownloadResult]:
        """Policy checks that do not need a network round-trip."""
        if self_destructing:
            # Deliberate: see README > "Self-destructing / view-once media".
            return DownloadResult(status="skipped_self_destructing")
        if not self.settings.download_media:
            return DownloadResult(status="skipped_disabled")
        if not info.downloadable:
            return DownloadResult(status="not_downloadable")
        limit = self.settings.max_media_size_bytes
        if limit and info.size_bytes and info.size_bytes > limit:
            log.info(
                "Skipping %s (%s) — larger than MAX_MEDIA_SIZE_MB=%s",
                info.kind,
                human_size(info.size_bytes),
                self.settings.max_media_size_mb,
            )
            return DownloadResult(
                status="skipped_too_large",
                size_bytes=info.size_bytes,
                error=f"size {info.size_bytes} exceeds limit {limit}",
            )
        return None

    async def download(
        self,
        message: Any,
        info: MediaInfo,
        *,
        chat_id: int,
        message_id: int,
        index: int = 0,
        self_destructing: bool = False,
    ) -> DownloadResult:
        """Download one attachment, returning what happened."""
        skip = self._precheck(info, self_destructing)
        if skip is not None:
            return skip

        final_path = self.storage.target_path(
            info.kind, chat_id, message_id, index, info.file_name
        )
        part_path = MediaStorage.part_path(final_path)

        if final_path.exists() and final_path.stat().st_size > 0:
            # Already downloaded on a previous run — do not fetch it again.
            log.debug("Media already present, skipping download: %s", final_path)
            return DownloadResult(
                status="downloaded",
                local_path=self.storage.relative(final_path),
                sha256=MediaStorage.file_sha256(final_path),
                size_bytes=final_path.stat().st_size,
            )

        try:
            await call_with_retry(
                lambda: self.client.download_media(message, file=str(part_path)),
                description=f"download {info.kind} from message {message_id}",
                max_retries=self.settings.max_retries,
            )
        except errors.FileReferenceExpiredError:
            # The file reference went stale; re-fetch the message and retry once.
            log.info("File reference expired for message %s, refetching", message_id)
            refreshed = await self._refetch(chat_id, message_id)
            if refreshed is None:
                return self._fail(part_path, "file reference expired and message not refetchable")
            try:
                await call_with_retry(
                    lambda: self.client.download_media(refreshed, file=str(part_path)),
                    description=f"re-download {info.kind} from message {message_id}",
                    max_retries=self.settings.max_retries,
                )
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return self._fail(part_path, f"{type(exc).__name__}: {exc}")
        except (errors.ChannelPrivateError, errors.ChatForwardsRestrictedError) as exc:
            return self._fail(part_path, f"no access to media: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a failed file must not kill the run
            return self._fail(part_path, f"{type(exc).__name__}: {exc}")

        if not part_path.exists():
            # Telethon returns None for media that has nothing downloadable
            # (web previews, polls, dice…).
            return DownloadResult(status="not_downloadable", error="nothing was written")

        size = part_path.stat().st_size
        if size == 0:
            part_path.unlink(missing_ok=True)
            return DownloadResult(status="failed", error="downloaded file was empty")

        try:
            MediaStorage.finalise(part_path, final_path)
        except OSError as exc:
            return self._fail(part_path, f"could not finalise download: {exc}")

        digest = MediaStorage.file_sha256(final_path)
        log.info(
            "Downloaded %s (%s) → %s",
            info.kind,
            human_size(size),
            self.storage.relative(final_path),
        )
        return DownloadResult(
            status="downloaded",
            local_path=self.storage.relative(final_path),
            sha256=digest,
            size_bytes=size,
        )

    @staticmethod
    def _fail(part_path: Path, error: str) -> DownloadResult:
        part_path.unlink(missing_ok=True)
        log.warning("Media download failed: %s", error)
        return DownloadResult(status="failed", error=error)

    async def _refetch(self, chat_id: int, message_id: int) -> Any:
        try:
            return await call_with_retry(
                lambda: self.client.get_messages(chat_id, ids=message_id),
                description=f"refetch message {message_id}",
                max_retries=2,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not refetch message %s: %s", message_id, exc)
            return None
