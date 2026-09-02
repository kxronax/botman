"""Serialisation helpers for raw Telegram objects.

Telethon objects expose ``to_dict()``, but the result contains ``datetime``
and ``bytes`` values that :mod:`json` cannot encode. :func:`dumps` handles
those so we can keep a faithful raw copy of every message in the database —
useful later when new fields need to be extracted from already archived data.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"__bytes_b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, set):
        return sorted(obj, key=repr)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:  # pragma: no cover - defensive
            pass
    return repr(obj)


def dumps(obj: Any) -> str:
    """JSON-encode any Telethon object graph, never raising on odd types."""
    try:
        return json.dumps(obj, default=_default, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        return json.dumps({"__unserialisable__": repr(obj)}, ensure_ascii=False)


def to_json(obj: Any) -> str | None:
    """Serialise a Telethon object (or ``None``) to a JSON string."""
    if obj is None:
        return None
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            obj = to_dict()
        except Exception:  # pragma: no cover - defensive
            return dumps(repr(obj))
    return dumps(obj)


def loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
