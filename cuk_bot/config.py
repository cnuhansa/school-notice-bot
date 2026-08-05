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

# gemini-2.5-* is retired from this chain by client decision (2026-08-05) —
# it goes end-of-life 2026-10-16 and there is no reason to build a habit on
# it. Every model below was checked against the F관 모집 공고 screenshot and
# read 신청 기간 2026-08-03~08-04 17:00, 대상 and 접수 이메일 correctly.
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
    "gemini-3.6-flash,gemini-flash-lite-latest").split(",") if m.strip()]
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

# USD per 1M tokens, (input, output). Used only to turn our own token counts
# into a rough figure — remaining free credit is not readable through any API
# (see docs/OPERATIONS.md), so self-metering is the only number we can get.
#
# Hand-maintained and therefore approximate: treat the output as an order of
# magnitude, never as a bill. Unlisted models fall back to DEFAULT_PRICE.
MODEL_PRICES = {
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
}
DEFAULT_PRICE = (0.30, 2.50)

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

# HANDOFF 11.5 asks for a reachable address so the university IT team sends an
# inquiry instead of silently blocking the crawler. The client chose to run
# without one (2026-08-05), accepting that trade. Left configurable so it can
# be added later — never hardcoded, since this repository is public and a
# committed address is a scrapable one.
CONTACT_EMAIL = os.environ.get("CUK_CONTACT_EMAIL", "")

_CONTACT = f"; contact: {CONTACT_EMAIL}" if CONTACT_EMAIL else ""

HEADERS = {
    "User-Agent": (
        f"CUK-Personal-Notice-Bot/0.1 (personal notice reminder{_CONTACT})"
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
    # Added 2026-08-05. The handoff guessed these URLs and every guess was
    # wrong — all four 404'd or returned nothing. These are the paths the
    # sites actually serve, each verified for 10+ listed items, dated rows
    # and a matching body selector before being added.
    {
        "id": "career",
        "name": "취·창업처 정보게시판",
        "url": "https://career.catholic.ac.kr/career/board/news.do",
        "group": "career",
    },
    {
        # OIA runs five boards. The other four carry 외국인 입학 and inbound
        # exchange material; only this one carries 파견 — 교환학생 오리엔테이션
        # and overseas scholarship calls, which is what a Korean undergraduate
        # actually needs.
        "id": "oia_exchange",
        "name": "국제교류처 교환학생·파견",
        "url": "https://oia.catholic.ac.kr/oia/program/international-notice.do",
        "group": "oia",
    },
    {
        "id": "college",
        "name": "학부대학 알림마당",
        "url": "https://catholic-college.catholic.ac.kr/catholic_college/notification/notice.do",
        "group": "college",
    },
    # 학과 소식(french/community/news.do) is deliberately absent: the board
    # exists but is empty ("등록된 글이 없습니다"), and an empty listing is
    # treated as a parse failure, so adding it would raise a standing false
    # alarm. Add it once it carries posts.
]

BOARDS_BY_ID = {b["id"]: b for b in BOARDS}
