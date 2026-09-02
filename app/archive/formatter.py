"""Render archived messages as readable text for the private archive chat.

Output is plain text (no Markdown/HTML parse mode) so message content can never
be mangled — or, worse, turn into unintended formatting — when it contains
asterisks, underscores or angle brackets.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from ..telegram.extract import ExtractedMessage, MediaInfo
from ..utils.text import MAX_CAPTION_LENGTH, human_size, truncate

HEADER = "[PRIVATE ARCHIVE]"

TYPE_LABELS = {
    "text": "TEXT",
    "photo": "PHOTO",
    "video": "VIDEO",
    "video_note": "VIDEO MESSAGE",
    "voice": "VOICE",
    "audio": "AUDIO",
    "document": "DOCUMENT",
    "sticker": "STICKER",
    "gif": "GIF / ANIMATION",
    "contact": "CONTACT",
    "geo": "LOCATION",
    "venue": "VENUE",
    "poll": "POLL",
    "link": "LINK",
    "dice": "DICE",
    "game": "GAME",
    "story": "STORY",
    "invoice": "INVOICE",
    "service": "SERVICE MESSAGE",
    "unsupported": "UNSUPPORTED MEDIA",
}


def _fmt_date(value: Optional[dt.datetime]) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _chat_label(extracted: ExtractedMessage) -> str:
    chat = extracted.chat
    name = chat.title or f"id {chat.id}"
    if chat.username:
        name = f"{name} (@{chat.username})"
    return f"{name} [{chat.type}, id {chat.id}]"


def _sender_label(extracted: ExtractedMessage) -> str:
    sender = extracted.sender
    if sender.id is None and sender.display_name is None:
        return "unknown"
    name = sender.display_name or f"id {sender.id}"
    if sender.username:
        name = f"{name} (@{sender.username})"
    if sender.id is not None:
        name = f"{name} [id {sender.id}]"
    if extracted.outgoing:
        name = f"{name} (me)"
    return name


def _media_lines(media: MediaInfo) -> list[str]:
    lines = [f"  - kind: {media.kind}"]
    if media.file_name:
        lines.append(f"    file name: {media.file_name}")
    if media.mime_type:
        lines.append(f"    mime type: {media.mime_type}")
    if media.size_bytes:
        lines.append(f"    size: {human_size(media.size_bytes)} ({media.size_bytes} bytes)")
    if media.tg_file_id:
        lines.append(f"    telegram file id: {media.tg_file_id}")
    if media.duration_seconds:
        lines.append(f"    duration: {media.duration_seconds:.0f}s")
    if media.width and media.height:
        lines.append(f"    dimensions: {media.width}x{media.height}")
    return lines


def format_message(
    extracted: ExtractedMessage,
    *,
    local_paths: Optional[list[str]] = None,
    note: Optional[str] = None,
) -> str:
    """The main archive record for a message."""
    lines: list[str] = [HEADER, ""]
    lines.append(f"Chat: {_chat_label(extracted)}")
    lines.append(f"Sender: {_sender_label(extracted)}")
    lines.append(f"Date: {_fmt_date(extracted.date)}")
    lines.append(f"Message ID: {extracted.message_id}")
    lines.append(f"Chat ID: {extracted.chat.id}")
    lines.append(f"Type: {TYPE_LABELS.get(extracted.message_type, extracted.message_type.upper())}")
    lines.append(f"Direction: {'outgoing' if extracted.outgoing else 'incoming'}")

    if extracted.grouped_id:
        lines.append(f"Album group: {extracted.grouped_id}")
    if extracted.reply_to_message_id:
        lines.append(f"Reply to message: {extracted.reply_to_message_id}")

    if extracted.forward:
        fwd = extracted.forward
        origin = fwd.get("from_name") or fwd.get("from_id") or "hidden origin"
        lines.append(f"Forwarded from: {origin}")
        if fwd.get("date"):
            lines.append(f"  original date: {fwd['date']}")
        if fwd.get("post_author"):
            lines.append(f"  post author: {fwd['post_author']}")
        if fwd.get("channel_post"):
            lines.append(f"  original post id: {fwd['channel_post']}")

    if extracted.service_action:
        lines.append(f"Service action: {extracted.service_action}")

    if extracted.is_self_destructing:
        ttl = extracted.ttl_seconds
        kind = "view once" if ttl and ttl > 10**6 else f"self-destructing after {ttl}s"
        lines.append(f"Self-destructing media detected: {kind}")
        lines.append(
            "  Content NOT archived by design — see README, section "
            "'Self-destructing / view-once media'."
        )

    if extracted.media:
        lines.append("")
        lines.append("Media:")
        for media in extracted.media:
            lines.extend(_media_lines(media))
        if local_paths:
            for path in local_paths:
                lines.append(f"    saved to: {path}")

    if extracted.contact:
        contact = extracted.contact
        lines.append("")
        lines.append("Contact:")
        full_name = " ".join(
            part for part in (contact.get("first_name"), contact.get("last_name")) if part
        )
        lines.append(f"  name: {full_name or 'unknown'}")
        lines.append(f"  phone: {contact.get('phone_number') or 'unknown'}")
        if contact.get("user_id"):
            lines.append(f"  user id: {contact['user_id']}")

    if extracted.geo:
        geo = extracted.geo
        lines.append("")
        lines.append("Location:")
        lines.append(f"  coordinates: {geo.get('lat')}, {geo.get('long')}")
        if geo.get("title"):
            lines.append(f"  venue: {geo['title']}")
        if geo.get("address"):
            lines.append(f"  address: {geo['address']}")
        if geo.get("period_seconds"):
            lines.append(f"  live for: {geo['period_seconds']}s")

    if extracted.poll:
        poll = extracted.poll
        lines.append("")
        lines.append("Poll:")
        lines.append(f"  question: {poll.get('question')}")
        for index, answer in enumerate(poll.get("answers") or [], start=1):
            lines.append(f"  {index}. {answer}")

    if extracted.links:
        lines.append("")
        lines.append("Links:")
        for link in extracted.links:
            lines.append(f"  {link}")

    if extracted.reactions:
        summary = ", ".join(
            f"{item['reaction']} x{item['count']}" for item in extracted.reactions
        )
        lines.append("")
        lines.append(f"Reactions: {summary}")

    caption_label = "Caption" if extracted.media else "Original message"
    lines.append("")
    lines.append(f"{caption_label}:")
    lines.append(extracted.text if extracted.text else "(no text)")

    if note:
        lines.append("")
        lines.append(f"Note: {note}")

    return "\n".join(lines)


def format_caption(extracted: ExtractedMessage) -> str:
    """A compact header used as the caption of a forwarded media file."""
    lines = [
        HEADER,
        f"Chat: {_chat_label(extracted)}",
        f"Sender: {_sender_label(extracted)}",
        f"Date: {_fmt_date(extracted.date)}",
        f"Message ID: {extracted.message_id}",
        f"Type: {TYPE_LABELS.get(extracted.message_type, extracted.message_type.upper())}",
    ]
    if extracted.text:
        lines.append("")
        lines.append("Caption:")
        lines.append(extracted.text)
    return truncate("\n".join(lines), MAX_CAPTION_LENGTH)


def format_deletion(message: Any, chat_title: Optional[str]) -> str:
    """Notice appended to the archive when a message we hold gets deleted."""
    lines = [
        HEADER + " — MESSAGE DELETED IN ORIGINAL CHAT",
        "",
        f"Chat: {chat_title or 'unknown'} [id {message.chat_id}]",
        f"Message ID: {message.message_id}",
        f"Original date: {_fmt_date(message.date)}",
        f"Sender: {message.sender_name or message.sender_id or 'unknown'}",
        f"Deleted detected at: {_fmt_date(dt.datetime.now(dt.timezone.utc))}",
        "",
        "The archived copy above is preserved. Original content:",
        truncate(message.text or "(no text)", 2000),
    ]
    if message.has_media:
        lines.append("")
        lines.append("This message had media; the archived file (if downloaded) is kept.")
    return "\n".join(lines)


def format_unknown_deletion(message_ids: list[int], chat_id: Optional[int]) -> str:
    """Notice for a deletion of something we never captured."""
    return "\n".join(
        [
            HEADER + " — DELETION OF UNARCHIVED MESSAGE(S)",
            "",
            f"Chat ID: {chat_id if chat_id is not None else 'unknown (private chat or basic group)'}",
            f"Message IDs: {', '.join(str(mid) for mid in message_ids)}",
            f"Detected at: {_fmt_date(dt.datetime.now(dt.timezone.utc))}",
            "",
            "These messages were deleted before the archiver captured them.",
            "Telegram does not include content in deletion updates, so the",
            "original content cannot be recovered.",
        ]
    )


def format_edit(
    message: Any,
    extracted: ExtractedMessage,
    version_no: int,
    previous_text: Optional[str],
) -> str:
    """Notice recording an edit, keeping both versions visible."""
    return "\n".join(
        [
            HEADER + " — MESSAGE EDITED",
            "",
            f"Chat: {_chat_label(extracted)}",
            f"Sender: {_sender_label(extracted)}",
            f"Message ID: {extracted.message_id}",
            f"Original date: {_fmt_date(message.date)}",
            f"Edited at: {_fmt_date(extracted.edit_date)}",
            f"New version number: {version_no}",
            "",
            f"Version {version_no - 1} (previous):",
            truncate(previous_text or "(no text)", 1500),
            "",
            f"Version {version_no} (current):",
            truncate(extracted.text or "(no text)", 1500),
        ]
    )
