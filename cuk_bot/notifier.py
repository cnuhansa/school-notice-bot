"""Telegram delivery and message shaping (HANDOFF section 9).

Alert types are kept separate on purpose. Pushing every notice immediately is
what gets a bot muted inside two weeks, which would defeat the whole point:

    actionable notice  →  immediate, once
    everything else    →  next 08:00 digest, title + link only
    approaching deadline → D-7 / D-3 / D-1 at 08:00
"""

import os
from datetime import date, datetime, timedelta

import requests

from . import db
from .config import BOARDS_BY_ID

REMINDER_KINDS = (("D-7", 7), ("D-3", 3), ("D-1", 1))
LOW_CONFIDENCE = 0.5


def _board_name(item: dict) -> str:
    """Human board name, falling back to the id only if it is unknown.

    Rows read straight from the database carry board_id but no display name,
    and "dorm_f_inout" in an alert tells the reader nothing.
    """
    if item.get("board_name"):
        return item["board_name"]
    board = BOARDS_BY_ID.get(item.get("board_id") or "")
    return board["name"] if board else (item.get("board_id") or "")


def esc(text: str) -> str:
    """Escape for Telegram HTML mode.

    Notice titles contain ★, brackets and occasionally &, and an unescaped
    one makes the API reject the whole message.
    """
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def send(text: str, log=print) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("  [!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 발송 생략")
        return False

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=15,
    )
    if not resp.ok:
        log(f"  [!] 텔레그램 발송 실패: {resp.text[:200]}")
    return resp.ok


MAX_SEND_ATTEMPTS = 5


def send_or_queue(con, text: str, kind: str, log=print) -> bool:
    """Send, and park the message for retry if delivery fails.

    Dropping a failed alert is unrecoverable — the extraction row is already
    written, so nothing would ever retry it. One Telegram outage would
    silently swallow a 모집 공고 notification.
    """
    if send(text, log=log):
        return True
    db.queue_message(con, kind, text, error="발송 실패")
    log(f"  [!] 발송 실패 — outbox 에 보관({kind}), 다음 실행에서 재시도")
    return False


def flush_outbox(con, log=print) -> int:
    """Retry parked messages. Returns how many finally got through."""
    sent = 0
    for row in db.pending_outbox(con, MAX_SEND_ATTEMPTS):
        if send(row["body"], log=log):
            db.drop_message(con, row["id"])
            sent += 1
        else:
            db.bump_attempt(con, row["id"], error="재시도 실패")

    if sent:
        log(f"  ↻ 보류됐던 알림 {sent}건 재발송 완료")

    stuck = db.stuck_messages(con, MAX_SEND_ATTEMPTS)
    if stuck:
        log(f"  [!] {stuck}건은 {MAX_SEND_ATTEMPTS}회 시도 후에도 발송 실패 "
            f"— 자동 재시도 중단, 원인 확인 필요")
    return sent


def fmt_deadline(value: str) -> str:
    if not value:
        return "미상"
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return str(value)

    left = (parsed.date() - date.today()).days
    stamp = parsed.strftime("%m/%d %H:%M") if "T" in value else parsed.strftime("%m/%d")
    if left > 0:
        return f"{stamp} (D-{left})"
    return f"{stamp} ({'오늘 마감' if left == 0 else '마감됨'})"


def _caveats(data: dict) -> list:
    out = []
    if data.get("source") == "image":
        out.append("🖼 본문이 이미지입니다. 원문에서 직접 확인하세요.")
    elif data.get("source") == "title":
        out.append("📄 본문을 읽지 못해 제목만으로 판단했습니다. 원문 확인 필요.")
    if (data.get("confidence") or 0) < LOW_CONFIDENCE:
        out.append("⚠️ 마감일이 불확실합니다. 첨부파일을 직접 확인하세요.")
    return out


def format_alert(item: dict, data: dict) -> str:
    lines = [
        f"🔔 <b>신청 공지</b> · {esc(data.get('category') or '일반')}",
        f"<b>{esc(item['title'])}</b>",
        "",
        f"📌 {esc(data.get('one_line') or '')}",
        f"⏰ 마감 {esc(fmt_deadline(data.get('apply_end')))}",
        f"👤 대상 {esc(data.get('target') or '미상')}",
        f"📝 방법 {esc(data.get('method') or '공지 참조')}",
        f"📂 {esc(_board_name(item))}",
    ]
    caveats = _caveats(data)
    if caveats:
        lines += [""] + [esc(c) for c in caveats]
    lines += ["", item["url"]]
    return "\n".join(lines)


def format_unjudged(items: list, reason: str) -> str:
    """Alert for notices forwarded without a verdict.

    When the free allowance runs out the bot stops filtering rather than
    stops notifying. Everything from the run goes into one message: every
    notice is still surfaced, but a quiet day and a busy one both cost one
    ping instead of one per notice.
    """
    lines = [
        "🚨 <b>판정 없이 전달</b>",
        f"<i>{esc(reason or '판정 불가')}</i>",
        "",
        f"아래 {len(items)}건은 신청 여부·마감일을 판정하지 못했습니다. "
        "직접 확인하세요.",
        "",
    ]
    for item in items:
        board = esc(_board_name(item))
        lines.append(f'• <a href="{item["url"]}">{esc(item["title"][:60])}</a>')
        if board:
            lines.append(f"  <i>{board}</i>")
    lines += ["", "한도가 회복되면 <code>--reextract</code> 로 다시 판정합니다."]
    return "\n".join(lines)


def format_alive(healthy: int, total: int) -> str:
    """The empty-day heartbeat.

    Says explicitly that nothing needed doing, so the absence of this message
    reads as a fault rather than as a calm day.
    """
    return "\n".join([
        "☀️ <b>오늘 새 공지 없음</b>",
        f"게시판 {healthy}/{total}곳 정상 수집 중입니다.",
        "",
        "<i>이 메시지가 안 오는 날은 봇에 문제가 생긴 것입니다.</i>",
    ])


def format_board_failure(board_ids: list, streak: int) -> str:
    """Tell the channel a board stopped parsing.

    A board that silently returns nothing is the failure the user cannot see
    — everything looks normal, but that board's notices never arrive.
    """
    names = [_board_name({"board_id": b}) for b in board_ids]
    lines = [
        "🛠 <b>게시판 수집 실패</b>",
        f"<i>{streak}회 연속 실패</i>",
        "",
        "아래 게시판을 읽지 못하고 있습니다. 학교 사이트 개편일 수 있습니다.",
        "",
    ]
    lines += [f"• {esc(n)}" for n in names]
    lines += ["", "해당 게시판은 <b>직접 확인</b>하세요."]
    return "\n".join(lines)


def format_digest(notices: list, reminders: list) -> str:
    lines = []
    if notices:
        lines.append("📰 <b>어제의 공지</b>")
        for row in notices:
            lines.append(f'• <a href="{row["url"]}">{esc(row["title"][:60])}</a>')

    if reminders:
        if lines:
            lines.append("")
        lines.append("⏰ <b>마감 임박</b>")
        for row in reminders:
            headline = row["one_line"] or row["title"][:50]
            lines.append(f'• <b>{esc(row["kind"])}</b> {esc(headline)}')
            lines.append(f'  마감 {esc(fmt_deadline(row["apply_end"]))} · '
                         f'<a href="{row["url"]}">바로가기</a>')
    return "\n".join(lines)


def schedule_reminders(con, board_id: str, article_no: str, apply_end: str) -> int:
    """Queue D-7/D-3/D-1. Dates already past are never scheduled."""
    if not apply_end:
        return 0
    try:
        due = datetime.fromisoformat(apply_end).date()
    except (TypeError, ValueError):
        return 0

    queued = 0
    for kind, days in REMINDER_KINDS:
        when = due - timedelta(days=days)
        if when >= date.today():
            con.execute("INSERT OR IGNORE INTO reminders VALUES (?,?,?,?,NULL)",
                        (board_id, article_no, when.isoformat(), kind))
            queued += 1
    con.commit()
    return queued


def due_reminders(con, on: str = None) -> list:
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


def mark_reminder_sent(con, row: dict) -> None:
    con.execute(
        "UPDATE reminders SET sent_at=? WHERE board_id=? AND article_no=? "
        "AND kind=?",
        (datetime.now().isoformat(timespec="seconds"),
         row["board_id"], row["article_no"], row["kind"]),
    )
