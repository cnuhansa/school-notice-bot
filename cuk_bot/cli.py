"""Command line entry point.

  python -m cuk_bot --dry-run     파싱 결과만 출력 (LLM·텔레그램 미사용)
  python -m cuk_bot --backfill    기존 글을 읽음 처리 (최초 1회 필수)
  python -m cuk_bot --check       새 글 수집 + 추출 + 즉시 알림
  python -m cuk_bot --digest      다이제스트 + 리마인더 발송
  python -m cuk_bot --reextract   저장된 본문으로 재추출 (프롬프트 튜닝용)
  python -m cuk_bot --status      수집 상태와 파싱 실패 이력
"""

import argparse
import sys

from . import db, notifier
from .collector import collect
from .config import (BOARDS_BY_ID, DAILY_REQUEST_LIMIT, MINUTE_REQUEST_LIMIT,
                     MODEL_CHAIN, NOTIFY_WHEN_UNJUDGED, READ_IMAGES)
from .content import resolve
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
            notifier.send(notifier.format_alert(item, data), log=log)
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
    if notifier.send(text, log=log):
        db.mark_unjudged_alerted(con, session.unjudged)
        con.commit()
    log(f"  ⚠ 미판정 {len(session.unjudged)}건 전달 완료")


def cmd_check(con, notify=True, log=print):
    log("새 글 수집 중...")
    items = collect(con, log=log)
    log(f"새 글 {len(items)}건")

    session = Judge(con)
    log(f"  모델별 잔여 한도 — {session.summary()}")

    for item in items:
        if item.get("is_crosspost"):
            log(f"  [dup] 재게시로 판단해 건너뜀: {item['title'][:44]}")
            db.queue_digest(con, item["board_id"], item["article_no"])
            continue
        _extract_and_route(con, item, session, notify=notify, log=log)

    _flush_unjudged(con, session, notify=notify, log=log)
    con.commit()


def cmd_digest(con, notify=True, log=print):
    notices = [dict(r) for r in con.execute("""
        SELECT n.title, n.url, p.board_id, p.article_no
        FROM pending_digest p
        JOIN notices n ON n.board_id = p.board_id
                      AND n.article_no = p.article_no
    """)]
    reminders = notifier.due_reminders(con)

    text = notifier.format_digest(notices, reminders)
    if not text:
        log("발송할 내용 없음")
        return

    if not notify or notifier.send(text, log=log):
        con.execute("DELETE FROM pending_digest")
        for row in reminders:
            notifier.mark_reminder_sent(con, row)
        con.commit()
    log(f"다이제스트: 공지 {len(notices)}건, 리마인더 {len(reminders)}건")


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
    rows = con.execute("""
        SELECT n.board_id, COUNT(*) AS notices,
               SUM(CASE WHEN e.article_no IS NOT NULL THEN 1 ELSE 0 END) AS extracted,
               SUM(CASE WHEN e.is_actionable = 1 THEN 1 ELSE 0 END) AS actionable
        FROM notices n
        LEFT JOIN extractions e ON e.board_id = n.board_id
                               AND e.article_no = n.article_no
        GROUP BY n.board_id ORDER BY n.board_id
    """).fetchall()

    log(f"  {'board':<18} {'notices':>8} {'extracted':>10} {'actionable':>11}")
    for row in rows:
        log(f"  {row['board_id']:<18} {row['notices']:>8} "
            f"{row['extracted'] or 0:>10} {row['actionable'] or 0:>11}")

    log("\n  모델별 무료 한도 (체인 순서)")
    for name in MODEL_CHAIN:
        budget = RequestBudget(con, DAILY_REQUEST_LIMIT,
                               MINUTE_REQUEST_LIMIT, model=name)
        observed = budget.observed_limit()
        source = f"API 관측 {observed}" if observed else "미관측"
        state = " · 소진" if budget.is_blocked() else ""
        log(f"    {name:<24} {budget.used_today():>3}/{budget.daily_limit:<4}"
            f" 잔여 {budget.remaining():<4} ({source}){state}")

    waiting = con.execute(
        "SELECT COUNT(*) FROM unjudged u LEFT JOIN extractions e "
        "ON e.board_id=u.board_id AND e.article_no=u.article_no "
        "WHERE e.article_no IS NULL").fetchone()[0]
    if waiting:
        log(f"  미판정 {waiting}건 — 한도 회복 후 --reextract 로 판정하세요")

    failures = db.failure_counts(con)
    if failures:
        log("\n  파싱 실패 이력 (최근 120일):")
        for row in failures:
            log(f"    {row['board_id']:<18} {row['failures']}회")
    else:
        log("\n  파싱 실패 없음")


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
