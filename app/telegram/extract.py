"""Turn a raw Telethon ``Message`` into plain, storable metadata.

Everything here is pure inspection of what the API already handed us — no
extra network calls beyond Telethon's own entity cache, and no attempt to
reach data the account does not have access to.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from telethon.tl import types
from telethon.utils import get_peer_id

from ..utils.jsonutil import dumps, to_json

log = logging.getLogger(__name__)

# Telegram marks "view once" media with this sentinel TTL.
VIEW_ONCE_TTL = 0x7FFFFFFF


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatInfo:
    id: int
    type: str = "unknown"
    title: Optional[str] = None
    username: Optional[str] = None


@dataclass(slots=True)
class SenderInfo:
    id: Optional[int] = None
    kind: str = "unknown"
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    display_name: Optional[str] = None
    is_bot: bool = False
    is_self: bool = False


@dataclass(slots=True)
class MediaInfo:
    kind: str = "other"
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    tg_file_id: Optional[int] = None
    tg_dc_id: Optional[int] = None
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    downloadable: bool = True


@dataclass(slots=True)
class ExtractedMessage:
    chat: ChatInfo
    message_id: int
    sender: SenderInfo = field(default_factory=SenderInfo)
    date: Optional[dt.datetime] = None
    edit_date: Optional[dt.datetime] = None
    outgoing: bool = False
    message_type: str = "text"
    text: Optional[str] = None
    grouped_id: Optional[int] = None
    reply_to_message_id: Optional[int] = None
    links: list[str] = field(default_factory=list)
    forward: Optional[dict[str, Any]] = None
    contact: Optional[dict[str, Any]] = None
    geo: Optional[dict[str, Any]] = None
    poll: Optional[dict[str, Any]] = None
    reactions: Optional[list[dict[str, Any]]] = None
    service_action: Optional[str] = None
    has_media: bool = False
    media: list[MediaInfo] = field(default_factory=list)
    is_self_destructing: bool = False
    ttl_seconds: Optional[int] = None
    raw_json: Optional[str] = None


# ---------------------------------------------------------------------------
# chat / sender
# ---------------------------------------------------------------------------


def chat_type_of(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "private"
    if isinstance(entity, types.Chat) or isinstance(entity, types.ChatForbidden):
        return "group"
    if isinstance(entity, types.Channel):
        if getattr(entity, "megagroup", False):
            return "supergroup"
        if getattr(entity, "gigagroup", False):
            return "supergroup"
        return "channel"
    if isinstance(entity, types.ChannelForbidden):
        return "channel"
    return "unknown"


def display_name_of(entity: Any) -> Optional[str]:
    if entity is None:
        return None
    if isinstance(entity, types.User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        if not name and entity.username:
            name = f"@{entity.username}"
        if not name:
            name = f"User {entity.id}"
        if getattr(entity, "deleted", False):
            name = name or "Deleted account"
        return name
    title = getattr(entity, "title", None)
    if title:
        return title
    return None


def build_chat_info(chat_id: int, entity: Any) -> ChatInfo:
    return ChatInfo(
        id=chat_id,
        type=chat_type_of(entity),
        title=display_name_of(entity),
        username=getattr(entity, "username", None),
    )


def build_sender_info(entity: Any, self_id: Optional[int] = None) -> SenderInfo:
    if entity is None:
        return SenderInfo()
    if isinstance(entity, types.User):
        return SenderInfo(
            id=entity.id,
            kind="user",
            username=entity.username,
            first_name=entity.first_name,
            last_name=entity.last_name,
            display_name=display_name_of(entity),
            is_bot=bool(entity.bot),
            is_self=self_id is not None and entity.id == self_id,
        )
    if isinstance(entity, (types.Channel, types.Chat)):
        return SenderInfo(
            id=get_peer_id(entity),
            kind="channel",
            username=getattr(entity, "username", None),
            display_name=display_name_of(entity),
        )
    return SenderInfo()


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------


def _document_kind(document: types.Document) -> tuple[str, MediaInfo]:
    """Classify a document and pull its attributes out."""
    info = MediaInfo(
        mime_type=getattr(document, "mime_type", None),
        size_bytes=getattr(document, "size", None),
        tg_file_id=getattr(document, "id", None),
        tg_dc_id=getattr(document, "dc_id", None),
    )
    kind = "document"
    is_animated = False
    for attr in getattr(document, "attributes", None) or []:
        if isinstance(attr, types.DocumentAttributeFilename):
            info.file_name = attr.file_name
        elif isinstance(attr, types.DocumentAttributeSticker):
            kind = "sticker"
        elif isinstance(attr, types.DocumentAttributeAnimated):
            is_animated = True
        elif isinstance(attr, types.DocumentAttributeAudio):
            info.duration_seconds = attr.duration
            kind = "voice" if attr.voice else "audio"
        elif isinstance(attr, types.DocumentAttributeVideo):
            info.duration_seconds = attr.duration
            info.width = attr.w
            info.height = attr.h
            if getattr(attr, "round_message", False):
                kind = "video_note"
            elif kind not in {"sticker", "voice", "audio"}:
                kind = "video"
        elif isinstance(attr, types.DocumentAttributeImageSize):
            info.width = attr.w
            info.height = attr.h

    if is_animated and kind in {"video", "document"}:
        kind = "gif"
    info.kind = kind
    return kind, info


def _photo_info(photo: types.Photo) -> MediaInfo:
    width = height = None
    size_bytes = None
    for size in getattr(photo, "sizes", None) or []:
        w = getattr(size, "w", None)
        h = getattr(size, "h", None)
        if w and h and (width is None or w > width):
            width, height = w, h
        candidate = getattr(size, "size", None)
        if isinstance(candidate, int) and (size_bytes is None or candidate > size_bytes):
            size_bytes = candidate
    return MediaInfo(
        kind="photo",
        mime_type="image/jpeg",
        size_bytes=size_bytes,
        tg_file_id=getattr(photo, "id", None),
        tg_dc_id=getattr(photo, "dc_id", None),
        width=width,
        height=height,
        file_name=f"photo_{getattr(photo, 'id', 'unknown')}.jpg",
    )


def classify_media(message: types.Message) -> tuple[str, list[MediaInfo], bool, Optional[int]]:
    """Return ``(message_type, media_infos, self_destructing, ttl_seconds)``."""
    media = getattr(message, "media", None)
    if media is None:
        return ("text", [], False, None)

    ttl = getattr(media, "ttl_seconds", None)
    self_destructing = ttl is not None
    view_once = ttl == VIEW_ONCE_TTL

    if isinstance(media, types.MessageMediaPhoto):
        photo = media.photo
        if photo is None or isinstance(photo, types.PhotoEmpty):
            return ("photo", [], self_destructing, ttl)
        info = _photo_info(photo)
        info.downloadable = not self_destructing
        return ("photo", [info], self_destructing, ttl)

    if isinstance(media, types.MessageMediaDocument):
        document = media.document
        if document is None or isinstance(document, types.DocumentEmpty):
            return ("document", [], self_destructing, ttl)
        kind, info = _document_kind(document)
        info.downloadable = not self_destructing
        return (kind, [info], self_destructing, ttl)

    if isinstance(media, types.MessageMediaContact):
        return ("contact", [], False, None)
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive)):
        return ("geo", [], False, None)
    if isinstance(media, types.MessageMediaVenue):
        return ("venue", [], False, None)
    if isinstance(media, types.MessageMediaPoll):
        return ("poll", [], False, None)
    if isinstance(media, types.MessageMediaWebPage):
        return ("link", [], False, None)
    if isinstance(media, types.MessageMediaDice):
        return ("dice", [], False, None)
    if isinstance(media, types.MessageMediaGame):
        return ("game", [], False, None)
    if isinstance(media, types.MessageMediaInvoice):
        return ("invoice", [], False, None)
    if isinstance(media, types.MessageMediaStory):
        return ("story", [], False, None)
    if isinstance(media, types.MessageMediaUnsupported):
        # Telegram added a media type newer than this Telethon build understands.
        return ("unsupported", [], False, None)

    return (type(media).__name__, [], False, None)


# ---------------------------------------------------------------------------
# structured fields
# ---------------------------------------------------------------------------


def extract_links(message: types.Message) -> list[str]:
    links: list[str] = []
    text = message.message or ""
    for entity in getattr(message, "entities", None) or []:
        if isinstance(entity, types.MessageEntityTextUrl):
            links.append(entity.url)
        elif isinstance(entity, types.MessageEntityUrl):
            links.append(text[entity.offset : entity.offset + entity.length])
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
        url = getattr(media.webpage, "url", None)
        if url:
            links.append(url)
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [link for link in links if not (link in seen or seen.add(link))]


def extract_forward(message: types.Message) -> Optional[dict[str, Any]]:
    fwd = getattr(message, "fwd_from", None)
    if fwd is None:
        return None
    origin_id = None
    if fwd.from_id is not None:
        try:
            origin_id = get_peer_id(fwd.from_id)
        except (TypeError, ValueError):
            origin_id = None
    return {
        "from_id": origin_id,
        "from_name": fwd.from_name,
        "date": fwd.date.isoformat() if fwd.date else None,
        "channel_post": fwd.channel_post,
        "post_author": fwd.post_author,
        "saved_from_msg_id": fwd.saved_from_msg_id,
        "imported": bool(getattr(fwd, "imported", False)),
    }


def extract_contact(message: types.Message) -> Optional[dict[str, Any]]:
    media = getattr(message, "media", None)
    if not isinstance(media, types.MessageMediaContact):
        return None
    return {
        "phone_number": media.phone_number,
        "first_name": media.first_name,
        "last_name": media.last_name,
        "user_id": media.user_id,
        "vcard": media.vcard or None,
    }


def extract_geo(message: types.Message) -> Optional[dict[str, Any]]:
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaVenue):
        point = media.geo
        return {
            "kind": "venue",
            "lat": getattr(point, "lat", None),
            "long": getattr(point, "long", None),
            "title": media.title,
            "address": media.address,
            "provider": media.provider,
            "venue_id": media.venue_id,
        }
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive)):
        point = media.geo
        data: dict[str, Any] = {
            "kind": "geo_live" if isinstance(media, types.MessageMediaGeoLive) else "geo",
            "lat": getattr(point, "lat", None),
            "long": getattr(point, "long", None),
            "accuracy_radius": getattr(point, "accuracy_radius", None),
        }
        if isinstance(media, types.MessageMediaGeoLive):
            data["period_seconds"] = media.period
            data["heading"] = media.heading
        return data
    return None


def extract_poll(message: types.Message) -> Optional[dict[str, Any]]:
    media = getattr(message, "media", None)
    if not isinstance(media, types.MessageMediaPoll):
        return None
    poll = media.poll
    answers = []
    for answer in poll.answers or []:
        text = answer.text
        # Newer layers wrap answer text in a TextWithEntities object.
        answers.append(getattr(text, "text", text))
    question = getattr(poll.question, "text", poll.question)
    return {
        "question": question,
        "answers": answers,
        "closed": bool(poll.closed),
        "public_voters": bool(poll.public_voters),
        "multiple_choice": bool(poll.multiple_choice),
        "quiz": bool(poll.quiz),
    }


def extract_reactions(message: types.Message) -> Optional[list[dict[str, Any]]]:
    """Reactions as the API exposes them at the time we read the message."""
    reactions = getattr(message, "reactions", None)
    if reactions is None:
        return None
    out: list[dict[str, Any]] = []
    for result in getattr(reactions, "results", None) or []:
        reaction = result.reaction
        if isinstance(reaction, types.ReactionEmoji):
            label = reaction.emoticon
        elif isinstance(reaction, types.ReactionCustomEmoji):
            label = f"custom:{reaction.document_id}"
        else:
            label = type(reaction).__name__
        out.append({"reaction": label, "count": result.count, "chosen": bool(result.chosen_order is not None)})
    return out or None


def extract_service_action(message: Any) -> Optional[str]:
    action = getattr(message, "action", None)
    if action is None:
        return None
    return type(action).__name__


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


async def extract_message(
    message: Any,
    *,
    chat_entity: Any = None,
    sender_entity: Any = None,
    self_id: Optional[int] = None,
    store_raw: bool = True,
) -> ExtractedMessage:
    """Build an :class:`ExtractedMessage` from a Telethon message object."""
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None and getattr(message, "peer_id", None) is not None:
        chat_id = get_peer_id(message.peer_id)
    if chat_id is None:
        raise ValueError("Message has no resolvable chat id")

    if chat_entity is None:
        try:
            chat_entity = await message.get_chat()
        except Exception as exc:  # entity may be uncached or inaccessible
            log.debug("Could not resolve chat %s: %s", chat_id, exc)

    if sender_entity is None:
        try:
            sender_entity = await message.get_sender()
        except Exception as exc:
            log.debug("Could not resolve sender for message %s: %s", message.id, exc)

    chat = build_chat_info(chat_id, chat_entity)
    sender = build_sender_info(sender_entity, self_id=self_id)
    if sender.id is None:
        # Anonymous admins and channel posts have no user sender.
        from_id = getattr(message, "from_id", None)
        if from_id is not None:
            try:
                sender.id = get_peer_id(from_id)
            except (TypeError, ValueError):
                pass
        if sender.display_name is None and chat.type == "channel":
            sender.display_name = chat.title
            sender.kind = "channel"

    is_service = isinstance(message, types.MessageService)
    if is_service:
        message_type = "service"
        media_infos: list[MediaInfo] = []
        self_destructing = False
        ttl = None
    else:
        message_type, media_infos, self_destructing, ttl = classify_media(message)

    reply_to_id = None
    reply_to = getattr(message, "reply_to", None)
    if reply_to is not None:
        reply_to_id = getattr(reply_to, "reply_to_msg_id", None)

    text = getattr(message, "message", None) or None

    return ExtractedMessage(
        chat=chat,
        message_id=int(message.id),
        sender=sender,
        date=getattr(message, "date", None),
        edit_date=getattr(message, "edit_date", None),
        outgoing=bool(getattr(message, "out", False)),
        message_type=message_type,
        text=text,
        grouped_id=getattr(message, "grouped_id", None),
        reply_to_message_id=reply_to_id,
        links=[] if is_service else extract_links(message),
        forward=None if is_service else extract_forward(message),
        contact=None if is_service else extract_contact(message),
        geo=None if is_service else extract_geo(message),
        poll=None if is_service else extract_poll(message),
        reactions=None if is_service else extract_reactions(message),
        service_action=extract_service_action(message) if is_service else None,
        has_media=bool(getattr(message, "media", None)) and not is_service,
        media=media_infos,
        is_self_destructing=self_destructing,
        ttl_seconds=ttl,
        raw_json=to_json(message) if store_raw else None,
    )


def content_fingerprint(extracted: ExtractedMessage) -> str:
    """What counts as "the content" when deciding whether an edit changed it."""
    from ..utils.text import content_hash

    return content_hash(
        extracted.text,
        extracted.message_type,
        [(m.kind, m.tg_file_id, m.file_name, m.size_bytes) for m in extracted.media],
        dumps(extracted.poll) if extracted.poll else None,
        dumps(extracted.geo) if extracted.geo else None,
        dumps(extracted.contact) if extracted.contact else None,
    )
