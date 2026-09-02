"""Telegram client creation and interactive authorisation.

Uses Telethon's MTProto client, i.e. the same official client API a desktop or
mobile Telegram app uses. The archiver therefore sees exactly what your own
account sees — nothing more.

The session file is what keeps you logged in between runs. It is a credential:
anyone holding it can act as your account. It lives in ``data/session/`` with
restrictive permissions and is never logged or sent anywhere.
"""

from __future__ import annotations

import getpass
import logging
import sys
from typing import Any, Optional

from telethon import TelegramClient
from telethon import errors
from telethon.sessions import StringSession

from ..config import Settings

log = logging.getLogger(__name__)

DEVICE_MODEL = "Personal Archiver"
SYSTEM_VERSION = "1.0"
APP_VERSION = "1.0"


def build_client(settings: Settings) -> TelegramClient:
    """Create the client without connecting."""
    settings.ensure_directories()

    if settings.session_string:
        # Used on hosts without persistent disk (Railway without a volume).
        session: Any = StringSession(settings.session_string)
        log.info("Using session from SESSION_STRING environment variable")
    else:
        session = str(settings.session_path)

    return TelegramClient(
        session,
        settings.api_id,
        settings.api_hash,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
        # Telethon sleeps through short FloodWaits itself; longer ones are
        # raised so our retry helper can log and decide.
        flood_sleep_threshold=settings.flood_sleep_threshold,
        connection_retries=None,  # retry forever — survives long outages
        retry_delay=5,
        auto_reconnect=True,
        request_retries=5,
    )


async def authorize(client: TelegramClient, settings: Settings) -> Any:
    """Connect and, on first run, walk through the login flow.

    Telegram sends the login code to your existing Telegram apps (or by SMS).
    Nothing entered here is written to the log.
    """
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        return me

    if not sys.stdin.isatty():
        # Typical on Railway or under systemd: nobody can type the login code.
        raise SystemExit(
            "This session is not authorised and there is no terminal to log in from.\n"
            "Log in once on your own machine with `python main.py`, then run\n"
            "`python main.py --export-session` and put the result in the host's\n"
            "SESSION_STRING environment variable (see README, Railway section)."
        )

    print()
    print("=" * 62)
    print("  First run — Telegram authorisation required")
    print("=" * 62)
    print("  The login code will arrive in your existing Telegram app.")
    print("  Nothing you type here is written to the log file.")
    print()

    phone = settings.phone or input("Phone number (international format, e.g. +49…): ").strip()

    try:
        await client.send_code_request(phone)
    except errors.PhoneNumberInvalidError:
        raise SystemExit("That phone number is not valid. Check the international format.")

    while True:
        code = input("Login code from Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
            break
        except errors.SessionPasswordNeededError:
            # Two-factor authentication is enabled on the account.
            while True:
                password = getpass.getpass("Two-step verification password (hidden): ")
                try:
                    await client.sign_in(password=password)
                    break
                except errors.PasswordHashInvalidError:
                    print("  Wrong password, try again.")
            break
        except errors.PhoneCodeInvalidError:
            print("  Wrong code, try again.")
        except errors.PhoneCodeExpiredError:
            print("  That code expired. Requesting a new one…")
            await client.send_code_request(phone)

    me = await client.get_me()
    print()
    print("  Authorisation complete. The session is saved, so this is a one-off.")
    if settings.session_string:
        print("  (Session came from SESSION_STRING; nothing was written to disk.)")
    else:
        print(f"  Session file: {settings.session_path}.session — keep it private.")
    print()
    return me


def describe_me(me: Any) -> str:
    if me is None:
        return "unknown account"
    username = getattr(me, "username", None)
    if username:
        return f"@{username}"
    name = " ".join(
        part for part in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if part
    )
    return name or f"id {getattr(me, 'id', '?')}"


async def export_session_string(client: TelegramClient) -> Optional[str]:
    """Return a portable session string (for deployments without a disk).

    Treat the result exactly like a password: it grants full account access.
    """
    try:
        # StringSession.save() serialises any session object that exposes
        # dc_id / auth_key / server_address / port, including the file session.
        return StringSession.save(client.session)
    except Exception as exc:  # pragma: no cover - depends on session backend
        log.error("Could not export session string: %s", exc)
        return None
