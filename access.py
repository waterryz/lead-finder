import sqlite3
from config import DB_PATH, ADMIN_ID


def init_access_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP,
            note TEXT
        )
    """)
    # admin is always approved
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name, status) VALUES (?, ?, ?, 'approved')",
        (ADMIN_ID, "admin", "Administrator"),
    )
    conn.commit()
    conn.close()


def get_status(user_id: int) -> str | None:
    """Return 'approved' / 'pending' / 'rejected' / None (not registered)."""
    if user_id == ADMIN_ID:
        return "approved"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def is_approved(user_id: int) -> bool:
    return get_status(user_id) == "approved"


def create_request(user_id: int, username: str, full_name: str) -> str:
    """Register a new access request. Returns resulting status."""
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if existing:
        conn.close()
        return existing[0]
    conn.execute(
        "INSERT INTO users (user_id, username, full_name, status) VALUES (?, ?, ?, 'pending')",
        (user_id, username, full_name),
    )
    conn.commit()
    conn.close()
    return "pending"


def set_status(user_id: int, status: str, note: str = "") -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE users SET status = ?, decided_at = CURRENT_TIMESTAMP, note = ? WHERE user_id = ?",
        (status, note, user_id),
    )
    changed = conn.total_changes > 0
    conn.commit()
    conn.close()
    return changed


def get_user(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users(status: str | None = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if status:
        rows = conn.execute(
            "SELECT * FROM users WHERE status = ? ORDER BY requested_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users ORDER BY requested_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
