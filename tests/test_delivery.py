"""A failed delivery must never undo or mislabel a verdict.

2026-11-04: main_notice/277453 was judged (actionable, 마감 11/19, confidence
1.0). The Telegram send then hit ConnectionResetError, which send() did not
catch. The exception skipped the outbox and the reminder scheduling, and the
per-notice guard in cmd_check forwarded the notice as "판정 없이 전달". The
real alert was lost, no D-7/D-3/D-1 was ever queued, and the reader was told
the bot could not judge a notice it had graded with full confidence.
"""

import json
import unittest
from unittest import mock

import requests

import support  # noqa: F401

from cuk_bot import cli, db, notifier, route  # noqa: E402
from support import days_out  # noqa: E402

RESET = requests.ConnectionError(
    ConnectionResetError(104, "Connection reset by peer"))
TELEGRAM_ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}


def verdict(**over):
    data = {"is_actionable": True, "category": "학사", "apply_start": None,
            "apply_end": days_out(14), "target": "재학생",
            "method": "트리니티", "one_line": "예비 수강신청",
            "confidence": 1.0, "source": "text"}
    data.update(over)
    return data


def count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")
        self.item = {"board_id": "main_notice", "article_no": "277453",
                     "board_name": "본교 공지사항", "title": "예비 수강신청 안내",
                     "url": "http://x/277453", "posted_at": "2026-11-04",
                     "body": "가" * 400, "images": [], "attachments": []}
        db.save_notice(self.con, "main_notice", self.item)
        self.session = mock.MagicMock()
        self.session.judge.return_value = verdict()


class TransportFailureIsAFailedSend(Fixture):
    def test_send_reports_false_instead_of_raising(self):
        with mock.patch.dict("os.environ", TELEGRAM_ENV), \
                mock.patch.object(requests, "post", side_effect=RESET):
            self.assertFalse(notifier.send("본문", log=lambda _: None))

    def test_the_message_is_parked_for_the_next_run(self):
        with mock.patch.dict("os.environ", TELEGRAM_ENV), \
                mock.patch.object(requests, "post", side_effect=RESET):
            notifier.send_or_queue(self.con, "본문", "alert", log=lambda _: None)
        self.assertEqual(count(self.con, "outbox"), 1)


class TheIncidentDoesNotRecur(Fixture):
    """The 11-04 sequence, end to end, with the real send path."""

    def route(self):
        with mock.patch.dict("os.environ", TELEGRAM_ENV), \
                mock.patch.object(requests, "post", side_effect=RESET):
            route.extract_and_route(self.con, self.item, self.session,
                                    notify=True, log=lambda _: None)

    def test_reminders_are_scheduled_although_the_send_failed(self):
        self.route()
        self.assertEqual(count(self.con, "reminders"), 3)

    def test_the_alert_waits_in_the_outbox(self):
        self.route()
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM outbox")]
        self.assertEqual(kinds, ["alert"])

    def test_a_judged_notice_is_not_reported_unjudged(self):
        self.route()
        self.session.forward_unjudged.assert_not_called()
        self.assertEqual(count(self.con, "unjudged"), 0)


class FailureAfterTheVerdict(Fixture):
    """Anything that breaks after judging is a routing failure, not a
    judging one — and it must not cost the reminders."""

    def check(self, notify=True):
        with mock.patch.object(cli, "collect", return_value=[self.item]), \
                mock.patch.object(cli, "Judge", return_value=self.session), \
                mock.patch.object(notifier, "format_alert",
                                  side_effect=KeyError("category")), \
                mock.patch.object(notifier, "send", return_value=True) as sent:
            self.session.unjudged = []
            self.session.dropped = []
            cli.cmd_check(self.con, notify=notify, log=lambda _: None)
        return sent

    def test_it_is_not_forwarded_as_unjudged(self):
        self.check()
        self.session.forward_unjudged.assert_not_called()

    def test_the_channel_gets_an_honest_message(self):
        sent = self.check()
        text = sent.call_args.args[0]
        self.assertIn("알림 처리 실패", text)
        self.assertIn("판정까지 했지만", text)
        self.assertNotIn("판정하지 못했습니다", text)

    def test_reminders_survive_it(self):
        self.check()
        self.assertEqual(count(self.con, "reminders"), 3)


class Redeliver(Fixture):
    """Repair for notices already caught in the 11-04 state."""

    def setUp(self):
        super().setUp()
        db.save_extraction(self.con, "main_notice", "277453", verdict(), "text")
        self.con.execute("UPDATE extractions SET extracted_at='2026-11-04T16:19:10'")
        db.mark_unjudged(self.con, "main_notice", "277453", "처리 실패: reset")
        self.con.commit()

    def redeliver(self):
        with mock.patch.object(notifier, "send", return_value=True) as sent:
            n = route.redeliver(self.con, log=lambda _: None)
        return n, sent

    def test_the_real_alert_goes_out_from_the_stored_verdict(self):
        n, sent = self.redeliver()
        self.assertEqual(n, 1)
        text = sent.call_args.args[0]
        self.assertIn("신청 공지", text)
        self.assertIn("예비 수강신청", text)

    def test_reminders_are_restored(self):
        self.redeliver()
        self.assertEqual(count(self.con, "reminders"), 3)

    def test_the_unjudged_record_is_cleared(self):
        self.redeliver()
        self.assertEqual(count(self.con, "unjudged"), 0)

    def test_the_original_verdict_is_left_untouched(self):
        self.redeliver()
        when = self.con.execute(
            "SELECT extracted_at FROM extractions").fetchone()[0]
        self.assertEqual(when, "2026-11-04T16:19:10")

    def test_running_it_again_sends_nothing(self):
        self.redeliver()
        n, sent = self.redeliver()
        self.assertEqual(n, 0)
        sent.assert_not_called()

    def test_genuinely_unjudged_notices_are_left_for_reextract(self):
        """No verdict means nothing to resend — that is reextract's job."""
        self.con.execute("DELETE FROM extractions")
        n, sent = self.redeliver()
        self.assertEqual(n, 0)
        self.assertEqual(count(self.con, "unjudged"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
