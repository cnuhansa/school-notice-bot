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
from .client import CredentialError, available_credentials, build, label
from .extractor import CredentialRejected, ModelBusy, ModelUnavailable, extract
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
        self._credentials = available_credentials()
        self.dropped = []
        self._exhausted = set()
        self.retired = set()
        self.busy = set()

    # ── resources ────────────────────────────────────────────
    @property
    def client(self):
        """Current credential's client, built lazily.

        Lazy so a run with no allowance left never needs a credential at all.
        """
        if self._client is not None:
            return self._client

        while self._credentials:
            kind = self._credentials[0]
            try:
                self._client = build(kind)
                return self._client
            except Exception as exc:
                self._drop_credential(f"{kind} 사용 불가: {str(exc)[:70]}")
        tried = f" ({'; '.join(self.dropped)})" if self.dropped else ""
        raise CredentialError(f"사용 가능한 자격증명 없음{tried}")

    def _drop_credential(self, reason: str) -> None:
        """Move to the next credential. A dead one must not stall the run.

        Vertex failing has to hand over to the free tier rather than turn
        every notice unjudged — otherwise a billing lapse silently switches
        the bot from filtering to forwarding everything.
        """
        if self._credentials:
            self.dropped.append(reason)
            self._credentials.pop(0)
        self._client = None

    def available(self) -> list:
        return [name for name in self.models
                if name not in self._exhausted
                and self.budgets[name].remaining() > 0]

    def credential(self) -> str:
        return label(self._credentials[0]) if self._credentials else "없음"

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
            raise QuotaExhausted(self._none_left(), scope="day")

        last = None
        for name in candidates:
            try:
                return self._extract(item, resolved, name)
            except CredentialError as exc:
                # Building the client failed outright — nothing to retry here.
                raise QuotaExhausted(f"자격증명 없음: {exc}", scope="day")
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
            except ModelBusy as exc:
                # A demand spike is per model and passes on its own; skip
                # the model for this run, the next run tries it again.
                self._exhausted.add(name)
                self.busy.add(name)
                last = exc

        if isinstance(last, QuotaExhausted):
            raise last
        raise QuotaExhausted(self._none_left(), scope="day")

    def _none_left(self) -> str:
        """Why no model can judge, worded for the channel.

        Saying "한도 소진" when every model was merely overloaded sends the
        reader to check quotas that are fine.
        """
        if self.busy:
            return "모든 모델 일시 과부하 — 다음 실행에서 다시 시도"
        if self.retired:
            return f"사용 가능한 모델 없음 ({', '.join(sorted(self.retired))})"
        return "모든 모델의 무료 한도 소진"

    def _extract(self, item: dict, resolved: dict, name: str) -> dict:
        """One model, moving down the credentials while they are refused.

        A 403 at call time is the same failure as one at build time — billing
        lapsed, key revoked — and must hand over the same way. From 2026-10-29
        it surfaced only as a per-notice error instead, so a refused Vertex
        project forwarded every notice unjudged while AI Studio sat unused.
        Runs out by raising CredentialError from self.client.
        """
        while True:
            kind = self._credentials[0] if self._credentials else "?"
            try:
                return extract(item, resolved, client=self.client,
                               budget=self.budgets[name], model=name)
            except CredentialRejected as exc:
                self._drop_credential(f"{kind} 거부: {exc}")

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
