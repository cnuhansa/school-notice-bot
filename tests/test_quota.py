"""Free-tier budgeting and the fail-open path.

Running out of allowance must never turn into a silently dropped notice —
that is the exact failure this project exists to prevent.
"""

import os
import unittest
from unittest import mock

from support import days_out

from cuk_bot import cli, db, extractor, judge, notifier, quota  # noqa: E402


def _quota_error(retry=None, quota_ids=(), limit=None):
    """A stand-in for the SDK's ClientError, shaped like a real 429 body."""
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": q, "quotaValue": limit}
                               for q in quota_ids]}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry})

    exc = Exception("429 RESOURCE_EXHAUSTED")
    exc.code = 429
    exc.details = {"error": {"code": 429, "details": details}}
    return exc


class FreeTierBudget(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")
        self.budget = quota.RequestBudget(self.con, daily_limit=3,
                                          minute_limit=2)

    def test_requests_are_counted_and_persisted(self):
        self.budget.consume()
        self.budget.consume()
        self.assertEqual(self.budget.used_today(), 2)
        self.assertEqual(self.budget.remaining(), 1)
        # A fresh object reads the same day's tally back out of the database.
        self.assertEqual(
            quota.RequestBudget(self.con, 3, 2).used_today(), 2)

    def test_daily_limit_stops_further_requests(self):
        for _ in range(3):
            self.budget.consume()
        with self.assertRaises(quota.QuotaExhausted):
            self.budget.check()

    def test_api_reported_exhaustion_blocks_immediately(self):
        self.budget.mark_blocked()
        self.assertEqual(self.budget.remaining(), 0)
        with self.assertRaises(quota.QuotaExhausted):
            self.budget.check()

    def test_check_passes_while_allowance_remains(self):
        self.budget.consume()
        self.budget.check()  # must not raise

    def test_minute_window_paces_requests(self):
        self.assertEqual(self.budget.seconds_until_slot(), 0.0)
        self.budget.consume()
        self.budget.consume()
        self.assertGreater(self.budget.seconds_until_slot(), 0)

    def test_short_retry_does_not_disguise_a_spent_day(self):
        """The exact shape gemini-2.5-flash returned on 2026-08-05.

        A daily quota came back with "retry in 38s". Believing the delay made
        the bot wait for an allowance that only returns at midnight.
        """
        exc = _quota_error(retry="38s", limit="20", quota_ids=[
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"])
        scope, retry, limit = quota.classify_quota_error(exc)
        self.assertEqual(scope, "day")
        self.assertEqual(limit, "20")

    def test_per_minute_violation_is_waited_out(self):
        exc = _quota_error(retry="23s", quota_ids=[
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"])
        scope, retry, _ = quota.classify_quota_error(exc)
        self.assertEqual(scope, "minute")
        self.assertEqual(retry, 23)

    def test_daily_wins_when_both_quotas_are_reported(self):
        exc = _quota_error(retry="23s", quota_ids=[
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"])
        self.assertEqual(quota.classify_quota_error(exc)[0], "day")

    def test_unrecognised_shape_is_not_assumed_to_be_daily(self):
        class Bare(Exception):
            pass

        self.assertEqual(quota.classify_quota_error(Bare("429"))[0], "unknown")

    def test_quota_errors_are_told_apart_from_real_failures(self):
        class Err(Exception):
            pass

        exhausted = Err("429 RESOURCE_EXHAUSTED: quota")
        broken = Err("500 INTERNAL: backend error")
        self.assertTrue(quota.is_quota_error(exhausted))
        self.assertFalse(quota.is_quota_error(broken))

        coded = Err("something")
        coded.code = 429
        self.assertTrue(quota.is_quota_error(coded))


class ThrottleHandling(unittest.TestCase):
    """A per-minute 429 must cost a pause, not the rest of the day."""

    def setUp(self):
        self.con = db.connect(":memory:")
        self.budget = quota.RequestBudget(self.con, 180, 5)
        self.item = {"board_id": "b", "article_no": "1", "title": "모집 공고",
                     "board_name": "게시판", "posted_at": "2026-07-31"}
        self.resolved = {"source": "text", "text": "본문", "images": [],
                         "notes": []}
        self.good = mock.Mock(text='{"is_actionable": true, "category": "기숙사",'
                                   ' "one_line": "신청", "confidence": 0.8}')

    def _client(self, *side_effect):
        client = mock.Mock()
        client.models.generate_content.side_effect = list(side_effect)
        return client

    def test_rate_limit_is_waited_out_and_the_verdict_survives(self):
        throttle = _quota_error(retry="23s", quota_ids=[
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"])
        client = self._client(throttle, self.good)

        with mock.patch.object(extractor.time, "sleep") as slept:
            data = extractor.extract(self.item, self.resolved, client=client,
                                     budget=self.budget)

        self.assertTrue(data["is_actionable"])
        slept.assert_called_once_with(23)
        self.assertFalse(self.budget.is_blocked(),
                         "분당 스로틀이 하루 전체를 차단하면 안 됨")

    def test_repeated_rate_limit_gives_up_without_blocking_the_day(self):
        throttle = _quota_error(retry="23s", quota_ids=[
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"])
        client = self._client(*[throttle] * (extractor.MAX_THROTTLE_RETRIES + 1))

        with mock.patch.object(extractor.time, "sleep") as slept:
            with self.assertRaises(quota.QuotaExhausted):
                extractor.extract(self.item, self.resolved, client=client,
                                  budget=self.budget)

        self.assertEqual(slept.call_count, extractor.MAX_THROTTLE_RETRIES)
        self.assertFalse(self.budget.is_blocked())

    def test_daily_exhaustion_blocks_the_day_immediately(self):
        spent = _quota_error(quota_ids=[
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"])
        client = self._client(spent)

        with self.assertRaises(quota.QuotaExhausted):
            extractor.extract(self.item, self.resolved, client=client,
                              budget=self.budget)

        self.assertTrue(self.budget.is_blocked())
        self.assertEqual(client.models.generate_content.call_count, 1)


class FailOpenOnExhaustion(unittest.TestCase):
    """Running out of allowance must notify, never silently skip."""

    def setUp(self):
        self.con = db.connect(":memory:")
        self.item = {"board_id": "b", "article_no": "1", "board_name": "게시판",
                     "title": "2026-2학기 기숙사 입사 신청 안내", "url": "http://x",
                     "posted_at": "2026-07-31", "body": "가" * 400,
                     "images": [], "attachments": []}

    def test_exhausted_notice_is_forwarded_not_dropped(self):
        session = judge.Judge(self.con, models=["m1"])
        with mock.patch.object(judge, "extract",
                               side_effect=quota.QuotaExhausted("한도 소진")):
            cli._extract_and_route(self.con, self.item, session,
                                   notify=False, log=lambda _: None)

        self.assertEqual(len(session.unjudged), 1)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM unjudged").fetchone()[0], 1)

    def test_unjudged_notice_writes_no_extraction_so_reextract_finds_it(self):
        db.save_notice(self.con, "b", self.item)
        session = judge.Judge(self.con, models=["m1"])
        with mock.patch.object(judge, "extract",
                               side_effect=quota.QuotaExhausted("한도 소진")):
            cli._extract_and_route(self.con, self.item, session,
                                   notify=False, log=lambda _: None)
        self.con.commit()

        pending = db.pending_extraction(self.con)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["article_no"], "1")

    def test_later_successful_extraction_clears_the_unjudged_flag(self):
        session = judge.Judge(self.con, models=["m1"])
        with mock.patch.object(judge, "extract",
                               side_effect=quota.QuotaExhausted("한도 소진")):
            cli._extract_and_route(self.con, self.item, session,
                                   notify=False, log=lambda _: None)

        graded = {"is_actionable": True, "confidence": 0.9, "one_line": "신청",
                  "apply_end": days_out(10), "source": "text"}
        later = judge.Judge(self.con, models=["m1"])
        later._client = mock.Mock()  # stand in for a working api client
        with mock.patch.object(judge, "extract", return_value=graded):
            cli._extract_and_route(self.con, self.item, later,
                                   notify=False, log=lambda _: None)
        self.con.commit()

        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM unjudged").fetchone()[0], 0)

    def test_alert_lists_every_unjudged_notice(self):
        items = [dict(self.item, article_no=str(i), title=f"공고 {i}")
                 for i in range(3)]
        text = notifier.format_unjudged(items, "Gemini 무료 한도 소진")
        self.assertIn("판정 없이 전달", text)
        self.assertIn("한도 소진", text)
        for item in items:
            self.assertIn(item["title"], text)

    def test_api_outage_also_forwards_rather_than_dropping(self):
        session = judge.Judge(self.con, models=["m1"])
        with mock.patch.object(judge, "extract",
                               side_effect=RuntimeError("backend error")):
            cli._extract_and_route(self.con, self.item, session,
                                   notify=False, log=lambda _: None)

        self.assertEqual(len(session.unjudged), 1)
        self.assertIn("추출 실패", session.reason)

    def test_missing_api_key_forwards_instead_of_swallowing(self):
        """A key that was never set is still a 'cannot judge' situation.

        The client is built lazily inside the guarded block; if it were built
        while evaluating the call arguments the error would escape as a plain
        failure and the notice would vanish.
        """
        session = judge.Judge(self.con, models=["m1"])
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "",
                                          "GOOGLE_API_KEY": ""}, clear=False):
            cli._extract_and_route(self.con, self.item, session,
                                   notify=False, log=lambda _: None)

        self.assertEqual(len(session.unjudged), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
