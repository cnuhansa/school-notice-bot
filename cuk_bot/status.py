"""Operational reporting: what has been collected, spent and left undelivered.

Split out of cuk_bot.cli so that reporting can grow without pushing the
command dispatch past the file size limit.
"""

import json

from . import db, health, notifier
from .client import describe
from .config import (DAILY_REQUEST_LIMIT, DEFAULT_PRICE, MINUTE_REQUEST_LIMIT,
                     MODEL_CHAIN, MODEL_PRICES)
from .extractor import normalize
from .quota import RequestBudget


def report(con, log=print) -> None:
    _boards(con, log)
    _models(con, log)
    _queues(con, log)
    _failures(con, log)


def _boards(con, log) -> None:
    rows = con.execute("""
        SELECT n.board_id, COUNT(*) AS notices,
               SUM(CASE WHEN e.article_no IS NOT NULL THEN 1 ELSE 0 END)
                   AS extracted,
               SUM(CASE WHEN e.is_actionable = 1 THEN 1 ELSE 0 END)
                   AS actionable
        FROM notices n
        LEFT JOIN extractions e ON e.board_id = n.board_id
                               AND e.article_no = n.article_no
        GROUP BY n.board_id ORDER BY n.board_id
    """).fetchall()

    log(f"  {'board':<18} {'notices':>8} {'extracted':>10} {'actionable':>11}")
    for row in rows:
        log(f"  {row['board_id']:<18} {row['notices']:>8} "
            f"{row['extracted'] or 0:>10} {row['actionable'] or 0:>11}")


def _models(con, log) -> None:
    log(f"\n  자격증명: {describe()}")
    log("  모델별 한도 (체인 순서)")

    cost = 0.0
    for name in MODEL_CHAIN:
        budget = RequestBudget(con, DAILY_REQUEST_LIMIT,
                               MINUTE_REQUEST_LIMIT, model=name)
        observed = budget.observed_limit()
        source = f"API 관측 {observed}" if observed else "미관측"
        state = " · 소진" if budget.is_blocked() else ""
        log(f"    {name:<24} {budget.used_today():>3}/{budget.daily_limit:<4}"
            f" 잔여 {budget.remaining():<4} ({source}){state}")

        tok_in, tok_out = budget.tokens_today()
        if tok_in or tok_out:
            price_in, price_out = MODEL_PRICES.get(name, DEFAULT_PRICE)
            spent = (tok_in * price_in + tok_out * price_out) / 1_000_000
            cost += spent
            log(f"      토큰 in {tok_in:,} / out {tok_out:,} → 약 ${spent:.4f}")

    if cost:
        # Only meaningful on the billed test project; the free tier costs
        # nothing regardless of what this says.
        log(f"    오늘 예상 비용 합계 ${cost:.4f} "
            f"(참고용 추정 — 실제 청구서와 다를 수 있음)")


def _queues(con, log) -> None:
    waiting = con.execute(
        "SELECT COUNT(*) FROM unjudged u LEFT JOIN extractions e "
        "ON e.board_id=u.board_id AND e.article_no=u.article_no "
        "WHERE e.article_no IS NULL").fetchone()[0]
    if waiting:
        log(f"  미판정 {waiting}건 — 한도 회복 후 --reextract 로 판정하세요")

    stuck = db.stuck_messages(con, notifier.MAX_SEND_ATTEMPTS)
    pending = len(db.pending_outbox(con, notifier.MAX_SEND_ATTEMPTS))
    if stuck or pending:
        log(f"  미발송 알림: 재시도 대기 {pending}건, 재시도 포기 {stuck}건")

    stale = sum(1 for r in con.execute(
        "SELECT source, is_actionable, confidence, payload FROM extractions")
        if _differs(r))
    if stale:
        log(f"  판정 규칙 변경으로 낡은 판정 {stale}건 "
            f"— --renormalize 로 갱신 (API 호출 없음)")


def _differs(row) -> bool:
    """Whether a stored verdict disagrees with what the rules say today."""
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return False
    fresh = normalize(payload, row["source"])
    return (bool(row["is_actionable"]) != fresh["is_actionable"]
            or abs((row["confidence"] or 0) - fresh["confidence"]) > 1e-9)


def _failures(con, log) -> None:
    failures = db.failure_counts(con)
    if failures:
        log("\n  파싱 실패 이력 (최근 120일):")
        for row in failures:
            log(f"    {row['board_id']:<18} {row['failures']}회")
    else:
        log("\n  파싱 실패 없음")

    broken = health.failing_boards(con)
    if broken:
        log(f"  현재 연속 실패 중: {', '.join(broken)}")
