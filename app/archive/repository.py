"""Data access layer.

Everything that touches the database goes through here, so swapping SQLite for
PostgreSQL — or adding a web panel on top — does not require rewriting the
Telegram side. Statements avoid dialect-specific upsert syntax on purpose.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..telegram.extract import ExtractedMessage, content_fingerprint
from ..utils.jsonutil import dumps
from .models import (
    AppState,
    Chat,
    DeletionEvent,
    ImportState,
    MediaFile,
    Message,
    MessageVersion,
    Sender,
    utcnow,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# chats & senders
# ---------------------------------------------------------------------------


async def upsert_chat(session: AsyncSession, info) -> Chat:
    chat = await session.get(Chat, info.id)
    if chat is None:
        chat = Chat(
            id=info.id,
            type=info.type,
            title=info.title,
            username=info.username,
        )
        session.add(chat)
        await session.flush()
        return chat
    if info.type and info.type != "unknown":
        chat.type = info.type
    if info.title:
        chat.title = info.title
    if info.username:
        chat.username = info.username
    chat.last_seen = utcnow()
    return chat


async def upsert_sender(session: AsyncSession, info) -> Optional[Sender]:
    if info.id is None:
        return None
    sender = await session.get(Sender, info.id)
    if sender is None:
        sender = Sender(
            id=info.id,
            kind=info.kind,
            username=info.username,
            first_name=info.first_name,
            last_name=info.last_name,
            display_name=info.display_name,
            is_bot=info.is_bot,
            is_self=info.is_self,
        )
        session.add(sender)
        await session.flush()
        return sender
    if info.username:
        sender.username = info.username
    if info.first_name:
        sender.first_name = info.first_name
    if info.last_name:
        sender.last_name = info.last_name
    if info.display_name:
        sender.display_name = info.display_name
    sender.is_bot = info.is_bot or sender.is_bot
    sender.is_self = info.is_self or sender.is_self
    return sender


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


async def get_message(
    session: AsyncSession, chat_id: int, message_id: int
) -> Optional[Message]:
    result = await session.execute(
        select(Message).where(Message.chat_id == chat_id, Message.message_id == message_id)
    )
    return result.scalar_one_or_none()


async def message_exists(session: AsyncSession, chat_id: int, message_id: int) -> bool:
    result = await session.execute(
        select(Message.id).where(
            Message.chat_id == chat_id, Message.message_id == message_id
        )
    )
    return result.scalar_one_or_none() is not None


async def existing_message_ids(
    session: AsyncSession, chat_id: int, message_ids: Sequence[int]
) -> set[int]:
    """Bulk dedup check used by the importer."""
    if not message_ids:
        return set()
    result = await session.execute(
        select(Message.message_id).where(
            Message.chat_id == chat_id, Message.message_id.in_(list(message_ids))
        )
    )
    return set(result.scalars().all())


def _apply_content(message: Message, extracted: ExtractedMessage) -> None:
    message.message_type = extracted.message_type
    message.text = extracted.text
    message.grouped_id = extracted.grouped_id
    message.reply_to_message_id = extracted.reply_to_message_id
    message.links_json = dumps(extracted.links) if extracted.links else None
    message.forward_json = dumps(extracted.forward) if extracted.forward else None
    message.contact_json = dumps(extracted.contact) if extracted.contact else None
    message.geo_json = dumps(extracted.geo) if extracted.geo else None
    message.poll_json = dumps(extracted.poll) if extracted.poll else None
    message.reactions_json = dumps(extracted.reactions) if extracted.reactions else None
    message.service_action = extracted.service_action
    message.raw_json = extracted.raw_json
    message.has_media = extracted.has_media
    message.is_self_destructing = extracted.is_self_destructing
    message.ttl_seconds = extracted.ttl_seconds


async def add_message(
    session: AsyncSession,
    extracted: ExtractedMessage,
    *,
    source: str = "live",
    archive_status: str = "pending",
) -> tuple[Message, bool]:
    """Insert a message if it is new.

    Returns ``(message, created)``. Deduplication is by ``(chat_id, message_id)``
    and is enforced by a UNIQUE constraint, so even a race between the live
    handler and the importer cannot produce a duplicate.
    """
    existing = await get_message(session, extracted.chat.id, extracted.message_id)
    if existing is not None:
        return existing, False

    await upsert_chat(session, extracted.chat)
    await upsert_sender(session, extracted.sender)

    message = Message(
        chat_id=extracted.chat.id,
        message_id=extracted.message_id,
        date=extracted.date,
        sender_id=extracted.sender.id,
        sender_name=extracted.sender.display_name,
        outgoing=extracted.outgoing,
        source=source,
        archive_status=archive_status,
        edit_count=0,
        last_edit_date=extracted.edit_date,
    )
    _apply_content(message, extracted)
    session.add(message)

    try:
        await session.flush()
    except IntegrityError:
        # Another path inserted the same message first — take theirs.
        await session.rollback()
        existing = await get_message(session, extracted.chat.id, extracted.message_id)
        if existing is not None:
            return existing, False
        raise

    session.add(
        MessageVersion(
            message_pk=message.id,
            version_no=1,
            text=extracted.text,
            content_hash=content_fingerprint(extracted),
            edit_date=extracted.edit_date,
            raw_json=extracted.raw_json,
        )
    )
    await session.flush()
    return message, True


async def add_media_records(
    session: AsyncSession, message: Message, extracted: ExtractedMessage
) -> list[MediaFile]:
    """Create one row per attachment, in the state it should start in."""
    if not extracted.media:
        return []
    # Query explicitly rather than touching ``message.media``: on a freshly
    # flushed object that relationship is not loaded yet and would trigger a
    # lazy load, which async SQLAlchemy cannot do implicitly.
    existing = await session.execute(
        select(MediaFile).where(MediaFile.message_pk == message.id)
    )
    already = list(existing.scalars().all())
    if already:  # recorded on an earlier pass
        return already

    rows: list[MediaFile] = []
    for info in extracted.media:
        if extracted.is_self_destructing:
            status = "skipped_self_destructing"
        elif not info.downloadable:
            status = "not_downloadable"
        else:
            status = "pending"
        row = MediaFile(
            message_pk=message.id,
            kind=info.kind,
            file_name=info.file_name,
            mime_type=info.mime_type,
            size_bytes=info.size_bytes,
            tg_file_id=info.tg_file_id,
            tg_dc_id=info.tg_dc_id,
            duration_seconds=info.duration_seconds,
            width=info.width,
            height=info.height,
            download_status=status,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def add_version_if_changed(
    session: AsyncSession, message: Message, extracted: ExtractedMessage
) -> Optional[MessageVersion]:
    """Append a new version when an edit actually changed the content.

    Telegram also fires edit updates for things like link-preview changes;
    comparing a content fingerprint keeps the version history meaningful.
    """
    fingerprint = content_fingerprint(extracted)
    result = await session.execute(
        select(MessageVersion)
        .where(MessageVersion.message_pk == message.id)
        .order_by(MessageVersion.version_no.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is not None and latest.content_hash == fingerprint:
        return None

    version = MessageVersion(
        message_pk=message.id,
        version_no=(latest.version_no + 1) if latest else 1,
        text=extracted.text,
        content_hash=fingerprint,
        edit_date=extracted.edit_date,
        raw_json=extracted.raw_json,
    )
    session.add(version)

    # The message row always mirrors the newest version; history lives in
    # message_versions so the original text is never overwritten.
    _apply_content(message, extracted)
    message.edit_count = (message.edit_count or 0) + 1
    message.last_edit_date = extracted.edit_date or utcnow()
    await session.flush()
    return version


async def update_reactions(
    session: AsyncSession, chat_id: int, message_id: int, reactions: list[dict]
) -> bool:
    message = await get_message(session, chat_id, message_id)
    if message is None:
        return False
    message.reactions_json = dumps(reactions) if reactions else None
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# deletions
# ---------------------------------------------------------------------------


async def find_deleted_candidates(
    session: AsyncSession, message_ids: Sequence[int], chat_id: Optional[int]
) -> list[Message]:
    """Resolve the messages referenced by a deletion update.

    Telegram's delete update for private chats and basic groups carries no
    chat id — but in those chats message ids come from a single per-account
    sequence, so an id identifies exactly one message. For channels and
    supergroups the update does carry the channel id and we match on both.
    """
    stmt = select(Message).where(Message.message_id.in_(list(message_ids)))
    if chat_id is not None:
        stmt = stmt.where(Message.chat_id == chat_id)
    else:
        # Restrict to non-channel chats, where the id space is account-global.
        stmt = stmt.join(Chat, Chat.id == Message.chat_id).where(
            Chat.type.in_(["private", "group", "unknown"])
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_deleted(session: AsyncSession, message: Message) -> bool:
    """Flag a message as deleted. The archived copy itself is never removed."""
    if message.is_deleted:
        return False
    message.is_deleted = True
    message.deleted_at = utcnow()
    await session.flush()
    return True


async def record_deletion_event(
    session: AsyncSession,
    message_id: int,
    chat_id: Optional[int],
    matched_pk: Optional[int],
) -> None:
    session.add(
        DeletionEvent(
            chat_id=chat_id, message_id=message_id, matched_message_pk=matched_pk
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# archive-chat delivery bookkeeping
# ---------------------------------------------------------------------------


async def set_archive_status(
    session: AsyncSession,
    message_pk: int,
    status: str,
    *,
    archive_message_id: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    values: dict[str, object] = {
        "archive_status": status,
        "archive_error": error,
        "archive_attempts": Message.archive_attempts + 1,
    }
    if archive_message_id is not None:
        values["archive_message_id"] = archive_message_id
    if status == "sent":
        values["archive_sent_at"] = utcnow()
    await session.execute(update(Message).where(Message.id == message_pk).values(**values))


async def pending_archive_messages(
    session: AsyncSession, limit: int = 200, max_attempts: int = 10
) -> list[Message]:
    """Messages whose archive-chat copy still has to be sent (crash recovery)."""
    result = await session.execute(
        select(Message)
        .where(
            Message.archive_status.in_(["pending", "failed"]),
            Message.archive_attempts < max_attempts,
        )
        .order_by(Message.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def pending_media(session: AsyncSession, limit: int = 200) -> list[MediaFile]:
    result = await session.execute(
        select(MediaFile)
        .where(MediaFile.download_status.in_(["pending", "failed"]), MediaFile.attempts < 5)
        .order_by(MediaFile.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def set_media_result(
    session: AsyncSession,
    media_pk: int,
    *,
    status: str,
    local_path: Optional[str] = None,
    sha256: Optional[str] = None,
    size_bytes: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    values: dict[str, object] = {
        "download_status": status,
        "download_error": error,
        "attempts": MediaFile.attempts + 1,
    }
    if local_path is not None:
        values["local_path"] = local_path
    if sha256 is not None:
        values["sha256"] = sha256
    if size_bytes is not None:
        values["size_bytes"] = size_bytes
    if status == "downloaded":
        values["downloaded_at"] = utcnow()
    await session.execute(update(MediaFile).where(MediaFile.id == media_pk).values(**values))


async def find_duplicate_media(
    session: AsyncSession, sha256: str, exclude_pk: int
) -> Optional[MediaFile]:
    """Find an already-downloaded identical file (content-level dedup)."""
    result = await session.execute(
        select(MediaFile)
        .where(
            MediaFile.sha256 == sha256,
            MediaFile.id != exclude_pk,
            MediaFile.download_status == "downloaded",
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# import state
# ---------------------------------------------------------------------------


async def get_import_state(session: AsyncSession, chat_id: int) -> Optional[ImportState]:
    return await session.get(ImportState, chat_id)


async def update_import_state(
    session: AsyncSession,
    chat_id: int,
    *,
    lowest_message_id: Optional[int] = None,
    highest_message_id: Optional[int] = None,
    imported_delta: int = 0,
    completed: Optional[bool] = None,
) -> ImportState:
    state = await session.get(ImportState, chat_id)
    if state is None:
        state = ImportState(chat_id=chat_id, imported_count=0)
        session.add(state)
    if lowest_message_id is not None:
        current = state.lowest_message_id
        state.lowest_message_id = (
            lowest_message_id if current is None else min(current, lowest_message_id)
        )
    if highest_message_id is not None:
        current_high = state.highest_message_id
        state.highest_message_id = (
            highest_message_id if current_high is None else max(current_high, highest_message_id)
        )
    if imported_delta:
        state.imported_count = (state.imported_count or 0) + imported_delta
    if completed is not None:
        state.completed = completed
    state.updated_at = utcnow()
    await session.flush()
    return state


# ---------------------------------------------------------------------------
# app state & stats
# ---------------------------------------------------------------------------


async def get_state(session: AsyncSession, key: str) -> Optional[str]:
    row = await session.get(AppState, key)
    return row.value if row else None


async def set_state(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
    await session.flush()


async def stats(session: AsyncSession) -> dict[str, int]:
    async def count(stmt) -> int:
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)

    return {
        "messages": await count(select(func.count()).select_from(Message)),
        "chats": await count(select(func.count()).select_from(Chat)),
        "media_files": await count(select(func.count()).select_from(MediaFile)),
        "media_downloaded": await count(
            select(func.count())
            .select_from(MediaFile)
            .where(MediaFile.download_status == "downloaded")
        ),
        "edited": await count(
            select(func.count()).select_from(Message).where(Message.edit_count > 0)
        ),
        "deleted": await count(
            select(func.count()).select_from(Message).where(Message.is_deleted.is_(True))
        ),
        "self_destructing_seen": await count(
            select(func.count())
            .select_from(Message)
            .where(Message.is_self_destructing.is_(True))
        ),
        "archive_pending": await count(
            select(func.count())
            .select_from(Message)
            .where(Message.archive_status.in_(["pending", "failed"]))
        ),
    }


async def newest_message_date(session: AsyncSession) -> Optional[dt.datetime]:
    result = await session.execute(select(func.max(Message.date)))
    return result.scalar_one_or_none()
