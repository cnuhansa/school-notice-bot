"""Crawl the boards and store new notices.

Only list pages are polled; a detail page is fetched only when an article_no
has not been seen before. Requests are serialised through cuk_bot.fetcher, so
adding boards raises run time, never concurrency against the school's server.
"""

from . import db
from .config import BOARDS
from .fetcher import http_get, list_url
from .parser import parse_detail, parse_list, title_key


def _crosspost_of(con, title: str, days: int = 14) -> bool:
    """Department boards re-post main campus notices under a new article_no.

    Comparing normalised titles catches the duplicate before it costs an LLM
    call and a second alert (HANDOFF 11.3).
    """
    key = title_key(title)
    if len(key) < 8:
        return False
    return any(title_key(other or "") == key
               for other in db.recent_titles(con, days))


def collect_board(con, board: dict, limit: int = 15, mark_seen_only: bool = False,
                  dry_run: bool = False, log=print) -> list:
    try:
        rows = parse_list(board, http_get(list_url(board, limit=limit)))
    except Exception as exc:
        log(f"  [!] {board['name']} 목록 실패: {exc}")
        db.log_crawl(con, board["id"], ok=False, count=0, error=str(exc))
        con.commit()
        return []

    # An empty listing is a parse failure, not a quiet board. These boards
    # always carry 15+ notices, so zero means the school changed the markup
    # and the selector stopped matching. Logging it as a success would let
    # the bot report healthy while silently seeing nothing.
    if not rows:
        log(f"  [!] {board['name']} 목록 0건 — 파싱 실패로 간주")
        db.log_crawl(con, board["id"], ok=False, count=0,
                     error="목록 0건 (셀렉터 불일치 의심)")
        con.commit()
        return []

    log(f"  {board['name']}: {len(rows)}건 확인")

    fresh = []
    for row in rows:
        if db.is_known(con, board["id"], row["article_no"]):
            continue

        if dry_run:
            log(f"    [NEW] {row['posted_at'] or '??????????'} {row['title'][:60]}")
            fresh.append({**row, "board_id": board["id"]})
            continue

        detail = {}
        if not mark_seen_only:
            try:
                detail = parse_detail(http_get(row["url"]), row["url"])
            except Exception as exc:
                log(f"  [!] 본문 실패 {row['article_no']}: {exc}")

        # Must be decided BEFORE the notice is stored, otherwise it matches
        # its own freshly written title and every notice looks like a repost.
        crosspost = (not mark_seen_only) and _crosspost_of(con, row["title"])

        db.save_notice(con, board["id"], row, detail)
        if not mark_seen_only:
            fresh.append({
                **row,
                "board_id": board["id"],
                "board_name": board["name"],
                "is_crosspost": crosspost,
                **detail,
            })

    db.log_crawl(con, board["id"], ok=True, count=len(rows))
    con.commit()
    log(f"    → 새 글 {len(fresh)}건")
    return fresh


def collect(con, boards: list = None, **kwargs) -> list:
    out = []
    for board in (boards or BOARDS):
        out.extend(collect_board(con, board, **kwargs))
    return out
