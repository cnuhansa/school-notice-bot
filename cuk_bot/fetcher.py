"""Single-threaded, rate-limited HTTP access.

One module-level session enforces the delay globally, so no caller can
accidentally bypass it. Requests are never issued in parallel (HANDOFF 11.5).
"""

import time

import requests

from .config import HEADERS, REQUEST_DELAY, REQUEST_TIMEOUT

_session = requests.Session()
_session.headers.update(HEADERS)

_last_request_at = 0.0


def http_get(url: str) -> str:
    """GET `url` as decoded text, sleeping so requests stay REQUEST_DELAY apart."""
    global _last_request_at

    wait = REQUEST_DELAY - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)

    resp = _session.get(url, timeout=REQUEST_TIMEOUT)
    _last_request_at = time.monotonic()
    resp.raise_for_status()

    # The CMS declares its charset in the headers; fall back to detection
    # only when the declaration is missing or the useless ISO-8859-1 default.
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def http_get_bytes(url: str) -> bytes:
    """GET `url` as raw bytes, under the same global rate limit as http_get."""
    global _last_request_at

    wait = REQUEST_DELAY - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)

    resp = _session.get(url, timeout=REQUEST_TIMEOUT)
    _last_request_at = time.monotonic()
    resp.raise_for_status()
    return resp.content


def list_url(board: dict, limit: int = 10, offset: int = 0) -> str:
    return f"{board['url']}?mode=list&articleLimit={limit}&article.offset={offset}"
