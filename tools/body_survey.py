#!/usr/bin/env python3
"""Measure how many notices actually carry extractable body text.

HANDOFF 14.3 makes attachment parsing a client decision, triggered when more
than 30% of a sample has no deadline in the body. The M1 probe showed dorm
notices are posted as screenshots, so that threshold needs a real number
rather than a single anecdote.

  python tools/body_survey.py --per-board 8
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuk_bot.config import BOARDS, BOARDS_BY_ID  # noqa: E402
from cuk_bot.fetcher import http_get, list_url  # noqa: E402
from cuk_bot.parser import parse_detail, parse_list  # noqa: E402

USABLE_CHARS = 300  # below this the LLM has nothing to reason about


def survey_board(board, per_board):
    rows = parse_list(board, http_get(list_url(board, limit=per_board * 2)))
    sample = rows[:per_board]
    print(f"\n{'=' * 74}\n[{board['id']}] {board['name']}  — {len(sample)} sampled")

    results = []
    for row in sample:
        try:
            detail = parse_detail(http_get(row["url"]))
        except Exception as exc:
            print(f"  [!] {row['article_no']} fetch failed: {exc}")
            continue

        chars = len(detail["body"])
        state = "TEXT" if chars >= USABLE_CHARS else (
            "THIN" if chars > 0 else "EMPTY")
        results.append({**detail, "chars": chars, "state": state,
                        "title": row["title"]})

        files = f" file={len(detail['attachments'])}" if detail["attachments"] else ""
        imgs = f" img={detail['image_count']}" if detail["image_count"] else ""
        print(f"  {state:<5} {chars:>5}c{imgs}{files}  {row['title'][:44]}")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-board", type=int, default=8)
    ap.add_argument("--board", action="append")
    args = ap.parse_args()

    boards = [BOARDS_BY_ID[b] for b in args.board] if args.board else BOARDS
    table = {}
    for board in boards:
        try:
            table[board["id"]] = survey_board(board, args.per_board)
        except Exception as exc:
            print(f"  [!] {board['id']} failed: {exc}")
            table[board["id"]] = []

    print(f"\n{'=' * 74}\nBODY SURVEY")
    print(f"  {'board':<18} {'n':>3} {'TEXT':>5} {'THIN':>5} {'EMPTY':>6} "
          f"{'usable%':>8}  {'img-only':>8}")
    total = usable = 0
    for bid, rows in table.items():
        n = len(rows)
        counts = {s: sum(1 for r in rows if r["state"] == s)
                  for s in ("TEXT", "THIN", "EMPTY")}
        img_only = sum(1 for r in rows
                       if r["chars"] < USABLE_CHARS and r["image_count"] > 0)
        pct = f"{counts['TEXT'] / n * 100:.0f}%" if n else "-"
        print(f"  {bid:<18} {n:>3} {counts['TEXT']:>5} {counts['THIN']:>5} "
              f"{counts['EMPTY']:>6} {pct:>8}  {img_only:>8}")
        total += n
        usable += counts["TEXT"]

    if total:
        print(f"\n  overall usable body: {usable}/{total} "
              f"({usable / total * 100:.0f}%)")
        print(f"  HANDOFF 14.3 threshold (>30% unusable → escalate to client): "
              f"{'TRIGGERED' if (total - usable) / total > 0.30 else 'not triggered'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
