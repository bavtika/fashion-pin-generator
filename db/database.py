import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "pins.db"

_PIN_UPDATE_FIELDS = frozenset({
    "status",
    "image_path",
    "reference_path",
    "prompt",
    "clothing_description",
    "amazon_product_title",
    "amazon_product_url",
    "amazon_asin",
    "pin_title",
    "pin_description",
    "pinterest_pin_id",
    "posted_at",
})

# Pins the user must finish reviewing before we generate more.
REVIEW_QUEUE_STATUSES = (
    "pending_approval",
    "regenerating",
    "regenerating_pose",
    "regenerating_custom",
)


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                status TEXT DEFAULT 'pending_approval',
                image_path TEXT,
                reference_path TEXT,
                prompt TEXT,
                clothing_description TEXT,
                amazon_product_title TEXT,
                amazon_product_url TEXT,
                amazon_asin TEXT,
                pin_title TEXT,
                pin_description TEXT,
                pinterest_pin_id TEXT,
                posted_at TEXT
            )
        """)
        # Idempotent migration for older DBs
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pins)").fetchall()}
        if "reference_path" not in cols:
            conn.execute("ALTER TABLE pins ADD COLUMN reference_path TEXT")
        conn.commit()


def create_pin(image_path: str, prompt: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO pins (image_path, prompt) VALUES (?, ?)",
            (image_path, prompt)
        )
        return cursor.lastrowid


def update_pin(pin_id: int, **kwargs):
    if not kwargs:
        return
    unknown = set(kwargs) - _PIN_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"Invalid pin fields: {', '.join(sorted(unknown))}")
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [pin_id]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE pins SET {fields} WHERE id = ?", values)
        conn.commit()


def get_pin(pin_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
        return dict(row) if row else None


def count_pending_approval() -> int:
    """Legacy name — counts every pin still in the human review queue."""
    return count_review_queue()


def count_review_queue() -> int:
    placeholders = ",".join("?" * len(REVIEW_QUEUE_STATUSES))
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM pins WHERE status IN ({placeholders})",
            REVIEW_QUEUE_STATUSES,
        ).fetchone()
        return row[0] if row else 0


def get_pending_pins() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pins WHERE status = 'approved'"
        ).fetchall()
        return [dict(r) for r in rows]


def requeue_stuck_pins() -> list[int]:
    """On startup: reset pins stuck in 'processing' back to 'approved' and
    return ids of all currently approved pins that need re-processing."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE pins SET status = 'approved' WHERE status = 'processing'"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM pins WHERE status = 'approved' ORDER BY id"
        ).fetchall()
    return [r[0] for r in rows]
