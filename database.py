import sqlite3
import json
from config import DB_PATH, AI_CACHE_TTL_DAYS

JSON_FIELDS = ("stack", "contacts", "channels", "hotness_reasons",
               "decision_makers", "attack_surface", "bb_platforms")

CRM_STATUSES = ("new", "contacted", "replied", "rejected", "client")

# New columns added on top of the original schema (name -> column definition).
_EXTRA_COLUMNS = {
    "backend_type": "TEXT",
    "backend_score": "INTEGER DEFAULT 0",
    "hotness_score": "INTEGER DEFAULT 0",
    "hotness_reasons": "TEXT",
    "quality_score": "INTEGER DEFAULT 0",
    "decision_makers": "TEXT",
    "pitch": "TEXT",
    "crm_status": "TEXT DEFAULT 'new'",
    "times_seen": "INTEGER DEFAULT 1",
    "first_query": "TEXT",
    "updated_at": "TIMESTAMP",
    "surface_score": "INTEGER DEFAULT 0",
    "attack_surface": "TEXT",
    "data_sensitivity": "TEXT",
    "has_bb": "INTEGER DEFAULT 0",
    "bb_platforms": "TEXT",
    "bb_url": "TEXT",
    "payout_score": "INTEGER DEFAULT 0",
    "payout_reason": "TEXT",
}


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # WAL + busy timeout so the bot and the web app can share the DB safely.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            domain TEXT UNIQUE,
            name TEXT,
            url TEXT,
            description TEXT,
            stack TEXT,
            size TEXT,
            contacts TEXT,
            channels TEXT,
            needs TEXT,
            ai_summary TEXT,
            raw_html_length INTEGER,
            status TEXT DEFAULT 'pending',
            backend_type TEXT,
            backend_score INTEGER DEFAULT 0,
            hotness_score INTEGER DEFAULT 0,
            hotness_reasons TEXT,
            quality_score INTEGER DEFAULT 0,
            decision_makers TEXT,
            pitch TEXT,
            crm_status TEXT DEFAULT 'new',
            times_seen INTEGER DEFAULT 1,
            first_query TEXT,
            surface_score INTEGER DEFAULT 0,
            attack_surface TEXT,
            data_sensitivity TEXT,
            has_bb INTEGER DEFAULT 0,
            bb_platforms TEXT,
            bb_url TEXT,
            payout_score INTEGER DEFAULT 0,
            payout_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_cache (
            key TEXT PRIMARY KEY,
            value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # Migrate older DBs: add any missing columns.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
    for col, ddl in _EXTRA_COLUMNS.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
    conn.commit()
    conn.close()


# ---------------- Companies ----------------

def domain_exists(domain: str) -> bool:
    conn = _connect()
    row = conn.execute("SELECT 1 FROM companies WHERE domain = ?", (domain,)).fetchone()
    conn.close()
    return row is not None


def save_company(data: dict) -> str:
    """Upsert by domain. Returns 'new' or 'updated'.

    On update: preserves crm_status and first_query, bumps times_seen,
    and refreshes the analysis fields.
    """
    domain = data.get("domain", "")
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT times_seen FROM companies WHERE domain = ?", (domain,)
        ).fetchone()

        payload = (
            data.get("query", ""),
            data.get("name", ""),
            data.get("url", ""),
            data.get("description", ""),
            json.dumps(data.get("stack", []), ensure_ascii=False),
            data.get("size", ""),
            json.dumps(data.get("contacts", {}), ensure_ascii=False),
            json.dumps(data.get("channels", {}), ensure_ascii=False),
            data.get("needs", ""),
            data.get("ai_summary", ""),
            data.get("raw_html_length", 0),
            data.get("status", "done"),
            data.get("backend_type", ""),
            int(data.get("backend_score", 0) or 0),
            int(data.get("hotness_score", 0) or 0),
            json.dumps(data.get("hotness_reasons", []), ensure_ascii=False),
            int(data.get("quality_score", 0) or 0),
            json.dumps(data.get("decision_makers", []), ensure_ascii=False),
            data.get("pitch", ""),
            int(data.get("surface_score", 0) or 0),
            json.dumps(data.get("attack_surface", []), ensure_ascii=False),
            data.get("data_sensitivity", ""),
            1 if data.get("has_bb") else 0,
            json.dumps(data.get("bb_platforms", []), ensure_ascii=False),
            data.get("bb_url", ""),
            int(data.get("payout_score", 0) or 0),
            data.get("payout_reason", ""),
        )

        if existing:
            conn.execute("""
                UPDATE companies SET
                    query=?, name=?, url=?, description=?, stack=?, size=?,
                    contacts=?, channels=?, needs=?, ai_summary=?, raw_html_length=?,
                    status=?, backend_type=?, backend_score=?, hotness_score=?,
                    hotness_reasons=?, quality_score=?, decision_makers=?, pitch=?,
                    surface_score=?, attack_surface=?, data_sensitivity=?,
                    has_bb=?, bb_platforms=?, bb_url=?,
                    payout_score=?, payout_reason=?,
                    times_seen = times_seen + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE domain=?
            """, payload + (domain,))
            conn.commit()
            return "updated"
        else:
            conn.execute("""
                INSERT INTO companies
                (domain, first_query, crm_status, query, name, url, description, stack, size,
                 contacts, channels, needs, ai_summary, raw_html_length, status,
                 backend_type, backend_score, hotness_score, hotness_reasons,
                 quality_score, decision_makers, pitch,
                 surface_score, attack_surface, data_sensitivity,
                 has_bb, bb_platforms, bb_url,
                 payout_score, payout_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (domain, data.get("query", ""), "new") + payload)
            conn.commit()
            return "new"
    finally:
        conn.close()


def _decode(row: dict) -> dict:
    for field in JSON_FIELDS:
        if row.get(field):
            try:
                row[field] = json.loads(row[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return row


def get_company_by_domain(domain: str) -> dict | None:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM companies WHERE domain = ?", (domain,)).fetchone()
    conn.close()
    return _decode(dict(row)) if row else None


def get_companies_by_query(query: str) -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM companies WHERE query = ? ORDER BY quality_score DESC, hotness_score DESC",
        (query,),
    ).fetchall()
    conn.close()
    return [_decode(dict(r)) for r in rows]


def get_all_companies() -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM companies ORDER BY id DESC").fetchall()
    conn.close()
    return [_decode(dict(r)) for r in rows]


def get_companies_filtered(query: str | None = None, min_quality: int = 0,
                           min_hotness: int = 0, has_email: bool = False,
                           has_telegram: bool = False, crm_status: str | None = None,
                           require_auth: bool = False, min_surface: int = 0,
                           require_bb: bool = False, exclude_bb: bool = False,
                           min_payout: int = 0) -> list[dict]:
    """Return companies matching export filters."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM companies WHERE 1=1"
    params: list = []
    if query:
        sql += " AND query = ?"
        params.append(query)
    if min_quality:
        sql += " AND quality_score >= ?"
        params.append(min_quality)
    if min_hotness:
        sql += " AND hotness_score >= ?"
        params.append(min_hotness)
    if min_surface:
        sql += " AND surface_score >= ?"
        params.append(min_surface)
    if min_payout:
        sql += " AND payout_score >= ?"
        params.append(min_payout)
    if require_auth:
        sql += " AND attack_surface IS NOT NULL AND attack_surface NOT IN ('', '[]')"
    if require_bb:
        sql += " AND has_bb = 1"
    if exclude_bb:
        sql += " AND (has_bb = 0 OR has_bb IS NULL)"
    if crm_status:
        sql += " AND crm_status = ?"
        params.append(crm_status)
    if has_email:
        sql += " AND contacts LIKE '%@%'"
    if has_telegram:
        sql += " AND channels LIKE '%telegram%'"
    sql += " ORDER BY surface_score DESC, quality_score DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_decode(dict(r)) for r in rows]


# ---------------- CRM ----------------

def set_crm_status(domain: str, status: str) -> bool:
    if status not in CRM_STATUSES:
        return False
    conn = _connect()
    conn.execute(
        "UPDATE companies SET crm_status = ?, updated_at = CURRENT_TIMESTAMP WHERE domain = ?",
        (status, domain),
    )
    changed = conn.total_changes > 0
    conn.commit()
    conn.close()
    return changed


def get_by_crm_status(status: str) -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM companies WHERE crm_status = ? ORDER BY updated_at DESC", (status,)
    ).fetchall()
    conn.close()
    return [_decode(dict(r)) for r in rows]


# ---------------- Stats / dashboard ----------------

def get_stats() -> dict:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM companies WHERE status='done'").fetchone()[0]
    queries = conn.execute("SELECT COUNT(DISTINCT query) FROM companies").fetchone()[0]
    conn.close()
    return {"total": total, "done": done, "queries": queries}


def get_dashboard() -> dict:
    conn = _connect()
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    hot = conn.execute("SELECT COUNT(*) FROM companies WHERE hotness_score >= 50").fetchone()[0]
    with_email = conn.execute("SELECT COUNT(*) FROM companies WHERE contacts LIKE '%@%'").fetchone()[0]
    with_tg = conn.execute("SELECT COUNT(*) FROM companies WHERE channels LIKE '%telegram%'").fetchone()[0]
    with_auth = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE attack_surface IS NOT NULL AND attack_surface NOT IN ('', '[]')"
    ).fetchone()[0]
    with_bb = conn.execute("SELECT COUNT(*) FROM companies WHERE has_bb = 1").fetchone()[0]

    by_backend = conn.execute("""
        SELECT COALESCE(NULLIF(backend_type,''),'unknown') AS bt, COUNT(*) c
        FROM companies GROUP BY bt ORDER BY c DESC
    """).fetchall()

    by_crm = conn.execute("""
        SELECT COALESCE(crm_status,'new') cs, COUNT(*) c
        FROM companies GROUP BY cs ORDER BY c DESC
    """).fetchall()

    by_query = conn.execute("""
        SELECT query, COUNT(*) c FROM companies GROUP BY query ORDER BY c DESC LIMIT 10
    """).fetchall()

    conn.close()
    return {
        "total": total,
        "hot": hot,
        "with_auth": with_auth,
        "with_bb": with_bb,
        "with_email": with_email,
        "with_telegram": with_tg,
        "by_backend": [(r["bt"], r["c"]) for r in by_backend],
        "by_crm": [(r["cs"], r["c"]) for r in by_crm],
        "by_query": [(r["query"], r["c"]) for r in by_query],
    }


# ---------------- AI cache ----------------

def get_ai_cache(key: str) -> str | None:
    conn = _connect()
    row = conn.execute(
        "SELECT value FROM ai_cache WHERE key = ? "
        "AND created_at >= datetime('now', ?)",
        (key, f"-{int(AI_CACHE_TTL_DAYS)} days"),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def set_ai_cache(key: str, value: str):
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO ai_cache (key, value, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    conn.commit()
    conn.close()
