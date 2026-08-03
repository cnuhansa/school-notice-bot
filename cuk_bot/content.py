"""Decide what the extractor actually reads for a given notice.

M1 measured that only 34% of notices carry usable body text; the dormitory
check-in boards that motivate this project sit at 0-25% because the school
posts notices as PNG screenshots. So the body text alone is not enough and
this module escalates through progressively more expensive sources:

    text  →  pdf attachment  →  screenshot images  →  title only

Escalation is lazy: a notice with a real text body never triggers a download.

robots.txt note
---------------
Board pages and `?mode=download` attachments are allowed for crawlers.
Editor images live under `/_attach/`, which robots.txt disallows. Reading
them was escalated to the client on 2026-08-03 and approved for this
personal, low-volume, non-redistributed use — it is the only path to the
dorm application deadlines. Fetching is therefore restricted to notices whose
body is empty, capped per notice, cached so an image is pulled at most once,
and never used for bulk mirroring. See docs/M1_RESULT.md section 3.
"""

import base64
import hashlib
import io
import os

from .config import (CACHE_DIR, MAX_IMAGES_PER_NOTICE, MIN_USABLE_CHARS,
                     THIN_TEXT_FLOOR)
from .fetcher import http_get_bytes

# Claude downsamples anything larger, so shrinking first cuts upload size and
# token cost without losing legibility.
MAX_IMAGE_EDGE = 1568
JPEG_QUALITY = 80


def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.sha256(url.encode()).hexdigest()[:32])


def fetch_cached(url: str) -> bytes:
    """Fetch `url`, reusing a local copy so --reextract costs no requests."""
    path = _cache_path(url)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()

    data = http_get_bytes(url)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return data


def prepare_image(raw: bytes):
    """Downscale to a JPEG block Claude can read, or None if undecodable."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    longest = max(img.size)
    if longest > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / longest
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))),
                         Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return {
        "media_type": "image/jpeg",
        "data": base64.b64encode(buf.getvalue()).decode(),
    }


def pdf_text(raw: bytes, max_chars: int = 6000) -> str:
    """Extract text from a PDF attachment. Empty string if it is a scan."""
    try:
        import fitz
    except ImportError:
        return ""

    try:
        with fitz.open(stream=raw, filetype="pdf") as doc:
            pages = [page.get_text() for page in doc[:10]]
    except Exception:
        return ""
    return "\n".join(pages).strip()[:max_chars]


def resolve(detail: dict, allow_images: bool = True) -> dict:
    """Pick the best available content for one notice.

    Returns the text the extractor should read, any image blocks to attach,
    the source label that ends up in the alert, and human-readable notes so a
    thin result can be explained instead of silently swallowed.
    """
    notes = []
    body = detail.get("body") or ""
    attachments = detail.get("attachments") or []

    # Attachment filenames are context even when unreadable: "입사신청서.hwp"
    # tells the model an application is being solicited.
    filenames = [a["name"] for a in attachments]
    file_note = ("\n\n[첨부파일]\n" + "\n".join(f"- {n}" for n in filenames)
                 if filenames else "")

    if len(body) >= MIN_USABLE_CHARS:
        return {"source": "text", "text": body + file_note,
                "images": [], "notes": notes}

    # PDFs are robots-allowed and often hold the actual 모집요강.
    for att in attachments:
        if att["kind"] != "pdf":
            continue
        try:
            text = pdf_text(fetch_cached(att["url"]))
        except Exception as exc:
            notes.append(f"pdf fetch failed ({att['name']}): {exc}")
            continue
        if len(text) >= MIN_USABLE_CHARS:
            notes.append(f"body from pdf: {att['name']}")
            return {"source": "pdf",
                    "text": (body + "\n\n[첨부 PDF 본문]\n" + text + file_note),
                    "images": [], "notes": notes}

    # HWP attachments are deliberately not parsed: M1 found they are blank
    # application forms and consent sheets, never the notice text itself.
    if any(a["kind"] == "hwp" for a in attachments):
        notes.append("hwp attachments present (forms — not parsed)")

    urls = detail.get("images") or []
    if allow_images and urls:
        blocks = []
        for url in urls[:MAX_IMAGES_PER_NOTICE]:
            try:
                block = prepare_image(fetch_cached(url))
            except Exception as exc:
                notes.append(f"image fetch failed: {exc}")
                continue
            if block:
                blocks.append(block)
        if len(urls) > MAX_IMAGES_PER_NOTICE:
            notes.append(f"image cap: read {MAX_IMAGES_PER_NOTICE} of {len(urls)}")
        if blocks:
            return {"source": "image", "text": body + file_note,
                    "images": blocks, "notes": notes}

    if urls and not allow_images:
        notes.append(f"{len(urls)} image(s) skipped (image reading disabled)")

    # No richer source exists. A short body is still worth reading — dropping
    # it here would discard real text and needlessly cap confidence.
    if len(body) >= THIN_TEXT_FLOOR:
        notes.append(f"thin body ({len(body)} chars) — no richer source")
        return {"source": "text", "text": body + file_note,
                "images": [], "notes": notes}

    notes.append("no readable content — title only")
    return {"source": "title", "text": body + file_note,
            "images": [], "notes": notes}
