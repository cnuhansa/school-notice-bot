"""Failure detection and delivery recovery.

Every test here guards a path where the bot would otherwise fail *quietly* —
looking healthy while seeing nothing, or losing an alert without a trace.
"""

import json
import unittest
from unittest import mock

import support  # noqa: F401

from cuk_bot import cli, collector, db, health, notifier  # noqa: E402
from support import days_out  # noqa: E402

LISTING = ('<table><tr><td><a href="?mode=view&articleNo=1">모집 공고</a></td>'
           '<td>2026.07.31</td></tr></table>')
EMPTY_LISTING = "<table></table>"
DETAIL = '<div class="b-content-box">본문 텍스트</div>'


class EmptyListingIsFailure(unittest.TestCase):
    """A board that returns nothing has broken, not gone quiet.

    These boards always carry 15+ notices. Logging zero as a success let the
    bot report healthy while silently seeing nothing at all.
    """

    def setUp(self):
        self.con = db.connect(":memory:")
        self.board = {"id": "b", "name": "게시판", "url": "http://x/b.do"}

    def crawl(self, html):
        with mock.patch.object(collector, "http_get", return_value=html):
            return collector.collect_board(self.con, self.board,
                                           log=lambda _: None)

    def last_log(self):
        return dict(self.con.execute(
            "SELECT ok, error FROM crawl_log ORDER BY rowid DESC LIMIT 1"
        ).fetchone())

    def test_empty_listing_is_recorded_as_failure(self):
        self.assertEqual(self.crawl(EMPTY_LISTING), [])
        row = self.last_log()
        self.assertEqual(row["ok"], 0)
        self.assertIn("0건", row["error"])

    def test_normal_listing_is_recorded_as_success(self):
        with mock.patch.object(collector, "http_get",
                               side_effect=[LISTING, DETAIL]):
            collector.collect_board(self.con, self.board, log=lambda _: None)
        self.assertEqual(self.last_log()["ok"], 1)

    def test_failure_streak_is_reported_once_then_suppressed(self):
        for _ in range(health.FAILURE_STREAK):
            self.crawl(EMPTY_LISTING)

        self.assertEqual(health.new_failures(self.con), ["b"])
        health.mark_warned(self.con, "b")
        self.assertEqual(health.new_failures(self.con), [],
                         "같은 장애로 매 실행마다 경고하면 안 됨")

    def test_streak_shorter_than_threshold_is_not_reported(self):
        for _ in range(health.FAILURE_STREAK - 1):
            self.crawl(EMPTY_LISTING)
        self.assertEqual(health.new_failures(self.con), [])

    def test_recovery_clears_the_warning_so_the_next_break_alerts_again(self):
        for _ in range(health.FAILURE_STREAK):
            self.crawl(EMPTY_LISTING)
        health.mark_warned(self.con, "b")

        with mock.patch.object(collector, "http_get",
                               side_effect=[LISTING, DETAIL]):
            collector.collect_board(self.con, self.board, log=lambda _: None)

        health.new_failures(self.con)  # recovery is observed here
        self.assertFalse(health.already_warned(self.con, "b"))


class OutboxRetry(unittest.TestCase):
    """A failed send must survive to the next run."""

    def setUp(self):
        self.con = db.connect(":memory:")

    def test_failed_send_is_parked_not_lost(self):
        with mock.patch.object(notifier, "send", return_value=False):
            ok = notifier.send_or_queue(self.con, "본문", "alert",
                                        log=lambda _: None)

        self.assertFalse(ok)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_successful_send_leaves_nothing_behind(self):
        with mock.patch.object(notifier, "send", return_value=True):
            notifier.send_or_queue(self.con, "본문", "alert", log=lambda _: None)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_parked_message_is_resent_on_the_next_run(self):
        with mock.patch.object(notifier, "send", return_value=False):
            notifier.send_or_queue(self.con, "본문", "alert", log=lambda _: None)

        with mock.patch.object(notifier, "send", return_value=True) as sent:
            resent = notifier.flush_outbox(self.con, log=lambda _: None)

        self.assertEqual(resent, 1)
        sent.assert_called_once()
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_attempts_are_counted_and_retries_eventually_stop(self):
        with mock.patch.object(notifier, "send", return_value=False):
            notifier.send_or_queue(self.con, "본문", "alert", log=lambda _: None)
            for _ in range(notifier.MAX_SEND_ATTEMPTS):
                notifier.flush_outbox(self.con, log=lambda _: None)

        self.assertEqual(
            db.stuck_messages(self.con, notifier.MAX_SEND_ATTEMPTS), 1)
        self.assertEqual(db.pending_outbox(self.con,
                                           notifier.MAX_SEND_ATTEMPTS), [])

    def test_exhausted_message_is_kept_for_inspection_not_deleted(self):
        with mock.patch.object(notifier, "send", return_value=False):
            notifier.send_or_queue(self.con, "잃으면 안 되는 알림", "alert",
                                   log=lambda _: None)
            for _ in range(notifier.MAX_SEND_ATTEMPTS + 2):
                notifier.flush_outbox(self.con, log=lambda _: None)

        body = self.con.execute("SELECT body FROM outbox").fetchone()["body"]
        self.assertEqual(body, "잃으면 안 되는 알림")


class Heartbeat(unittest.TestCase):
    def test_ping_is_a_noop_without_a_configured_url(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(health.ping("success"))

    def test_ping_never_raises_when_the_watchdog_is_unreachable(self):
        with mock.patch.dict("os.environ",
                             {"CUK_HEALTHCHECK_URL": "http://127.0.0.1:1/x"}):
            self.assertFalse(health.ping("success", log=lambda _: None))

    def test_state_selects_the_right_endpoint(self):
        seen = []
        with mock.patch.dict("os.environ",
                             {"CUK_HEALTHCHECK_URL": "http://hc/uuid"}):
            with mock.patch("urllib.request.urlopen",
                            side_effect=lambda u, timeout: seen.append(u)
                            or mock.MagicMock()):
                health.ping("start")
                health.ping("success")
                health.ping("fail")

        self.assertEqual(seen, ["http://hc/uuid/start", "http://hc/uuid",
                                "http://hc/uuid/fail"])

    def test_digest_uses_its_own_monitor(self):
        seen = []
        with mock.patch.dict("os.environ",
                             {"CUK_HEALTHCHECK_DIGEST_URL": "http://hc/daily"}):
            with mock.patch("urllib.request.urlopen",
                            side_effect=lambda u, timeout: seen.append(u)
                            or mock.MagicMock()):
                health.ping("success", env=health.DIGEST_URL_ENV)

        self.assertEqual(seen, ["http://hc/daily"])

    def test_empty_day_still_produces_a_message(self):
        text = notifier.format_alive(8, 8)
        self.assertIn("오늘 새 공지 없음", text)
        self.assertIn("8/8", text)
        self.assertIn("문제가 생긴 것", text)


class OneBadNoticeDoesNotEndTheRun(unittest.TestCase):
    """A single unroutable notice must not abort cmd_check.

    It did: an exception in the routing path escaped the loop, so the
    remaining new notices were never processed and the workflow's DB commit
    step was skipped. The next run re-read the same notice and died at the
    same place, which is why the same failure repeated across days.
    """

    def setUp(self):
        self.con = db.connect(":memory:")
        self.items = [
            {"board_id": "b", "article_no": "1", "title": "터지는 공지",
             "url": "http://x/1", "board_name": "게시판"},
            {"board_id": "b", "article_no": "2", "title": "멀쩡한 공지",
             "url": "http://x/2", "board_name": "게시판"},
        ]

    def run_check(self, side_effect):
        with mock.patch.object(cli, "collect", return_value=self.items),              mock.patch.object(cli, "Judge") as judge_cls,              mock.patch.object(cli, "_extract_and_route",
                               side_effect=side_effect) as routed:
            judge_cls.return_value = mock.MagicMock(
                unjudged=[], reason=None, summary=lambda: "")
            cli.cmd_check(self.con, notify=False, log=lambda _: None)
            return routed, judge_cls.return_value

    def test_remaining_notices_are_still_processed(self):
        routed, _ = self.run_check([RuntimeError("boom"), None])

        self.assertEqual(len(routed.call_args_list), 2,
                         "첫 공지에서 터지고 나머지가 버려짐")

    def test_the_failed_notice_is_forwarded_not_dropped(self):
        _, session = self.run_check([RuntimeError("boom"), None])

        session.forward_unjudged.assert_called_once()
        self.assertIs(session.forward_unjudged.call_args.args[0],
                      self.items[0])

    def test_the_run_itself_succeeds(self):
        """cmd_check must return normally so the DB commit step still runs."""
        try:
            self.run_check([RuntimeError("boom"), None])
        except Exception as exc:
            self.fail(f"한 건 실패가 실행 전체를 중단시킴: {exc}")

    def test_a_none_title_cannot_crash_the_log_line(self):
        """The crosspost branch logs the title directly — None killed it."""
        self.items[0]["title"] = None
        self.items[0]["is_crosspost"] = True
        routed, _ = self.run_check([None])

        # The crosspost is skipped, so only the second notice is routed —
        # reaching it at all means the log line survived the None.
        self.assertEqual(len(routed.call_args_list), 1)
        self.assertIs(routed.call_args.args[1], self.items[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
