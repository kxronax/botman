"""SQLAlchemy models for the archive.

Design notes
------------
* Only portable column types are used (``BigInteger``, ``Text``, ``Boolean``,
  ``DateTime``), so the same models run on SQLite today and PostgreSQL later.
* ``(chat_id, message_id)`` is the natural key of a Telegram message and
  carries a UNIQUE constraint — that constraint *is* the deduplication.
* Raw Telegram payloads are kept as JSON text so information we do not model
  explicitly today is still recoverable from the archive tomorrow.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Chat(Base):
    """A dialog: private chat, basic group, supergroup or channel."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(Text, default="unknown")
    title: Mapped[Optional[str]] = mapped_column(Text)
    username: Mapped[Optional[str]] = mapped_column(Text)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Sender(Base):
    """A user (or channel acting as an author) who sent archived messages."""

    __tablename__ = "senders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(Text, default="user")  # user | channel | unknown
    username: Mapped[Optional[str]] = mapped_column(Text)
    first_name: Mapped[Optional[str]] = mapped_column(Text)
    last_name: Mapped[Optional[str]] = mapped_column(Text)
    display_name: Mapped[Optional[str]] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Message(Base):
    """One archived Telegram message."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_messages_chat_message"),
        Index("ix_messages_message_id", "message_id"),
        Index("ix_messages_date", "date"),
        Index("ix_messages_archive_status", "archive_status"),
        Index("ix_messages_grouped_id", "grouped_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- identity -------------------------------------------------------
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    grouped_id: Mapped[Optional[int]] = mapped_column(BigInteger)  # media album id

    # --- who / when -----------------------------------------------------
    date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("senders.id"))
    sender_name: Mapped[Optional[str]] = mapped_column(Text)
    outgoing: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- content --------------------------------------------------------
    message_type: Mapped[str] = mapped_column(Text, default="text")
    text: Mapped[Optional[str]] = mapped_column(Text)
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    links_json: Mapped[Optional[str]] = mapped_column(Text)
    forward_json: Mapped[Optional[str]] = mapped_column(Text)
    contact_json: Mapped[Optional[str]] = mapped_column(Text)
    geo_json: Mapped[Optional[str]] = mapped_column(Text)
    poll_json: Mapped[Optional[str]] = mapped_column(Text)
    reactions_json: Mapped[Optional[str]] = mapped_column(Text)
    service_action: Mapped[Optional[str]] = mapped_column(Text)
    raw_json: Mapped[Optional[str]] = mapped_column(Text)

    # --- media ----------------------------------------------------------
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    # Self-destructing ("view once" / timer) media. See docs/limitations:
    # content of these is intentionally NOT stored, only the fact it existed.
    is_self_destructing: Mapped[bool] = mapped_column(Boolean, default=False)
    ttl_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    # --- lifecycle ------------------------------------------------------
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    edit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_edit_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    # --- bookkeeping ----------------------------------------------------
    source: Mapped[str] = mapped_column(Text, default="live")  # live | import
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # pending | sent | skipped | failed | disabled
    archive_status: Mapped[str] = mapped_column(Text, default="pending")
    archive_error: Mapped[Optional[str]] = mapped_column(Text)
    archive_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    archive_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    archive_attempts: Mapped[int] = mapped_column(Integer, default=0)

    versions: Mapped[list["MessageVersion"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageVersion.version_no",
        lazy="selectin",
    )
    media: Mapped[list["MediaFile"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class MessageVersion(Base):
    """A point-in-time copy of a message's content.

    Version 1 is what we saw first; every edit that actually changes the
    content appends a new row, so the original text survives the edit.
    """

    __tablename__ = "message_versions"
    __table_args__ = (
        UniqueConstraint("message_pk", "version_no", name="uq_version_no"),
        Index("ix_versions_message_pk", "message_pk"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    edit_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_json: Mapped[Optional[str]] = mapped_column(Text)

    message: Mapped[Message] = relationship(back_populates="versions")


class MediaFile(Base):
    """A media attachment belonging to an archived message."""

    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_media_message_pk", "message_pk"),
        Index("ix_media_status", "download_status"),
        Index("ix_media_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, default="other")
    file_name: Mapped[Optional[str]] = mapped_column(Text)
    mime_type: Mapped[Optional[str]] = mapped_column(Text)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Telegram's own identifiers for the file (stable document/photo id).
    tg_file_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    tg_dc_id: Mapped[Optional[int]] = mapped_column(Integer)
    local_path: Mapped[Optional[str]] = mapped_column(Text)
    sha256: Mapped[Optional[str]] = mapped_column(Text)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    # pending | downloaded | skipped_too_large | skipped_disabled |
    # skipped_self_destructing | failed | not_downloadable
    download_status: Mapped[str] = mapped_column(Text, default="pending")
    download_error: Mapped[Optional[str]] = mapped_column(Text)
    downloaded_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    message: Mapped[Message] = relationship(back_populates="media")


class ImportState(Base):
    """Resume point for the historical import, one row per chat."""

    __tablename__ = "import_state"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # Lowest message id we have already imported; the next pass continues below it.
    lowest_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    highest_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AppState(Base):
    """Generic key/value state (schema version, counters, …)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DeletionEvent(Base):
    """Every deletion Telegram tells us about, including unmatched ones.

    If a message was deleted before we ever saw it, its content is gone for
    good (Telegram does not send content in delete updates). We still record
    that a deletion happened so the gap is visible in the archive.
    """

    __tablename__ = "deletion_events"
    __table_args__ = (Index("ix_deletion_seen_at", "seen_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    matched_message_pk: Mapped[Optional[int]] = mapped_column(Integer)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
