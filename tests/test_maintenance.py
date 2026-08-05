"""Maintenance paths: previews, rule re-application, schema upgrades.

Each of these keeps a long-lived deployment honest — the database is carried
forward across every run, so a preview that mutates it or a rule change that
cannot be re-applied compounds instead of resetting.
"""

import json
import unittest
from unittest import mock

import support  # noqa: F401

from cuk_bot import cli, db, notifier  # noqa: E402
from support import days_out  # noqa: E402


class DigestPreview(unittest.TestCase):
    """--no-notify must be read-only.

    The first version cleared pending_digest and marked reminders sent even
    when nothing was delivered, so previewing the digest silently threw away
    the day's notices and stopped those D-day reminders from ever firing.
    """

    def setUp(self):
        self.con = db.connect(":memory:")
        self.con.execute("INSERT INTO notices (board_id, article_no, title, url)"
                         " VALUES ('b','1','공지','http://x')")
        self.con.execute("INSERT INTO extractions (board_id, article_no, "
                         "one_line, apply_end) VALUES ('b','1','한 줄', ?)",
                         (days_out(1),))
        db.queue_digest(self.con, "b", "1")
        notifier.schedule_reminders(self.con, "b", "1", days_out(1))
        self.con.commit()

    def pending(self):
        return self.con.execute(
            "SELECT COUNT(*) FROM pending_digest").fetchone()[0]

    def unsent_reminders(self):
        return self.con.execute(
            "SELECT COUNT(*) FROM reminders WHERE sent_at IS NULL").fetchone()[0]

    def test_preview_leaves_the_queue_intact(self):
        cli.cmd_digest(self.con, notify=False, log=lambda _: None)

        self.assertEqual(self.pending(), 1)
        self.assertEqual(self.unsent_reminders(), 1)

    def test_preview_sends_nothing(self):
        with mock.patch.object(notifier, "send") as sent:
            cli.cmd_digest(self.con, notify=False, log=lambda _: None)
        sent.assert_not_called()

    def test_real_run_consumes_the_queue(self):
        with mock.patch.object(notifier, "send", return_value=True):
            cli.cmd_digest(self.con, notify=True, log=lambda _: None)

        self.assertEqual(self.pending(), 0)
        self.assertEqual(self.unsent_reminders(), 0)

    def test_failed_send_keeps_the_queue_for_the_next_run(self):
        with mock.patch.object(notifier, "send", return_value=False):
            cli.cmd_digest(self.con, notify=True, log=lambda _: None)

        self.assertEqual(self.pending(), 1, "발송 실패인데 대기열이 비워짐")
        self.assertEqual(self.unsent_reminders(), 1)


class Renormalize(unittest.TestCase):
    """Judgement rules must be re-appliable without paying to extract again.

    Storing the model's untouched reply is the whole point of keeping a
    payload column; without this the rules can improve but every verdict
    already in the database stays as it was.
    """

    def setUp(self):
        self.con = db.connect(":memory:")
        self.con.execute("INSERT INTO notices (board_id, article_no, title, url)"
                         " VALUES ('b','1','공지','http://x')")
        # A verdict written before the expired rule existed.
        self.con.execute(
            "INSERT INTO extractions (board_id, article_no, payload, "
            "is_actionable, apply_end, confidence, source) "
            "VALUES ('b','1',?,1,?,0.9,'text')",
            (json.dumps({"is_actionable": True, "apply_end": days_out(-3),
                         "confidence": 0.9}), days_out(-3)))
        self.con.commit()

    def test_stale_verdict_is_corrected(self):
        cli.cmd_renormalize(self.con, log=lambda _: None)

        row = self.con.execute(
            "SELECT is_actionable FROM extractions").fetchone()
        self.assertEqual(row["is_actionable"], 0, "마감 지난 공지가 그대로 남음")

    def test_second_run_changes_nothing(self):
        cli.cmd_renormalize(self.con, log=lambda _: None)
        logged = []
        cli.cmd_renormalize(self.con, log=logged.append)
        self.assertIn("0건", logged[-1])

    def test_original_reply_survives_so_rules_stay_reversible(self):
        cli.cmd_renormalize(self.con, log=lambda _: None)

        payload = json.loads(self.con.execute(
            "SELECT payload FROM extractions").fetchone()["payload"])
        self.assertTrue(payload["raw"]["is_actionable"],
                        "모델 원본 응답이 덮어써짐 — 규칙을 되돌릴 수 없게 됨")
        self.assertEqual(payload["raw"]["confidence"], 0.9)


class SchemaMigration(unittest.TestCase):
    """A database carried forward must gain new columns, not break.

    The operating design commits the SQLite file to the repo and reuses it on
    every run, so CREATE TABLE IF NOT EXISTS alone would leave an old file
    permanently one schema behind.
    """

    def test_missing_column_is_added_to_an_existing_table(self):
        con = db.connect(":memory:")
        con.execute("DROP TABLE api_usage")
        con.execute("CREATE TABLE api_usage (day TEXT NOT NULL, "
                    "model TEXT NOT NULL DEFAULT '', used INTEGER DEFAULT 0, "
                    "blocked INTEGER DEFAULT 0, PRIMARY KEY (day, model))")
        con.execute("INSERT INTO api_usage (day, model, used) "
                    "VALUES ('2026-08-05','m',7)")
        con.commit()

        db._migrate(con)

        columns = {r["name"] for r in con.execute("PRAGMA table_info(api_usage)")}
        self.assertIn("tok_in", columns)
        self.assertIn("tok_out", columns)
        # Existing rows survive with a sane default.
        row = con.execute("SELECT used, tok_in FROM api_usage").fetchone()
        self.assertEqual((row["used"], row["tok_in"]), (7, 0))

    def test_migration_is_idempotent(self):
        con = db.connect(":memory:")
        db._migrate(con)
        db._migrate(con)  # must not raise "duplicate column name"


if __name__ == "__main__":
    unittest.main(verbosity=2)
