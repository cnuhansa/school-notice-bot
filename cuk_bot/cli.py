"""Command line entry point.

  python -m cuk_bot --dry-run     파싱 결과만 출력 (LLM·텔레그램 미사용)
  python -m cuk_bot --backfill    기존 글을 읽음 처리 (최초 1회 필수)
  python -m cuk_bot --check       새 글 수집 + 추출 + 즉시 알림
  python -m cuk_bot --digest      다이제스트 + 리마인더 발송
  python -m cuk_bot --reextract   저장된 본문으로 재추출 (프롬프트 튜닝용)
  python -m cuk_bot --status      수집 상태와 파싱 실패 이력
  python -m cuk_bot --renormalize 판정 규칙 재적용 (API 미사용)
"""

import argparse
import sys

import json

from . import db, health, notifier, status
from .client import describe
from .collector import collect
from .config import (BOARDS_BY_ID, DAILY_REQUEST_LIMIT, MINUTE_REQUEST_LIMIT,
                     MODEL_CHAIN, NOTIFY_WHEN_UNJUDGED, READ_IMAGES)
from .content import resolve
from .extractor import normalize
from .judge import Judge
from .quota import QuotaExhausted, RequestBudget


def _extract_and_route(con, item, session, notify=True, log=print):
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
        log(f"  ⚠ 한도 소진 — 판정 없이 전달: {item['title'][:40]}")
        return None
    except Exception as exc:
        session.forward_unjudged(item, f"추출 실패: {str(exc)[:100]}")
        log(f"  ⚠ 추출 실패 — 판정 없이 전달 ({item['title'][:30]}): "
            f"{str(exc)[:80]}")
        return None

    db.clear_unjudged(con, item["board_id"], item["article_no"])
    db.save_extraction(con, item["board_id"], item["article_no"], data,
                       resolved["source"])

    if data["is_actionable"]:
        if notify:
            notifier.send_or_queue(
                con, notifier.format_alert(item, data), "alert", log=log)
        queued = notifier.schedule_reminders(
            con, item["board_id"], item["article_no"], data.get("apply_end"))
        log(f"  🔔 {item['title'][:44]} — 마감 "
            f"{notifier.fmt_deadline(data.get('apply_end'))}, 리마인더 {queued}건")
    else:
        db.queue_digest(con, item["board_id"], item["article_no"])
        log(f"  · 다이제스트 대기: {item['title'][:44]}")

    con.commit()
    return data


def _flush_unjudged(con, session, notify=True, log=print):
    """Send the one catch-all alert for everything that went unjudged."""
    if not session.unjudged:
        return
    if not NOTIFY_WHEN_UNJUDGED:
        log(f"  {len(session.unjudged)}건 미판정 — 알림 비활성화 상태")
        return

    text = notifier.format_unjudged(session.unjudged, session.reason)
    if not notify:
        log(text)
        return
    if notifier.send_or_queue(con, text, "unjudged", log=log):
        db.mark_unjudged_alerted(con, session.unjudged)
        con.commit()
    log(f"  ⚠ 미판정 {len(session.unjudged)}건 전달 완료")


def _report_board_failures(con, notify=True, log=print):
    """Tell the channel about boards that stopped parsing, once each."""
    broken = health.new_failures(con)
    if not broken:
        return
    log(f"  [!] 수집 실패 게시판: {', '.join(broken)}")
    text = notifier.format_board_failure(broken, health.FAILURE_STREAK)
    if notify:
        notifier.send_or_queue(con, text, "warning", log=log)
    for board_id in broken:
        health.mark_warned(con, board_id)


def cmd_check(con, notify=True, log=print):
    health.ping("start")
    try:
        if notify:
            notifier.flush_outbox(con, log=log)

        log("새 글 수집 중...")
        items = collect(con, log=log)
        log(f"새 글 {len(items)}건")

        session = Judge(con)
        log(f"  자격증명: {describe()}")
        log(f"  모델별 잔여 한도 — {session.summary()}")

        for item in items:
            if item.get("is_crosspost"):
                log(f"  [dup] 재게시로 판단해 건너뜀: {item['title'][:44]}")
                db.queue_digest(con, item["board_id"], item["article_no"])
                continue
            _extract_and_route(con, item, session, notify=notify, log=log)

        _flush_unjudged(con, session, notify=notify, log=log)
        _report_board_failures(con, notify=notify, log=log)
        con.commit()
    except Exception:
        # The watchdog must hear about a crash, not just a missing ping —
        # otherwise a run that dies fast looks identical to one never started.
        health.ping("fail")
        raise
    health.ping("success")


def cmd_digest(con, notify=True, log=print):
    health.ping("start", env=health.DIGEST_URL_ENV)
    if notify:
        notifier.flush_outbox(con, log=log)

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


def cmd_reextract(con, only_missing=True, log=print):
    """Re-run extraction on stored bodies — no crawling, no re-download.

    This is what makes prompt tuning cheap: change the prompt, re-grade the
    same sample, compare.
    """
    if only_missing:
        targets = db.pending_extraction(con)
    else:
        targets = [dict(r) for r in con.execute(
            "SELECT board_id, article_no FROM notices")]

    session = Judge(con)
    log(f"  자격증명: {describe()}")
    log(f"재추출 대상 {len(targets)}건 — 잔여 {session.summary()}")

    for target in targets:
        item = db.load_notice(con, target["board_id"], target["article_no"])
        if not item:
            continue
        board = BOARDS_BY_ID.get(item["board_id"])
        item["board_name"] = board["name"] if board else item["board_id"]
        _extract_and_route(con, item, session, notify=False, log=log)

    con.commit()
    if session.unjudged:
        log(f"  ⚠ {len(session.unjudged)}건은 한도 소진으로 여전히 미판정 "
            f"— 한도 회복 후 다시 실행하세요")


def cmd_status(con, log=print):
    status.report(con, log=log)


def cmd_renormalize(con, log=print):
    """Re-apply the judgement rules to stored replies. No API calls.

    When a rule in normalize() changes, every verdict already in the database
    is stale. Re-extracting would mean paying for answers we already have, so
    the stored raw reply is re-judged instead.
    """
    updated = 0
    for row in con.execute("SELECT board_id, article_no, source, payload, "
                           "is_actionable, confidence FROM extractions").fetchall():
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        fresh = normalize(payload, row["source"])
        if (bool(row["is_actionable"]) == fresh["is_actionable"]
                and abs((row["confidence"] or 0) - fresh["confidence"]) < 1e-9):
            continue
        db.save_extraction(con, row["board_id"], row["article_no"], fresh,
                           row["source"])
        updated += 1
    con.commit()
    log(f"판정 갱신 {updated}건 (API 호출 없음)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cuk_bot")
    ap.add_argument("--dry-run", action="store_true", help="파싱 결과만 출력")
    ap.add_argument("--backfill", action="store_true", help="기존 글 읽음 처리")
    ap.add_argument("--check", action="store_true", help="새 글 수집 + 알림")
    ap.add_argument("--digest", action="store_true", help="다이제스트 + 리마인더")
    ap.add_argument("--reextract", action="store_true", help="저장 본문 재추출")
    ap.add_argument("--all", action="store_true",
                    help="--reextract 시 추출 완료분까지 다시")
    ap.add_argument("--status", action="store_true", help="수집 상태 요약")
    ap.add_argument("--reset-quota", action="store_true",
                    help="오늘의 한도 소진 표시 해제")
    ap.add_argument("--renormalize", action="store_true",
                    help="저장된 응답에 판정 규칙 재적용 (API 미사용)")
    ap.add_argument("--no-notify", action="store_true", help="발송 없이 출력만")
    args = ap.parse_args(argv)

    con = db.connect()
    notify = not args.no_notify

    if args.dry_run:
        collect(con, dry_run=True)
    elif args.backfill:
        collect(con, mark_seen_only=True)
        print("기존 글을 읽음 처리했습니다. 이제 --check 를 스케줄에 거세요.")
    elif args.check:
        cmd_check(con, notify=notify)
    elif args.digest:
        cmd_digest(con, notify=notify)
    elif args.reextract:
        cmd_reextract(con, only_missing=not args.all)
    elif args.status:
        cmd_status(con)
    elif args.renormalize:
        cmd_renormalize(con)
    elif args.reset_quota:
        for name in MODEL_CHAIN:
            RequestBudget(con, DAILY_REQUEST_LIMIT, MINUTE_REQUEST_LIMIT,
                          model=name).unblock()
        print(f"{len(MODEL_CHAIN)}개 모델의 한도 소진 표시를 해제했습니다.")
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
