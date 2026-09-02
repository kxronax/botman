#!/usr/bin/env python3
"""Personal Telegram archiver.

Thin launcher so the project can be started with `python main.py`.
The real entry point is app/main.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
