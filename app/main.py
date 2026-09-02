"""Entry point and run modes.

    python main.py                 monitor new messages (the normal mode)
    python main.py --import        import existing history, then exit
    python main.py --import --monitor   import first, then keep monitoring
    python main.py --stats         print what is in the archive and exit
    python main.py --check         verify config, login and archive chat
    python main.py --export-session  print a portable session string
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any, Optional

from .archive import repository as repo
from .archive.database import Database
from .archive.pipeline import ArchivePipeline
from .archive.sender import ArchiveSendError, ArchiveSender
from .archive.storage import MediaStorage
from .config import ConfigError, Settings, load_settings
from .logging_setup import setup_logging
from .telegram.client import authorize, build_client, describe_me, export_session_string
from .telegram.handlers import register_handlers
from .telegram.importer import HistoryImporter

log = logging.getLogger(__name__)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Personal Telegram archiver — archives your own accessible chats.",
    )
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="import existing history before doing anything else",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="keep monitoring after an import (default when no other mode is given)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="messages per chat during import (overrides IMPORT_LIMIT_PER_CHAT)",
    )
    parser.add_argument(
        "--chats",
        type=int,
        default=None,
        help="how many chats to import (overrides IMPORT_CHAT_LIMIT, 0 = all)",
    )
    parser.add_argument(
        "--only-chat",
        type=int,
        action="append",
        dest="only_chats",
        help="import only this chat id (repeatable)",
    )
    parser.add_argument("--stats", action="store_true", help="print archive statistics and exit")
    parser.add_argument(
        "--check", action="store_true", help="verify configuration and access, then exit"
    )
    parser.add_argument(
        "--export-session",
        action="store_true",
        help="print a portable session string (a secret — treat it like a password)",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    return parser.parse_args(argv)


class Application:
    """Wires the components together and owns their lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.sqlalchemy_url)
        self.storage = MediaStorage(settings.media_dir)
        self.client: Any = None
        self.sender: Optional[ArchiveSender] = None
        self.pipeline: Optional[ArchivePipeline] = None
        self.me: Any = None
        self._shutdown = asyncio.Event()

    async def startup(self, *, connect: bool = True) -> None:
        self.settings.ensure_directories()
        self.storage.ensure_ready()
        self.storage.cleanup_partials()
        await self.db.create_schema()
        if not await self.db.healthcheck():
            raise SystemExit("Database is not reachable — check DATABASE_URL / disk space.")

        if not connect:
            return

        self.client = build_client(self.settings)
        self.me = await authorize(self.client, self.settings)
        print(f"Connected as {describe_me(self.me)}")

        self.sender = ArchiveSender(self.client, self.settings)
        if self.sender.enabled:
            try:
                target = await self.sender.resolve_target()
                name = getattr(target, "title", None) or describe_me(target)
                print(f"Archive chat: {name} (id {self.settings.archive_chat_id})")
            except ArchiveSendError as exc:
                log.error("Archive chat unusable: %s", exc)
                print(
                    "WARNING: archive chat is not usable — messages will still be "
                    "archived locally, but no copies will be sent."
                )
                self.settings.send_to_archive_chat = False
        else:
            print(
                "Archive chat: not configured (set ARCHIVE_CHAT_ID to also receive "
                "copies in Telegram). Local archive is active."
            )

        self.pipeline = ArchivePipeline(
            client=self.client,
            database=self.db,
            settings=self.settings,
            storage=self.storage,
            sender=self.sender,
            self_id=getattr(self.me, "id", None),
        )

    async def shutdown(self) -> None:
        if self.pipeline is not None:
            print("Finishing queued work before exit…")
            await self.pipeline.stop_worker(drain=True)
            log.info("Session summary: %s", self.pipeline.summary())
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        await self.db.close()

    # ------------------------------------------------------------------ modes
    async def run_monitor(self) -> None:
        assert self.pipeline is not None
        await self.pipeline.start_worker()
        await self.pipeline.requeue_pending()

        register_handlers(self.client, self.pipeline)
        print("Monitoring chats… (press Ctrl+C to stop)")

        # Ask Telegram for updates we missed while offline. Telegram's update
        # backlog is limited, so this is best-effort, not a substitute for
        # --import.
        try:
            await self.client.catch_up()
        except Exception as exc:  # noqa: BLE001
            log.debug("catch_up failed (non-fatal): %s", exc)

        await self._wait_for_shutdown()

    async def run_import(self, args: argparse.Namespace) -> None:
        assert self.pipeline is not None
        importer = HistoryImporter(
            client=self.client,
            database=self.db,
            settings=self.settings,
            pipeline=self.pipeline,
        )
        print("Starting historical import — this can take a while.")
        totals = await importer.run(
            limit_per_chat=args.limit,
            chat_limit=args.chats,
            only_chats=set(args.only_chats) if args.only_chats else None,
        )
        print(
            f"Import complete: {totals['messages']} new message(s) "
            f"from {totals['chats']} chat(s)."
        )

    async def run_stats(self) -> None:
        async with self.db.session() as session:
            data = await repo.stats(session)
            newest = await repo.newest_message_date(session)
        print("Archive statistics")
        print("------------------")
        for key, value in data.items():
            print(f"  {key.replace('_', ' '):<24} {value}")
        print(f"  newest message date      {newest or 'n/a'}")
        print(f"  data directory           {self.settings.data_dir.resolve()}")

    async def run_check(self) -> None:
        print("Configuration:")
        for key, value in self.settings.safe_summary().items():
            print(f"  {key:<18} {value}")
        print("\nAccount: connected and authorised.")
        if self.sender and self.sender.enabled:
            print("Archive chat: reachable.")
        print("Database: reachable.")
        print("\nEverything checks out. Run `python main.py` to start monitoring.")

    # ------------------------------------------------------------------ signals
    async def _wait_for_shutdown(self) -> None:
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            print("\nStopping…")
            self._shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: _request_stop())

        disconnected = asyncio.ensure_future(self.client.disconnected)
        stop_wait = asyncio.ensure_future(self._shutdown.wait())
        done, pending = await asyncio.wait(
            {disconnected, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if disconnected in done and not self._shutdown.is_set():
            log.warning("Telegram connection closed — exiting so the process can restart")


async def async_main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        settings.log_level,
        settings.logs_dir,
        secrets=[settings.api_hash, settings.session_string or ""],
    )
    log.info("Starting personal Telegram archiver")
    log.debug("Effective configuration: %s", settings.safe_summary())

    needs_connection = not args.stats
    app = Application(settings)

    try:
        await app.startup(connect=needs_connection)

        if args.stats:
            await app.run_stats()
            return 0

        if args.export_session:
            token = await export_session_string(app.client)
            if not token:
                print("Could not export the session string.", file=sys.stderr)
                return 1
            print("\nSESSION_STRING (secret — treat it like your password):\n")
            print(token)
            print("\nStore it in the environment of your host, never in git.\n")
            return 0

        if args.check:
            await app.run_check()
            return 0

        if args.do_import:
            assert app.pipeline is not None
            await app.run_import(args)
            if not args.monitor:
                return 0

        await app.run_monitor()
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - report cleanly instead of a traceback wall
        log.exception("Fatal error: %s", exc)
        return 1
    finally:
        await app.shutdown()


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
