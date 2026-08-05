"""Shared configuration: target boards, HTTP politeness settings, paths.

All values that a deploy might need to change are environment-overridable.
"""

import os

# ─────────────────────────────────────────────────────────────
# Paths / models
# ─────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("CUK_DB", "cuk_notices.db")
CACHE_DIR = os.environ.get("CUK_CACHE", ".cache")

# ─────────────────────────────────────────────────────────────
# Gemini (free tier)
# ─────────────────────────────────────────────────────────────
#
# The free tier comes from the Gemini Developer API (AI Studio) key in
# GEMINI_API_KEY. A GCP service-account JSON authenticates to Vertex AI
# instead, which is billed — it will not keep this bot free.

# gemini-2.5-* and 2.0-* retire 2026-10-16, so the 3.x models lead. All four
# below were checked against the F관 모집 공고 screenshot on 2026-08-05 and
# read 신청 기간 2026-08-03~08-04 17:00, 대상, and the 접수 이메일 correctly.
MODEL = os.environ.get("CUK_MODEL", "gemini-3.1-flash-lite")

# The free allowance is granted per model, so exhausting one leaves the next
# untouched. Walking this chain multiplies the daily budget while staying
# free — the measured 20/day per model is well under a busy weekday.
#
# A retired name raises ModelUnavailable and is skipped rather than failing
# the notice, so the October retirement degrades capacity instead of breaking
# the bot. The trailing alias always resolves to a current model, which is
# what keeps the chain from emptying entirely.
MODEL_CHAIN = [m.strip() for m in os.environ.get(
    "CUK_MODEL_CHAIN",
    "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.5-flash,"
    "gemini-3.6-flash,gemini-2.5-flash-lite,gemini-2.5-flash,"
    "gemini-flash-lite-latest").split(",") if m.strip()]
if MODEL not in MODEL_CHAIN:
    MODEL_CHAIN.insert(0, MODEL)

# Google publishes free-tier limits per model and revises them, so these are
# a conservative local guard, not a claim about the current allowance. The
# authoritative signal is a 429 from the API; this only avoids hammering it.
# A starting guess only. The real allowance is learned from the API's 429
# and stored per model, so this stops mattering after the first exhaustion.
DAILY_REQUEST_LIMIT = int(os.environ.get("CUK_GEMINI_RPD", "180"))

# Screenshot notices carry several images per request, so the free tier's
# per-minute allowance binds long before the daily one. Measured on
# 2026-08-05: image-heavy calls drew a 429 with a 23s retry at roughly five
# per minute. These are empirical guards, not published limits.
MINUTE_REQUEST_LIMIT = int(os.environ.get("CUK_GEMINI_RPM", "3"))

# When the allowance runs out the bot stops judging and notifies about every
# new notice instead. Over-notifying is recoverable; a missed 입사 신청
# deadline is not. Set to 0 to fall back to silence.
NOTIFY_WHEN_UNJUDGED = os.environ.get("CUK_NOTIFY_UNJUDGED", "1") != "0"

# ─────────────────────────────────────────────────────────────
# Content resolution (see cuk_bot/content.py, docs/M1_RESULT.md)
# ─────────────────────────────────────────────────────────────

# Below this the resolver looks for a richer source (pdf, screenshots).
MIN_USABLE_CHARS = 300

# ...but a short body is still a body. Text this long is kept and read
# normally when no richer source exists; only below it is a notice treated as
# title-only, which caps how confident the extractor is allowed to be.
THIN_TEXT_FLOOR = 60

# Dorm notices run up to 20 screenshots. The deadline lives in the opening
# pages; the rest are forms and floor plans. Verified on the F관 추가 모집
# 공고, where the 모집일정 table sits on the first image. Three keeps requests
# inside the free tier's per-minute allowance. Capping is logged, never silent.
MAX_IMAGES_PER_NOTICE = int(os.environ.get("CUK_MAX_IMAGES", "3"))

# Reading /_attach/ images is a client-approved exception to robots.txt
# (2026-08-03). Set to 0 to fall back to title-only alerts.
READ_IMAGES = os.environ.get("CUK_READ_IMAGES", "1") != "0"

# ─────────────────────────────────────────────────────────────
# HTTP politeness (HANDOFF 11.5)
# ─────────────────────────────────────────────────────────────

# Minimum seconds between requests. Never issue requests in parallel.
REQUEST_DELAY = float(os.environ.get("CUK_REQUEST_DELAY", "1.5"))
REQUEST_TIMEOUT = 20

# A reachable address makes the university IT team send an inquiry
# instead of silently blocking the crawler.
CONTACT_EMAIL = os.environ.get("CUK_CONTACT_EMAIL", "cnuhansa@gmail.com")

HEADERS = {
    "User-Agent": (
        f"CUK-Personal-Notice-Bot/0.1 "
        f"(personal notice reminder; contact: {CONTACT_EMAIL})"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# ─────────────────────────────────────────────────────────────
# Target boards (HANDOFF 4.2 — verified)
# ─────────────────────────────────────────────────────────────
#
# Every site runs the same CMS, so one parser covers all of them:
#   list   {url}?mode=list&articleLimit=N&article.offset=0
#   detail {url}?mode=view&articleNo=NNNNNN
#
# `group` marks the site family. If A4 fails and markup diverges,
# parser selection branches on this field rather than on `id`.

BOARDS = [
    # Main campus — general/academic/scholarship/career are mixed into one
    # board as categories. Do NOT split by srCategoryId (duplicate collection).
    {
        "id": "main_notice",
        "name": "본교 공지사항(전체)",
        "url": "https://www.catholic.ac.kr/ko/campuslife/notice.do",
        "group": "main",
    },
    {
        "id": "main_event",
        "name": "본교 행사안내",
        "url": "https://www.catholic.ac.kr/ko/campuslife/notice_event.do",
        "group": "main",
    },
    # Dormitory — never mirrored on the main notice board, and split
    # across four boards. Admission calls appear only on the "입퇴사" ones.
    {
        "id": "dorm_ka_general",
        "name": "기숙사 일반공지(K관/A관)",
        "url": "https://dorm.catholic.ac.kr/dormitory/board/comm_notice.do",
        "group": "dorm",
    },
    {
        "id": "dorm_ka_inout",
        "name": "기숙사 입퇴사공지(K관/A관)",  # admission calls land here
        "url": "https://dorm.catholic.ac.kr/dormitory/board/checkin-out_notice1.do",
        "group": "dorm",
    },
    {
        "id": "dorm_f_general",
        "name": "기숙사 일반공지(프란치스코관)",
        "url": "https://dorm.catholic.ac.kr/dormitory/board/comm_notice3.do",
        "group": "dorm",
    },
    {
        "id": "dorm_f_inout",
        "name": "기숙사 입퇴사공지(프란치스코관)",  # admission calls land here
        "url": "https://dorm.catholic.ac.kr/dormitory/board/checkin-out_notice.do",
        "group": "dorm",
    },
    # Department — scholarship and report deadlines appear nowhere else.
    {
        "id": "french_notice",
        "name": "프랑스어문화학과 공지",
        "url": "https://french.catholic.ac.kr/french/community/notice.do",
        "group": "dept",
    },
    {
        "id": "french_recruit",
        "name": "프랑스어문화학과 채용정보",
        "url": "https://french.catholic.ac.kr/french/community/recruitment.do",
        "group": "dept",
    },
]

BOARDS_BY_ID = {b["id"]: b for b in BOARDS}
