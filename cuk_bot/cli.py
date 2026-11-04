"""Command line entry point.

  python -m cuk_bot --dry-run     파싱 결과만 출력 (LLM·텔레그램 미사용)
  python -m cuk_bot --backfill    기존 글을 읽음 처리 (최초 1회 필수)
  python -m cuk_bot --check       새 글 수집 + 추출 + 즉시 알림
  python -m cuk_bot --digest      다이제스트 + 리마인더 발송
  python -m cuk_bot --reextract   저장된 본문으로 재추출 (프롬프트 튜닝용)
  python -m cuk_bot --reextract --unjudged  판정 없이 전달된 공지만 재판정
  python -m cuk_bot --redeliver   판정됐지만 알림이 안 나간 공지 재전송
  python -m cuk_bot --status      수집 상태와 파싱 실패 이력
  python -m cuk_bot --renormalize 판정 규칙 재적용 (API 미사용)
"""

import argparse
import sys
from datetime import date

import json

from . import db, digest, health, notifier, route, status
from .client import describe
from .collector import collect
from .config import (BOARDS_BY_ID, DAILY_REQUEST_LIMIT, MINUTE_REQUEST_LIMIT,
                     MODEL_CHAIN, NOTIFY_WHEN_UNJUDGED)
from .extractor import brief_error, normalize
from .judge import Judge
from .quota import RequestBudget

# Bound here so tests can patch the routing step on this module.
_title = route.title
_extract_and_route = route.extract_and_route


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


def _report_credential_switch(session, log=print):
    """Say in the run log that a credential was dropped and why.

    The handover keeps judging alive, which also hides the cause — a billed
    project quietly refusing is otherwise invisible until someone reads code.
    """
    for note in session.dropped:
        log(f"  [!] 자격증명 전환 — {note} → 남은 자격증명: {session.credential()}")


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
            # One malformed notice must not end the run. Before this guard a
            # single unexpected value aborted cmd_check, so the remaining new
            # notices went unprocessed AND the DB commit step was skipped —
            # the next run then re-read the same notice and died again.
            # Anything we cannot judge is forwarded unjudged, which is the
            # same promise the extraction path already makes. A notice that
            # WAS judged must not be — see route.RouteFailed.
            try:
                if item.get("is_crosspost"):
                    log(f"  [dup] 재게시로 판단해 건너뜀: {_title(item)[:44]}")
                    db.queue_digest(con, item["board_id"], item["article_no"])
                    continue
                _extract_and_route(con, item, session, notify=notify, log=log)
            except route.RouteFailed as exc:
                log(f"  [!] 판정 후 처리 실패 ({_title(item)[:30]}): "
                    f"{str(exc.__cause__)[:400]}")
                if notify:
                    notifier.send_or_queue(
                        con, notifier.format_route_failure(item, str(exc)),
                        "warning", log=log)
            except Exception as exc:
                log(f"  [!] 처리 실패 — 판정 없이 전달 "
                    f"({_title(item)[:30]}): {str(exc)[:400]}")
                session.forward_unjudged(item, f"처리 실패: {brief_error(exc)}")

        _report_credential_switch(session, log=log)
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
    digest.run(con, notify=notify, log=log)


def cmd_reextract(con, only_missing=True, unjudged=False, notify=False,
                  log=print):
    """Re-run extraction on stored bodies — no crawling, no re-download.

    This is what makes prompt tuning cheap: change the prompt, re-grade the
    same sample, compare. With `unjudged`, only notices that went out without
    a verdict are graded, and an actionable one finally gets its real alert.
    """
    if unjudged:
        targets = db.pending_unjudged(con)
    elif only_missing:
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
        _extract_and_route(con, item, session, notify=notify, log=log)

    _report_credential_switch(session, log=log)
    con.commit()
    if session.unjudged:
        log(f"  ⚠ {len(session.unjudged)}건은 여전히 미판정({session.reason}) "
            f"— 원인이 풀린 뒤 다시 실행하세요")


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
    ap.add_argument("--unjudged", action="store_true",
                    help="--reextract 시 판정 없이 전달된 공지만 (신청 공지는 알림 발송)")
    ap.add_argument("--redeliver", action="store_true",
                    help="판정은 됐는데 알림이 안 나간 공지 재전송 (API 미사용)")
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
        cmd_reextract(con, only_missing=not args.all, unjudged=args.unjudged,
                      notify=notify and args.unjudged)
    elif args.redeliver:
        route.redeliver(con, notify=notify)
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
