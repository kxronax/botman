"""Delivery of archived content into the private archive chat.

For a media message two Telegram messages are produced: the file itself with a
short caption, and a full metadata record sent as a reply to it (a caption can
only hold 1024 characters, while the record is often longer).

Sending is deliberately serialised and paced by the worker that calls this, so
a busy day of chats cannot trigger a long FloodWait.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from telethon import errors

from ..config import Settings
from ..telegram.extract import ExtractedMessage
from ..utils.ratelimit import FloodWaitTooLong, call_with_retry
from ..utils.text import split_message
from .formatter import format_caption, format_message

log = logging.getLogger(__name__)


class ArchiveSendError(RuntimeError):
    """Raised when a copy could not be delivered to the archive chat."""


class ArchiveSender:
    """Pushes archived items into the configured archive chat."""

    def __init__(self, client: Any, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._resolved_target: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.send_to_archive_chat and self.settings.archive_chat_id)

    # ------------------------------------------------------------------ setup
    async def resolve_target(self) -> Any:
        """Resolve the archive chat once and verify we can post to it."""
        if self._resolved_target is not None:
            return self._resolved_target
        if not self.enabled:
            raise ArchiveSendError("archive chat is not configured")
        chat_id = self.settings.archive_chat_id
        try:
            entity = await self.client.get_entity(chat_id)
        except (ValueError, TypeError) as exc:
            raise ArchiveSendError(
                f"ARCHIVE_CHAT_ID={chat_id} could not be resolved. Send one message to "
                "that chat from your account first so Telegram exposes it to the client, "
                "and check the id (channels/supergroups start with -100)."
            ) from exc
        self._resolved_target = entity
        return entity

    # ------------------------------------------------------------------ text
    async def send_text(self, text: str, reply_to: Optional[int] = None) -> Optional[int]:
        """Send plain text, splitting it if it exceeds Telegram's limit."""
        target = await self.resolve_target()
        first_id: Optional[int] = None
        for chunk in split_message(text):
            sent = await call_with_retry(
                lambda chunk=chunk: self.client.send_message(
                    target, chunk, link_preview=False, reply_to=reply_to
                ),
                description="send archive text",
                max_retries=self.settings.max_retries,
            )
            if first_id is None:
                first_id = getattr(sent, "id", None)
        return first_id

    # ------------------------------------------------------------------ media
    async def send_message_record(
        self,
        extracted: ExtractedMessage,
        *,
        source_message: Any = None,
        local_files: Optional[list[Path]] = None,
        note: Optional[str] = None,
    ) -> Optional[int]:
        """Send a full archive record, with the media file when we have one."""
        target = await self.resolve_target()
        local_files = [p for p in (local_files or []) if p and p.exists()]
        record = format_message(
            extracted,
            local_paths=[str(p) for p in local_files] or None,
            note=note,
        )

        file_message_id: Optional[int] = None
        if extracted.has_media and not extracted.is_self_destructing:
            file_message_id = await self._send_media(
                target, extracted, source_message, local_files
            )

        text_message_id = await self.send_text(record, reply_to=file_message_id)
        return file_message_id or text_message_id

    async def _send_media(
        self,
        target: Any,
        extracted: ExtractedMessage,
        source_message: Any,
        local_files: list[Path],
    ) -> Optional[int]:
        """Try the cheapest way to get the file into the archive chat.

        1. Re-send the existing Telegram file (no upload, no bandwidth).
        2. Upload the local copy we downloaded.
        3. Give up on the file and let the text record carry the metadata.
        """
        caption = format_caption(extracted)

        if source_message is not None and getattr(source_message, "media", None) is not None:
            try:
                sent = await call_with_retry(
                    lambda: self.client.send_file(
                        target,
                        source_message.media,
                        caption=caption,
                        force_document=extracted.message_type == "document",
                    ),
                    description=f"re-send media of message {extracted.message_id}",
                    max_retries=2,
                )
                return getattr(sent, "id", None)
            except (
                errors.ChatForwardsRestrictedError,
                errors.MediaEmptyError,
                errors.MediaInvalidError,
                errors.FileReferenceExpiredError,
                FloodWaitTooLong,
            ) as exc:
                log.info(
                    "Could not re-send media reference for message %s (%s); "
                    "falling back to local upload",
                    extracted.message_id,
                    type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Unexpected error re-sending media for message %s: %s: %s",
                    extracted.message_id,
                    type(exc).__name__,
                    exc,
                )

        for path in local_files:
            try:
                sent = await call_with_retry(
                    lambda path=path: self.client.send_file(
                        target, str(path), caption=caption
                    ),
                    description=f"upload archived file {path.name}",
                    max_retries=self.settings.max_retries,
                )
                return getattr(sent, "id", None)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not upload %s to archive chat: %s", path, exc)

        return None

    # ------------------------------------------------------------------ notices
    async def send_notice(self, text: str) -> Optional[int]:
        """Send a standalone notice (deletion, edit, startup summary)."""
        return await self.send_text(text)
