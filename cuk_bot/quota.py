"""Free-tier request budgeting.

Two layers, and only one of them is authoritative:

  local counter  a conservative pre-check so the bot does not hammer an API
                 it already knows is spent. Limits are configured, not
                 discovered, so they may be wrong in either direction.
  429 response   the real boundary. Whatever the counter believes, a
                 RESOURCE_EXHAUSTED reply means the allowance is gone and the
                 day is marked spent.

Running out is not an error state. It flips the bot from judging notices to
forwarding them unjudged, because a missed application deadline costs more
than an extra notification.
"""

from datetime import date, datetime, timedelta

MINUTE = timedelta(minutes=1)


class QuotaExhausted(RuntimeError):
    """Raised instead of calling the API when the allowance is gone."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RequestBudget:
    """Per-day and per-minute request budget backed by the notices database."""

    def __init__(self, con, daily_limit: int, minute_limit: int):
        self.con = con
        self.daily_limit = daily_limit
        self.minute_limit = minute_limit
        self._recent = []

    # ── state ────────────────────────────────────────────────
    def _today(self) -> str:
        return date.today().isoformat()

    def used_today(self) -> int:
        row = self.con.execute(
            "SELECT used, blocked FROM api_usage WHERE day=?", (self._today(),)
        ).fetchone()
        return row["used"] if row else 0

    def is_blocked(self) -> bool:
        """True once the API itself has reported the day's allowance spent."""
        row = self.con.execute(
            "SELECT blocked FROM api_usage WHERE day=?", (self._today(),)
        ).fetchone()
        return bool(row and row["blocked"])

    def remaining(self) -> int:
        if self.is_blocked():
            return 0
        return max(0, self.daily_limit - self.used_today())

    # ── accounting ───────────────────────────────────────────
    def _touch(self, used_delta: int = 0, blocked: bool = None) -> None:
        day = self._today()
        self.con.execute(
            "INSERT OR IGNORE INTO api_usage (day, used, blocked) VALUES (?,0,0)",
            (day,))
        if used_delta:
            self.con.execute(
                "UPDATE api_usage SET used = used + ? WHERE day=?",
                (used_delta, day))
        if blocked is not None:
            self.con.execute("UPDATE api_usage SET blocked=? WHERE day=?",
                             (int(blocked), day))
        self.con.commit()

    def check(self) -> None:
        """Raise QuotaExhausted if a request must not be made right now."""
        if self.is_blocked():
            raise QuotaExhausted("API가 일일 무료 한도 소진을 통보함")
        used = self.used_today()
        if used >= self.daily_limit:
            raise QuotaExhausted(
                f"일일 요청 한도 도달 ({used}/{self.daily_limit})")

    def consume(self) -> None:
        self._touch(used_delta=1)
        self._recent.append(datetime.now())

    def mark_blocked(self) -> None:
        """Record that the API refused for quota reasons."""
        self._touch(blocked=True)

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
