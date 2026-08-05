"""Falling through the model chain as each free tier runs out."""

import os
import unittest
from unittest import mock

import support  # noqa: F401

from cuk_bot import client, db, judge  # noqa: E402
from cuk_bot.extractor import ModelUnavailable  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
