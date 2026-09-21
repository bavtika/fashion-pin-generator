"""Container / process health probe — exit 0 when the service is ready enough.

Checks:
  - required env vars present
  - data/ is writable
  - SQLite schema can be initialized
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat or chat == "0":
        print("healthcheck: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    data = Path("data")
    try:
        data.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data, delete=True) as fh:
            fh.write(b"ok")
    except OSError as exc:
        print(f"healthcheck: data/ not writable: {exc}", file=sys.stderr)
        return 1

    # Import after path is usable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from db.database import init_db

        init_db()
    except Exception as exc:  # noqa: BLE001 — health probe must never crash the process
        print(f"healthcheck: db init failed: {exc}", file=sys.stderr)
        return 1

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
