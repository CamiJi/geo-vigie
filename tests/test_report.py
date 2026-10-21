import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from report import gather, send


def _make_conn() -> sqlite3.Connection:
    """Schéma multiprofil en mémoire : offers (sans score) + scores."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE offers (
            id INTEGER PRIMARY KEY, source TEXT, keyword_hit INTEGER,
            posted_at TEXT, first_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE scores (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            score_reason TEXT DEFAULT '',
            UNIQUE (offer_id, profile_id)
        )
    """)
    conn.execute("""
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE (offer_id, profile_id)
        )
    """)
    conn.execute("""
        CREATE TABLE matches (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER NOT NULL,
            profile_id TEXT NOT NULL,
            term TEXT DEFAULT '',
            UNIQUE (offer_id, profile_id)
        )
    """)
    conn.executemany(
        "INSERT INTO offers VALUES (?, ?, ?, ?, ?)",
        [
            (1, "reed", 1, "2026-10-01", "2026-10-01"),
            (2, "egu", 1, "2026-10-05", "2026-10-05"),
            (3, "inactive-old-news", 1, "2026-10-06", "2026-10-06"),
            (4, "egu", 0, "2026-10-07", "2026-10-07"),
            (5, "egu", 1, "2026-10-04", "2026-10-04"),
            (6, "reed", 1, "2026-10-03", "2026-10-03"),
        ],
    )
    conn.executemany(
        "INSERT INTO scores (offer_id, profile_id, score) VALUES (?, 'quentin', ?)",
        [(1, 15), (2, 90), (3, 100), (4, 100), (5, 40), (6, 60)],
    )
    return conn


CONFIG = {
    "profile_id": "quentin",
    "scoring": {"min_score": 45, "top_n": 15},
    "sources": [
        {"name": "reed", "enabled": True},
        {"name": "egu", "enabled": True},
        {"name": "inactive-old-news", "enabled": False},
    ],
}


class RecentReportTests(unittest.TestCase):
    def test_latest_mode_uses_active_sources_and_posted_date(self):
        conn = _make_conn()
        result = gather(CONFIG, conn, latest=2)
        self.assertEqual([row["id"] for row in result["top"]], [2, 6])
        self.assertEqual(result["mode"], "latest")
        conn.close()

    def test_daily_mode_is_profile_scoped_and_skips_already_sent(self):
        conn = _make_conn()
        # Offres au-dessus du seuil (45) pour quentin : 2 (90), 3 (100),
        # 4 (100), 6 (60). Comportement historique conservé : un envoi du JOUR
        # reste inclus dans le relevé ; seul un envois antérieur exclut.
        conn.execute("INSERT INTO reports (offer_id, profile_id, sent_at) "
                     "VALUES (2, 'quentin', '2026-10-01T00:00:00+00:00')")
        result = gather(CONFIG, conn)
        self.assertEqual(result["profile"], "quentin")
        self.assertEqual([row["id"] for row in result["top"]], [4, 3, 6])
        # Un envoi fait pour un AUTRE profil ne masque rien pour quentin.
        conn.execute("INSERT INTO reports (offer_id, profile_id, sent_at) "
                     "VALUES (6, 'gabriela', '2026-10-01T00:00:00+00:00')")
        result = gather(CONFIG, conn)
        self.assertEqual([row["id"] for row in result["top"]], [4, 3, 6])
        # Envoi aujourd'hui pour quentin : l'offre reste dans le relevé du jour.
        conn.execute("INSERT INTO reports (offer_id, profile_id, sent_at) "
                     "VALUES (6, 'quentin', ?)",
                     (gather(CONFIG, conn)["today"],))
        self.assertIn(6, [row["id"] for row in gather(CONFIG, conn)["top"]])
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
