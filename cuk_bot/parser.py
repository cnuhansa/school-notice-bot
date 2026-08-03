"""HTML parsing for the shared CMS.

M1 verified that every target site (main / dorm / department) renders the
same markup, so a single parser covers all eight boards:

    list    table rows containing a[href*="mode=view"]
    detail  div.b-content-box  ← pinned selector, verified on all 8 boards

There is deliberately no "longest div" fallback. Silently grabbing the wrong
node feeds the navigation menu to the LLM, which then invents a deadline —
the worst failure mode this system has.
"""

import re
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

BODY_SELECTOR = "div.b-content-box"
TITLE_SELECTOR = "p.b-title"
FILE_SELECTOR = "div.b-file-box a"

DATE_RE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")
_TRAILING_NOISE = re.compile(r"\s*(첨부파일|new|N)\s*$", re.I)


def article_no_of(href: str):
    nos = parse_qs(urlparse(href).query).get("articleNo")
    return nos[0] if nos else None


def parse_list(board: dict, html: str) -> list:
    """Return list rows as dicts, deduped by article_no.

    Pinned notices are printed again inside the numbered list, so article_no
    is the dedup key. Hashing the title instead would resurface a post every
    time the school edits a typo into it.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()

    for anchor in soup.select('a[href*="mode=view"]'):
        href = anchor.get("href", "")
        no = article_no_of(href)
        if not no or no in seen:
            continue
        seen.add(no)

        title = " ".join(anchor.get_text(" ", strip=True).split())
        title = _TRAILING_NOISE.sub("", title).strip()

        posted = None
        container = anchor.find_parent("tr") or anchor.find_parent("li")
        if container:
            match = DATE_RE.search(container.get_text(" ", strip=True))
            if match:
                posted = (f"{match.group(1)}-{int(match.group(2)):02d}"
                          f"-{int(match.group(3)):02d}")

        rows.append({
            "article_no": no,
            "title": title,
            "url": urljoin(board["url"], href),
            "posted_at": posted,
        })
    return rows


def parse_detail(html: str, page_url: str = "", max_chars: int = 6000) -> dict:
    """Extract the notice body plus the signals that explain an empty body.

    Dorm notices are usually posted as screenshots, leaving `body` empty. The
    caller needs to tell "parser broke" apart from "the post genuinely has no
    text", so image and attachment URLs come back alongside the text and the
    content resolver decides what to read (see cuk_bot.content).
    """
    soup = BeautifulSoup(html, "html.parser")

    node = soup.select_one(BODY_SELECTOR)
    body = node.get_text("\n", strip=True)[:max_chars] if node else ""

    title_node = soup.select_one(TITLE_SELECTOR)
    title = " ".join(title_node.get_text(" ", strip=True).split()) if title_node else None

    posted = None
    meta = soup.select_one("div.b-etc-box")
    if meta:
        match = DATE_RE.search(meta.get_text(" ", strip=True))
        if match:
            posted = (f"{match.group(1)}-{int(match.group(2)):02d}"
                      f"-{int(match.group(3)):02d}")

    # The CMS tags each download link with its file type as a css class
    # ("file-down-btn hwp"), which is cheaper and more reliable than sniffing
    # the filename extension.
    attachments = []
    for link in soup.select(FILE_SELECTOR):
        name = " ".join(link.get_text(" ", strip=True).split())
        name = re.sub(r"\s*다운로드\s*$", "", name).strip()
        if not name:
            continue
        kinds = [c for c in link.get("class", []) if c != "file-down-btn"]
        attachments.append({
            "name": name,
            "kind": (kinds[0] if kinds else _ext_of(name)).lower(),
            "url": urljoin(page_url, link.get("href", "")),
        })

    images = []
    if node:
        for img in node.select("img"):
            src = img.get("src")
            if src:
                images.append(urljoin(page_url, src))

    return {
        "body": body,
        "title": title,
        "posted_at": posted,
        "selector_found": node is not None,
        "images": images,
        "image_count": len(images),
        "attachments": attachments,
    }


def _ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1] if "." in name else ""


def title_key(title: str) -> str:
    """Comparison key with bracket prefixes and symbols stripped.

    Department boards re-post main campus notices under a different
    article_no; comparing on this key catches the duplicate. Titles often mix
    Korean, English and Chinese on one line, so only the leading Korean run is
    kept to stop translations from splitting the key.
    """
    text = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", title or "")
    korean_run = re.match(r"[\s0-9가-힣ㆍ·()\[\]./-]+", text)
    if korean_run and len(korean_run.group(0).strip()) >= 8:
        text = korean_run.group(0)
    return re.sub(r"[^0-9가-힣a-zA-Z]", "", text).lower()
