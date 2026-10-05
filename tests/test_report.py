import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from report import gather, send


class RecentReportTests(unittest.TestCase):
    def test_latest_mode_uses_active_sources_and_posted_date(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE offers (
                id INTEGER PRIMARY KEY, source TEXT, keyword_hit INTEGER,
                score INTEGER, posted_at TEXT, first_seen TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO offers VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "reed", 1, 15, "2026-10-01", "2026-10-01"),
                (2, "egu", 1, 90, "2026-10-05", "2026-10-05"),
                (3, "inactive-old-news", 1, 100, "2026-10-06", "2026-10-06"),
                (4, "egu", 0, 100, "2026-10-07", "2026-10-07"),
                (5, "egu", 1, 40, "2026-10-04", "2026-10-04"),
                (6, "reed", 1, 60, "2026-10-03", "2026-10-03"),
            ],
        )
        config = {
            "scoring": {"min_score": 45, "top_n": 15},
            "sources": [
                {"name": "reed", "enabled": True},
                {"name": "egu", "enabled": True},
                {"name": "inactive-old-news", "enabled": False},
            ],
        }
        result = gather(config, conn, latest=2)
        self.assertEqual([row["id"] for row in result["top"]], [2, 6])
        self.assertEqual(result["mode"], "latest")
        conn.close()

    def test_smtp_sends_to_recipient_and_cc(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        env = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_SECURITY": "starttls",
            "SMTP_USER": "smtp-login",
            "SMTP_PASS": "smtp-secret",
            "MAIL_FROM": "sender@example.test",
            "MAIL_TO": "quentin@example.test",
            "MAIL_CC": "camille@example.test",
        }
        with patch("report.smtplib.SMTP", return_value=smtp):
            send(env, "Subject", "Text", "<p>HTML</p>")
        kwargs = smtp.send_message.call_args.kwargs
        self.assertEqual(kwargs["to_addrs"], [
            "quentin@example.test", "camille@example.test"
        ])
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "quentin@example.test")
        self.assertEqual(msg["Cc"], "camille@example.test")


if __name__ == "__main__":
    unittest.main()
