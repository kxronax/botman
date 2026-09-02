"""Retry helpers for Telegram API calls.

Two distinct failure modes need different treatment:

* ``FloodWaitError`` — Telegram tells us exactly how long to wait. We sleep
  that long (plus a small margin) and retry. This is not an error, it is
  normal back-pressure, so it is logged at INFO.
* transient network / server errors — retried with exponential backoff.

Anything else propagates to the caller.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

from telethon import errors

log = logging.getLogger(__name__)

T = TypeVar("T")

# Errors that are worth retrying after a short backoff.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    errors.ServerError,
    errors.TimedOutError,
    errors.RpcCallFailError,
    ConnectionError,
    asyncio.TimeoutError,
    OSError,
)

# Errors that mean "this will never work" — retrying only wastes quota.
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    errors.ChatWriteForbiddenError,
    errors.ChannelPrivateError,
    errors.MessageIdInvalidError,
    errors.UserBannedInChannelError,
    errors.ChatAdminRequiredError,
)

# Cap a single FloodWait sleep. Longer waits are surfaced to the caller so the
# worker can move on instead of blocking the whole archiver for hours.
MAX_FLOOD_WAIT_SECONDS = 3600


class FloodWaitTooLong(RuntimeError):
    """Raised when Telegram asks us to wait longer than we are willing to."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"FloodWait of {seconds}s exceeds the maximum we will sleep")
        self.seconds = seconds


async def call_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    description: str,
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_flood_wait: int = MAX_FLOOD_WAIT_SECONDS,
) -> T:
    """Await ``func()``, handling FloodWait and transient errors.

    ``func`` must be a zero-argument coroutine factory (use ``functools.partial``
    or a lambda) because it may be called several times.
    """
    attempt = 0
    while True:
        try:
            return await func()
        except errors.FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 0) or 0)
            if seconds > max_flood_wait:
                log.warning(
                    "FloodWait: %s needs %ss which is over the %ss limit — skipping for now",
                    description,
                    seconds,
                    max_flood_wait,
                )
                raise FloodWaitTooLong(seconds) from exc
            wait_for = seconds + 2
            log.info("FloodWait: waiting %ss before retrying %s", wait_for, description)
            await asyncio.sleep(wait_for)
            # FloodWait retries do not count towards max_retries: Telegram has
            # told us the request is valid, just too soon.
            continue
        except PERMANENT_ERRORS as exc:
            log.warning("%s failed permanently: %s", description, type(exc).__name__)
            raise
        except TRANSIENT_ERRORS as exc:
            attempt += 1
            if attempt > max_retries:
                log.error(
                    "%s failed after %s retries: %s: %s",
                    description,
                    max_retries,
                    type(exc).__name__,
                    exc,
                )
                raise
            delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.1)  # jitter
            log.warning(
                "%s failed (%s: %s) — retry %s/%s in %.1fs",
                description,
                type(exc).__name__,
                exc,
                attempt,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
