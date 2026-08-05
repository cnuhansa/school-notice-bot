"""Judging a notice across a chain of models, each with its own free tier.

The free-tier allowance is granted per model: gemini-2.5-flash and
gemini-2.5-flash-lite each reported 20 requests/day on 2026-08-05. That is
below the 20-40 notices this bot sees on a busy weekday, so a single model
would spend its day early and forward the rest unjudged.

Because the quotas are independent, falling through the chain multiplies the
daily allowance without leaving the free tier. Only when every model is spent
does the bot stop judging — and even then it still notifies.
"""

from . import db
from .config import (DAILY_REQUEST_LIMIT, MINUTE_REQUEST_LIMIT, MODEL_CHAIN)
from .extractor import ModelUnavailable, extract, make_client
from .quota import QuotaExhausted, RequestBudget


class Judge:
    """Owns the API client, one budget per model, and the unjudged pile."""

    def __init__(self, con, models=None):
        self.con = con
        self.models = list(models or MODEL_CHAIN)
        self.budgets = {
            name: RequestBudget(con, DAILY_REQUEST_LIMIT,
                                MINUTE_REQUEST_LIMIT, model=name)
            for name in self.models
        }
        self.unjudged = []
        self.reason = None
        self._client = None
        self._exhausted = set()
        self.retired = set()

    # ── resources ────────────────────────────────────────────
    @property
    def client(self):
        """Built lazily so a run with no allowance never needs a key."""
        if self._client is None:
            self._client = make_client()
        return self._client

    def available(self) -> list:
        return [name for name in self.models
                if name not in self._exhausted
                and self.budgets[name].remaining() > 0]

    def summary(self) -> str:
        parts = []
        for name in self.models:
            budget = self.budgets[name]
            parts.append(f"{name.replace('gemini-', '')}:"
                         f"{budget.remaining()}")
        return " ".join(parts)

    # ── judging ──────────────────────────────────────────────
    def judge(self, item: dict, resolved: dict) -> dict:
        """Return a verdict, moving down the chain as models run out.

        Raises QuotaExhausted only when no model in the chain has anything
        left, so the caller can forward the notice unjudged.
        """
        candidates = self.available()
        if not candidates:
            raise QuotaExhausted("모든 모델의 무료 한도 소진", scope="day")

        last = None
        for name in candidates:
            try:
                return extract(item, resolved, client=self.client,
                               budget=self.budgets[name], model=name)
            except QuotaExhausted as exc:
                # Both a spent day and a stubborn rate limit are per model,
                # so the next model in the chain is worth trying.
                self._exhausted.add(name)
                last = exc
            except ModelUnavailable as exc:
                # A retired model must not take the bot down with it: drop it
                # for this run and carry on. gemini-2.5-* retires 2026-10-16.
                self._exhausted.add(name)
                self.retired.add(name)
                last = exc

        if isinstance(last, QuotaExhausted):
            raise last
        raise QuotaExhausted(
            f"사용 가능한 모델 없음 ({', '.join(sorted(self.retired)) or '원인 미상'})",
            scope="day")

    def forward_unjudged(self, item: dict, reason: str) -> None:
        """Give up on judging this notice, but never on surfacing it.

        Committed immediately: this row is the record that a notice still
        needs a verdict, and losing it to an uncommitted transaction would
        drop the notice out of --reextract's queue entirely.
        """
        self.reason = self.reason or reason
        db.mark_unjudged(self.con, item["board_id"], item["article_no"], reason)
        self.con.commit()
        self.unjudged.append(item)
