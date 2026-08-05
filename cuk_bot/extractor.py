"""Turn a notice into {is_actionable, apply_end, ...} via Gemini.

Rule-based pre-filtering is deliberately absent (HANDOFF 11.2): a title like
"2026학년도 1학기 생활관 입사 안내" carries no 신청/모집 keyword, and skipping
it is exactly the failure this project exists to prevent. Every new notice is
sent to the model — until the free allowance runs out, at which point the
caller forwards notices unjudged rather than dropping them.
"""

import json
import re
import time
from datetime import date, datetime

from .client import CredentialError, make_client  # noqa: F401
from .config import MODEL
from .prompt import (build_contents, build_prompt, make_config,
                     mark_thinking_required)
from .quota import QuotaExhausted, classify_quota_error, is_quota_error

# Longest pause worth taking for a rate limit. A --check run fires every ten
# minutes, so a wait beyond this would collide with the next run.
MAX_THROTTLE_WAIT = 90
MAX_THROTTLE_RETRIES = 2


class ExtractionError(RuntimeError):
    pass


ExtractionError.__doc__ = "A failure confined to one notice."

class ModelUnavailable(RuntimeError):
    """The model name is not served — retired, renamed, or not on this key.

    gemini-2.5-* retires 2026-10-16, so this has to be survivable: the caller
    moves to the next model in the chain instead of failing the notice.
    """


# Gemini 3.x rejects thinking_budget=0 with INVALID_ARGUMENT, while 2.5 needs
# it disabled or reasoning eats the output budget. Which family a model
# belongs to is discovered on first use rather than hardcoded, so a new model
# name does not need a code change.

def _is_invalid_argument(exc) -> bool:
    return (getattr(exc, "code", None) == 400
            and "INVALID_ARGUMENT" in str(exc).upper())


def _is_missing_model(exc) -> bool:
    text = str(exc).upper()
    return getattr(exc, "code", None) == 404 or "NOT_FOUND" in text

def _is_missing_model(exc) -> bool:
    text = str(exc).upper()
    return getattr(exc, "code", None) == 404 or "NOT_FOUND" in text


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def extract(item: dict, resolved: dict, client=None, budget=None,
            model: str = None) -> dict:
    """Call Gemini once, retrying a single time on malformed JSON.

    Raises QuotaExhausted when the free allowance is gone, and
    ModelUnavailable when the model itself is gone, so the caller can move to
    the next model. Any other failure raises ExtractionError and only affects
    this one notice.
    """
    if budget is not None:
        budget.check()
        wait = budget.seconds_until_slot()
        if wait > 0:
            time.sleep(wait)

    client = client or make_client()
    name = model or MODEL
    contents = build_contents(build_prompt(item, resolved),
                               resolved.get("images") or [])

    last_error = None
    throttles = 0
    for attempt in (1, 2, 3, 4):
        try:
            resp = client.models.generate_content(
                model=name, contents=contents, config=make_config(name))
            if budget is not None:
                budget.consume(tokens=_token_count(resp))
            return normalize(_parse_json(resp.text), resolved["source"])
        except json.JSONDecodeError as exc:
            if budget is not None:
                budget.consume()
            last_error = f"json decode failed (attempt {attempt}): {exc}"
            continue
        except Exception as exc:
            if _is_missing_model(exc):
                raise ModelUnavailable(f"{name} 사용 불가 (종료·오타·권한)")

            # Newer families require reasoning. Learn that and retry once
            # with a budget large enough that the JSON is not truncated.
            if _is_invalid_argument(exc) and mark_thinking_required(name):
                continue

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


def _token_count(resp) -> tuple:
    """(input, output) tokens for one call, for self-metering.

    Vertex bills real money and free-tier credit is not readable through any
    API, so counting what we send is the only way to answer "how much did
    that cost" with a number instead of a guess.
    """
    usage = getattr(resp, "usage_metadata", None)
    if not usage:
        return (0, 0)

    def count(field):
        try:
            return int(getattr(usage, field, 0) or 0)
        except (TypeError, ValueError):
            return 0

    # Accounting must never break a verdict: a response with an odd or
    # missing usage payload costs us a metric, not the notice.
    return (count("prompt_token_count"),
            count("candidates_token_count") + count("thoughts_token_count"))


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

    # No deadline extracted means low confidence by definition. The prompt
    # says so, but the model was observed returning 0.9 with apply_end=null
    # on a 채용 공고 — which suppressed the "마감일이 불확실합니다" warning on
    # exactly the alert that needed it.
    if not out.get("apply_end"):
        out["confidence"] = min(out["confidence"], 0.3)

    # A deadline already past cannot be acted on, so alerting about it is
    # pure noise. Enforced in code rather than in the prompt: the model was
    # observed judging two near-identical 정기퇴사 notices differently, and a
    # date comparison does not need judgement.
    if out["is_actionable"] and _is_past(out.get("apply_end")):
        out["is_actionable"] = False
        out["expired"] = True

    out["source"] = source
    return out


def _is_past(value) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(str(value)).date() < date.today()
    except (TypeError, ValueError):
        return False
