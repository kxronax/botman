"""Async database engine and session management.

SQLite is the default and needs a little care: WAL mode so a reader (a future
web panel) never blocks the writer, and ``busy_timeout`` so concurrent writes
wait instead of raising ``database is locked``. Switching to PostgreSQL is a
matter of setting ``DATABASE_URL`` — nothing else in the code changes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

log = logging.getLogger(__name__)


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        self._is_sqlite = url.startswith("sqlite")
        kwargs: dict[str, object] = {"echo": echo, "future": True}
        if self._is_sqlite:
            # aiosqlite serialises access through a single thread anyway; the
            # timeout makes concurrent writers wait rather than fail.
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if self._is_sqlite:
            self._install_sqlite_pragmas()
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    def _install_sqlite_pragmas(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    async def create_schema(self) -> None:
        """Create any missing tables. Safe to run on every startup."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.debug("Database schema ready")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope: commits on success, rolls back on error."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # pragma: no cover - depends on environment
            log.error("Database healthcheck failed: %s", exc)
            return False

    async def close(self) -> None:
        await self.engine.dispose()
