#!/usr/bin/env python3
"""M1 parsing verification probe (HANDOFF 5, 10).

Checks the assumptions the project's risk is concentrated in:

  A1  list links are reachable via a[href*="mode=view"]
  A2  the detail body comes out of a pinned selector
  A3  the posted date sits in the same <tr>/<li> as the link
  A4  dorm/department sites share the main campus markup

Parsing itself lives in cuk_bot.parser so this probe verifies the code that
actually ships. `--diagnose` re-ranks candidate containers from scratch and is
the tool to reach for if the pinned selector ever stops matching.

  python tools/m1_probe.py
  python tools/m1_probe.py --board dorm_ka_inout --diagnose
"""

import argparse
import os
import re
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuk_bot.config import BOARDS, BOARDS_BY_ID  # noqa: E402
from cuk_bot.fetcher import http_get, list_url  # noqa: E402
from cuk_bot.parser import BODY_SELECTOR, parse_detail, parse_list  # noqa: E402

LIST_LIMIT = 15
MIN_LIST_ITEMS = 10   # M1 completion bar
MIN_BODY_CHARS = 300  # M1 completion bar

BOILERPLATE = ("script", "style", "nav", "header", "footer", "form")
HINT_RE = re.compile(r"cont|view|body|article|bbs|board|txt|detail", re.I)


def diagnose_containers(html, top=8):
    """Rank plausible body containers — only for when the pinned selector dies.

    Wrappers that contain a better-scoped candidate are skipped so the ranking
    is not won by the page shell.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(BOILERPLATE):
        tag.decompose()

    out = []
    for node in soup.find_all(["div", "td", "section", "article"]):
        ident = " ".join(node.get("class", []) + [node.get("id") or ""]).strip()
        if not ident or not HINT_RE.search(ident):
            continue
        if any(HINT_RE.search(" ".join(c.get("class", []) + [c.get("id") or ""]))
               for c in node.find_all(["div", "td", "section", "article"])):
            continue
        selector = node.name + (f"#{node['id']}" if node.get("id") else "")
        selector += "".join(f".{c}" for c in node.get("class", []))
        out.append((len(node.get_text("\n", strip=True)), selector))

    out.sort(reverse=True)
    return out[:top]


def probe(board, diagnose=False):
    print(f"\n{'=' * 74}\n[{board['id']}] {board['name']}")
    verdict = {"a1": False, "a2": False, "a3": False}

    try:
        rows = parse_list(board, http_get(list_url(board, limit=LIST_LIMIT)))
    except Exception as exc:
        print(f"  A1 FAIL — request error: {exc}")
        return verdict

    dated = sum(1 for r in rows if r["posted_at"])
    verdict["a1"] = len(rows) >= MIN_LIST_ITEMS
    verdict["a3"] = bool(rows) and dated == len(rows)
    print(f"  A1 {'PASS' if verdict['a1'] else 'FAIL'} — {len(rows)} links "
          f"(need >= {MIN_LIST_ITEMS})")
    print(f"  A3 {'PASS' if verdict['a3'] else 'FAIL'} — {dated}/{len(rows)} dated")
    for row in rows[:3]:
        print(f"    · {row['posted_at'] or '????-??-??'}  #{row['article_no']:>7}  "
              f"{row['title'][:48]}")

    if not rows:
        return verdict

    html = http_get(rows[0]["url"])
    detail = parse_detail(html)
    chars = len(detail["body"])

    # A2 asks whether the parser finds the right node. A body that is short
    # because the notice is a screenshot is a scope problem, not a parser bug,
    # so the two are reported separately.
    verdict["a2"] = detail["selector_found"]
    verdict["chars"] = chars
    print(f"  A2 {'PASS' if verdict['a2'] else 'FAIL'} — {BODY_SELECTOR} "
          f"{'matched' if detail['selector_found'] else 'MISSING'}, "
          f"{chars} chars, img={detail['image_count']}, "
          f"file={len(detail['attachments'])}")
    if chars < MIN_BODY_CHARS:
        print(f"    ⚠ body under {MIN_BODY_CHARS} chars — likely image-only "
              f"(see tools/body_survey.py)")
    else:
        print(f"    preview: {' '.join(detail['body'].split())[:110]}")

    if diagnose:
        print("    candidate containers:")
        for length, selector in diagnose_containers(html):
            print(f"      {length:>6}  {selector}")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", action="append", help="board id (repeatable)")
    ap.add_argument("--diagnose", action="store_true",
                    help="re-rank body container candidates")
    args = ap.parse_args()

    boards = [BOARDS_BY_ID[b] for b in args.board] if args.board else BOARDS
    results = {b["id"]: probe(b, args.diagnose) for b in boards}

    print(f"\n{'=' * 74}\nM1 SUMMARY")
    print(f"  {'board':<18} {'A1':<6} {'A3':<6} {'A2':<6} {'body chars':>10}")
    ok = True
    for bid, v in results.items():
        print(f"  {bid:<18} {'PASS' if v['a1'] else 'FAIL':<6} "
              f"{'PASS' if v['a3'] else 'FAIL':<6} "
              f"{'PASS' if v['a2'] else 'FAIL':<6} {v.get('chars', 0):>10}")
        ok = ok and all((v["a1"], v["a2"], v["a3"]))

    thin = [b for b, v in results.items() if v.get("chars", 0) < MIN_BODY_CHARS]
    print(f"\n  A4 — one selector ({BODY_SELECTOR}) covers all sites: "
          f"{'PASS' if ok else 'see failures above'}")
    print(f"  parser verdict: {'PASS' if ok else 'FAIL'}")
    if thin:
        print(f"  content verdict: {len(thin)} board(s) returned thin bodies "
              f"→ {', '.join(thin)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
