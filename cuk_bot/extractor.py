"""Turn a notice into {is_actionable, apply_end, ...} via Claude.

Rule-based pre-filtering is deliberately absent (HANDOFF 11.2): a title like
"2026학년도 1학기 생활관 입사 안내" carries no 신청/모집 keyword, and skipping
it is exactly the failure this project exists to prevent. Every new notice is
sent to the model.
"""

import json
import re
from datetime import date

from .config import MODEL

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


def _content_blocks(prompt: str, images: list) -> list:
    blocks = []
    for img in images:
        blocks.append({
            "type": "image",
            "source": {"type": "base64",
                       "media_type": img["media_type"],
                       "data": img["data"]},
        })
    blocks.append({"type": "text", "text": prompt})
    return blocks


def _parse_json(raw: str) -> dict:
    """Strip code fences the model sometimes adds, then parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def extract(item: dict, resolved: dict, client=None, model: str = None) -> dict:
    """Call Claude once, retrying a single time if the JSON comes back broken.

    A second failure returns None rather than raising, so one malformed
    response cannot stall a whole crawl run.
    """
    if client is None:
        from anthropic import Anthropic
        client = Anthropic()

    prompt = build_prompt(item, resolved)
    blocks = _content_blocks(prompt, resolved.get("images") or [])

    last_error = None
    for attempt in (1, 2):
        try:
            resp = client.messages.create(
                model=model or MODEL,
                max_tokens=800,
                system=SYSTEM,
                messages=[{"role": "user", "content": blocks}],
            )
            raw = "".join(b.text for b in resp.content if b.type == "text")
            data = _parse_json(raw)
        except json.JSONDecodeError as exc:
            last_error = f"json decode failed (attempt {attempt}): {exc}"
            continue
        except Exception as exc:
            last_error = f"api call failed (attempt {attempt}): {exc}"
            break

        return normalize(data, resolved["source"])

    raise ExtractionError(last_error)


class ExtractionError(RuntimeError):
    pass


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
