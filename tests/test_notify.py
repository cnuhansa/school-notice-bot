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
        self.assertEqual(normalize({"confidence": 5}, "text")["confidence"], 1.0)
        self.assertEqual(normalize({"confidence": "x"}, "text")["confidence"], 0.0)

    def test_title_only_reads_cannot_be_confident(self):
        self.assertEqual(
            normalize({"confidence": 0.95}, "title")["confidence"], 0.4)

    def test_null_like_strings_become_none(self):
        out = normalize({"apply_end": "null", "target": "미상"}, "text")
        self.assertIsNone(out["apply_end"])
        self.assertIsNone(out["target"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
