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
from .config import BOARDS_BY_ID, READ_IMAGES
from .content import resolve
from .extractor import extract


def _extract_and_route(con, item, notify=True, log=print):
    """Extract one notice, then either alert immediately or queue a digest."""
    resolved = resolve(item, allow_images=READ_IMAGES)
    try:
        data = extract(item, resolved)
    except Exception as exc:
        log(f"  [!] 추출 실패 ({item['title'][:30]}): {str(exc)[:120]}")
        return None

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


def cmd_check(con, notify=True, log=print):
    log("새 글 수집 중...")
    items = collect(con, log=log)
    log(f"새 글 {len(items)}건")

    for item in items:
        if item.get("is_crosspost"):
            log(f"  [dup] 재게시로 판단해 건너뜀: {item['title'][:44]}")
            db.queue_digest(con, item["board_id"], item["article_no"])
            continue
        _extract_and_route(con, item, notify=notify, log=log)
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

    log(f"재추출 대상 {len(targets)}건")
    for target in targets:
        item = db.load_notice(con, target["board_id"], target["article_no"])
        if not item:
            continue
        board = BOARDS_BY_ID.get(item["board_id"])
        item["board_name"] = board["name"] if board else item["board_id"]
        _extract_and_route(con, item, notify=False, log=log)


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
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
