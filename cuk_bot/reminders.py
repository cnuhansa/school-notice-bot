"""D-7 / D-3 / D-1 reminder scheduling.

Split from cuk_bot.notifier, which shapes and delivers messages. Scheduling
is the opposite concern: it must not depend on delivery working at all. When
an alert fails to go out, these reminders are what still reaches the reader
before the deadline.
"""

from datetime import date, datetime, timedelta

KINDS = (("D-7", 7), ("D-3", 3), ("D-1", 1))


def schedule(con, board_id: str, article_no: str, apply_end: str) -> int:
    """Queue D-7/D-3/D-1. Dates already past are never scheduled.

    Does not commit: the caller commits the verdict and its reminders as one
    unit, before anything goes over the network.
    """
    if not apply_end:
        return 0
    try:
        due_on = datetime.fromisoformat(apply_end).date()
    except (TypeError, ValueError):
        return 0

    queued = 0
    for kind, days in KINDS:
        when = due_on - timedelta(days=days)
        if when >= date.today():
            con.execute("INSERT OR IGNORE INTO reminders VALUES (?,?,?,?,NULL)",
                        (board_id, article_no, when.isoformat(), kind))
            queued += 1
    return queued


def due(con, on: str = None) -> list:
    on = on or date.today().isoformat()
    return [dict(r) for r in con.execute("""
        SELECT r.kind, r.board_id, r.article_no, e.one_line, e.apply_end,
               n.title, n.url
        FROM reminders r
        JOIN notices n ON n.board_id = r.board_id
                      AND n.article_no = r.article_no
        JOIN extractions e ON e.board_id = r.board_id
                          AND e.article_no = r.article_no
        WHERE r.due_date = ? AND r.sent_at IS NULL
    """, (on,))]


def mark_sent(con, row: dict) -> None:
    con.execute(
        "UPDATE reminders SET sent_at=? WHERE board_id=? AND article_no=? "
        "AND kind=?",
        (datetime.now().isoformat(timespec="seconds"),
         row["board_id"], row["article_no"], row["kind"]),
    )
