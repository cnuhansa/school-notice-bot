"""SQLite storage (HANDOFF section 7).

`notices` and `extractions` are deliberately separate tables: prompt changes
must be re-runnable without re-crawling, which is what makes `--reextract`
possible and prompt tuning fast. `extractions.payload` keeps the raw LLM JSON
so a schema change never destroys past results.
"""

import json
import sqlite3
from datetime import datetime

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    board_id    TEXT NOT NULL,
    article_no  TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    posted_at   TEXT,
    body        TEXT,
    images      TEXT,          -- JSON array of absolute image urls
    attachments TEXT,          -- JSON array of {name, kind, url}
    seen_at     TEXT,
    PRIMARY KEY (board_id, article_no)
);
CREATE TABLE IF NOT EXISTS extractions (
    board_id     TEXT NOT NULL,
    article_no   TEXT NOT NULL,
    payload      TEXT,         -- raw LLM JSON, kept verbatim
    is_actionable INTEGER,
    apply_end    TEXT,
    one_line     TEXT,
    confidence   REAL,
    source       TEXT,         -- text | pdf | image | title
    extracted_at TEXT,
    PRIMARY KEY (board_id, article_no)
);
CREATE TABLE IF NOT EXISTS reminders (
    board_id   TEXT NOT NULL,
    article_no TEXT NOT NULL,
    due_date   TEXT NOT NULL,  -- date the reminder should go out
    kind       TEXT NOT NULL,  -- D-7 | D-3 | D-1
    sent_at    TEXT,
    PRIMARY KEY (board_id, article_no, kind)
);
CREATE TABLE IF NOT EXISTS pending_digest (
    board_id   TEXT NOT NULL,
    article_no TEXT NOT NULL,
    PRIMARY KEY (board_id, article_no)
);
CREATE TABLE IF NOT EXISTS crawl_log (
    board_id   TEXT NOT NULL,
    ran_at     TEXT NOT NULL,
    ok         INTEGER,
    item_count INTEGER,
    error      TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    day     TEXT PRIMARY KEY,
    used    INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0   -- API itself reported quota spent
);
CREATE TABLE IF NOT EXISTS unjudged (
    board_id   TEXT NOT NULL,
    article_no TEXT NOT NULL,
    reason     TEXT,
    noticed_at TEXT,
    alerted_at TEXT,
    PRIMARY KEY (board_id, article_no)
);
CREATE INDEX IF NOT EXISTS idx_notices_seen ON notices(seen_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_date, sent_at);
"""


def connect(path: str = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────
# notices
# ─────────────────────────────────────────────────────────────

def is_known(con, board_id: str, article_no: str) -> bool:
    return con.execute(
        "SELECT 1 FROM notices WHERE board_id=? AND article_no=?",
        (board_id, article_no),
    ).fetchone() is not None


def save_notice(con, board_id: str, row: dict, detail: dict = None) -> None:
    detail = detail or {}
    con.execute(
        "INSERT OR REPLACE INTO notices "
        "(board_id, article_no, title, url, posted_at, body, images, "
        " attachments, seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (board_id, row["article_no"], row["title"], row["url"],
         row["posted_at"] or detail.get("posted_at"),
         detail.get("body", ""),
         json.dumps(detail.get("images", []), ensure_ascii=False),
         json.dumps(detail.get("attachments", []), ensure_ascii=False),
         now()),
    )


def load_notice(con, board_id: str, article_no: str) -> dict:
    row = con.execute(
        "SELECT * FROM notices WHERE board_id=? AND article_no=?",
        (board_id, article_no),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["images"] = json.loads(item["images"] or "[]")
    item["attachments"] = json.loads(item["attachments"] or "[]")
    return item


def recent_titles(con, days: int = 14) -> list:
    """Titles seen recently, for the cross-post check (HANDOFF 11.3)."""
    return [r[0] for r in con.execute(
        "SELECT title FROM notices WHERE seen_at >= date('now', ?)",
        (f"-{days} days",),
    )]


# ─────────────────────────────────────────────────────────────
# extractions
# ─────────────────────────────────────────────────────────────

def save_extraction(con, board_id: str, article_no: str, data: dict,
                    source: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO extractions "
        "(board_id, article_no, payload, is_actionable, apply_end, one_line, "
        " confidence, source, extracted_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (board_id, article_no, json.dumps(data, ensure_ascii=False),
         int(bool(data.get("is_actionable"))), data.get("apply_end"),
         data.get("one_line"), data.get("confidence"), source, now()),
    )


def pending_extraction(con) -> list:
    """Stored notices that have no extraction yet (used by --reextract)."""
    return [dict(r) for r in con.execute(
        "SELECT n.board_id, n.article_no FROM notices n "
        "LEFT JOIN extractions e ON e.board_id=n.board_id "
        "  AND e.article_no=n.article_no WHERE e.article_no IS NULL"
    )]


# ─────────────────────────────────────────────────────────────
# reminders / digest / log
# ─────────────────────────────────────────────────────────────

def queue_digest(con, board_id: str, article_no: str) -> None:
    con.execute("INSERT OR IGNORE INTO pending_digest VALUES (?,?)",
                (board_id, article_no))


def mark_unjudged(con, board_id: str, article_no: str, reason: str) -> None:
    """Record a notice that was forwarded without an LLM verdict.

    No extraction row is written, so pending_extraction() picks it up on the
    next --reextract once the allowance resets and it can be graded properly.
    """
    con.execute(
        "INSERT OR IGNORE INTO unjudged (board_id, article_no, reason, "
        "noticed_at) VALUES (?,?,?,?)", (board_id, article_no, reason, now()))


def mark_unjudged_alerted(con, rows: list) -> None:
    for row in rows:
        con.execute(
            "UPDATE unjudged SET alerted_at=? WHERE board_id=? AND article_no=?",
            (now(), row["board_id"], row["article_no"]))


def clear_unjudged(con, board_id: str, article_no: str) -> None:
    con.execute("DELETE FROM unjudged WHERE board_id=? AND article_no=?",
                (board_id, article_no))


def log_crawl(con, board_id: str, ok: bool, count: int, error: str = None) -> None:
    con.execute("INSERT INTO crawl_log VALUES (?,?,?,?,?)",
                (board_id, now(), int(ok), count, error))


def failure_counts(con, days: int = 120) -> list:
    """Per-board parse failures — the HANDOFF section 3 stability metric."""
    return [dict(r) for r in con.execute(
        "SELECT board_id, COUNT(*) AS failures FROM crawl_log "
        "WHERE ok=0 AND ran_at >= date('now', ?) GROUP BY board_id "
        "ORDER BY failures DESC", (f"-{days} days",),
    )]
