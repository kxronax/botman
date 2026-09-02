"""End-to-end smoke test that runs without a Telegram connection.

It builds real Telethon message objects, pushes them through the pipeline
against a temporary SQLite database and checks the behaviour that matters:
deduplication, edit versioning, deletion flagging, self-destructing-media
policy, media path layout and crash recovery of the archive queue.

Run it with:  python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.tl import types  # noqa: E402

from app.archive import repository as repo  # noqa: E402
from app.archive.database import Database  # noqa: E402
from app.archive.formatter import format_deletion, format_message  # noqa: E402
from app.archive.models import Message  # noqa: E402
from app.archive.pipeline import ArchivePipeline  # noqa: E402
from app.archive.sender import ArchiveSender  # noqa: E402
from app.archive.storage import MediaStorage  # noqa: E402
from app.archive.formatter import format_unknown_deletion  # noqa: E402
from app.config import Settings  # noqa: E402
from app.telegram.extract import extract_message  # noqa: E402
from app.utils.text import split_message  # noqa: E402

PASSED: list[str] = []


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED: {label}")
    PASSED.append(label)
    print(f"  ok  {label}")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

USER = types.User(id=555, first_name="Ivan", last_name="Petrov", username="ivan")
CHAT_ENTITY = USER  # a private chat's entity is the other user


def make_message(
    message_id: int,
    text: str | None = "hello",
    *,
    media=None,
    edit_date: dt.datetime | None = None,
    out: bool = False,
) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerUser(user_id=555),
        date=dt.datetime(2026, 9, 2, 18, 30, tzinfo=dt.timezone.utc),
        message=text or "",
        out=out,
        from_id=types.PeerUser(user_id=555),
        media=media,
        edit_date=edit_date,
    )


def photo_media(ttl: int | None = None) -> types.MessageMediaPhoto:
    photo = types.Photo(
        id=987654321,
        access_hash=1,
        file_reference=b"\x00",
        date=dt.datetime.now(dt.timezone.utc),
        sizes=[types.PhotoSize(type="x", w=1280, h=720, size=204800)],
        dc_id=2,
        has_stickers=False,
        video_sizes=[],
    )
    return types.MessageMediaPhoto(photo=photo, ttl_seconds=ttl)


def voice_media() -> types.MessageMediaDocument:
    document = types.Document(
        id=111222333,
        access_hash=1,
        file_reference=b"\x00",
        date=dt.datetime.now(dt.timezone.utc),
        mime_type="audio/ogg",
        size=48000,
        dc_id=2,
        attributes=[
            types.DocumentAttributeAudio(duration=7, voice=True),
            types.DocumentAttributeFilename(file_name="voice.ogg"),
        ],
    )
    return types.MessageMediaDocument(document=document)


class StubClient:
    """Stands in for TelegramClient; the pipeline must never need more."""

    def __init__(self) -> None:
        self.refetched: list[int] = []

    async def get_messages(self, chat_id, ids=None):
        self.refetched.append(ids)
        return None

    async def download_media(self, message, file=None):  # pragma: no cover
        raise AssertionError("no download should be attempted in this test")


def build_settings(tmp: Path) -> Settings:
    return Settings(
        api_id=1,
        api_hash="not-a-real-hash",
        data_dir=tmp,
        send_to_archive_chat=False,
        archive_chat_id=None,
        download_media=False,
    )


# --------------------------------------------------------------------------
# the test
# --------------------------------------------------------------------------


async def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="archiver-test-"))
    settings = build_settings(tmp)
    settings.ensure_directories()

    db = Database(settings.sqlalchemy_url)
    await db.create_schema()
    check(await db.healthcheck(), "database is reachable and schema created")

    storage = MediaStorage(settings.media_dir)
    storage.ensure_ready()
    client = StubClient()
    sender = ArchiveSender(client, settings)
    pipeline = ArchivePipeline(
        client=client,
        database=db,
        settings=settings,
        storage=storage,
        sender=sender,
        self_id=999,
    )

    # -- 1. capture ---------------------------------------------------------
    message = make_message(12345, "Original text")
    job = await pipeline.capture(message, chat_entity=CHAT_ENTITY, sender_entity=USER)
    check(job is not None, "a new message is captured")

    async with db.session() as session:
        row = await repo.get_message(session, 555, 12345)
        check(row is not None and row.text == "Original text", "message stored with its text")
        check(len(row.versions) == 1, "version 1 recorded on capture")

    # -- 2. deduplication ---------------------------------------------------
    duplicate = await pipeline.capture(
        make_message(12345, "Original text"), chat_entity=CHAT_ENTITY, sender_entity=USER
    )
    check(duplicate is None, "re-processing the same message id is a no-op (dedup)")
    check(pipeline.counters["duplicates"] == 1, "the duplicate was counted, not stored")

    # -- 3. edits keep the original ----------------------------------------
    edited = make_message(
        12345, "Edited text", edit_date=dt.datetime(2026, 9, 2, 19, 0, tzinfo=dt.timezone.utc)
    )
    await pipeline.handle_edit(edited, chat_entity=CHAT_ENTITY)
    async with db.session() as session:
        row = await repo.get_message(session, 555, 12345)
        texts = [v.text for v in row.versions]
        check(texts == ["Original text", "Edited text"], "both versions kept, in order")
        check(row.edit_count == 1, "edit counter incremented")
        check(row.text == "Edited text", "message row mirrors the newest version")

    # An edit update that changes nothing must not create a version.
    await pipeline.handle_edit(
        make_message(12345, "Edited text", edit_date=edited.edit_date), chat_entity=CHAT_ENTITY
    )
    async with db.session() as session:
        row = await repo.get_message(session, 555, 12345)
        check(len(row.versions) == 2, "a no-op edit does not add a version")

    # -- 4. deletion preserves the copy ------------------------------------
    await pipeline.handle_deletion([12345], None)
    async with db.session() as session:
        row = await repo.get_message(session, 555, 12345)
        check(row.is_deleted and row.deleted_at is not None, "deleted message flagged")
        check(row.text == "Edited text", "archived content survives the deletion")
        check(len(row.versions) == 2, "version history survives the deletion")

    # A deletion we never captured is recorded, honestly, as unrecoverable.
    await pipeline.handle_deletion([999999], None)
    async with db.session() as session:
        events = await repo.find_deleted_candidates(session, [999999], None)
        check(events == [], "an uncaptured deletion matches no stored message")

    # -- 5. self-destructing media policy ----------------------------------
    view_once = make_message(200, "", media=photo_media(ttl=0x7FFFFFFF))
    await pipeline.capture(view_once, chat_entity=CHAT_ENTITY, sender_entity=USER)
    async with db.session() as session:
        row = await repo.get_message(session, 555, 200)
        check(row.is_self_destructing, "view-once media is detected and logged")
        check(
            row.media[0].download_status == "skipped_self_destructing",
            "view-once content is not downloaded (policy)",
        )

    # -- 6. media classification and paths ---------------------------------
    voice = make_message(201, "listen", media=voice_media())
    await pipeline.capture(voice, chat_entity=CHAT_ENTITY, sender_entity=USER)
    async with db.session() as session:
        row = await repo.get_message(session, 555, 201)
        check(row.message_type == "voice", "voice message classified correctly")
        check(row.media[0].mime_type == "audio/ogg", "media mime type captured")
        check(row.media[0].duration_seconds == 7, "media duration captured")

    path = storage.target_path("photo", -1001234567890, 42, 0, "my photo.jpg")
    check("photos" in path.parts, "photos are routed to media/photos/")
    check(path.name == "42_0_my photo.jpg", "media file name includes the message id")
    check(
        MediaStorage.part_path(path).name.endswith(".part"),
        "downloads use a .part file until complete",
    )
    MediaStorage.part_path(path).write_bytes(b"partial")
    check(storage.cleanup_partials() == 1, "stale partial downloads are cleaned up on start")

    # -- 7. formatter -------------------------------------------------------
    extracted = await extract_message(
        make_message(12345, "Original text"),
        chat_entity=CHAT_ENTITY,
        sender_entity=USER,
        self_id=999,
    )
    record = format_message(extracted)
    check("[PRIVATE ARCHIVE]" in record, "archive record carries the header")
    check("Message ID: 12345" in record, "archive record carries the message id")
    check("Chat ID: 555" in record, "archive record carries the chat id")
    check("Ivan Petrov" in record, "archive record carries the sender name")
    check("2026-09-02 18:30:00 UTC" in record, "archive record carries the date")

    async with db.session() as session:
        stored = await repo.get_message(session, 555, 12345)
    check("DELETED" in format_deletion(stored, "Ivan"), "deletion notice renders")
    check(
        "cannot be recovered" in format_unknown_deletion([1, 2], None),
        "notice is honest that lost content cannot be recovered",
    )

    long_text = "x" * 9000
    chunks = split_message(long_text)
    check(all(len(c) <= 4096 for c in chunks), "long records are split under Telegram's limit")
    check(sum(len(c) for c in chunks) == 9000, "splitting loses no characters")

    # -- 8. crash recovery --------------------------------------------------
    async with db.session() as session:
        row = await repo.get_message(session, 555, 201)
        await repo.set_archive_status(session, row.id, "pending")
    recovered = await pipeline.requeue_pending()
    check(recovered >= 1, "unfinished archive jobs are requeued after a restart")

    # Processing a job whose source message is gone must not crash.
    job = await pipeline.queue.get()
    await pipeline.process_job(job)
    check(True, "a job whose source message vanished is handled without crashing")

    # -- 9. stats -----------------------------------------------------------
    async with db.session() as session:
        data = await repo.stats(session)
    check(data["messages"] == 3, "statistics count stored messages")
    check(data["deleted"] == 1, "statistics count deletions")
    check(data["self_destructing_seen"] == 1, "statistics count self-destructing media")

    # -- 10. FloodWait and retry behaviour ---------------------------------
    from telethon import errors  # noqa: PLC0415 - local to this section

    from app.utils.ratelimit import FloodWaitTooLong, call_with_retry

    attempts = {"n": 0}

    async def flooded():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise errors.FloodWaitError(request=None)
        return "done"

    # Patch the reported wait down so the test does not actually sleep long.
    original_init = errors.FloodWaitError.__init__

    def short_flood(self, request):
        original_init(self, request)
        self.seconds = 0

    errors.FloodWaitError.__init__ = short_flood
    try:
        result = await call_with_retry(flooded, description="test call", max_retries=1)
        check(result == "done" and attempts["n"] == 2, "FloodWait is waited out and retried")

        long_flood = errors.FloodWaitError(request=None)
        long_flood.seconds = 99999

        async def always_flooded():
            raise long_flood

        raised = False
        try:
            await call_with_retry(
                always_flooded, description="long flood", max_flood_wait=10
            )
        except FloodWaitTooLong:
            raised = True
        check(raised, "an unreasonably long FloodWait is surfaced, not slept through")

        transient = {"n": 0}

        async def flaky():
            transient["n"] += 1
            if transient["n"] < 3:
                raise ConnectionError("network blip")
            return "recovered"

        result = await call_with_retry(
            flaky, description="flaky call", max_retries=5, base_delay=0.01
        )
        check(result == "recovered", "transient network errors are retried with backoff")

        async def forbidden():
            raise errors.MessageIdInvalidError(request=None)

        permanent_raised = False
        try:
            await call_with_retry(forbidden, description="permanent", max_retries=5)
        except errors.MessageIdInvalidError:
            permanent_raised = True
        check(permanent_raised, "permanent errors are not retried")
    finally:
        errors.FloodWaitError.__init__ = original_init

    # -- 11. filters --------------------------------------------------------
    settings.exclude_chats = {555}
    excluded = await pipeline.capture(
        make_message(777), chat_entity=CHAT_ENTITY, sender_entity=USER
    )
    check(excluded is None, "EXCLUDE_CHATS filter is honoured")
    settings.exclude_chats = set()
    settings.archive_chat_id = 555
    check(not settings.chat_is_allowed(555, "private"), "the archive chat itself is never archived")

    await db.close()
    print(f"\n{len(PASSED)} checks passed.")


if __name__ == "__main__":
    asyncio.run(run())
