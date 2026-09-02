"""Live event handlers.

What Telegram actually delivers, and what that means for archiving:

``NewMessage``
    Fires for every message in every dialog the account can see. The handler
    persists it immediately (see :meth:`ArchivePipeline.capture`) — that speed
    is the only reason a message deleted moments later still ends up archived.

``MessageEdited``
    Carries the *new* content only. Because the previous version is already in
    our database, we can store both and show the difference.

``MessageDeleted``
    Carries only message ids — **never the deleted content**. If we captured
    the message earlier, our copy is kept and flagged as deleted. If we never
    saw it, its content is gone: Telegram does not offer any way to retrieve a
    deleted message, and this project does not pretend otherwise.

``UpdateMessageReactions``
    Reaction changes, applied to the stored message when we hold it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from telethon import events
from telethon.tl import types
from telethon.utils import get_peer_id

from ..archive.pipeline import ArchivePipeline
from .extract import extract_reactions

log = logging.getLogger(__name__)


def register_handlers(client: Any, pipeline: ArchivePipeline) -> None:
    """Attach every live handler to the client."""

    @client.on(events.NewMessage())
    async def _on_new_message(event: events.NewMessage.Event) -> None:
        try:
            await pipeline.capture(event.message, source="live")
        except Exception:  # noqa: BLE001 - a bad message must not stop the stream
            log.exception("Failed to handle new message")

    @client.on(events.MessageEdited())
    async def _on_message_edited(event: events.MessageEdited.Event) -> None:
        try:
            await pipeline.handle_edit(event.message)
        except Exception:  # noqa: BLE001
            log.exception("Failed to handle edited message")

    @client.on(events.MessageDeleted())
    async def _on_message_deleted(event: events.MessageDeleted.Event) -> None:
        try:
            # event.chat_id is set for channels/supergroups only; for private
            # chats and basic groups the update genuinely has no peer, which
            # the repository layer handles by matching on the global id space.
            await pipeline.handle_deletion(list(event.deleted_ids), event.chat_id)
        except Exception:  # noqa: BLE001
            log.exception("Failed to handle deleted message(s)")

    @client.on(events.Raw(types.UpdateMessageReactions))
    async def _on_reactions(update: types.UpdateMessageReactions) -> None:
        try:
            chat_id = _peer_to_id(update.peer)
            if chat_id is None:
                return
            reactions = extract_reactions(_ReactionCarrier(update.reactions))
            await pipeline.handle_reactions(chat_id, update.msg_id, reactions or [])
        except Exception:  # noqa: BLE001
            log.debug("Could not handle reaction update", exc_info=True)

    log.debug("Live handlers registered")


class _ReactionCarrier:
    """Adapter so :func:`extract_reactions` can read a raw reactions update."""

    __slots__ = ("reactions",)

    def __init__(self, reactions: Any) -> None:
        self.reactions = reactions


def _peer_to_id(peer: Any) -> Optional[int]:
    try:
        return get_peer_id(peer)
    except (TypeError, ValueError):
        return None
