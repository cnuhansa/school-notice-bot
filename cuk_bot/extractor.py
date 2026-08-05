"""Turn a notice into {is_actionable, apply_end, ...} via Gemini.

Rule-based pre-filtering is deliberately absent (HANDOFF 11.2): a title like
"2026학년도 1학기 생활관 입사 안내" carries no 신청/모집 keyword, and skipping
it is exactly the failure this project exists to prevent. Every new notice is
sent to the model — until the free allowance runs out, at which point the
caller forwards notices unjudged rather than dropping them.
"""

import base64
import json
import re
import time
from datetime import date

from .config import MODEL
from .quota import QuotaExhausted, classify_quota_error, is_quota_error

# Longest pause worth taking for a rate limit. A --check run fires every ten
# minutes, so a wait beyond this would collide with the next run.
MAX_THROTTLE_WAIT = 90
MAX_THROTTLE_RETRIES = 2

SYSTEM = (
    "너는 대학교 공지사항에서 '학생이 기한 내에 해야 할 행동'을 뽑아내는 추출기다. "
    "설명이나 마크다운 없이 JSON 객체 하나만 출력한다."
)

SCHEMA_BLOCK = """{
  "is_actionable": true/false,
  "category": "기숙사|장학|수강|등록|취업|교환학생|행사|일반",
  "apply_start": "YYYY-MM-DD 또는 null",
  "apply_end": "YYYY-MM-DDTHH:MM 또는 YYYY-MM-DD 또는 null",
  "target": "대상자 요약 또는 null",
  "method": "신청 방법 요약 또는 null",
  "one_line": "무엇을 언제까지 해야 하는지 한 줄로",
  "confidence": 0.0~1.0
}"""

RULES = """판정 기준:
- is_actionable: 학생이 기한 내에 신청·제출·등록·납부 등 행동을 해야 하면 true.
  단순 안내, 결과 발표, 변경 통보, 시설 점검·소독·공사 안내는 false.
- 오늘 날짜는 {today}이다. "3월 14일"처럼 연도가 없으면 가장 가까운 미래로 해석한다.
- 학사연도와 마감일을 혼동하지 마라. 예: "2026학년도 2학기 기숙사 모집"의 신청 마감이
  2026-07-25라면 apply_end는 2026-07-25다. 2026학년도라는 표현에 끌려 연말 날짜를
  적으면 안 된다.
- confidence는 마감일 추출에 대한 확신도다. 마감일이 어디에도 없으면 apply_end는
  null, confidence는 0.3 이하로 준다.
- 본문이 비어 있고 제목만 주어졌다면 제목만으로 판정하되 confidence는 0.4를 넘기지
  마라. 모집·신청 공고로 보이면 is_actionable은 true로 둔다. 놓치는 것이 잘못된
  알림보다 나쁘다."""

IMAGE_HINT = """이 공지는 본문이 이미지로 게시되어 있다. 첨부된 이미지를 읽고
신청 기간과 대상을 찾아라. 이미지에서 날짜를 읽을 수 없으면 apply_end는 null이다."""


class ExtractionError(RuntimeError):
    pass


# Constraining the reply removes the two ways the model used to break the
# parser: prose around the JSON, and a field name it invented.
def _response_schema():
    from google.genai import types

    text = types.Schema(type="STRING", nullable=True)
    return types.Schema(
        type="OBJECT",
        properties={
            "is_actionable": types.Schema(type="BOOLEAN"),
            "category": types.Schema(type="STRING"),
            "apply_start": text,
            "apply_end": text,
            "target": text,
            "method": text,
            "one_line": types.Schema(type="STRING"),
            "confidence": types.Schema(type="NUMBER"),
        },
        required=["is_actionable", "category", "one_line", "confidence"],
    )


def build_prompt(item: dict, resolved: dict) -> str:
    parts = [
        "다음 대학교 공지사항을 아래 JSON 스키마로만 정리하라.",
        "",
        SCHEMA_BLOCK,
        "",
        RULES.format(today=date.today().isoformat()),
    ]
    if resolved["source"] == "image":
        parts += ["", IMAGE_HINT]

    parts += [
        "",
        f"[게시판] {item.get('board_name') or item.get('board_id')}",
        f"[제목] {item['title']}",
        f"[등록일] {item.get('posted_at') or '미상'}",
        "[본문]",
        resolved["text"].strip() or "(본문 텍스트 없음)",
    ]
    return "\n".join(parts)


def _build_contents(prompt: str, images: list) -> list:
    """Images first, then the instructions — Gemini follows the trailing text."""
    from google.genai import types

    contents = []
    for img in images:
        contents.append(types.Part.from_bytes(
            data=base64.b64decode(img["data"]), mime_type=img["media_type"]))
    contents.append(prompt)
    return contents


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def make_client(api_key: str = None):
    """Build a Gemini Developer API client.

    The key must be an AI Studio key: that is what carries the free tier. A
    service-account credential would route to Vertex AI, which bills.
    """
    import os

    from google import genai

    key = api_key or os.environ.get("GEMINI_API_KEY") or \
        os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ExtractionError(
            "GEMINI_API_KEY 미설정 — aistudio.google.com 에서 발급하세요")
    return genai.Client(api_key=key)


def extract(item: dict, resolved: dict, client=None, budget=None,
            model: str = None) -> dict:
    """Call Gemini once, retrying a single time on malformed JSON.

    Raises QuotaExhausted when the free allowance is gone so the caller can
    fall back to notifying without a verdict. Any other failure raises
    ExtractionError and only affects this one notice.
    """
    from google.genai import types

    if budget is not None:
        budget.check()
        wait = budget.seconds_until_slot()
        if wait > 0:
            time.sleep(wait)

    client = client or make_client()
    contents = _build_contents(build_prompt(item, resolved),
                               resolved.get("images") or [])
    # gemini-2.5-flash reasons before answering, and those tokens come out of
    # the same output budget. At 800 tokens the model spent 764 thinking and
    # returned a JSON object cut off mid-string, so thinking is disabled and
    # the budget raised: this is extraction, not a problem that needs a
    # scratchpad.
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=_response_schema(),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        temperature=0.0,
        max_output_tokens=1024,
    )

    last_error = None
    throttles = 0
    for attempt in (1, 2, 3, 4):
        try:
            resp = client.models.generate_content(
                model=model or MODEL, contents=contents, config=config)
            if budget is not None:
                budget.consume()
            return normalize(_parse_json(resp.text), resolved["source"])
        except json.JSONDecodeError as exc:
            if budget is not None:
                budget.consume()
            last_error = f"json decode failed (attempt {attempt}): {exc}"
            continue
        except Exception as exc:
            if not is_quota_error(exc):
                last_error = f"api call failed (attempt {attempt}): {exc}"
                break

            scope, retry_after, limit = classify_quota_error(exc)
            if scope == "day":
                if budget is not None:
                    budget.remember_limit(limit)
                    budget.mark_blocked()
                cap = f" (하루 {limit}건)" if limit else ""
                raise QuotaExhausted(
                    f"{model or MODEL} 일일 무료 한도 소진{cap}", scope="day")

            # A rate limit clears on its own. Waiting keeps the verdict;
            # giving up here would downgrade a judgeable notice to unjudged.
            if throttles >= MAX_THROTTLE_RETRIES:
                raise QuotaExhausted(
                    f"{model or MODEL} 분당 한도 반복 초과", scope="minute")
            throttles += 1
            time.sleep(min(retry_after or 60, MAX_THROTTLE_WAIT))
            continue

    raise ExtractionError(last_error)


def normalize(data: dict, source: str) -> dict:
    """Coerce the model's output into the shape the rest of the code assumes."""
    out = dict(data)
    out["is_actionable"] = bool(data.get("is_actionable"))

    try:
        out["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    for key in ("apply_start", "apply_end", "target", "method"):
        if out.get(key) in ("null", "", "미상", "불명확"):
            out[key] = None

    # A title-only read can never justify a confident deadline, whatever the
    # model claims.
    if source == "title":
        out["confidence"] = min(out["confidence"], 0.4)

    out["source"] = source
    return out
