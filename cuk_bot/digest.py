"""The morning digest, its reminders, and the one-time catch-up.

Split from cuk_bot.cli so the daily-delivery logic can carry its own
explanation without pushing the command dispatch past the file size limit.
"""

from datetime import date

from . import db, health, notifier
from .config import BOARDS_BY_ID

CATCHUP_FLAG = "catchup_sent"


def _catchup_once(con, notify=True, log=print):
    """Send the month's existing notices — exactly once, ever.

    Gated on a database flag rather than a date, because the digest cron
    fires daily and anything time-based would resend on every run that
    matched. The flag is set only after delivery succeeds, so a failed send
    is retried tomorrow instead of being lost.
    """
    if con.execute("SELECT 1 FROM meta WHERE key=?",
                   (CATCHUP_FLAG,)).fetchone():
        return

    month = date.today().strftime("%Y-%m")
    rows = [dict(r) for r in con.execute("""
        SELECT n.board_id, n.title, n.url, n.posted_at, e.is_actionable
        FROM notices n
        LEFT JOIN extractions e ON e.board_id = n.board_id
                               AND e.article_no = n.article_no
        WHERE n.posted_at LIKE ?
        ORDER BY n.board_id, n.posted_at DESC
    """, (f"{month}%",))]

    if not rows:
        log(f"  이번 달({month}) 공지 없음 — 모아보기 생략")
        return

    text = notifier.format_catchup(rows, month)
    if not notify:
        log(text)
        return

    if notifier.send_or_queue(con, text, "catchup", log=log):
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                    (CATCHUP_FLAG, db.now()))
        con.commit()
        log(f"  📚 {month} 공지 {len(rows)}건 모아보기 발송 (1회성)")


def run(con, notify=True, log=print):
    health.ping("start", env=health.DIGEST_URL_ENV)
    if notify:
        notifier.flush_outbox(con, log=log)
    _catchup_once(con, notify=notify, log=log)

    notices = [dict(r) for r in con.execute("""
        SELECT n.title, n.url, p.board_id, p.article_no
        FROM pending_digest p
        JOIN notices n ON n.board_id = p.board_id
                      AND n.article_no = p.article_no
    """)]
    reminders = notifier.due_reminders(con)

    # Sent even when empty. Silence is the one failure mode the user cannot
    # detect on their own: a dead bot and a quiet day look identical. A daily
    # message that stops arriving is a signal they will notice.
    text = notifier.format_digest(notices, reminders) or notifier.format_alive(
        len(BOARDS_BY_ID) - len(health.failing_boards(con)), len(BOARDS_BY_ID))

    # A preview must not consume the queue. Clearing pending_digest and
    # marking reminders sent without delivering anything would drop the day's
    # digest and stop those D-day reminders from ever firing.
    if not notify:
        log(text)
        log(f"[미리보기] 공지 {len(notices)}건, 리마인더 {len(reminders)}건 "
            f"— 발송하지 않았고 대기열도 그대로입니다")
        return

    if notifier.send_or_queue(con, text, "digest", log=log):
        con.execute("DELETE FROM pending_digest")
        for row in reminders:
            notifier.mark_reminder_sent(con, row)
        con.commit()
    log(f"다이제스트: 공지 {len(notices)}건, 리마인더 {len(reminders)}건")
    health.ping("success", env=health.DIGEST_URL_ENV)
