"""Initial import of existing history.

Runs over the dialogs the account can access and archives past messages, newest
first. Progress is checkpointed per chat in the ``import_state`` table, so an
interrupted import resumes where it stopped instead of starting over — and
messages already archived by the live monitor are skipped by the same
``(chat_id, message_id)`` deduplication.

Volume control lives in the config: ``IMPORT_LIMIT_PER_CHAT`` and
``IMPORT_CHAT_LIMIT``. Importing everything from every chat is possible but
can take hours and will hit FloodWait repeatedly on large accounts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from telethon import errors

from ..archive import repository as repo
from ..archive.database import Database
from ..archive.pipeline import ArchivePipeline
from ..config import Settings
from .extract import chat_type_of, display_name_of

log = logging.getLogger(__name__)


class HistoryImporter:
    def __init__(
        self,
        *,
        client: Any,
        database: Database,
        settings: Settings,
        pipeline: ArchivePipeline,
    ) -> None:
        self.client = client
        self.db = database
        self.settings = settings
        self.pipeline = pipeline

    async def run(
        self,
        *,
        limit_per_chat: Optional[int] = None,
        chat_limit: Optional[int] = None,
        only_chats: Optional[set[int]] = None,
    ) -> dict[str, int]:
        """Import history. Safe to run repeatedly — it resumes and dedups."""
        limit_per_chat = (
            self.settings.import_limit_per_chat if limit_per_chat is None else limit_per_chat
        )
        chat_limit = self.settings.import_chat_limit if chat_limit is None else chat_limit

        totals = {"chats": 0, "messages": 0, "skipped_chats": 0}

        dialogs = await self._list_dialogs()
        log.info("Found %s dialog(s) accessible to this account", len(dialogs))

        for dialog in dialogs:
            if chat_limit and totals["chats"] >= chat_limit:
                log.info("Reached IMPORT_CHAT_LIMIT=%s — stopping", chat_limit)
                break

            chat_id = dialog["id"]
            chat_type = dialog["type"]
            title = dialog["title"] or str(chat_id)

            if only_chats and chat_id not in only_chats:
                continue
            if not self.settings.chat_is_allowed(chat_id, chat_type):
                totals["skipped_chats"] += 1
                log.debug("Skipping %s (filtered out by configuration)", title)
                continue

            log.info(
                "Importing from %s [%s, id %s] (up to %s messages)",
                title,
                chat_type,
                chat_id,
                limit_per_chat or "all",
            )
            try:
                imported = await self._import_chat(
                    dialog["entity"], chat_id, limit_per_chat
                )
            except errors.ChannelPrivateError:
                log.warning("No longer have access to %s — skipping", title)
                continue
            except Exception as exc:  # noqa: BLE001 - one bad chat must not stop the import
                log.exception("Import failed for %s: %s", title, exc)
                continue

            totals["chats"] += 1
            totals["messages"] += imported
            log.info("  → %s new message(s) archived from %s", imported, title)

        log.info(
            "Import finished: %s new message(s) from %s chat(s)",
            totals["messages"],
            totals["chats"],
        )
        return totals

    # ------------------------------------------------------------------ dialogs
    async def _list_dialogs(self) -> list[dict[str, Any]]:
        dialogs: list[dict[str, Any]] = []
        while True:
            try:
                async for dialog in self.client.iter_dialogs():
                    entity = dialog.entity
                    dialogs.append(
                        {
                            "id": dialog.id,
                            "entity": entity,
                            "type": chat_type_of(entity),
                            "title": display_name_of(entity) or dialog.name,
                        }
                    )
                return dialogs
            except errors.FloodWaitError as exc:
                log.info("FloodWait: waiting %ss before listing dialogs", exc.seconds)
                await asyncio.sleep(exc.seconds + 2)
                dialogs.clear()

    # ------------------------------------------------------------------ one chat
    async def _import_chat(self, entity: Any, chat_id: int, limit: int) -> int:
        async with self.db.session() as session:
            state = await repo.get_import_state(session, chat_id)
            offset_id = state.lowest_message_id if state and state.lowest_message_id else 0
            already_complete = bool(state and state.completed)

        if already_complete:
            log.debug("Chat %s already fully imported", chat_id)
            return 0

        imported = 0
        lowest_seen: Optional[int] = None
        highest_seen: Optional[int] = None
        exhausted = False

        while True:
            remaining = (limit - imported) if limit else None
            if remaining is not None and remaining <= 0:
                break

            batch: list[Any] = []
            try:
                async for message in self.client.iter_messages(
                    entity,
                    limit=min(remaining, 100) if remaining else 100,
                    offset_id=offset_id,
                ):
                    batch.append(message)
            except errors.FloodWaitError as exc:
                log.info("FloodWait: waiting %ss before continuing import", exc.seconds)
                await asyncio.sleep(exc.seconds + 2)
                continue
            except (errors.ServerError, errors.TimedOutError, ConnectionError) as exc:
                log.warning("Transient error while reading history (%s) — retrying", exc)
                await asyncio.sleep(5)
                continue

            if not batch:
                exhausted = True
                break

            for message in batch:
                message_id = int(message.id)
                lowest_seen = message_id if lowest_seen is None else min(lowest_seen, message_id)
                highest_seen = (
                    message_id if highest_seen is None else max(highest_seen, message_id)
                )
                job = await self.pipeline.capture(
                    message,
                    source="import",
                    chat_entity=entity,
                    send_to_archive=self.settings.import_send_to_archive_chat,
                    download_media=self.settings.import_media,
                    enqueue=False,
                )
                if job is None:
                    continue  # filtered out or already archived
                imported += 1
                # Import processes jobs inline: it keeps memory flat and the
                # request rate predictable on a long backfill.
                await self.pipeline.process_job(job)

            offset_id = batch[-1].id

            async with self.db.session() as session:
                await repo.update_import_state(
                    session,
                    chat_id,
                    lowest_message_id=lowest_seen,
                    highest_message_id=highest_seen,
                    imported_delta=0,
                )

        async with self.db.session() as session:
            await repo.update_import_state(
                session,
                chat_id,
                lowest_message_id=lowest_seen,
                highest_message_id=highest_seen,
                imported_delta=imported,
                completed=exhausted,
            )
        return imported
