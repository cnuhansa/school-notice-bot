"""Prompt text and request configuration for the extractor.

Kept apart from cuk_bot.extractor so that tuning what we ask never touches
the retry, quota and fallback logic that decides what happens when the call
goes wrong.
"""

import base64
from datetime import date

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
  알림보다 나쁘다.
- **애매하면 true로 둔다.** 신청·접수가 필요한지 본문만으로 확실하지 않은 행사·특강·
  프로그램 안내는 is_actionable을 true로 하고 confidence를 0.4 이하로 낮춰라. 그러면
  알림에 경고 표시가 붙어 사용자가 원문을 확인하게 된다. 반대로 false로 두면 그
  공지는 사용자 눈에 띄지 않고 사라진다.
  단 아래는 애매한 경우가 아니므로 false를 유지한다:
  결과·합격자 발표, 이미 종료된 행사, 시설 점검·소독·공사, 단순 변경 통보."""

IMAGE_HINT = """이 공지는 본문이 이미지로 게시되어 있다. 첨부된 이미지를 읽고
신청 기간과 대상을 찾아라. 이미지에서 날짜를 읽을 수 없으면 apply_end는 null이다."""


_THINKING_ALWAYS_ON = set()


def mark_thinking_required(model: str) -> bool:
    """Remember that a model refuses thinking_budget=0. False if already known,
    so the caller does not retry the same rejection forever."""
    if model in _THINKING_ALWAYS_ON:
        return False
    _THINKING_ALWAYS_ON.add(model)
    return True

MAX_OUTPUT_WITHOUT_THINKING = 1024
MAX_OUTPUT_WITH_THINKING = 4096  # gemini-3.6-flash spent 717 tokens thinking


def make_config(model: str):
    from google.genai import types

    kwargs = dict(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=response_schema(),
        temperature=0.0,
    )
    if model in _THINKING_ALWAYS_ON:
        kwargs["max_output_tokens"] = MAX_OUTPUT_WITH_THINKING
    else:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        kwargs["max_output_tokens"] = MAX_OUTPUT_WITHOUT_THINKING
    return types.GenerateContentConfig(**kwargs)


# Constraining the reply removes the two ways the model used to break the
# parser: prose around the JSON, and a field name it invented.
def response_schema():
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


def build_contents(prompt: str, images: list) -> list:
    """Images first, then the instructions — Gemini follows the trailing text."""
    from google.genai import types

    contents = []
    for img in images:
        contents.append(types.Part.from_bytes(
            data=base64.b64decode(img["data"]), mime_type=img["media_type"]))
    contents.append(prompt)
    return contents
