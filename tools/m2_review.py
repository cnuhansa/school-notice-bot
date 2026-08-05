#!/usr/bin/env python3
"""M2 extraction review harness (HANDOFF section 10).

M2 is passed by hand-checking a sample, not by the code running without
errors. This collects a sample, runs the real resolve→extract path, and
prints one line per notice so is_actionable and apply_end can be eyeballed
against the source page.

  python tools/m2_review.py --offline --limit 4   # content resolution only
  python tools/m2_review.py --limit 4             # full extraction (uses API)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuk_bot import db  # noqa: E402
from cuk_bot.collector import collect_board  # noqa: E402
from cuk_bot.config import (BOARDS, BOARDS_BY_ID, DAILY_REQUEST_LIMIT,  # noqa: E402
                            MINUTE_REQUEST_LIMIT, READ_IMAGES)
from cuk_bot.content import resolve  # noqa: E402
from cuk_bot.extractor import extract, make_client  # noqa: E402
from cuk_bot.quota import QuotaExhausted, RequestBudget  # noqa: E402


def review_board(con, board, limit, offline, client=None, budget=None):
    print(f"\n{'=' * 78}\n[{board['id']}] {board['name']}")
    fresh = collect_board(con, board, limit=limit * 2, log=lambda m: None)

    if not fresh:
        stored = con.execute(
            "SELECT board_id, article_no FROM notices WHERE board_id=? "
            "ORDER BY posted_at DESC LIMIT ?", (board["id"], limit),
        ).fetchall()
        fresh = [db.load_notice(con, r["board_id"], r["article_no"])
                 for r in stored]
        for item in fresh:
            item["board_name"] = board["name"]

    rows = []
    for item in fresh[:limit]:
        resolved = resolve(item, allow_images=READ_IMAGES)
        imgs = len(resolved["images"])
        head = (f"  {resolved['source']:<6} txt={len(resolved['text']):>5} "
                f"img={imgs}  {item['title'][:46]}")

        if offline:
            print(head)
            for note in resolved["notes"]:
                print(f"         · {note}")
            rows.append({"source": resolved["source"], "data": None})
            continue

        try:
            data = extract(item, resolved, client=client, budget=budget)
        except QuotaExhausted as exc:
            print(head + f"\n         ! 무료 한도 소진 — 검수 중단: {exc}")
            raise
        except Exception as exc:
            print(head + "\n         ! extraction failed: " + str(exc)[:90])
            continue

        db.save_extraction(con, item["board_id"], item["article_no"], data,
                           resolved["source"])
        con.commit()

        flag = "ACT " if data["is_actionable"] else "    "
        print(f"  {flag}{resolved['source']:<6} conf={data['confidence']:.2f} "
              f"end={data.get('apply_end') or '-':<16} {item['title'][:40]}")
        print(f"         → {data.get('one_line', '')[:88]}")
        for note in resolved["notes"]:
            print(f"         · {note}")
        rows.append({"source": resolved["source"], "data": data})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4, help="notices per board")
    ap.add_argument("--board", action="append")
    ap.add_argument("--offline", action="store_true",
                    help="resolve content only, no API calls")
    args = ap.parse_args()

    boards = [BOARDS_BY_ID[b] for b in args.board] if args.board else BOARDS
    con = db.connect()

    client = budget = None
    if not args.offline:
        budget = RequestBudget(con, DAILY_REQUEST_LIMIT, MINUTE_REQUEST_LIMIT)
        print(f"Gemini 무료 한도: 오늘 {budget.used_today()}건 사용, "
              f"잔여 {budget.remaining()}건")
        client = make_client()

    all_rows = []
    for board in boards:
        try:
            all_rows.extend(review_board(con, board, args.limit, args.offline,
                                         client=client, budget=budget))
        except QuotaExhausted:
            print("\n한도가 소진되어 나머지 게시판 검수를 중단합니다. "
                  "내일 다시 실행하세요.")
            break

    print(f"\n{'=' * 78}\nM2 REVIEW SUMMARY   (image reading: "
          f"{'ON' if READ_IMAGES else 'OFF'})")
    sources = {}
    for row in all_rows:
        sources[row["source"]] = sources.get(row["source"], 0) + 1
    print(f"  content source: " +
          ", ".join(f"{k}={v}" for k, v in sorted(sources.items())))

    graded = [r["data"] for r in all_rows if r["data"]]
    if graded:
        act = sum(1 for d in graded if d["is_actionable"])
        dated = sum(1 for d in graded if d.get("apply_end"))
        print(f"  extracted: {len(graded)}  actionable: {act}  "
              f"with deadline: {dated} ({dated / len(graded) * 100:.0f}%)")
        print("\n  다음 단계: 위 판정을 원문과 대조해 오판을 세고 프롬프트를 고친다.")
        print("  M2 통과 기준 — is_actionable 90%↑, 마감일 85%↑ (표본 30건 이상)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
