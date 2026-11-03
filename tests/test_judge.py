"""Falling through the model chain as each free tier runs out."""

import os
import unittest
from unittest import mock

import support  # noqa: F401

from cuk_bot import client, db, judge  # noqa: E402
from cuk_bot import extractor  # noqa: E402
from cuk_bot.extractor import (CredentialRejected, ModelUnavailable,  # noqa: E402
                               brief_error)
from cuk_bot.quota import QuotaExhausted  # noqa: E402

VERDICT = {"is_actionable": True, "category": "기숙사", "one_line": "신청",
           "confidence": 0.8, "source": "text"}


def spent(model):
    return QuotaExhausted(f"{model} 일일 무료 한도 소진 (하루 20건)", scope="day")


class CredentialChain(unittest.TestCase):
    """Vertex carries the load; AI Studio catches it when Vertex cannot.

    A billing lapse or revoked service account must hand over to the free
    tier. Without the fallback the bot keeps running but stops judging, and
    forwarding everything unjudged looks like working software.
    """

    def setUp(self):
        self.con = db.connect(":memory:")

    def test_vertex_is_tried_before_studio(self):
        with mock.patch.dict(os.environ, {
                "CUK_VERTEX_CREDENTIALS": "/tmp/sa.json",
                "GEMINI_API_KEY": "AIzaTest"}, clear=True):
            self.assertEqual(client.available_credentials(),
                             [client.VERTEX, client.STUDIO])

    def test_studio_alone_when_vertex_is_unconfigured(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaTest"},
                             clear=True):
            self.assertEqual(client.available_credentials(), [client.STUDIO])

    def test_failing_vertex_hands_over_to_studio(self):
        with mock.patch.dict(os.environ, {
                "CUK_VERTEX_CREDENTIALS": "/nonexistent.json",
                "GEMINI_API_KEY": "AIzaTest"}, clear=True):
            j = judge.Judge(self.con, models=["m1"])
            stub = mock.Mock()

            def build(kind):
                if kind == client.VERTEX:
                    raise client.CredentialError("결제 중단")
                return stub

            with mock.patch.object(judge, "build", side_effect=build):
                self.assertIs(j.client, stub)

        self.assertEqual(j._credentials, [client.STUDIO])
        self.assertTrue(any("결제 중단" in d for d in j.dropped))

    def test_handover_is_reported_not_silent(self):
        with mock.patch.dict(os.environ, {
                "CUK_VERTEX_CREDENTIALS": "/nonexistent.json",
                "GEMINI_API_KEY": "AIzaTest"}, clear=True):
            j = judge.Judge(self.con, models=["m1"])
            with mock.patch.object(judge, "build",
                                   side_effect=[client.CredentialError("x"),
                                                mock.Mock()]):
                j.client
        self.assertEqual(len(j.dropped), 1,
                         "자격증명 전환이 기록되지 않으면 조용히 과금이 바뀐다")

    def test_no_credentials_at_all_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            j = judge.Judge(self.con, models=["m1"])
            with self.assertRaises(client.CredentialError):
                j.client

    def _judge_with(self, refuse):
        """Judge over both credentials; `refuse` names those whose calls 403."""
        clients = {client.VERTEX: mock.Mock(), client.STUDIO: mock.Mock()}

        def fake_extract(item, resolved, client, budget, model):
            for kind in refuse:
                if client is clients[kind]:
                    raise CredentialRejected("API 403 PERMISSION_DENIED")
            return VERDICT

        with mock.patch.dict(os.environ, {
                "CUK_VERTEX_CREDENTIALS_JSON": "{}",
                "GEMINI_API_KEY": "AIzaTest"}, clear=True):
            j = judge.Judge(self.con, models=["m1", "m2"])
        with mock.patch.object(judge, "build", side_effect=clients.get), \
                mock.patch.object(judge, "extract",
                                  side_effect=fake_extract) as called:
            try:
                result = j.judge({"title": "t"}, {"source": "text"})
            except QuotaExhausted as exc:
                result = exc
        return j, result, called

    def test_vertex_refused_at_call_time_hands_over_to_studio(self):
        """2026-10-29: Vertex returned 403 on every call. Build succeeded, so
        the old fallback never fired and 15 notices went out unjudged."""
        j, result, called = self._judge_with(refuse=[client.VERTEX])

        self.assertEqual(result["one_line"], "신청")
        self.assertEqual(j._credentials, [client.STUDIO])
        self.assertTrue(any("vertex 거부" in d for d in j.dropped))
        self.assertEqual([c.kwargs["model"] for c in called.call_args_list],
                         ["m1", "m1"], "같은 모델로 다음 자격증명에서 재시도")

    def test_every_credential_refused_still_fails_open_with_reason(self):
        j, result, _ = self._judge_with(refuse=[client.VERTEX, client.STUDIO])

        self.assertIsInstance(result, QuotaExhausted)
        self.assertIn("vertex 거부", str(result))
        self.assertIn("studio 거부", str(result))

    def test_a_built_client_is_reused_not_rebuilt(self):
        j = judge.Judge(self.con, models=["m1"])
        stub = mock.Mock()
        j._client = stub
        self.assertIs(j.client, stub)


class ModelChain(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")
        self.judge = judge.Judge(self.con, models=["first", "second", "third"])
        self.judge._client = mock.Mock()
        self.item = {"board_id": "b", "article_no": "1", "title": "모집 공고"}
        self.resolved = {"source": "text", "text": "본문", "images": []}

    def run_judge(self, side_effect):
        with mock.patch.object(judge, "extract",
                               side_effect=side_effect) as called:
            try:
                return self.judge.judge(self.item, self.resolved), called
            except QuotaExhausted as exc:
                return exc, called

    def test_first_model_is_used_while_it_has_allowance(self):
        result, called = self.run_judge([VERDICT])
        self.assertEqual(result["one_line"], "신청")
        self.assertEqual(called.call_args.kwargs["model"], "first")

    def test_exhausted_model_falls_through_to_the_next(self):
        result, called = self.run_judge([spent("first"), VERDICT])
        self.assertEqual(result["one_line"], "신청")
        self.assertEqual([c.kwargs["model"] for c in called.call_args_list],
                         ["first", "second"])

    def test_a_spent_model_is_skipped_for_the_rest_of_the_run(self):
        """The second notice must not waste a call on a model already spent."""
        self.run_judge([spent("first"), VERDICT])
        _, called = self.run_judge([VERDICT])
        self.assertEqual(called.call_args.kwargs["model"], "second")

    def test_only_when_every_model_is_spent_does_judging_stop(self):
        result, called = self.run_judge(
            [spent("first"), spent("second"), spent("third")])
        self.assertIsInstance(result, QuotaExhausted)
        self.assertEqual(len(called.call_args_list), 3)

    def test_available_shrinks_as_models_are_spent(self):
        self.assertEqual(len(self.judge.available()), 3)
        self.run_judge([spent("first"), VERDICT])
        self.assertNotIn("first", self.judge.available())

    def test_rate_limited_model_also_yields_to_the_next(self):
        throttled = QuotaExhausted("first 분당 한도 반복 초과", scope="minute")
        result, called = self.run_judge([throttled, VERDICT])
        self.assertEqual(result["one_line"], "신청")
        self.assertEqual(called.call_args.kwargs["model"], "second")

    def test_a_retired_model_is_skipped_not_fatal(self):
        """gemini-2.5-* retires 2026-10-16; the bot must outlive it."""
        gone = ModelUnavailable("first 사용 불가")
        result, called = self.run_judge([gone, VERDICT])

        self.assertEqual(result["one_line"], "신청")
        self.assertEqual(called.call_args.kwargs["model"], "second")
        self.assertIn("first", self.judge.retired)

    def test_every_model_retired_still_fails_open(self):
        gone = ModelUnavailable("사용 불가")
        result, _ = self.run_judge([gone, gone, gone])

        self.assertIsInstance(result, QuotaExhausted)
        self.assertIn("사용 가능한 모델 없음", str(result))

    def test_unjudged_record_is_committed_not_left_in_a_transaction(self):
        """An uncommitted row vanishes, taking the notice out of the queue."""
        self.judge.forward_unjudged(
            {"board_id": "b", "article_no": "7"}, "한도 소진")

        self.assertFalse(self.con.in_transaction)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM unjudged").fetchone()[0], 1)

    def test_ordinary_failures_are_not_retried_on_another_model(self):
        """A malformed reply is not a quota problem; burning the chain on it
        would spend three models' allowance on one broken notice."""
        with mock.patch.object(judge, "extract",
                               side_effect=RuntimeError("boom")) as called:
            with self.assertRaises(RuntimeError):
                self.judge.judge(self.item, self.resolved)
        self.assertEqual(len(called.call_args_list), 1)


class ApiError(Exception):
    """Stands in for google.genai.errors.APIError: a .code plus a long body."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


SEEN_403 = ("403 PERMISSION_DENIED. {'error': {'code': 403, 'message': "
            "'This API method requires billing to be enabled. Please enable "
            "billing on project #123 by visiting https://console...'}}")


class CredentialRefusal(unittest.TestCase):
    def test_403_from_the_call_is_raised_as_credential_rejected(self):
        api = mock.Mock()
        api.models.generate_content.side_effect = ApiError(403, SEEN_403)
        budget = mock.Mock(seconds_until_slot=mock.Mock(return_value=0))

        with self.assertRaises(CredentialRejected):
            extractor.extract({"title": "모집 공고"},
                              {"source": "text", "text": "본문", "images": []},
                              client=api, budget=budget, model="m1")
        budget.consume.assert_not_called()

    def test_reason_for_the_channel_is_short_not_the_raw_body(self):
        """The raw body cut at 100 chars landed in Telegram as half a dict."""
        reason = brief_error(Exception(
            f"api call failed (attempt 1): {SEEN_403}"))

        self.assertEqual(reason, "API 403 PERMISSION_DENIED · 결제 비활성 의심")
        self.assertNotIn("{", reason)

    def test_malformed_reply_reads_as_format_error(self):
        self.assertEqual(
            brief_error(Exception("json decode failed (attempt 4): x")),
            "응답 형식 오류")

    def test_unrecognised_error_is_capped(self):
        self.assertLessEqual(len(brief_error(Exception("x" * 500))), 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
