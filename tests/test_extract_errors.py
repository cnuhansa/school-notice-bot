"""How API errors are classified: refused credential, overload, short reason."""

import unittest
from unittest import mock

import support  # noqa: F401

from cuk_bot import extractor  # noqa: E402
from cuk_bot.extractor import (CredentialRejected, ModelBusy,  # noqa: E402
                               brief_error)


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

    def _call(self, *errors):
        api = mock.Mock()
        api.models.generate_content.side_effect = list(errors)
        budget = mock.Mock(seconds_until_slot=mock.Mock(return_value=0))
        with mock.patch.object(extractor.time, "sleep") as slept:
            try:
                extractor.extract(
                    {"title": "모집 공고"},
                    {"source": "text", "text": "본문", "images": []},
                    client=api, budget=budget, model="m1")
            except Exception as exc:  # noqa: BLE001 - the type is the result
                return exc, api, slept
        return None, api, slept

    def test_503_waits_once_then_gives_way_to_the_next_model(self):
        busy = ApiError(503, "503 UNAVAILABLE. {'error': {'message': "
                             "'This model is currently experiencing high demand.'}}")
        exc, api, slept = self._call(busy, busy)

        self.assertIsInstance(exc, ModelBusy)
        self.assertEqual(api.models.generate_content.call_count, 2)
        slept.assert_called_once_with(extractor.OVERLOAD_WAIT)
        self.assertIn("API 503 UNAVAILABLE", str(exc))

    def test_a_429_mentioning_503_in_its_retry_delay_is_not_overload(self):
        quota = ApiError(429, "429 RESOURCE_EXHAUSTED. Please retry in "
                              "13.503542s. limit: 20, model: m1")
        exc, _, _ = self._call(quota)
        self.assertNotIsInstance(exc, ModelBusy)

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
