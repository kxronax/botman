"""Application configuration.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Nothing is hardcoded and no secret is ever echoed back:
:meth:`Settings.safe_summary` deliberately omits API_HASH and session data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or malformed."""


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean value, got {raw!r}")


def _get_int(name: str, default: Optional[int]) -> Optional[int]:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_id_set(name: str) -> set[int]:
    """Parse a comma separated list of chat ids."""
    raw = _get(name)
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"{name} must contain numeric chat ids separated by commas, got {chunk!r}"
            ) from exc
    return out


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Settings:
    """Runtime configuration for the archiver."""

    # --- Telegram credentials -------------------------------------------------
    api_id: int
    api_hash: str
    phone: Optional[str] = None
    session_name: str = "archiver"
    session_string: Optional[str] = None

    # --- archive destination --------------------------------------------------
    archive_chat_id: Optional[int] = None
    send_to_archive_chat: bool = True

    # --- storage --------------------------------------------------------------
    data_dir: Path = Path("data")
    database_url: Optional[str] = None

    # --- behaviour ------------------------------------------------------------
    download_media: bool = True
    max_media_size_mb: int = 512
    archive_outgoing: bool = True
    archive_private: bool = True
    archive_groups: bool = True
    archive_channels: bool = True
    include_chats: set[int] = field(default_factory=set)
    exclude_chats: set[int] = field(default_factory=set)

    # --- initial import -------------------------------------------------------
    import_limit_per_chat: int = 200
    import_chat_limit: int = 0  # 0 == all dialogs
    import_media: bool = True
    import_send_to_archive_chat: bool = False

    # --- reliability ----------------------------------------------------------
    flood_sleep_threshold: int = 60
    max_retries: int = 5
    archive_send_delay: float = 1.0
    worker_concurrency: int = 1

    # --- logging --------------------------------------------------------------
    log_level: str = "INFO"

    # ------------------------------------------------------------------ paths
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "session"

    @property
    def session_path(self) -> Path:
        return self.session_dir / self.session_name

    @property
    def sqlalchemy_url(self) -> str:
        """Database URL. Defaults to SQLite inside ``data/``.

        Set ``DATABASE_URL`` to e.g.
        ``postgresql+asyncpg://user:pass@host/db`` to move to PostgreSQL
        without touching the code.
        """
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'database.sqlite3').as_posix()}"

    @property
    def max_media_size_bytes(self) -> int:
        return max(0, self.max_media_size_mb) * 1024 * 1024

    # ------------------------------------------------------------------ misc
    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.media_dir, self.logs_dir, self.session_dir):
            path.mkdir(parents=True, exist_ok=True)
        # The session file grants full access to the account: keep it private.
        try:
            self.session_dir.chmod(0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass

    def safe_summary(self) -> dict[str, object]:
        """Loggable view of the config. Never contains secrets."""
        return {
            "api_id": self.api_id,
            "api_hash": "***redacted***",
            "session": self.session_name,
            "session_source": "SESSION_STRING" if self.session_string else "file",
            "archive_chat_id": self.archive_chat_id,
            "data_dir": str(self.data_dir),
            "database": _redact_url(self.sqlalchemy_url),
            "download_media": self.download_media,
            "max_media_size_mb": self.max_media_size_mb,
            "archive_outgoing": self.archive_outgoing,
            "include_chats": sorted(self.include_chats) or "all",
            "exclude_chats": sorted(self.exclude_chats) or "none",
            "log_level": self.log_level,
        }

    def chat_is_allowed(self, chat_id: int, chat_type: str) -> bool:
        """Apply the include/exclude/type filters to a chat id."""
        if self.archive_chat_id is not None and chat_id == self.archive_chat_id:
            return False
        if chat_id in self.exclude_chats:
            return False
        if self.include_chats and chat_id not in self.include_chats:
            return False
        if chat_type == "private" and not self.archive_private:
            return False
        if chat_type in {"group", "supergroup"} and not self.archive_groups:
            return False
        if chat_type == "channel" and not self.archive_channels:
            return False
        return True


def _redact_url(url: str) -> str:
    """Strip credentials from a database URL before logging it."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        rest = "***@" + rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}"


def load_settings(env_file: Optional[str] = ".env") -> Settings:
    """Read configuration from the environment (and ``.env`` when present)."""
    if env_file:
        load_dotenv(env_file, override=False)

    api_id = _get_int("API_ID", None)
    api_hash = _get("API_HASH")
    if not api_id or not api_hash:
        raise ConfigError(
            "API_ID and API_HASH are required. Copy .env.example to .env and fill "
            "them in — get the values from https://my.telegram.org > API development tools."
        )

    data_dir = Path(_get("DATA_DIR", "data")).expanduser()

    settings = Settings(
        api_id=api_id,
        api_hash=api_hash,
        phone=_get("PHONE"),
        session_name=_get("SESSION_NAME", "archiver") or "archiver",
        session_string=_get("SESSION_STRING"),
        archive_chat_id=_get_int("ARCHIVE_CHAT_ID", None),
        send_to_archive_chat=_get_bool("SEND_TO_ARCHIVE_CHAT", True),
        data_dir=data_dir,
        database_url=_get("DATABASE_URL"),
        download_media=_get_bool("DOWNLOAD_MEDIA", True),
        max_media_size_mb=_get_int("MAX_MEDIA_SIZE_MB", 512) or 0,
        archive_outgoing=_get_bool("ARCHIVE_OUTGOING", True),
        archive_private=_get_bool("ARCHIVE_PRIVATE", True),
        archive_groups=_get_bool("ARCHIVE_GROUPS", True),
        archive_channels=_get_bool("ARCHIVE_CHANNELS", True),
        include_chats=_get_id_set("INCLUDE_CHATS"),
        exclude_chats=_get_id_set("EXCLUDE_CHATS"),
        import_limit_per_chat=_get_int("IMPORT_LIMIT_PER_CHAT", 200) or 0,
        import_chat_limit=_get_int("IMPORT_CHAT_LIMIT", 0) or 0,
        import_media=_get_bool("IMPORT_MEDIA", True),
        import_send_to_archive_chat=_get_bool("IMPORT_SEND_TO_ARCHIVE_CHAT", False),
        flood_sleep_threshold=_get_int("FLOOD_SLEEP_THRESHOLD", 60) or 60,
        max_retries=_get_int("MAX_RETRIES", 5) or 5,
        archive_send_delay=float(_get("ARCHIVE_SEND_DELAY", "1.0") or 1.0),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
    )

    if settings.send_to_archive_chat and settings.archive_chat_id is None:
        # Not fatal: the local archive still works. main.py warns about it.
        settings.send_to_archive_chat = False

    return settings
