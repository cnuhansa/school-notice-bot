"""Shared configuration: target boards, HTTP politeness settings, paths.

All values that a deploy might need to change are environment-overridable.
"""

import os

# ─────────────────────────────────────────────────────────────
# Paths / models
# ─────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("CUK_DB", "cuk_notices.db")
CACHE_DIR = os.environ.get("CUK_CACHE", ".cache")
MODEL = os.environ.get("CUK_MODEL", "claude-sonnet-5")

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
# pages; the rest are forms and floor plans. Capping is logged, never silent.
MAX_IMAGES_PER_NOTICE = int(os.environ.get("CUK_MAX_IMAGES", "5"))

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
