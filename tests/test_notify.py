"""Alert shaping and reminder scheduling.

HANDOFF M3 calls this out explicitly: a reminder that fires on the wrong
day, twice, or for an already-closed deadline is worse than none at all.
"""

import unittest
from datetime import date

from support import days_out

from cuk_bot import db, notifier  # noqa: E402
from cuk_bot.extractor import normalize  # noqa: E402


class ReminderScheduling(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")

    def scheduled(self):
        return {(r["kind"], r["due_date"]) for r in
                self.con.execute("SELECT kind, due_date FROM reminders")}

    def test_all_three_queued_for_a_distant_deadline(self):
        notifier.schedule_reminders(self.con, "b", "1", days_out(30))
        self.assertEqual({k for k, _ in self.scheduled()}, {"D-7", "D-3", "D-1"})

    def test_past_deadline_queues_nothing(self):
        queued = notifier.schedule_reminders(self.con, "b", "1", days_out(-5))
        self.assertEqual(queued, 0)
        self.assertEqual(self.scheduled(), set())

    def test_near_deadline_only_queues_future_reminders(self):
        # 2 days out: D-7 and D-3 are already in the past, D-1 is tomorrow.
        notifier.schedule_reminders(self.con, "b", "1", days_out(2))
        self.assertEqual({k for k, _ in self.scheduled()}, {"D-1"})

    def test_missing_or_malformed_deadline_is_ignored(self):
        self.assertEqual(notifier.schedule_reminders(self.con, "b", "1", None), 0)
        self.assertEqual(
            notifier.schedule_reminders(self.con, "b", "1", "상시모집"), 0)

    def test_rescheduling_does_not_duplicate(self):
        notifier.schedule_reminders(self.con, "b", "1", days_out(30))
        notifier.schedule_reminders(self.con, "b", "1", days_out(30))
        self.assertEqual(len(self.scheduled()), 3)

    def test_due_reminder_is_only_returned_until_sent(self):
        deadline = days_out(1)
        self.con.execute(
            "INSERT INTO notices (board_id, article_no, title, url) "
            "VALUES ('b','1','제목','http://x')")
        self.con.execute(
            "INSERT INTO extractions (board_id, article_no, one_line, apply_end)"
            " VALUES ('b','1','한 줄', ?)", (deadline,))
        notifier.schedule_reminders(self.con, "b", "1", deadline)

        due = notifier.due_reminders(self.con, on=date.today().isoformat())
        self.assertEqual(len(due), 1)
        notifier.mark_reminder_sent(self.con, due[0])
        self.con.commit()
        self.assertEqual(notifier.due_reminders(self.con), [])


class DeadlineFormatting(unittest.TestCase):
    def test_future_deadline_shows_dday(self):
        self.assertIn("D-3", notifier.fmt_deadline(days_out(3)))

    def test_today_and_past_are_distinguished(self):
        self.assertIn("오늘 마감", notifier.fmt_deadline(days_out(0)))
        self.assertIn("마감됨", notifier.fmt_deadline(days_out(-1)))

    def test_missing_deadline(self):
        self.assertEqual(notifier.fmt_deadline(None), "미상")

    def test_unparseable_value_is_passed_through(self):
        self.assertEqual(notifier.fmt_deadline("상시"), "상시")


class MessageEscaping(unittest.TestCase):
    def test_title_markup_cannot_break_the_message(self):
        item = {"title": "★<b>모집</b> & 안내", "url": "http://x",
                "board_name": "게시판"}
        data = {"is_actionable": True, "one_line": "신청", "confidence": 0.9,
                "apply_end": days_out(5), "source": "text"}
        text = notifier.format_alert(item, data)
        self.assertIn("&lt;b&gt;모집&lt;/b&gt; &amp; 안내", text)

    def test_image_sourced_notice_carries_a_caveat(self):
        item = {"title": "공고", "url": "http://x", "board_name": "게시판"}
        data = {"is_actionable": True, "one_line": "신청", "confidence": 0.9,
                "apply_end": None, "source": "image"}
        self.assertIn("본문이 이미지입니다", notifier.format_alert(item, data))


class Normalisation(unittest.TestCase):
    def test_confidence_is_clamped(self):
        # A deadline is supplied so the no-deadline cap does not interfere.
        high = normalize({"confidence": 5, "apply_end": days_out(3)}, "text")
        self.assertEqual(high["confidence"], 1.0)
        junk = normalize({"confidence": "x", "apply_end": days_out(3)}, "text")
        self.assertEqual(junk["confidence"], 0.0)

    def test_title_only_reads_cannot_be_confident(self):
        out = normalize({"confidence": 0.95, "apply_end": days_out(3)}, "title")
        self.assertEqual(out["confidence"], 0.4)

    def test_no_deadline_forces_low_confidence(self):
        """Observed on a live 채용 공고: conf 0.9 with apply_end null.

        High confidence suppresses the "마감일이 불확실합니다" caveat, so the
        one alert that most needed the warning would not have carried it.
        """
        out = normalize({"is_actionable": True, "apply_end": None,
                         "confidence": 0.9}, "text")
        self.assertLessEqual(out["confidence"], 0.3)

    def test_confidence_is_untouched_when_a_deadline_exists(self):
        out = normalize({"is_actionable": True, "apply_end": days_out(5),
                         "confidence": 0.9}, "text")
        self.assertEqual(out["confidence"], 0.9)

    def test_deadline_already_passed_is_forced_not_actionable(self):
        """Decided in code, not by the prompt.

        The model judged two near-identical 정기퇴사 notices differently, and
        "is this date in the past" needs no judgement. Alerting about a closed
        deadline is pure noise.
        """
        out = normalize({"is_actionable": True, "apply_end": days_out(-1),
                         "confidence": 0.9}, "text")
        self.assertFalse(out["is_actionable"])
        self.assertTrue(out["expired"])

    def test_today_deadline_is_still_actionable(self):
        out = normalize({"is_actionable": True, "apply_end": days_out(0),
                         "confidence": 0.9}, "text")
        self.assertTrue(out["is_actionable"])

    def test_missing_or_unparseable_deadline_is_left_alone(self):
        for value in (None, "상시모집"):
            out = normalize({"is_actionable": True, "apply_end": value,
                             "confidence": 0.9}, "text")
            self.assertTrue(out["is_actionable"], f"{value} 에서 오판")

    def test_null_like_strings_become_none(self):
        out = normalize({"apply_end": "null", "target": "미상"}, "text")
        self.assertIsNone(out["apply_end"])
        self.assertIsNone(out["target"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
