"""Route one notice: judge it, record the verdict, schedule, then deliver.

Split from cuk_bot.cli. The order of the steps below is what this module is
about, and it needed room to say why:

    judge  →  store verdict + schedule reminders (one commit)  →  send

Everything before the send is local and either works or is a bug. The send
crosses a network this run does not control, so it goes last. Whatever it
does, the verdict and the D-7/D-3/D-1 reminders are already safe on disk.
"""

import json

from . import db, notifier, reminders
from .config import READ_IMAGES
from .content import resolve
from .extractor import brief_error
from .quota import QuotaExhausted


def title(item: dict) -> str:
    """A title that is always a string.

    parse_detail can return None for a field it could not find, and a None
    title used to crash the log line that reported the notice.
    """
    return item.get("title") or "(제목 없음)"


class RouteFailed(Exception):
    """The notice was judged, then storing or alerting it failed.

    A separate type so the caller can tell it apart from a failure to judge.
    Both used to land in the same "판정 없이 전달" message, which told the
    reader the bot could not judge a notice it had graded with confidence
    1.0, and pointed at reextract — which skips anything with a verdict.
    """


def extract_and_route(con, item, session, notify=True, log=print):
    """Extract one notice, then either alert immediately or queue a digest.

    Every path that cannot produce a verdict — spent free tier, missing key,
    API outage, unparseable response — forwards the notice unjudged. Silently
    dropping one would reproduce the exact failure this bot was built to
    prevent, so "couldn't judge" always still means "you get told".
    """
    resolved = resolve(item, allow_images=READ_IMAGES)

    try:
        data = session.judge(item, resolved)
    except QuotaExhausted as exc:
        session.forward_unjudged(item, str(exc))
        log(f"  ⚠ 판정 불가({exc}) — 판정 없이 전달: {title(item)[:40]}")
        return None
    except Exception as exc:
        # The channel gets a short reason; the full SDK error goes to the run
        # log, which is the only place it can be diagnosed from.
        session.forward_unjudged(item, f"추출 실패: {brief_error(exc)}")
        log(f"  ⚠ 추출 실패 — 판정 없이 전달 ({title(item)[:30]}): "
            f"{str(exc)[:400]}")
        return None

    try:
        _record_and_send(con, item, data, resolved["source"], notify, log)
    except Exception as exc:
        raise RouteFailed(brief_error(exc)) from exc
    return data


def _record_and_send(con, item, data, source, notify, log):
    db.clear_unjudged(con, item["board_id"], item["article_no"])
    db.save_extraction(con, item["board_id"], item["article_no"], data, source)
    _schedule_and_send(con, item, data, notify, log)


def _schedule_and_send(con, item, data, notify, log):
    board_id, article_no = item["board_id"], item["article_no"]
    queued = 0
    if data["is_actionable"]:
        queued = reminders.schedule(con, board_id, article_no,
                                    data.get("apply_end"))
    else:
        db.queue_digest(con, board_id, article_no)
    con.commit()

    if not data["is_actionable"]:
        log(f"  · 다이제스트 대기: {title(item)[:44]}")
        return
    if notify:
        notifier.send_or_queue(
            con, notifier.format_alert(item, data), "alert", log=log)
    log(f"  🔔 {title(item)[:44]} — 마감 "
        f"{notifier.fmt_deadline(data.get('apply_end'))}, 리마인더 {queued}건")


def _undelivered(con) -> list:
    """Notices that have a verdict but are still recorded as unjudged.

    That pairing should never exist: a verdict clears the unjudged row. It
    is the footprint of the 2026-11-04 failure, where the alert send raised
    after the verdict was stored, and the notice was then forwarded as if
    it had never been judged. Its real alert and reminders never went out.
    """
    return [dict(r) for r in con.execute(
        "SELECT u.board_id, u.article_no, e.payload FROM unjudged u "
        "JOIN extractions e ON e.board_id=u.board_id "
        "  AND e.article_no=u.article_no ORDER BY u.noticed_at")]


def redeliver(con, notify=True, log=print) -> int:
    """Send the real alert for judged-but-undelivered notices. No API calls.

    The stored payload is the normalised verdict, so the alert is rebuilt
    exactly as it would have gone out. --reextract cannot do this: it only
    picks up notices with no verdict, and re-judging would spend allowance
    on an answer already on disk.
    """
    targets = _undelivered(con)
    log(f"재전송 대상 {len(targets)}건 (판정됨·미전달, API 미사용)")
    for row in targets:
        item = db.load_notice(con, row["board_id"], row["article_no"])
        if not item:
            continue
        # The verdict is left as stored — re-saving it would overwrite the
        # original extracted_at with today's date.
        db.clear_unjudged(con, row["board_id"], row["article_no"])
        _schedule_and_send(con, item, json.loads(row["payload"]), notify, log)
    return len(targets)
