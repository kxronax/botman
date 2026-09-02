"""The archiving pipeline.

The key design decision lives here: **capture and delivery are separate**.

``capture()`` does nothing but write the message to the local database. It is
called straight from the event handler and finishes in milliseconds, which is
what makes it possible to keep a copy of a message that is deleted seconds
later. Only afterwards is a job queued for the slow work — downloading media
and copying into the archive chat — which is retried independently and
survives restarts because its state lives in the database, not in memory.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..config import Settings
from ..telegram.extract import (
    ChatInfo,
    ExtractedMessage,
    MediaInfo,
    SenderInfo,
    extract_message,
)
from ..telegram.media import MediaDownloader
from ..utils.jsonutil import loads
from ..utils.ratelimit import FloodWaitTooLong
from . import repository as repo
from .database import Database
from .formatter import format_deletion, format_edit, format_unknown_deletion
from .models import Chat, Message
from .sender import ArchiveSendError, ArchiveSender
from .storage import MediaStorage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ArchiveJob:
    """Slow work for one already-captured message."""

    message_pk: int
    chat_id: int
    message_id: int
    raw_message: Any = None
    extracted: Optional[ExtractedMessage] = None
    send_to_archive: bool = True
    download_media: bool = True


class ArchivePipeline:
    def __init__(
        self,
        *,
        client: Any,
        database: Database,
        settings: Settings,
        storage: MediaStorage,
        sender: ArchiveSender,
        self_id: Optional[int] = None,
    ) -> None:
        self.client = client
        self.db = database
        self.settings = settings
        self.storage = storage
        self.sender = sender
        self.self_id = self_id
        self.downloader = MediaDownloader(client, storage, settings)
        self.queue: asyncio.Queue[Optional[ArchiveJob]] = asyncio.Queue(maxsize=10000)
        self._worker_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.counters = {
            "captured": 0,
            "duplicates": 0,
            "edits": 0,
            "deletions": 0,
            "media_downloaded": 0,
            "archive_sent": 0,
            "archive_failed": 0,
        }

    # ================================================================= capture

    async def capture(
        self,
        message: Any,
        *,
        source: str = "live",
        chat_entity: Any = None,
        sender_entity: Any = None,
        send_to_archive: Optional[bool] = None,
        download_media: Optional[bool] = None,
        enqueue: bool = True,
    ) -> Optional[ArchiveJob]:
        """Persist a message immediately; queue the slow work afterwards.

        Returns the queued job, or ``None`` when the message was filtered out
        or had already been archived.
        """
        try:
            extracted = await extract_message(
                message,
                chat_entity=chat_entity,
                sender_entity=sender_entity,
                self_id=self.self_id,
            )
        except Exception as exc:  # noqa: BLE001 - never let one message stop the run
            log.exception("Could not read message: %s", exc)
            return None

        if not self._passes_filters(extracted):
            return None

        want_send = (
            send_to_archive
            if send_to_archive is not None
            else self.sender.enabled
        )

        async with self.db.session() as session:
            message_row, created = await repo.add_message(
                session,
                extracted,
                source=source,
                archive_status="pending" if want_send else "disabled",
            )
            if not created:
                self.counters["duplicates"] += 1
                log.debug(
                    "Message %s in chat %s already archived — skipping",
                    extracted.message_id,
                    extracted.chat.id,
                )
                return None
            await repo.add_media_records(session, message_row, extracted)
            message_pk = message_row.id

        self.counters["captured"] += 1
        log.info(
            "Archived message #%s from %s (%s)",
            extracted.message_id,
            extracted.chat.title or extracted.chat.id,
            extracted.message_type,
        )
        if extracted.is_self_destructing:
            log.warning(
                "Self-destructing media detected in chat %s message %s "
                "(ttl=%s) — metadata archived, content intentionally not stored",
                extracted.chat.id,
                extracted.message_id,
                extracted.ttl_seconds,
            )

        job = ArchiveJob(
            message_pk=message_pk,
            chat_id=extracted.chat.id,
            message_id=extracted.message_id,
            raw_message=message,
            extracted=extracted,
            send_to_archive=want_send,
            download_media=(
                self.settings.download_media if download_media is None else download_media
            ),
        )
        if enqueue:
            await self.queue.put(job)
        return job

    def _passes_filters(self, extracted: ExtractedMessage) -> bool:
        if not self.settings.chat_is_allowed(extracted.chat.id, extracted.chat.type):
            return False
        if extracted.outgoing and not self.settings.archive_outgoing:
            return False
        return True

    # ================================================================== edits

    async def handle_edit(self, message: Any, chat_entity: Any = None) -> None:
        """Record a new version, keeping every earlier one intact."""
        try:
            extracted = await extract_message(
                message, chat_entity=chat_entity, self_id=self.self_id
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not read edited message: %s", exc)
            return

        if not self._passes_filters(extracted):
            return

        async with self.db.session() as session:
            existing = await repo.get_message(
                session, extracted.chat.id, extracted.message_id
            )
            if existing is None:
                # We never saw the original: archive the current state instead,
                # marked as first-seen-after-edit.
                log.info(
                    "Edit for unarchived message %s in chat %s — storing current version",
                    extracted.message_id,
                    extracted.chat.id,
                )
                message_row, created = await repo.add_message(
                    session,
                    extracted,
                    source="live-edit",
                    archive_status="pending" if self.sender.enabled else "disabled",
                )
                if created:
                    await repo.add_media_records(session, message_row, extracted)
                    job = ArchiveJob(
                        message_pk=message_row.id,
                        chat_id=extracted.chat.id,
                        message_id=extracted.message_id,
                        raw_message=message,
                        extracted=extracted,
                        send_to_archive=self.sender.enabled,
                    )
                    await self.queue.put(job)
                return

            previous_text = existing.text
            version = await repo.add_version_if_changed(session, existing, extracted)
            if version is None:
                log.debug(
                    "Edit update for message %s changed no content — ignored",
                    extracted.message_id,
                )
                return
            version_no = version.version_no

        self.counters["edits"] += 1
        log.info(
            "Message #%s edited — saved version %s (previous version kept)",
            extracted.message_id,
            version_no,
        )

        if self.sender.enabled:
            notice = format_edit(existing, extracted, version_no, previous_text)
            await self._safe_notice(notice)

    # =============================================================== deletions

    async def handle_deletion(
        self, message_ids: list[int], chat_id: Optional[int]
    ) -> None:
        """Mark archived copies as deleted; never remove them."""
        if not message_ids:
            return

        matched: list[tuple[Message, Optional[str]]] = []
        unmatched: list[int] = []

        async with self.db.session() as session:
            candidates = await repo.find_deleted_candidates(session, message_ids, chat_id)
            found_ids = {row.message_id for row in candidates}
            unmatched = [mid for mid in message_ids if mid not in found_ids]

            titles: dict[int, Optional[str]] = {}
            for row in candidates:
                newly = await repo.mark_deleted(session, row)
                await repo.record_deletion_event(session, row.message_id, row.chat_id, row.id)
                if not newly:
                    continue  # already recorded as deleted on an earlier update
                if row.chat_id not in titles:
                    chat = await session.get(Chat, row.chat_id)
                    titles[row.chat_id] = chat.title if chat else None
                matched.append((row, titles[row.chat_id]))

            for mid in unmatched:
                await repo.record_deletion_event(session, mid, chat_id, None)

        self.counters["deletions"] += len(matched)
        for row, title in matched:
            log.info(
                "Archived deleted message #%s from %s (copy preserved)",
                row.message_id,
                title or row.chat_id,
            )
        if unmatched:
            log.info(
                "%s message(s) deleted that were never captured "
                "(ids %s) — content is unrecoverable",
                len(unmatched),
                ", ".join(str(m) for m in unmatched[:10]),
            )

        if self.sender.enabled:
            for row, title in matched:
                await self._safe_notice(format_deletion(row, title))
            if unmatched:
                await self._safe_notice(format_unknown_deletion(unmatched, chat_id))

    # ================================================================ reactions

    async def handle_reactions(
        self, chat_id: int, message_id: int, reactions: list[dict]
    ) -> None:
        async with self.db.session() as session:
            updated = await repo.update_reactions(session, chat_id, message_id, reactions)
        if updated:
            log.debug("Updated reactions for message %s in chat %s", message_id, chat_id)

    # =================================================================== worker

    async def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._worker_loop(), name="archive-worker")
            log.debug("Archive worker started")

    async def stop_worker(self, drain: bool = True) -> None:
        if self._worker_task is None:
            return
        self._stopping = True
        if drain:
            await self.queue.put(None)
            try:
                await asyncio.wait_for(self._worker_task, timeout=60)
            except asyncio.TimeoutError:
                log.warning("Archive worker did not finish in time; cancelling")
                self._worker_task.cancel()
        else:
            self._worker_task.cancel()
        self._worker_task = None

    async def _worker_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is None:  # shutdown sentinel
                    return
                await self.process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the worker must never die
                log.exception("Archive job for message %s failed: %s", job.message_id, exc)
            finally:
                self.queue.task_done()

    async def process_job(self, job: ArchiveJob) -> None:
        """Do the slow work: download media, then copy to the archive chat."""
        async with self.db.session() as session:
            row = await session.get(Message, job.message_pk)
            if row is None:
                return
            media_rows = [
                {
                    "id": media.id,
                    "kind": media.kind,
                    "file_name": media.file_name,
                    "mime_type": media.mime_type,
                    "size_bytes": media.size_bytes,
                    "tg_file_id": media.tg_file_id,
                    "tg_dc_id": media.tg_dc_id,
                    "duration_seconds": media.duration_seconds,
                    "width": media.width,
                    "height": media.height,
                    "status": media.download_status,
                    "local_path": media.local_path,
                }
                for media in row.media
            ]
            archive_status = row.archive_status
            is_self_destructing = row.is_self_destructing
            extracted = job.extracted or self._extracted_from_row(row)

        raw_message = job.raw_message
        needs_message = (
            job.download_media
            and any(m["status"] in {"pending", "failed"} for m in media_rows)
        ) or (archive_status in {"pending", "failed"} and extracted.has_media)
        if raw_message is None and needs_message:
            raw_message = await self._refetch(job.chat_id, job.message_id)

        # ---- media -------------------------------------------------------
        local_files: list[Path] = []
        for index, media in enumerate(media_rows):
            if media["status"] == "downloaded" and media["local_path"]:
                local_files.append(self.storage.root.parent / media["local_path"])
                continue
            if media["status"] not in {"pending", "failed"}:
                continue
            if not job.download_media:
                async with self.db.session() as session:
                    await repo.set_media_result(
                        session, media["id"], status="skipped_disabled"
                    )
                continue
            if raw_message is None:
                async with self.db.session() as session:
                    await repo.set_media_result(
                        session,
                        media["id"],
                        status="failed",
                        error="source message no longer retrievable",
                    )
                continue

            info = MediaInfo(
                kind=media["kind"],
                file_name=media["file_name"],
                mime_type=media["mime_type"],
                size_bytes=media["size_bytes"],
                tg_file_id=media["tg_file_id"],
                tg_dc_id=media["tg_dc_id"],
                duration_seconds=media["duration_seconds"],
                width=media["width"],
                height=media["height"],
            )
            result = await self.downloader.download(
                raw_message,
                info,
                chat_id=job.chat_id,
                message_id=job.message_id,
                index=index,
                self_destructing=is_self_destructing,
            )
            async with self.db.session() as session:
                await repo.set_media_result(
                    session,
                    media["id"],
                    status=result.status,
                    local_path=result.local_path,
                    sha256=result.sha256,
                    size_bytes=result.size_bytes,
                    error=result.error,
                )
            if result.ok:
                self.counters["media_downloaded"] += 1
                if result.local_path:
                    local_files.append(self.storage.root.parent / result.local_path)

        # ---- archive chat -------------------------------------------------
        if not job.send_to_archive or not self.sender.enabled:
            if archive_status in {"pending", "failed"}:
                async with self.db.session() as session:
                    await repo.set_archive_status(session, job.message_pk, "disabled")
            return
        if archive_status not in {"pending", "failed"}:
            return

        try:
            archive_message_id = await self.sender.send_message_record(
                extracted, source_message=raw_message, local_files=local_files
            )
        except (ArchiveSendError, FloodWaitTooLong) as exc:
            log.warning("Archive copy of message %s deferred: %s", job.message_id, exc)
            async with self.db.session() as session:
                await repo.set_archive_status(
                    session, job.message_pk, "failed", error=str(exc)
                )
            self.counters["archive_failed"] += 1
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not copy message %s to archive chat", job.message_id)
            async with self.db.session() as session:
                await repo.set_archive_status(
                    session, job.message_pk, "failed", error=f"{type(exc).__name__}: {exc}"
                )
            self.counters["archive_failed"] += 1
            return

        async with self.db.session() as session:
            await repo.set_archive_status(
                session, job.message_pk, "sent", archive_message_id=archive_message_id
            )
        self.counters["archive_sent"] += 1
        if self.settings.archive_send_delay:
            await asyncio.sleep(self.settings.archive_send_delay)

    # ============================================================== recovery

    async def requeue_pending(self, limit: int = 500) -> int:
        """After a restart, pick up work that never finished."""
        async with self.db.session() as session:
            pending = await repo.pending_archive_messages(session, limit=limit)
            jobs = [
                ArchiveJob(
                    message_pk=row.id,
                    chat_id=row.chat_id,
                    message_id=row.message_id,
                    send_to_archive=True,
                )
                for row in pending
            ]
        for job in jobs:
            await self.queue.put(job)
        if jobs:
            log.info("Resuming %s unfinished archive job(s) from the previous run", len(jobs))
        return len(jobs)

    async def _refetch(self, chat_id: int, message_id: int) -> Any:
        """Fetch a message again (used after a restart, or on stale references)."""
        try:
            result = await self.client.get_messages(chat_id, ids=message_id)
        except Exception as exc:  # noqa: BLE001
            log.info("Could not refetch message %s in chat %s: %s", message_id, chat_id, exc)
            return None
        if isinstance(result, list):
            result = result[0] if result else None
        if result is None:
            log.info(
                "Message %s in chat %s is gone (deleted before we could fetch its media)",
                message_id,
                chat_id,
            )
        return result

    async def _safe_notice(self, text: str) -> None:
        try:
            await self.sender.send_notice(text)
        except (ArchiveSendError, FloodWaitTooLong) as exc:
            log.warning("Could not deliver archive notice: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not deliver archive notice: %s: %s", type(exc).__name__, exc)

    # ------------------------------------------------------------------ helper
    def _extracted_from_row(self, row: Message) -> ExtractedMessage:
        """Rebuild a record from the database when the original is unavailable.

        Used on restart, and when the source message was deleted before the
        slow half of the pipeline got to it — the archived copy still gets
        delivered to the archive chat.
        """
        media = [
            MediaInfo(
                kind=item.kind,
                file_name=item.file_name,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                tg_file_id=item.tg_file_id,
                tg_dc_id=item.tg_dc_id,
                duration_seconds=item.duration_seconds,
                width=item.width,
                height=item.height,
            )
            for item in row.media
        ]
        return ExtractedMessage(
            chat=ChatInfo(id=row.chat_id, title=None, type="unknown"),
            message_id=row.message_id,
            sender=SenderInfo(id=row.sender_id, display_name=row.sender_name),
            date=row.date,
            edit_date=row.last_edit_date,
            outgoing=row.outgoing,
            message_type=row.message_type,
            text=row.text,
            grouped_id=row.grouped_id,
            reply_to_message_id=row.reply_to_message_id,
            links=loads(row.links_json) or [],
            forward=loads(row.forward_json),
            contact=loads(row.contact_json),
            geo=loads(row.geo_json),
            poll=loads(row.poll_json),
            reactions=loads(row.reactions_json),
            service_action=row.service_action,
            has_media=row.has_media,
            media=media,
            is_self_destructing=row.is_self_destructing,
            ttl_seconds=row.ttl_seconds,
        )

    def summary(self) -> str:
        return (
            f"captured={self.counters['captured']} "
            f"duplicates={self.counters['duplicates']} "
            f"edits={self.counters['edits']} "
            f"deletions={self.counters['deletions']} "
            f"media={self.counters['media_downloaded']} "
            f"archive_sent={self.counters['archive_sent']} "
            f"archive_failed={self.counters['archive_failed']}"
        )
