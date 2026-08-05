"""Free-tier request budgeting.

Two layers, and only one of them is authoritative:

  local counter  a per-day, per-model pre-check so the bot does not hammer an
                 API it already knows is spent. It starts from a configured
                 guess and is corrected by whatever the API reports.
  429 response   the real boundary, and the source of truth for the limit
                 itself: the quotaValue in a RESOURCE_EXHAUSTED reply is
                 stored so the guess stops mattering after the first
                 exhaustion.

Running out is not an error state. It flips the bot from judging notices to
forwarding them unjudged, because a missed application deadline costs more
than an extra notification.
"""

import re
from datetime import date, datetime, timedelta

MINUTE = timedelta(minutes=1)


class QuotaExhausted(RuntimeError):
    """Raised instead of calling the API when the allowance is gone.

    `scope` says whether the allowance returns at midnight ('day') or in
    seconds ('minute'), which decides whether moving to another model is
    worth trying before giving up on a verdict.
    """

    def __init__(self, reason: str, scope: str = "day"):
        super().__init__(reason)
        self.reason = reason
        self.scope = scope


class RequestBudget:
    """Per-day and per-minute request budget backed by the notices database.

    The configured daily limit is only a starting guess. Once the API reports
    its real allowance in a 429 the number is remembered per model, so the
    guard stops being a guess after the first exhaustion.
    """

    def __init__(self, con, daily_limit: int, minute_limit: int,
                 model: str = None):
        self.con = con
        self.configured_limit = daily_limit
        self.minute_limit = minute_limit
        self.model = model
        self._recent = []

    @property
    def daily_limit(self) -> int:
        observed = self.observed_limit()
        return min(self.configured_limit, observed) if observed else \
            self.configured_limit

    def observed_limit(self):
        """The allowance the API last reported for this model, if any."""
        if not self.model:
            return None
        row = self.con.execute("SELECT value FROM meta WHERE key=?",
                               (f"daily_limit:{self.model}",)).fetchone()
        try:
            return int(row["value"]) if row else None
        except (TypeError, ValueError):
            return None

    def remember_limit(self, value) -> None:
        if not self.model or value in (None, ""):
            return
        self.con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
            (f"daily_limit:{self.model}", str(value)))
        self.con.commit()

    # ── state ────────────────────────────────────────────────
    def _today(self) -> str:
        return date.today().isoformat()

    def _key(self):
        return (self._today(), self.model or "")

    def used_today(self) -> int:
        row = self.con.execute(
            "SELECT used FROM api_usage WHERE day=? AND model=?", self._key()
        ).fetchone()
        return row["used"] if row else 0

    def is_blocked(self) -> bool:
        """True once the API itself has reported this model's day spent."""
        row = self.con.execute(
            "SELECT blocked FROM api_usage WHERE day=? AND model=?", self._key()
        ).fetchone()
        return bool(row and row["blocked"])

    def remaining(self) -> int:
        if self.is_blocked():
            return 0
        return max(0, self.daily_limit - self.used_today())

    # ── accounting ───────────────────────────────────────────
    def _touch(self, used_delta: int = 0, blocked: bool = None,
               tokens: tuple = None) -> None:
        key = self._key()
        self.con.execute(
            "INSERT OR IGNORE INTO api_usage (day, model, used, blocked) "
            "VALUES (?,?,0,0)", key)
        if used_delta:
            self.con.execute(
                "UPDATE api_usage SET used = used + ? WHERE day=? AND model=?",
                (used_delta, *key))
        if tokens:
            self.con.execute(
                "UPDATE api_usage SET tok_in = tok_in + ?, "
                "tok_out = tok_out + ? WHERE day=? AND model=?",
                (tokens[0], tokens[1], *key))
        if blocked is not None:
            self.con.execute(
                "UPDATE api_usage SET blocked=? WHERE day=? AND model=?",
                (int(blocked), *key))
        self.con.commit()

    def check(self) -> None:
        """Raise QuotaExhausted if a request must not be made right now."""
        if self.is_blocked():
            raise QuotaExhausted("API가 일일 무료 한도 소진을 통보함", scope="day")
        used = self.used_today()
        if used >= self.daily_limit:
            raise QuotaExhausted(
                f"일일 요청 한도 도달 ({used}/{self.daily_limit})", scope="day")

    def consume(self, tokens: tuple = None) -> None:
        self._touch(used_delta=1, tokens=tokens)
        self._recent.append(datetime.now())

    def tokens_today(self) -> tuple:
        row = self.con.execute(
            "SELECT tok_in, tok_out FROM api_usage WHERE day=? AND model=?",
            self._key()).fetchone()
        return (row["tok_in"], row["tok_out"]) if row else (0, 0)

    def mark_blocked(self) -> None:
        """Record that the API refused for quota reasons."""
        self._touch(blocked=True)

    def unblock(self) -> None:
        """Clear today's block so judging can resume.

        The flag is only meant to be set by a genuine daily exhaustion, but a
        misread 429 would otherwise silence judgment until midnight with no
        way back.
        """
        self._touch(blocked=False)

    def seconds_until_slot(self) -> float:
        """How long to wait so the per-minute rate is respected."""
        now = datetime.now()
        self._recent = [t for t in self._recent if now - t < MINUTE]
        if len(self._recent) < self.minute_limit:
            return 0.0
        return max(0.0, (MINUTE - (now - self._recent[0])).total_seconds())


def is_quota_error(exc: Exception) -> bool:
    """Whether an SDK exception means 'out of allowance' rather than 'broken'.

    Checked by status code first; the message is a fallback because the
    wording differs between the Developer API and Vertex.
    """
    if getattr(exc, "code", None) == 429:
        return True
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or "429" in text


# A 429 that clears in seconds and one that lasts until midnight need opposite
# responses: wait, versus stop judging for the day. Image-heavy requests trip
# the per-minute token allowance easily, so treating every 429 as "the day is
# over" would abandon judgment on the first busy minute.
SHORT_RETRY_CEILING = 180  # seconds; beyond this, waiting is not viable


def classify_quota_error(exc: Exception):
    """Return (scope, retry_seconds, limit) with scope in minute/day/unknown.

    The violated quotaId is authoritative. retryDelay is NOT: a spent daily
    allowance comes back with a short delay too — gemini-2.5-flash returned
    "retry in 38s" alongside quotaId GenerateRequestsPerDayPerProjectPerModel
    with quotaValue 20 (measured 2026-08-05). Trusting the delay made the bot
    wait and retry against a quota that would not return until midnight,
    instead of failing open and notifying.

    When both a daily and a per-minute quota are reported, daily wins: the
    cost of waiting out a limit that will not lift is silence.
    """
    details = getattr(exc, "details", None) or {}
    if not isinstance(details, dict):
        details = {}

    ids, retry, limit = [], None, None
    for entry in (details.get("error", {}).get("details") or []):
        for violation in (entry.get("violations") or []):
            ids.append(str(violation.get("quotaId") or ""))
            limit = limit or violation.get("quotaValue")
        if str(entry.get("@type", "")).endswith("RetryInfo"):
            match = re.match(r"(\d+)", str(entry.get("retryDelay") or ""))
            if match:
                retry = int(match.group(1))

    joined = " ".join(ids)
    if "PerDay" in joined:
        return "day", retry, limit
    if "PerMinute" in joined:
        return "minute", retry or 60, limit
    if retry is not None and retry <= SHORT_RETRY_CEILING:
        return "minute", retry, limit
    return "unknown", retry, limit
