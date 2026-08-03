"""Unit tests for the logic that cannot be checked by eyeballing a crawl.

Reminder scheduling is the one HANDOFF M3 calls out explicitly: a reminder
that fires on the wrong day, twice, or for an already-closed deadline is
worse than no reminder at all.
"""

import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cuk_bot import collector, db, notifier  # noqa: E402
from cuk_bot.content import resolve  # noqa: E402
from cuk_bot.extractor import normalize  # noqa: E402
from cuk_bot.parser import parse_detail, parse_list, title_key  # noqa: E402


def days_out(n):
    return (date.today() + timedelta(days=n)).isoformat()


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


class CrossPostKey(unittest.TestCase):
    def test_bracket_prefix_and_symbols_are_ignored(self):
        self.assertEqual(
            title_key("[학사지원팀] 복수전공 변경 신청 안내"),
            title_key("복수전공 변경 신청 안내!"))

    def test_translated_suffix_does_not_split_the_key(self):
        korean = "[A관] 기숙사 에어컨 필터 교체 안내"
        mixed = ("[A관] 기숙사 에어컨 필터 교체 안내 "
                 "Replacement of Air conditioner Filter A馆 空调滤网更换通知")
        self.assertEqual(title_key(korean), title_key(mixed))

    def test_different_notices_do_not_collide(self):
        self.assertNotEqual(title_key("장학금 신청 안내"),
                            title_key("기숙사 입사 안내"))


class CrossPostDetection(unittest.TestCase):
    """A notice must never be judged a repost of itself.

    Storing before comparing made every notice match its own title, which
    routed all alerts to the digest and silenced the bot entirely.
    """

    def setUp(self):
        self.con = db.connect(":memory:")
        self.board = {"id": "main_notice", "name": "본교", "url": "http://x"}

    def store(self, board_id, article_no, title):
        db.save_notice(self.con, board_id,
                       {"article_no": article_no, "title": title,
                        "url": "http://x", "posted_at": "2026-07-31"})
        self.con.commit()

    def test_first_sighting_is_not_a_crosspost(self):
        self.assertFalse(collector._crosspost_of(self.con, "기숙사 모집 공고 안내"))

    def test_same_notice_on_another_board_is_a_crosspost(self):
        self.store("main_notice", "1", "[기숙사운영팀] 기숙사 모집 공고 안내")
        self.assertTrue(
            collector._crosspost_of(self.con, "[F관] 기숙사 모집 공고 안내"))

    def test_unrelated_notice_is_not_a_crosspost(self):
        self.store("main_notice", "1", "[기숙사운영팀] 기숙사 모집 공고 안내")
        self.assertFalse(collector._crosspost_of(self.con, "장학금 신청 마감 안내"))

    def test_short_titles_never_match(self):
        self.store("main_notice", "1", "공지")
        self.assertFalse(collector._crosspost_of(self.con, "공지"))

    def test_collect_does_not_flag_a_notice_against_its_own_stored_row(self):
        """The regression test for the ordering bug.

        collect_board writes the notice and then decides whether it is a
        repost. If the write lands first the notice matches itself, so this
        exercises the real path rather than _crosspost_of in isolation.
        """
        listing = ('<table><tr><td><a href="?mode=view&articleNo=1">'
                   '기숙사 입사 모집 공고 안내</a></td>'
                   '<td>2026.07.31</td></tr></table>')
        detail = '<div class="b-content-box">본문 텍스트</div>'

        with mock.patch.object(collector, "http_get",
                               side_effect=[listing, detail]):
            fresh = collector.collect_board(self.con, self.board,
                                            log=lambda _: None)

        self.assertEqual(len(fresh), 1)
        self.assertFalse(fresh[0]["is_crosspost"],
                         "새 공지가 자기 자신의 재게시로 판정됨")

    def test_collect_flags_a_genuine_repost_from_another_board(self):
        self.store("main_notice", "9", "[학사지원팀] 기숙사 입사 모집 공고 안내")
        listing = ('<table><tr><td><a href="?mode=view&articleNo=1">'
                   '[F관] 기숙사 입사 모집 공고 안내</a></td>'
                   '<td>2026.07.31</td></tr></table>')
        detail = '<div class="b-content-box">본문 텍스트</div>'

        board = {"id": "dorm_f_inout", "name": "기숙사", "url": "http://x/b.do"}
        with mock.patch.object(collector, "http_get",
                               side_effect=[listing, detail]):
            fresh = collector.collect_board(self.con, board, log=lambda _: None)

        self.assertTrue(fresh[0]["is_crosspost"])


HTML = """
<table><tr>
  <td><a href="?mode=view&articleNo=100">[K관] 모집 공고</a></td>
  <td>2026.07.31</td>
</tr><tr>
  <td><a href="?mode=view&articleNo=100">[K관] 모집 공고</a></td>
  <td>2026.07.31</td>
</tr></table>
<div class="b-content-box"><p>본문 텍스트</p>
  <img src="/_attach/a.png"/></div>
<div class="b-file-box"><a class="file-down-btn hwp"
   href="?mode=download&articleNo=100&attachNo=1">신청서.hwp 다운로드</a></div>
"""


class Parsing(unittest.TestCase):
    def setUp(self):
        self.board = {"id": "b", "name": "게시판",
                      "url": "https://dorm.catholic.ac.kr/dormitory/board/x.do"}

    def test_pinned_notice_repeated_in_the_list_is_deduped(self):
        rows = parse_list(self.board, HTML)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["article_no"], "100")
        self.assertEqual(rows[0]["posted_at"], "2026-07-31")

    def test_detail_returns_absolute_image_and_attachment_urls(self):
        detail = parse_detail(HTML, self.board["url"])
        self.assertEqual(detail["images"],
                         ["https://dorm.catholic.ac.kr/_attach/a.png"])
        self.assertEqual(detail["attachments"][0]["kind"], "hwp")
        self.assertEqual(detail["attachments"][0]["name"], "신청서.hwp")
        self.assertTrue(
            detail["attachments"][0]["url"].endswith("x.do?mode=download"
                                                     "&articleNo=100&attachNo=1"))


class ContentResolution(unittest.TestCase):
    def test_long_body_never_triggers_a_download(self):
        detail = {"body": "가" * 400, "images": ["http://x/a.png"],
                  "attachments": []}
        out = resolve(detail)
        self.assertEqual(out["source"], "text")
        self.assertEqual(out["images"], [])

    def test_short_body_without_a_richer_source_is_still_read_as_text(self):
        detail = {"body": "가" * 100, "images": [], "attachments": []}
        out = resolve(detail)
        self.assertEqual(out["source"], "text")

    def test_empty_body_with_no_sources_falls_back_to_title(self):
        out = resolve({"body": "", "images": [], "attachments": []})
        self.assertEqual(out["source"], "title")

    def test_images_are_skipped_when_reading_is_disabled(self):
        detail = {"body": "", "images": ["http://x/a.png"], "attachments": []}
        out = resolve(detail, allow_images=False)
        self.assertEqual(out["source"], "title")
        self.assertTrue(any("skipped" in n for n in out["notes"]))

    def test_attachment_names_are_given_to_the_model(self):
        detail = {"body": "", "images": [], "attachments": [
            {"name": "입사신청서.hwp", "kind": "hwp", "url": "http://x"}]}
        out = resolve(detail)
        self.assertIn("입사신청서.hwp", out["text"])
        self.assertTrue(any("hwp" in n for n in out["notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
